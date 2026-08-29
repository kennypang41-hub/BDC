"""Independent check of the dataset behind the workbook and the site.

Recomputes the matrices straight from ``commitments.json`` -- the same shape the
workbook's SUMIFS produce -- so the numbers can be eyeballed without opening
Excel, and flags anything structurally wrong (unknown tickers or quarters,
duplicate observations, non-positive values, implausible jumps).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "commitments.json"
METRICS = ["uncommenced_leases", "purchase_commitments"]


def main() -> int:
    d = json.loads(DATA.read_text())
    tickers = [c["ticker"] for c in d["companies"]]
    quarters = d["quarters"]
    problems: list[str] = []

    seen: set[tuple[str, str, str]] = set()
    for o in d["observations"]:
        key = (o["ticker"], o["quarter"], o["metric"])
        if key in seen:
            problems.append(f"duplicate observation {key}")
        seen.add(key)
        if o["ticker"] not in tickers:
            problems.append(f"unknown ticker {o['ticker']}")
        if o["quarter"] not in quarters:
            problems.append(f"unknown quarter {o['quarter']}")
        if o["metric"] not in METRICS:
            problems.append(f"unknown metric {o['metric']}")
        if not isinstance(o["value"], (int, float)) or o["value"] <= 0:
            problems.append(f"non-positive value {key}")
        if o["provenance"] not in d["meta"]["provenance"]:
            problems.append(f"unknown provenance {key}")

    grid = {(o["ticker"], o["quarter"], o["metric"]): o["value"] for o in d["observations"]}

    for metric in METRICS:
        print(f"\n=== {metric}  (USD bn) ===")
        print("quarter  " + "".join(f"{t:>10}" for t in tickers) + f"{'TOTAL':>11}{'n':>5}")
        for q in quarters:
            vals = [grid.get((t, q, metric)) for t in tickers]
            cells = "".join(f"{v:>10.1f}" if v is not None else f"{'-':>10}" for v in vals)
            present = [v for v in vals if v is not None]
            total = f"{sum(present):>11.1f}" if present else f"{'-':>11}"
            print(f"{q}  {cells}{total}{len(present):>5}")

    # A quarter-on-quarter move of more than 4x on an existing base is worth a
    # second look: it is usually a year mixed up, not a real signing.
    for metric in METRICS:
        for t in tickers:
            series = [(q, grid[(t, q, metric)]) for q in quarters if (t, q, metric) in grid]
            for (q0, v0), (q1, v1) in zip(series, series[1:]):
                if v0 > 0 and (v1 / v0 > 4 or v0 / v1 > 4):
                    problems.append(f"large jump {t} {metric} {q0} {v0} -> {q1} {v1}")

    print(f"\nobservations: {len(d['observations'])} of {len(tickers) * len(quarters) * len(METRICS)} cells")
    strong = sum(1 for o in d["observations"] if o["provenance"] in ("filing", "filing_table"))
    print(f"from filing text or tables: {strong}; from press or derived: {len(d['observations']) - strong}")

    if problems:
        print("\nPROBLEMS")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nno structural problems found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
