"""Primary source: the SEC's quarterly BDC Data Sets.

DERA re-publishes every BDC's XBRL Schedule of Investments as a flat TSV, one
row per tagged position, for every BDC that filed in the quarter. Pulling eight
of these zips covers the whole universe with eight requests instead of several
hundred filing downloads, so this is the default path; :mod:`bdctracker.sources.xbrl`
fills the gaps.

Dataset home: https://www.sec.gov/data-research/sec-markets-data/bdc-data-sets
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from bdctracker import normalize
from bdctracker.config import SETTINGS, SecUnreachable, configure_edgar
from bdctracker.models import Position

log = logging.getLogger(__name__)

BASE_URL = "https://www.sec.gov/files/datastandardsinnovation/data/business-development-company-bdc-data-sets"

#: DERA labels the SOI columns with taxonomy labels; map them onto our fields.
COLUMN_MAP = {
    "Investment, Identifier Axis": "investment_id",
    "Investment, Issuer Name Axis": "issuer",
    "Investment, Name Axis": "investment_name",
    "Investment Type Axis": "investment_type",
    "Industry Sector Axis": "industry",
    "Geographical Axis": "country",
    "Statement Geographical Axis": "country",
    "Investment, Country Axis": "country",
    "Investment, Issuer Affiliation Axis": "affiliation",
    "Lien Category Axis": "lien_category",
    "Asset Class Axis": "asset_class",
    "Financial Instrument Axis": "instrument_type",
    "Fair Value Hierarchy and NAV Axis": "fair_value_level",
    "Valuation Approach and Technique Axis": "valuation_method",
    "Investment Owned, Fair Value": "fair_value",
    "Investment Owned, Cost": "cost",
    "Investment Owned, Balance, Principal Amount": "principal",
    "Investment Owned, Balance, Shares": "shares",
    "Investment Owned, Net Assets, Percentage": "pct_net_assets",
    "Investment Interest Rate": "interest_rate",
    "Investment, Basis Spread, Variable Rate": "spread",
    "Investment, Interest Rate, Paid in Kind": "pik_rate",
    "Investment Maturity Date": "maturity_date",
    "Investment, Acquisition Date": "acquisition_date",
    "ddate": "period_end",
    "inlineurl": "filing_url",
}

_MEMBER_SUFFIX = " [Member]"


@dataclass(frozen=True)
class Quarter:
    year: int
    quarter: int

    def __str__(self) -> str:
        return f"{self.year}Q{self.quarter}"

    @property
    def filename(self) -> str:
        return f"{self.year}q{self.quarter}_bdc.zip"

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.filename}"

    @classmethod
    def parse(cls, text: str) -> "Quarter":
        cleaned = text.strip().upper().replace("-", "")
        year, _, quarter = cleaned.partition("Q")
        return cls(int(year), int(quarter))


def quarters_between(start: Quarter, end: Quarter) -> list[Quarter]:
    """Inclusive range of quarters, oldest first."""
    out: list[Quarter] = []
    year, quarter = start.year, start.quarter
    while (year, quarter) <= (end.year, end.quarter):
        out.append(Quarter(year, quarter))
        quarter += 1
        if quarter > 4:
            year, quarter = year + 1, 1
    return out


def download_quarter(quarter: Quarter, *, refresh: bool = False) -> Path | None:
    """Download one quarterly zip into the cache; return its path.

    Returns ``None`` when the SEC has not published that quarter yet, which is
    the normal state for the most recent one or two quarters.

    Raises :class:`SecUnreachable` for anything that is not a plain "not
    published" — an unpublished quarter is expected and skippable, a blocked
    network is not, and treating the two alike hides the real problem behind a
    list of missing quarters.
    """
    SETTINGS.ensure_dirs()
    target = SETTINGS.dera_dir / quarter.filename
    if target.exists() and not refresh:
        return target

    configure_edgar()
    import httpx
    from edgar.httprequests import get_with_retry

    try:
        response = get_with_retry(quarter.url)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            log.info("BDC data set %s not published yet", quarter)
            return None
        raise SecUnreachable(f"fetching {quarter}: {exc}") from None
    except httpx.ProxyError as exc:
        raise SecUnreachable(
            f"Blocked before reaching the SEC while fetching {quarter}: {exc}"
        ) from None
    except httpx.HTTPError as exc:
        raise SecUnreachable(f"fetching {quarter}: {type(exc).__name__}: {exc}") from None

    if response.status_code == 404:
        log.info("BDC data set %s not published yet", quarter)
        return None
    if response.status_code >= 400:
        raise SecUnreachable(f"{quarter.url} returned HTTP {response.status_code}")

    target.write_bytes(response.content)
    log.info("Cached %s (%.1f MB)", quarter, len(response.content) / 1e6)
    return target


def _read_member(zip_path: Path, name: str) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as archive:
        for candidate in (name, f"datasets/{name}", f"datasets\\{name}"):
            if candidate in archive.namelist():
                with archive.open(candidate) as handle:
                    return pd.read_csv(io.BytesIO(handle.read()), sep="\t", low_memory=False)
    return pd.DataFrame()


def read_quarter(zip_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(soi, submissions)`` frames from a cached quarterly zip."""
    soi = _read_member(zip_path, "soi.tsv")
    if soi.empty:
        soi = _read_member(zip_path, "soi.txt")
    subs = _read_member(zip_path, "sub.tsv")
    if subs.empty:
        subs = _read_member(zip_path, "sub.txt")
    return soi, subs


#: Filers define their own axes, so any column whose label is *about* industry
#: or geography feeds those fields even when its name is not in COLUMN_MAP.
_SUBJECT_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"industr|sector", re.I), "industry"),
    (re.compile(r"geograph|countr|region|jurisdiction", re.I), "country"),
)


def _subject_columns(columns) -> dict[str, str]:
    """Map unrecognised axis columns onto the field they describe."""
    out: dict[str, str] = {}
    for column in columns:
        if column in COLUMN_MAP.values():
            continue
        for pattern, field in _SUBJECT_PATTERNS:
            if pattern.search(str(column)):
                out.setdefault(field, str(column))
                break
    return out


def tidy(soi: pd.DataFrame) -> pd.DataFrame:
    """Rename DERA's label columns and strip ``[Member]`` decoration."""
    if soi.empty:
        return soi
    frame = soi.rename(columns=COLUMN_MAP)
    for column in frame.columns:
        # pandas 3 gives text columns a `str` dtype rather than `object`.
        if pd.api.types.is_object_dtype(frame[column]) or pd.api.types.is_string_dtype(frame[column]):
            frame[column] = frame[column].map(
                lambda v: v.replace(_MEMBER_SUFFIX, "").strip() if isinstance(v, str) else v
            )
    return frame


def _first(row: pd.Series, *names: str):
    for name in names:
        if name in row.index:
            value = row[name]
            if value is not None and not (isinstance(value, float) and pd.isna(value)):
                return value
    return None


def to_positions(frame: pd.DataFrame, ciks: Iterable[int] | None = None) -> list[Position]:
    """Convert a tidied SOI frame into :class:`Position` records."""
    if frame.empty:
        return []
    if ciks is not None and "cik" in frame.columns:
        frame = frame[frame["cik"].isin(set(ciks))]
    if frame.empty:
        return []

    extra = _subject_columns(frame.columns)
    positions: list[Position] = []
    for row in frame.to_dict(orient="records"):
        series = pd.Series(row)
        period_end = normalize.to_date(_first(series, "period_end", "ddate"))
        if period_end is None:
            continue
        cik = _first(series, "cik")
        if cik is None:
            continue

        identifier = _first(series, "investment_id", "investment_name", "issuer") or ""
        issuer = _first(series, "issuer") or ""
        descriptor = " ".join(
            str(v)
            for v in (
                _first(series, "investment_name"),
                _first(series, "investment_type"),
                _first(series, "lien_category"),
                _first(series, "asset_class"),
                _first(series, "instrument_type"),
                identifier,
            )
            if v
        )

        position = Position(
            cik=int(cik),
            period_end=period_end,
            identifier=str(identifier),
            issuer_name=str(issuer) if issuer else "",
            tranche_text=descriptor or None,
            industry=_first(series, "industry", *(
                [extra["industry"]] if "industry" in extra else [])),
            country=_first(series, "country", *(
                [extra["country"]] if "country" in extra else [])),
            fair_value=normalize.to_decimal(_first(series, "fair_value")),
            cost=normalize.to_decimal(_first(series, "cost")),
            principal=normalize.to_decimal(_first(series, "principal")),
            shares=normalize.to_decimal(_first(series, "shares")),
            interest_rate=normalize.to_rate_pct(_first(series, "interest_rate")),
            spread=normalize.to_rate_pct(_first(series, "spread")),
            pik_rate=normalize.to_rate_pct(_first(series, "pik_rate")),
            pct_net_assets=normalize.to_rate_pct(_first(series, "pct_net_assets")),
            maturity_date=normalize.to_date(_first(series, "maturity_date")),
            acquisition_date=normalize.to_date(_first(series, "acquisition_date")),
            fair_value_level=_first(series, "fair_value_level"),
            accession=_first(series, "adsh"),
            form=_first(series, "form"),
            filed_date=normalize.to_date(_first(series, "filed")),
            source="dera",
        )
        positions.append(normalize.finalize(position))
    return positions


def harvest(
    quarters: Sequence[Quarter],
    ciks: Iterable[int] | None = None,
    *,
    refresh: bool = False,
) -> tuple[list[Position], list[Quarter]]:
    """Download and parse a run of quarters.

    Returns the positions plus the quarters that could not be fetched, so the
    caller can fall back to per-filing XBRL for exactly those.
    """
    collected: list[Position] = []
    missing: list[Quarter] = []
    for quarter in quarters:
        path = download_quarter(quarter, refresh=refresh)
        if path is None:
            missing.append(quarter)
            continue
        soi, _subs = read_quarter(path)
        if soi.empty:
            missing.append(quarter)
            continue
        collected.extend(to_positions(tidy(soi), ciks))
    return collected, missing


def latest_published_quarter(today: date | None = None) -> Quarter:
    """Best guess at the newest quarter DERA could have published.

    DERA lags filings by roughly a quarter, so start probing one quarter back
    rather than burning a request on a period that cannot exist yet.
    """
    today = today or date.today()
    quarter = (today.month - 1) // 3 + 1
    year = today.year
    quarter -= 1
    if quarter == 0:
        year, quarter = year - 1, 4
    return Quarter(year, quarter)
