# Extraction

## The two sources

### 1. SEC DERA BDC Data Sets — the default

`https://www.sec.gov/files/datastandardsinnovation/data/business-development-company-bdc-data-sets/{year}q{q}_bdc.zip`

Each zip holds `sub.tsv` (submissions), `num.tsv` (numeric facts), `pre.tsv`
(presentation) and **`soi.tsv`** — the Schedule of Investments, one row per
tagged position, already joined to CIK, form, filing date and period end.

Columns arrive under taxonomy labels (`Investment, Identifier Axis`,
`Investment Owned, Fair Value`, …) with `[Member]` decoration on the axis
values. `bdctracker/sources/dera.py` maps them in `COLUMN_MAP` and strips the
decoration.

Zips are cached under `data/cache/dera/`, so a re-run costs nothing. A quarter
the SEC has not published yet 404s; that is expected for the newest one or two
and is reported as a missing quarter rather than an error.

### 2. Per-filing XBRL — the fallback

For quarters DERA has not published and BDCs missing from a bulk set,
`bdctracker/sources/xbrl.py` walks a company's 10-K/10-Q filings via edgartools
and reads the facts directly.

It takes **every instant period** in the filing, not just the latest, because a
10-K carries the prior year-end schedule alongside the current one — one
download, two quarters of marks.

Non-accrual comes from `edgar.bdc.extract_nonaccrual`, which reads the
footnotes. It is best-effort: a miss leaves the flag unset rather than asserting
"accruing".

## Pipeline order

`bdctracker/pipeline.py::clean` runs four steps, and the order matters:

1. **Merge within a filing.** Same-kind facilities to one borrower are summed.
   Rates are weighted by fair value so a small add-on cannot swing the coupon.
2. **Check units, per filing.** The median debt mark is compared against par;
   if the filing is off by 100× or 1000×, principal, cost and shares are
   rescaled. Doing this *before* cross-filing dedupe keeps a mis-scaled filing's
   rows from mixing with a correct one's and hiding the problem.
3. **Resolve accrual status, per filing.** A filing that disclosed any
   non-accrual resolves its remaining unknowns to accruing; one that disclosed
   none leaves them unknown.
4. **Dedupe across filings.** The same (loan, quarter) reported by a 10-Q and
   the following 10-K is one mark. The more complete row wins; ties go to the
   later filing.

## Rate handling

XBRL is supposed to carry rates as decimal fractions (`0.1125` = 11.25%) but
filers routinely tag the percentage instead. `to_rate_pct` takes anything above
1.5 at face value: a genuine sub-1.5% coupon is vanishingly rare in a BDC book,
and the alternative — reading `11.25` as 1125% — is far more damaging.

## Rate limiting

The SEC asks for no more than ten requests a second and rejects traffic without
a contact address in the User-Agent. `EDGAR_IDENTITY` is mandatory —
`configure_edgar` raises immediately rather than letting a run fail hundreds of
requests later. Filing harvests default to four workers.

## What to expect on the first real run

This code has not been run against live EDGAR (see the README). On the first
run, check in this order:

1. **`COLUMN_MAP` coverage.** Print `dera.tidy(soi).columns` and confirm every
   field mapped. DERA has moved its dataset URL before and can rename labels.
2. **The flag histogram.** `bdc harvest` prints `quality.flags`. A large
   `unclassified` count means `identity.py`'s `_TYPE_RULES` need extending for
   wordings this universe uses; a large `no_principal` count is normal (equity)
   but should track the equity share.
3. **Loan continuity.** `SELECT COUNT(*) FROM marks GROUP BY loan_id` should
   average five to seven marks per loan over two years. Much lower means keys
   are breaking between quarters — inspect `loans.identifier` for a borrower you
   know is held throughout and look at what changed.
4. **Cross-BDC matches.** `bdc report disagreements` should surface widely
   syndicated names. If almost nothing matches, `canonical_issuer` is being too
   strict for this universe's spellings.
