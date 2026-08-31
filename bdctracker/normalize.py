"""Coercion, unit fixes and data-quality checks for raw SOI rows."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence

from bdctracker import identity
from bdctracker.models import Position

_REFERENCE_RATES = (
    ("TERM SOFR", re.compile(r"term\s*sofr", re.I)),
    ("SOFR", re.compile(r"\bsofr\b", re.I)),
    ("LIBOR", re.compile(r"\blibor\b|\bl\s*\+", re.I)),
    ("EURIBOR", re.compile(r"\beuribor\b", re.I)),
    ("SONIA", re.compile(r"\bsonia\b", re.I)),
    ("PRIME", re.compile(r"\bprime\b|\bp\s*\+", re.I)),
    ("BASE", re.compile(r"\bbase\s*rate\b", re.I)),
)

_CURRENCIES = (
    ("EUR", re.compile(r"\beur\b|€", re.I)),
    ("GBP", re.compile(r"\bgbp\b|£", re.I)),
    ("CAD", re.compile(r"\bcad\b", re.I)),
    ("AUD", re.compile(r"\baud\b", re.I)),
    ("SEK", re.compile(r"\bsek\b", re.I)),
    ("DKK", re.compile(r"\bdkk\b", re.I)),
    ("NOK", re.compile(r"\bnok\b", re.I)),
    ("CHF", re.compile(r"\bchf\b", re.I)),
)

_NONACCRUAL = re.compile(r"non[\s-]*accrual|nonaccrual", re.I)

#: Filers who do not tag a maturity date usually still write it in the label.
_MATURITY_IN_LABEL = re.compile(
    # Whitespace and punctuation only between the keyword and the date: a gap
    # that could match letters eats into the month name ("due March" -> "rch").
    r"\b(?:due|matur(?:es|ity|ing))\b[\s:,-]{0,6}"
    r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|[A-Za-z]{3,9}\.?\s+\d{4})",
    re.I,
)

#: Two-letter and common long-form country names seen in SOI geography members.
_COUNTRY_ALIASES = {
    "US": "United States", "USA": "United States", "U.S.": "United States",
    "UNITED STATES OF AMERICA": "United States", "UNITED STATES": "United States",
    "UK": "United Kingdom", "U.K.": "United Kingdom",
    "GREAT BRITAIN": "United Kingdom", "UNITED KINGDOM": "United Kingdom",
    "CA": "Canada", "CANADA": "Canada", "AU": "Australia", "AUSTRALIA": "Australia",
    "DE": "Germany", "GERMANY": "Germany", "FR": "France", "FRANCE": "France",
    "NL": "Netherlands", "NETHERLANDS": "Netherlands", "IE": "Ireland",
    "IRELAND": "Ireland", "LU": "Luxembourg", "LUXEMBOURG": "Luxembourg",
    "CH": "Switzerland", "SWITZERLAND": "Switzerland", "SE": "Sweden",
    "SWEDEN": "Sweden", "ES": "Spain", "SPAIN": "Spain", "IT": "Italy",
    "ITALY": "Italy", "NZ": "New Zealand", "NEW ZEALAND": "New Zealand",
    "SG": "Singapore", "SINGAPORE": "Singapore", "IN": "India", "INDIA": "India",
    "IL": "Israel", "ISRAEL": "Israel", "BM": "Bermuda", "BERMUDA": "Bermuda",
    "KY": "Cayman Islands", "CAYMAN ISLANDS": "Cayman Islands",
}

#: Currencies imply a country only when the filer said nothing else.
_CURRENCY_COUNTRY = {
    "GBP": "United Kingdom", "CAD": "Canada", "AUD": "Australia",
    "SEK": "Sweden", "DKK": "Denmark", "NOK": "Norway", "CHF": "Switzerland",
}


def maturity_from_label(*texts: str | None) -> date | None:
    """Read a maturity out of the position label.

    Three quarters of positions arrive without a tagged maturity date, but the
    label almost always carries one — "due 6/30/2029" — because it is what the
    schedule prints.
    """
    joined = " ".join(t for t in texts if t)
    match = _MATURITY_IN_LABEL.search(joined)
    if not match:
        return None
    text = match.group(1).strip().rstrip(",")
    parsed = to_date(text)
    if parsed:
        return parsed
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(text.replace(".", ""), fmt).date()
        except ValueError:
            continue
    return None


def canonical_country(value: str | None, currency: str | None = None) -> str | None:
    """Normalise a geography member; fall back to what the currency implies."""
    if value:
        text = re.sub(r"\[member\]|member$", "", str(value), flags=re.I).strip(" .,")
        text = _WS.sub(" ", text) if (_WS := re.compile(r"\s+")) else text
        if text:
            return _COUNTRY_ALIASES.get(text.upper(), text)
    if currency and currency != "USD":
        return _CURRENCY_COUNTRY.get(currency)
    return "United States" if currency == "USD" else None

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d", "%d-%b-%Y", "%b %d, %Y")


def to_decimal(value) -> Decimal | None:
    """Parse a money-ish value, tolerating "$1,234", "(500)" and blanks."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "-", "—"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("$", "").replace(",", "").replace("%", "").strip()
    if not text:
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return -result if negative else result


def to_float(value) -> float | None:
    result = to_decimal(value)
    return None if result is None else float(result)


def to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # DERA ships period ends as YYYYMMDD integers.
    if text.isdigit() and len(text) == 8:
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def to_rate_pct(value) -> float | None:
    """Normalise an interest rate to percentage points.

    XBRL is supposed to carry rates as decimal fractions (0.1125 = 11.25%) but
    filers routinely tag the percentage instead, so anything above 1.5 is taken
    at face value. A genuine sub-1.5% coupon is vanishingly rare in a BDC book,
    and the alternative — reading 11.25 as 1125% — is far more damaging.
    """
    result = to_float(value)
    if result is None:
        return None
    if abs(result) <= 1.5:
        return result * 100.0
    return result


def detect_reference_rate(*texts: str | None) -> str | None:
    joined = " ".join(t for t in texts if t)
    for name, pattern in _REFERENCE_RATES:
        if pattern.search(joined):
            return name
    return None


def detect_currency(*texts: str | None) -> str:
    joined = " ".join(t for t in texts if t)
    for code, pattern in _CURRENCIES:
        if pattern.search(joined):
            return code
    return "USD"


def detect_non_accrual(*texts: str | None) -> bool | None:
    """True if a label says non-accrual, otherwise unknown — never False.

    Most filers disclose non-accrual in a footnote rather than in the position
    label, so the absence of the phrase says nothing. Positions are only
    resolved to False once :func:`bdctracker.pipeline.infer_accrual_status`
    establishes that the filing discloses non-accruals at all.
    """
    joined = " ".join(t for t in texts if t)
    if not joined.strip():
        return None
    return True if _NONACCRUAL.search(joined) else None


def finalize(position: Position) -> Position:
    """Derive classification and keys once the raw fields are populated."""
    issuer, remainder = identity.split_identifier(position.identifier)
    if not position.issuer_name:
        position.issuer_name = issuer
    if position.tranche_text is None:
        position.tranche_text = remainder or position.identifier

    label_sources = (position.tranche_text, position.identifier, position.industry)
    if position.investment_type in (None, "", "UNKNOWN"):
        position.investment_type, position.lien = identity.classify_investment(*label_sources)
    elif position.lien in (None, "", "NA"):
        _, position.lien = identity.classify_investment(position.investment_type, *label_sources)

    position.facility = position.facility or identity.facility_kind(*label_sources)
    position.is_debt = identity.is_debt(position.investment_type)
    if position.currency == "USD":
        position.currency = detect_currency(position.tranche_text, position.identifier)
    position.reference_rate = position.reference_rate or detect_reference_rate(
        position.tranche_text, position.identifier
    )
    if position.is_non_accrual is None:
        position.is_non_accrual = detect_non_accrual(position.tranche_text, position.identifier)
    if position.maturity_date is None:
        position.maturity_date = maturity_from_label(position.identifier, position.tranche_text)
    position.country = canonical_country(position.country, position.currency)

    position.issuer_id = identity.issuer_key(position.issuer_name)
    position.credit_id = identity.credit_key(position.issuer_name, position.investment_type)
    position.loan_id = identity.loan_key(
        position.cik,
        position.issuer_name,
        position.investment_type,
        position.tranche_text,
        position.currency,
    )
    return position


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------

#: A mark outside this band is almost always a units problem, not a valuation.
PLAUSIBLE_MARK_RANGE = (1.0, 200.0)


def within_window(
    positions: Iterable[Position],
    start: date | None = None,
    end: date | None = None,
) -> list[Position]:
    """Keep only marks whose balance-sheet date is real and in scope.

    Filings carry more period ends than the schedule being reported: prior-year
    comparatives, restated columns, and forward-dated contexts. Left in, they
    fragment the panel and — worse — a future-dated period becomes ``MAX(period_end)``,
    so the tracker's "latest quarter" lands on a date with almost no positions
    and every headline total collapses.
    """
    today = date.today()
    kept: list[Position] = []
    for position in positions:
        if position.period_end > today:
            continue
        if start and position.period_end < start:
            continue
        if end and position.period_end > end:
            continue
        kept.append(position)
    return kept


def flag_quality(positions: Sequence[Position]) -> Sequence[Position]:
    """Annotate positions with data-quality flags.

    The two failure modes that actually corrupt a BDC dataset are (a) a filer
    tagging thousands where the taxonomy wants units, which makes a whole
    filing's marks look like 0.1, and (b) a fair value with no par to divide by.
    Both are flagged rather than dropped, so downstream code can decide.
    """
    for position in positions:
        if position.fair_value is None:
            position.flags.append("no_fair_value")
        if position.is_debt and not position.principal:
            position.flags.append("no_principal")
        mark = position.mark
        if mark is not None and not (PLAUSIBLE_MARK_RANGE[0] <= mark <= PLAUSIBLE_MARK_RANGE[1]):
            position.flags.append("implausible_mark")
        if position.investment_type == "UNKNOWN":
            position.flags.append("unclassified")
    return positions


def detect_scale_anomaly(positions: Sequence[Position]) -> float | None:
    """Return a suggested multiplier if a filing's marks look mis-scaled.

    Compares the median debt mark against par. A filing whose median mark sits
    near 0.1 or 100,000 has a units problem, and the factor of 1000 is the fix.
    """
    marks = [
        p.mark for p in positions
        if p.is_debt and p.mark is not None and p.mark > 0
    ]
    if len(marks) < 10:
        return None
    marks.sort()
    median = marks[len(marks) // 2]
    if median <= 0:
        return None
    for factor in (1000.0, 1 / 1000.0, 100.0, 1 / 100.0):
        if 80.0 <= median * factor <= 120.0:
            return factor
    return None


def apply_scale(positions: Sequence[Position], factor: float) -> Sequence[Position]:
    """Rescale principal/cost/shares so marks land back in a sane band."""
    multiplier = Decimal(str(factor))
    for position in positions:
        for attr in ("principal", "cost", "shares"):
            value = getattr(position, attr)
            if value is not None:
                setattr(position, attr, value / multiplier)
        position.flags.append(f"rescaled_x{factor:g}")
    return positions


def merge_within_filing(positions: Iterable[Position]) -> list[Position]:
    """Sum positions that share a loan key inside a single filing.

    A BDC can hold two facilities of the same kind to one borrower. They share a
    loan key by design (see :func:`bdctracker.identity.loan_key`), so they are
    added together here — before cross-filing dedupe, which would otherwise
    throw one of them away.
    """
    groups: dict[tuple[str, str | None, date], list[Position]] = defaultdict(list)
    for position in positions:
        groups[(position.loan_id, position.accession, position.period_end)].append(position)

    merged: list[Position] = []
    for members in groups.values():
        if len(members) == 1:
            merged.append(members[0])
            continue
        merged.append(_sum_positions(drop_restatements(members)))
    return merged


def drop_restatements(members: Sequence[Position]) -> list[Position]:
    """Remove rows that restate a sibling's fair value rather than adding to it.

    Filers disclose the same position twice: once as the investment, with par,
    cost and fair value, and again under a sub-identifier carrying *only* fair
    value — the fair-value-hierarchy or valuation-technique breakdown of money
    already counted. Main Street tags each facility a second time this way,
    which doubled every one of its fair values when summed.

    So within a group, a member priced with par or cost is a facility; a member
    with fair value alone, alongside priced siblings, is a restatement of it.
    Where *no* member is priced, nothing can be distinguished and all are kept.
    """
    priced = [m for m in members if m.principal is not None or m.cost is not None]
    if not priced or len(priced) == len(members):
        return list(members)

    for dropped in (m for m in members if m not in priced):
        dropped.flags.append("dropped_restatement")
    return priced


def _sum_positions(members: Sequence[Position]) -> Position:
    base = max(members, key=_completeness)
    totals = {attr: _sum_attr(members, attr) for attr in ("fair_value", "cost", "principal", "shares")}
    # Rates are weighted by fair value so a tiny add-on cannot swing the coupon.
    # Computed before the totals land on `base`, which is itself one of the
    # members — overwriting its fair value first would corrupt the weights.
    rates = {attr: _weighted(members, attr) for attr in ("interest_rate", "spread", "pik_rate")}
    pct_net_assets = _sum_float(members, "pct_net_assets")

    for attr, value in totals.items():
        setattr(base, attr, value)
    for attr, value in rates.items():
        if value is not None:
            setattr(base, attr, value)
    base.pct_net_assets = pct_net_assets
    base.maturity_date = max(
        (m.maturity_date for m in members if m.maturity_date), default=base.maturity_date
    )
    base.is_non_accrual = any(bool(m.is_non_accrual) for m in members) or base.is_non_accrual
    base.flags.append(f"merged_{len(members)}_facilities")
    return base


def _sum_attr(members: Sequence[Position], attr: str) -> Decimal | None:
    values = [getattr(m, attr) for m in members if getattr(m, attr) is not None]
    return sum(values, Decimal(0)) if values else None


def _sum_float(members: Sequence[Position], attr: str) -> float | None:
    values = [getattr(m, attr) for m in members if getattr(m, attr) is not None]
    return sum(values) if values else None


def _weighted(members: Sequence[Position], attr: str) -> float | None:
    pairs = [
        (getattr(m, attr), float(m.fair_value or 0))
        for m in members
        if getattr(m, attr) is not None
    ]
    if not pairs:
        return None
    total_weight = sum(w for _, w in pairs)
    if total_weight <= 0:
        return sum(v for v, _ in pairs) / len(pairs)
    return sum(v * w for v, w in pairs) / total_weight


def dedupe(positions: Iterable[Position]) -> list[Position]:
    """Collapse the same (loan, period) reported by more than one filing.

    A 10-K carries the current *and* prior year-end schedules, and consecutive
    filings overlap, so the same mark arrives several times. Prefer the row with
    the most populated fields, then the most recently filed.
    """
    best: dict[tuple[str, date], Position] = {}
    for position in positions:
        key = (position.loan_id, position.period_end)
        incumbent = best.get(key)
        if incumbent is None or _completeness(position) > _completeness(incumbent):
            best[key] = position
    return list(best.values())


def _completeness(position: Position) -> tuple[int, date]:
    filled = sum(
        1
        for value in (
            position.fair_value, position.cost, position.principal, position.interest_rate,
            position.spread, position.maturity_date, position.industry, position.pct_net_assets,
        )
        if value is not None
    )
    return filled, position.filed_date or date.min


def summarize_quality(positions: Sequence[Position]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for position in positions:
        for flag in position.flags:
            counts[flag] += 1
    return {
        "positions": len(positions),
        "debt_positions": sum(1 for p in positions if p.is_debt),
        "with_mark": sum(1 for p in positions if p.mark is not None),
        "flags": dict(counts),
    }
