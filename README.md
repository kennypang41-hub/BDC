# BDC Tracker

Loan-level valuation marks for US Business Development Companies, extracted from
SEC EDGAR and served as a tracker.

A BDC's 10-K/10-Q carries a **Consolidated Schedule of Investments**: every loan
it holds, with principal, cost and fair value. Fair value over principal is *the
mark* — the
lender's own opinion of what the loan is worth. Collect the marks across every
BDC and every quarter and you can see which borrowers are deteriorating, which
lenders are slow to write down, and where two lenders looking at the same credit
disagree.

Target coverage: **43 BDCs**, on the order of **11,000 loans** and **70,000
marks** across roughly two years of quarters.

---

## Where the marks actually come from

Since 2020 BDCs tag the Schedule of Investments in XBRL, one set of facts per
position, dimensioned by `us-gaap:InvestmentIdentifierAxis`:

| Tag | Field |
|---|---|
| `InvestmentOwnedAtFairValue` | the mark's numerator |
| `InvestmentOwnedBalancePrincipalAmount` | par — the denominator |
| `InvestmentOwnedAtCost` | what the BDC paid |
| `InvestmentInterestRate`, `InvestmentBasisSpreadVariableRate`, `InvestmentInterestRatePaidInKind` | coupon, spread, PIK |
| `InvestmentMaturityDate` | maturity |

There are two ways to read them, and this project uses both:

1. **SEC DERA BDC Data Sets** (`bdctracker/sources/dera.py`) — the SEC
   re-publishes those facts as a quarterly TSV covering every BDC that filed.
   Eight zip files cover two years of the whole universe. This is the default.
2. **Per-filing XBRL** (`bdctracker/sources/xbrl.py`) — reads the same facts
   straight out of a filing via [edgartools](https://github.com/dgunning/edgartools).
   Slower, but it covers the newest quarter before DERA publishes it, and any
   BDC missing from a bulk set. A 10-K carries the prior year-end schedule too,
   so one download yields two quarters.

Both paths produce the same `Position` records and the same loan keys, so they
reconcile instead of double-counting. There is a test that asserts exactly that
(`tests/test_sources.py::test_xbrl_and_dera_agree_on_the_loan_key`).

## The hard part: identity

Extraction is the easy half. The dataset is only useful if the same loan carries
the same identity from one quarter to the next, because otherwise there is no
quarter-over-quarter mark change and no cross-BDC comparison.

Filers re-word the label every quarter — the rate resets, the maturity rolls,
the borrower picks up a "(dba …)". So `bdctracker/identity.py` keys on structure
rather than text:

- **`issuer_key`** — the borrower with punctuation, parentheticals and
  structuring suffixes stripped, so `Project Alpha Bidco Limited` and
  `Project Alpha BidCo Ltd.` are one borrower.
- **`loan_key`** — `(BDC, borrower, instrument type, tranche signature, currency)`.
  The tranche signature keeps only the markers a filer holds constant (facility
  kind, tranche letter, first/last out) and discards everything that moves.
- **`credit_key`** — `(borrower, lien family)`, deliberately *not* scoped to a
  BDC. This is what makes "two lenders, one credit, two different marks"
  computable.

Two facilities of the same kind to the same borrower collapse onto one loan key
and are summed within a filing. That is intentional: the alternative is a key
that depends on row order, which is not stable across quarters.

## Data quality

BDC XBRL is filed by 43 different teams and it shows. The pipeline handles the
two failure modes that actually corrupt the dataset:

- **Wrong units.** A filer tags thousands where the taxonomy wants units and a
  whole filing's marks come out near 0.1. Each filing's median debt mark is
  checked against par and rescaled if it is off by a factor of 100 or 1000, with
  a `rescaled_x1000` flag on every affected row.
- **Blocked vs. unpublished.** A quarter the SEC has not published yet 404s and
  is skipped; a proxy denial or connection failure raises instead of quietly
  becoming a "missing quarter". `harvest` probes sec.gov once before it starts,
  so a blocked network costs two seconds rather than several minutes of retries.
- **Unknown vs. zero.** Non-accrual status lives in footnotes, not in a tagged
  flag. A filing that names even one non-accrual is telling us it discloses
  them, so the rest of that schedule is marked accruing; a filing that names
  none leaves its positions **unknown**, and the UI shows `n/d` rather than
  claiming a clean book.

Rows are never silently dropped — they are flagged (`no_fair_value`,
`no_principal`, `implausible_mark`, `unclassified`) and kept.

---

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[web,dev]"

# The SEC blocks unidentified traffic. This is mandatory.
export EDGAR_IDENTITY="Your Name your@email.com"

bdc doctor                              # confirm this machine can reach EDGAR
bdc universe list                       # the 43 covered BDCs
bdc harvest --start 2023Q4              # extract marks into data/bdc.db
bdc stats                               # what landed
bdc report bdcs                         # every BDC side by side
bdc serve                               # http://127.0.0.1:8000
```

Get the data out:

```bash
bdc excel                  # every mark as .xlsx — Marks, BDC summary, Disagreements, Read me
bdc bundle                 # the whole site as one self-contained .html file
bdc export --out web/data  # the views as JSON, for static hosting of web/index.html
```

No EDGAR access handy? Load a synthetic dataset of the same shape to try the UI.
It is stamped `synthetic` and the page shows a banner, so it can never be
mistaken for extracted data:

```bash
bdc demo && bdc export --out web/data
```

### Commands

| Command | What it does |
|---|---|
| `bdc harvest` | Extract marks (bulk data sets, then filings for the gaps) and load them |
| `bdc backfill --tickers ARCC,TSLX` | Re-extract specific BDCs straight from filings |
| `bdc universe list` / `sync` | Show the covered BDCs; verify CIKs against the SEC BDC Report |
| `bdc stats` | Row counts, period coverage, latest portfolio fair value |
| `bdc report <view>` | `bdcs`, `nonaccruals`, `disagreements`, `markdowns`, `maturities`, `deteriorating` |
| `bdc excel` | Every mark as a workbook, one row per (loan, quarter) |
| `bdc bundle` | The whole site as one self-contained HTML file — no server needed |
| `bdc export` | Freeze every view to JSON for static hosting |
| `bdc serve` | FastAPI app + front end |
| `bdc demo` | Synthetic dataset for UI work |
| `bdc doctor` | Check `EDGAR_IDENTITY` and that sec.gov is actually reachable |

## The tracker

Seven views, all reading the same `marks` table:

- **Overview** — portfolio mark, non-accrual trajectory, the distribution of
  every debt mark, and the largest quarter-over-quarter declines.
- **BDCs** — every BDC side by side: mark, QoQ fair-value change, non-accrual
  and PIK exposure, stressed share, top-ten concentration.
- **Non-accruals** — trajectory, concentration by industry and by lender, and
  every current non-accrual with how long it has been one.
- **Marks** — weighted average mark by sector over time, and the positions
  furthest below cost.
- **Maturities** — the refinancing wall, with the stressed slice split out.
- **Disagreements** — credits held by two or more BDCs, ranked by how far apart
  the marks are.
- **Positions** — the full schedule for any BDC, filterable.

## Layout

```
bdctracker/
  universe.py        the 43 BDCs, with a live verifier against the SEC BDC Report
  sources/dera.py    SEC DERA bulk BDC data sets  (primary)
  sources/xbrl.py    per-filing XBRL extraction   (fallback / newest quarter)
  identity.py        issuer, loan and credit keys — the backbone of the dataset
  normalize.py       coercion, unit fixes, merge/dedupe, quality flags
  pipeline.py        harvest -> normalise -> load
  db.py              SQLite schema (bdcs / issuers / loans / marks) + loaders
  analytics.py       the tracker's questions, as SQL over `marks`
  export.py, api.py  one named view registry, served live or frozen to JSON
web/                 front end: no build step, no CDN, light and dark
```

`marks` is the grain everything reads: **one row per (loan, quarter)**.

## Running it where the SEC is reachable

The extraction needs `sec.gov`. If your network blocks it — a corporate proxy, a
sandboxed environment — `bdc doctor` will say so in two seconds rather than
letting a harvest grind through retries.

The repository carries a workflow that runs the harvest on GitHub's runners,
which have open egress: **Actions → Harvest BDC marks → Run workflow**. Set an
`EDGAR_IDENTITY` repository secret (`Name email@example.com`) or pass the
identity input, choose a starting quarter, and the run uploads `bdc-marks.xlsx`,
`bdc-tracker.html`, the SQLite database and the JSON export as artifacts. The
quarterly SEC data sets are cached between runs.

## Status

Run against live EDGAR on 2026-08-28 (`--start 2023Q4`, all 43 BDCs, 18 minutes
on a GitHub runner):

```
bdcs_with_marks       43        marks             152,272
issuers           26,717        periods                35
loans             40,934        earliest_period   2023-10-31
debt_loans        26,305        latest_period     2026-06-30
total_fair_value  $202,414,257,256
```

The bulk data sets carried it in 6 minutes; the filing top-up added the newest
quarter in 9 more.

**Known gap: identity is under-collapsing.** 40,934 loans and 26,717 borrowers
is roughly three times what a mature tracker of this universe reports (~11,400
loans). Fair value and the marks themselves look right — $202bn is the correct
order of magnitude for the sector — so this is a keying problem, not an
extraction one: the same borrower spelled differently across filers, or the
same facility described differently across quarters, is landing on more than
one key.

Where to look, in order:

1. **Issuer names.** `Investment, Issuer Name Axis` is not tagged by every
   filer, so `split_identifier` falls back to splitting the full investment
   label on its first comma. A filer that writes "First Lien Term Loan — Acme
   Corp" rather than "Acme Corp, First Lien Term Loan" gets the instrument as
   its borrower. Sample `issuers.display_name` and see which shapes appear.
2. **Tranche signatures.** Check whether one borrower at one BDC has several
   loan ids that ought to be one.
3. **Period ends.** 35 distinct dates over eleven quarters is plausible given
   BDCs run different fiscal year ends (Golub in September, most in December),
   but worth confirming none are artefacts.

## Licence## Licence

MIT.
