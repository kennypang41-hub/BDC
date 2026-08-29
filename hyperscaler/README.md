# Hyperscaler off-balance-sheet commitments

Twelve quarters (2023Q3 – 2026Q2) of two footnote disclosures for Alphabet, Microsoft,
Meta, Amazon, NVIDIA and Oracle:

1. **Uncommenced lease commitments** — leases signed but not yet commenced. Under ASC 842 a
   lease becomes a liability only when the lessor makes the asset available, so until then the
   whole obligation sits in a footnote.
2. **Purchase commitments** — non-cancelable purchase, supply and unconditional purchase
   obligations from the commitments footnote.

## Files

| Path | What it is |
|---|---|
| `data/commitments.json` | The dataset. One record per (company, quarter, measure), each with a source note and a provenance code. |
| `build_workbook.py` | Renders `output/hyperscaler_commitments.xlsx`. |
| `build_site.py` | Renders `web/index.html` and `output/artifact.html` from `web/page.template.html`. |
| `verify.py` | Recomputes the matrices from the JSON and checks for duplicates, unknown keys and implausible jumps. |
| `output/hyperscaler_commitments.xlsx` | The workbook: Read me, Data, one matrix sheet per measure, Combined, Coverage. |

```
python3 hyperscaler/verify.py          # check the data
python3 hyperscaler/build_workbook.py  # -> output/hyperscaler_commitments.xlsx
python3 hyperscaler/build_site.py      # -> web/index.html
```

The workbook's matrix sheets compute from the `Data` sheet with `SUMIFS` rather than storing
numbers twice, so correcting a row on `Data` updates every view.

## Coverage, honestly

59 of a possible 144 cells are populated. That is not an extraction failure so much as the
shape of the disclosure:

- Several of these companies only began disclosing a material uncommenced-lease balance during
  2025, so 2023 and most of 2024 are genuinely thin.
- NVIDIA does not disclose an uncommenced-lease balance at all; its off-balance-sheet exposure
  sits in supply and capacity purchase obligations instead.
- 2026Q1 is the one quarter where all six disclose a purchase-commitment figure.

**An empty cell means no disclosure was sourced for that quarter. It is never a zero and never
an estimate.** 55 of the 59 observations come from filing text or a filing maturity table; the
remaining 4 are flagged `press` or `derived` everywhere they appear.

## How the figures were collected

SEC.gov and every filing mirror were unreachable from the build environment — the egress policy
blocked every host except GitHub — so figures were collected by searching filing text rather
than parsing EDGAR directly. Two checks were used to keep that honest:

- Where a maturity table was available, its year-by-year rows were required to sum to the stated
  total. That arithmetic check settled a real conflict on Amazon's 2026Q1 purchase commitments
  (`$103.768bn`, against a contaminated `$146bn` from another summary).
- The 2026Q1 cross-section was tested against Morgan Stanley's published $982bn. Alphabet, Meta,
  Amazon and Oracle each land within a billion of it independently. The whole $36bn residual is
  NVIDIA, where this dataset carries the $119bn disclosed for the quarter ended 26 April 2026
  and Morgan Stanley used $155bn — most likely NVIDIA's January fiscal year-end on a broader
  definition. Microsoft's figure for that quarter was itself taken from the chart, so it
  corroborates nothing and is marked as such.

If SEC access is available in a later session, `data/commitments.json` is the only file that
needs repopulating; the workbook and site rebuild from it unchanged.

## A caveat that matters

The six filers do not use one definition. Alphabet reports "purchase commitments and other
contractual obligations", Microsoft a contractual-obligations "purchase commitments" line, Meta
"non-cancelable contractual commitments", Amazon "unconditional purchase obligations", NVIDIA
"purchase commitments and supply obligations", Oracle "unconditional purchase and certain other
obligations". Cross-company totals are indicative, not like-for-like.
