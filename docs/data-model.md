# Data model

Everything the tracker shows is derived from one table.

## Grain

`marks` holds **one row per (loan, quarter)**. That is the mark: a single BDC's
fair value for a single position at a single balance-sheet date.

```
bdcs      ──< loans >── issuers
                │
                └──< marks
```

| Table | Grain | Notes |
|---|---|---|
| `bdcs` | one per filer | ticker, CIK, name — from `bdctracker/data/universe.json` |
| `issuers` | one per borrower | `issuer_id` is shared across BDCs; `display_name` is picked deterministically from the filers' different spellings |
| `loans` | one per (BDC, borrower, instrument, tranche) | carries `credit_id` for cross-BDC matching, and `first_period` / `last_period` |
| `marks` | one per (loan, quarter) | the fact table |
| `filings` | one per accession | provenance |
| `runs` | one per harvest | what was pulled, when, and which quarters were missing |

`v_marks` joins the four together and is what every analytics query reads.

## Key columns on `marks`

| Column | Meaning |
|---|---|
| `fair_value` | the BDC's fair value, in dollars |
| `principal` | par outstanding — the denominator of the mark for debt |
| `cost` | amortised cost — the denominator for equity, and the basis for unrealised |
| `mark` | `100 × fair_value / principal` for debt, `/ cost` for equity |
| `unrealized` | `fair_value − cost` |
| `is_non_accrual` | `1` / `0` / **NULL when the filing did not disclose it** |
| `interest_rate`, `spread`, `pik_rate` | percentage points, normalised |
| `reference_rate` | SOFR / TERM SOFR / LIBOR / EURIBOR / PRIME, read off the label |
| `flags` | comma-separated data-quality flags |
| `source` | `dera`, `xbrl` or `demo` |

Money is REAL dollars. BDC portfolios top out around $30bn, comfortably inside
double precision, and keeping it numeric lets the analytics run as plain SQL.

## Quality flags

| Flag | Meaning |
|---|---|
| `no_fair_value` | position tagged without a fair value |
| `no_principal` | a debt position with no par, so no mark |
| `implausible_mark` | mark outside 1–200; usually a units problem the rescaler could not resolve |
| `unclassified` | the label did not match any instrument rule |
| `merged_N_facilities` | N same-kind facilities to one borrower summed within a filing |
| `rescaled_xN` | the filing's units were off by N and were corrected |

Flagged rows are kept, not dropped. Filter on `flags` when a view needs to be
strict.

## Identity keys

| Key | Scope | Used for |
|---|---|---|
| `issuer_id` | borrower | grouping one borrower across all lenders |
| `loan_id` | (BDC, borrower, instrument, tranche, currency) | the mark time series |
| `credit_id` | (borrower, lien family) | comparing lenders' marks on one credit |

All three are truncated SHA-1 digests of normalised inputs, so they are stable
across runs and machines without a lookup table.

## Reloading

`load_positions` upserts on `(loan_id, period_end)`, so re-harvesting a quarter
overwrites rather than duplicates. Non-accrual is the one column that never
regresses to NULL on update: a later source that does not know the status will
not erase a status an earlier one established.
