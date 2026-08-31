"""The bundle: one named payload per view, served live or frozen to JSON.

The API and the static export read the same registry, so the front end has a
single code path whether it is talking to a server or to a folder of files.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from bdctracker import analytics

#: name -> builder. Everything the front end can ask for.
BUNDLE: dict[str, Callable[[sqlite3.Connection, str | None], object]] = {
    "overview": lambda conn, period: analytics.overview(conn, period),
    "bdcs": lambda conn, period: analytics.bdc_summary(conn, period),
    "nonaccrual_trend": lambda conn, period: analytics.nonaccrual_trend(conn),
    "nonaccruals": lambda conn, period: analytics.nonaccrual_positions(conn, period),
    "nonaccrual_by_industry": lambda conn, period: analytics.nonaccrual_by(conn, "industry", period),
    "nonaccrual_by_bdc": lambda conn, period: analytics.nonaccrual_by(conn, "ticker", period),
    "mark_histogram": lambda conn, period: analytics.mark_histogram(conn, period),
    "sector_marks": lambda conn, period: analytics.sector_marks(conn),
    "maturity_wall": lambda conn, period: analytics.maturity_wall(conn, period),
    "markdowns": lambda conn, period: analytics.biggest_markdowns(conn, period, limit=200),
    "deteriorating": lambda conn, period: analytics.deteriorating(conn, period, limit=200),
    "disagreements": lambda conn, period: analytics.disagreements(conn, period, limit=300),
    "shared_credits": lambda conn, period: analytics.shared_credits(conn, period),
    "quarterly_marks": lambda conn, period: analytics.quarterly_bdc_marks(conn),
    "quarterly_nonaccrual_marks": lambda conn, period: analytics.quarterly_nonaccrual_marks(conn),
    "quarterly_nonaccrual_share": lambda conn, period: analytics.quarterly_nonaccrual_share(conn),
    "country_exposure": lambda conn, period: analytics.country_exposure(conn, period),
}


def build(conn: sqlite3.Connection, name: str, period: str | None = None):
    if name not in BUNDLE:
        raise KeyError(name)
    return BUNDLE[name](conn, period)


def meta(conn: sqlite3.Connection, period: str | None = None, synthetic: bool | None = None) -> dict:
    period = period or analytics.latest_period(conn)
    if synthetic is None:
        row = conn.execute("SELECT COUNT(*) FROM marks WHERE source = 'demo'").fetchone()
        total = conn.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
        synthetic = bool(row[0]) and row[0] == total
    return {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "period_end": period,
        "periods": analytics.periods(conn),
        "tickers": [r["ticker"] for r in analytics.bdc_summary(conn, period)],
        "synthetic": synthetic,
        "sources": [r[0] for r in conn.execute("SELECT DISTINCT source FROM marks") if r[0]],
    }


def _default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


def _write(target: Path, name: str, payload) -> Path:
    path = target / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=_default, separators=(",", ":")))
    return path


def export_all(conn: sqlite3.Connection, target: Path, period: str | None = None) -> list[Path]:
    """Freeze every view, plus one positions file per BDC."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    period = period or analytics.latest_period(conn)

    written = [_write(target, f"{name}.json", build(conn, name, period)) for name in BUNDLE]
    document = meta(conn, period)
    for ticker in document["tickers"]:
        written.append(
            _write(target, f"positions/{ticker}.json", analytics.bdc_positions(conn, ticker, period))
        )
    written.append(_write(target, "meta.json", document))
    return written
