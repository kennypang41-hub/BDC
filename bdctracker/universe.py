"""The BDC coverage universe: which filers we pull marks for."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

UNIVERSE_PATH = Path(__file__).with_name("data") / "universe.json"


@dataclass(frozen=True)
class BDC:
    ticker: str
    cik: int
    name: str
    exchange: str | None = None

    @property
    def cik10(self) -> str:
        """Zero-padded CIK as EDGAR paths use it."""
        return f"{self.cik:010d}"


@lru_cache(maxsize=None)
def load_universe(path: str | Path | None = None) -> tuple[BDC, ...]:
    """Load the coverage universe, skipping entries whose CIK is unresolved."""
    doc = json.loads(Path(path or UNIVERSE_PATH).read_text())
    out = []
    for row in doc["bdcs"]:
        if row.get("cik") is None:
            continue
        out.append(
            BDC(
                ticker=row["ticker"],
                cik=int(row["cik"]),
                name=row.get("name") or row["ticker"],
                exchange=row.get("exchange"),
            )
        )
    return tuple(out)


def universe_ciks(path: str | Path | None = None) -> frozenset[int]:
    return frozenset(b.cik for b in load_universe(path))


def by_cik(path: str | Path | None = None) -> dict[int, BDC]:
    return {b.cik: b for b in load_universe(path)}


def by_ticker(path: str | Path | None = None) -> dict[str, BDC]:
    return {b.ticker: b for b in load_universe(path)}


def resolve(key: str | int, path: str | Path | None = None) -> BDC:
    """Look a BDC up by ticker (case-insensitive) or CIK."""
    if isinstance(key, int) or str(key).isdigit():
        cik = int(key)
        try:
            return by_cik(path)[cik]
        except KeyError:
            raise KeyError(f"CIK {cik} is not in the coverage universe") from None
    ticker = str(key).upper()
    try:
        return by_ticker(path)[ticker]
    except KeyError:
        raise KeyError(f"Ticker {ticker!r} is not in the coverage universe") from None


def sync_universe(path: str | Path | None = None) -> dict:
    """Verify the universe against live SEC data.

    Cross-checks every entry against the SEC BDC Report (the authoritative list
    of 814-* filers) and the current ticker file, and tries to resolve any
    watchlist entry that has no CIK yet. Requires network access to sec.gov.

    Returns a report dict; the caller decides whether to write it back.
    """
    from edgar import get_bdc_list
    from edgar.reference.tickers import get_company_tickers

    from bdctracker.config import configure_edgar

    configure_edgar()
    target = Path(path or UNIVERSE_PATH)
    doc = json.loads(target.read_text())

    bdc_list = get_bdc_list()
    official = {}
    for entity in bdc_list:
        official[int(entity.cik)] = entity

    tickers = get_company_tickers()
    ticker_to_cik = {r.ticker: int(r.cik) for r in tickers.itertuples()}
    cik_to_name = {int(r.cik): r.company for r in tickers.itertuples()}

    report = {"confirmed": [], "not_in_bdc_report": [], "renamed": [], "resolved_watchlist": []}

    for row in doc["bdcs"]:
        cik = row.get("cik")
        if cik is None:
            cik = ticker_to_cik.get(row["ticker"])
            if cik is not None:
                row["cik"] = cik
                report["resolved_watchlist"].append(row["ticker"])
        if cik is None:
            continue
        entity = official.get(int(cik))
        if entity is None:
            report["not_in_bdc_report"].append(row["ticker"])
        else:
            row["file_number"] = entity.file_number
            row["last_filing_date"] = (
                entity.last_filing_date.isoformat() if entity.last_filing_date else None
            )
            row["active"] = entity.is_active
            report["confirmed"].append(row["ticker"])
        current_name = cik_to_name.get(int(cik))
        if current_name and current_name != row.get("name"):
            report["renamed"].append({"ticker": row["ticker"], "was": row.get("name"), "now": current_name})
            row["name"] = current_name

    for row in doc.get("watchlist", []):
        if row.get("cik") is None:
            cik = ticker_to_cik.get(row["ticker"])
            if cik is not None:
                row["cik"] = cik
                report["resolved_watchlist"].append(row["ticker"])

    report["document"] = doc
    return report
