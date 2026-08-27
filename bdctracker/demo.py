"""Synthetic dataset for developing the UI and exercising the analytics.

Nothing here comes from a filing. Every dataset it produces is stamped
``synthetic: true`` in ``meta.json`` and the front end shows a banner, so demo
output can never be mistaken for extracted SEC data.
"""
from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from pathlib import Path

from bdctracker import db, normalize
from bdctracker.models import Position
from bdctracker.universe import load_universe

INDUSTRIES = [
    "Software", "Health Care Providers", "Business Services", "Insurance Services",
    "Specialty Retail", "Aerospace & Defense", "Consumer Products", "Media",
    "Chemicals", "Automotive", "Restaurants", "Education",
]

TYPES = [
    ("First Lien Senior Secured Loan", 0.62),
    ("First Lien Senior Secured Delayed Draw Term Loan", 0.08),
    ("First Lien Senior Secured Revolver", 0.07),
    ("Second Lien Senior Secured Loan", 0.09),
    ("Subordinated Debt", 0.04),
    ("Preferred Equity", 0.05),
    ("Common Equity", 0.05),
]


def quarters(count: int, end: date = date(2025, 12, 31)) -> list[date]:
    ends = []
    year, month = end.year, end.month
    for _ in range(count):
        ends.append(date(year, month, [31, 30, 30, 31][month // 3 - 1] if month != 12 else 31))
        month -= 3
        if month <= 0:
            year, month = year - 1, month + 12
    return sorted(ends)


def generate(
    n_quarters: int = 8,
    issuers_per_bdc: int = 180,
    shared_issuers: int = 400,
    seed: int = 20260101,
) -> list[Position]:
    """Build a portfolio per BDC and walk its marks forward through the quarters."""
    rng = random.Random(seed)
    universe = load_universe()
    periods = quarters(n_quarters)

    shared_pool = [f"Meridian Holdco {i}" for i in range(shared_issuers)]
    positions: list[Position] = []

    for bdc in universe:
        names = [f"{bdc.ticker} Portfolio Co {i}" for i in range(issuers_per_bdc)]
        # A third of each book is drawn from the shared pool so the cross-BDC
        # disagreement view has something real to compare.
        names += rng.sample(shared_pool, k=min(len(shared_pool), issuers_per_bdc // 2))

        for name in names:
            label = _weighted_choice(rng, TYPES)
            industry = rng.choice(INDUSTRIES)
            par = Decimal(rng.randrange(2_000_000, 60_000_000, 250_000))
            spread = round(rng.uniform(4.5, 9.0), 2)
            coupon = round(spread + 4.3, 2)
            pik = round(rng.uniform(0.0, 3.0), 2) if rng.random() < 0.15 else 0.0
            maturity = date(rng.randint(2026, 2032), rng.choice([3, 6, 9, 12]), 30)
            mark = rng.gauss(98.5, 4.0)
            drift = rng.gauss(0.0, 0.9)
            impaired = rng.random() < 0.04
            # Books turn over: a position is originated part-way through the
            # window and may be repaid before the end, so the marks-per-loan
            # ratio looks like a real panel rather than a perfect rectangle.
            first = rng.choices(range(n_quarters), weights=[6] + [1] * (n_quarters - 1))[0]
            last = n_quarters - 1
            if rng.random() < 0.28:
                last = rng.randint(first, n_quarters - 1)

            for index, period in enumerate(periods):
                if index < first or index > last:
                    continue
                mark = max(5.0, min(103.0, mark + rng.gauss(drift, 1.2)))
                if impaired and index >= n_quarters // 2:
                    mark = max(5.0, mark - rng.uniform(2.0, 9.0))
                non_accrual = mark < 65.0
                fair_value = (par * Decimal(str(round(mark, 4)))) / Decimal(100)

                position = Position(
                    cik=bdc.cik,
                    period_end=period,
                    identifier=f"{name}, {label}, SOFR + {spread}%, {coupon}%, due {maturity:%m/%d/%Y}",
                    issuer_name=name,
                    industry=industry,
                    fair_value=fair_value.quantize(Decimal("1")),
                    cost=par * Decimal("0.995"),
                    principal=par,
                    interest_rate=coupon,
                    spread=spread,
                    pik_rate=pik,
                    maturity_date=maturity,
                    is_non_accrual=non_accrual,
                    accession=f"demo-{bdc.cik}-{period:%Y%m%d}",
                    form="10-Q" if period.month != 12 else "10-K",
                    filed_date=period,
                    source="demo",
                )
                positions.append(normalize.finalize(position))

    return positions


def _weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    roll = rng.random()
    cumulative = 0.0
    for label, weight in options:
        cumulative += weight
        if roll <= cumulative:
            return label
    return options[-1][0]


def build(db_path: str | Path | None = None, **kwargs) -> dict:
    """Generate and load a synthetic database."""
    positions = normalize.dedupe(normalize.merge_within_filing(generate(**kwargs)))
    normalize.flag_quality(positions)
    with db.session(db_path) as conn:
        db.upsert_bdcs(conn)
        counts = db.load_positions(conn, positions)
        summary = db.stats(conn)
    return {"loaded": counts, "db": summary, "synthetic": True}
