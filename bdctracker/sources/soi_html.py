"""Read the Schedule of Investments as it is printed, not as it is tagged.

Filers tag fair value, cost, principal and rates per investment, and stop there.
Industry, country, maturity and acquisition date are printed as columns and
section headings in the rendered schedule and appear nowhere in the XBRL — a
concept census over the two largest BDCs found neither date nor either axis. So
those fields have to come from the table itself.

This parses that table and returns attributes only. The numbers stay with the
XBRL, which is unambiguous; matching a parsed row to a tagged position by
borrower and facility is reliable enough to carry a sector, and not reliable
enough to carry a valuation.

Coverage varies by how a filer lays the schedule out. Against Blackstone
Secured Lending it parses about 1,370 rows a filing and carries an acquisition
date on 99% of them, a maturity on 90% and a sector on all of them. Against
Main Street it recognises the column layout and still returns only the
money-market holdings at the end: that schedule is split across dozens of
page-sized tables and its rows do not line up with the header found for them.
So the parse fills what it can and leaves the rest to the tagging.
`bdc schedule --debug` prints each table's header, its classification and each
row's populated cells, which is how a filer that returns nothing gets diagnosed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

from bdctracker import identity, normalize

log = logging.getLogger(__name__)

#: Header wording varies by filer; match on what a column is for.
COLUMN_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("acquisition", re.compile(r"acquisition\s*date|date\s*acquired|initial\s*acquisition", re.I)),
    ("maturity", re.compile(r"maturity|due\s*date", re.I)),
    ("industry", re.compile(r"industry|sector", re.I)),
    ("country", re.compile(r"country|geograph|domicile", re.I)),
    ("fair_value", re.compile(r"fair\s*value", re.I)),
    ("cost", re.compile(r"\bcost\b|amortized\s*cost", re.I)),
    ("principal", re.compile(r"principal|\bpar\b|face\s*amount", re.I)),
    ("instrument", re.compile(r"investment\s*type|type\s*of\s*investment|security|instrument", re.I)),
    ("issuer", re.compile(r"portfolio\s*compan|company|issuer|investment(?!\s*type)", re.I)),
)

#: Rows that aggregate rather than describe a position.
_TOTAL_ROW = re.compile(r"^\s*(sub)?total|^\s*net\s|aggregate", re.I)

#: A heading cell that names a section rather than a borrower.
_INDUSTRY_PREFIX = re.compile(r"^\s*(industry|sector)\s*[:\-—]\s*", re.I)

_NUMERIC = re.compile(r"^\(?\s*[$€£]?\s*[\d,]+(\.\d+)?\s*\)?$")

#: Filers split one logical column across several cells — a "$" in its own cell,
#: the number in the next, a footnote marker after — and a header that spans
#: three columns lands its label on the first of them. So a value is looked for
#: in a small window from where its header sits, not in that exact cell.
_COLUMN_WINDOW = 4


@dataclass(slots=True)
class SoiAttributes:
    """What the printed schedule knows and the XBRL does not."""

    issuer: str
    industry: str | None = None
    country: str | None = None
    maturity_date: date | None = None
    acquisition_date: date | None = None
    instrument: str | None = None
    fair_value: float | None = None
    #: The unit the fair value was printed to, in the schedule's own scale.
    fair_value_step: float = 1.0

    @property
    def issuer_id(self) -> str:
        return identity.issuer_key(self.issuer)

    @property
    def tranche(self) -> str:
        return identity.tranche_signature(self.instrument or "")


def cell_text(cell) -> str:
    """Flatten a cell to plain text, whether it holds a string or a node."""
    content = getattr(cell, "content", cell)
    if content is None:
        return ""
    if not isinstance(content, str):
        content = getattr(content, "text", None) or str(content)
    return re.sub(r"\s+", " ", str(content)).strip()


def _classify(headers: list[str]) -> dict[str, int]:
    """Map each field onto the column that supplies it.

    Patterns run most specific first, and a column is claimed once: "Acquisition
    Date" must not also answer to the maturity pattern, and the bare "Investment"
    header must not outrank "Investment Type".
    """
    columns: dict[str, int] = {}
    claimed: set[int] = set()
    for field, pattern in COLUMN_PATTERNS:
        for index, header in enumerate(headers):
            if index in claimed or not header:
                continue
            if pattern.search(header):
                columns[field] = index
                claimed.add(index)
                break
    return columns


def _flatten_headers(table) -> list[str]:
    """Join stacked header rows, so "Fair" over "Value" reads as one column."""
    width = 0
    for row in getattr(table, "headers", []) or []:
        width = max(width, sum(max(1, getattr(c, "colspan", 1)) for c in row))
    if not width:
        return []

    merged = [""] * width
    for row in table.headers:
        position = 0
        for cell in row:
            text = cell_text(cell)
            span = max(1, getattr(cell, "colspan", 1))
            for offset in range(span):
                if position + offset < width and text:
                    merged[position + offset] = f"{merged[position + offset]} {text}".strip()
            position += span
    return merged


#: A schedule describes each holding as well as pricing it. Requiring one of
#: these keeps the balance sheet out: it also names things and carries figures,
#: and on a loose test its rows arrive as borrowers called "550,612" in a sector
#: called "LIABILITIES".
_DESCRIPTIVE = ("instrument", "maturity", "acquisition", "industry")


def is_schedule_of_investments(columns: dict[str, int]) -> bool:
    """A schedule names a borrower, describes the holding, and prices it."""
    priced = "fair_value" in columns and ("cost" in columns or "principal" in columns)
    described = any(field in columns for field in _DESCRIPTIVE)
    return priced and described and "issuer" in columns


def looks_like_a_borrower(text: str) -> bool:
    """Text that could name a company, rather than a figure or a section label."""
    if not text or _NUMERIC.match(text) or _TOTAL_ROW.match(text):
        return False
    letters = sum(c.isalpha() for c in text)
    if letters < 3:
        return False
    # Balance-sheet captions shout; borrowers do not.
    stripped = re.sub(r"[^A-Za-z]", "", text)
    return not (stripped.isupper() and len(stripped) > 4)


def _row_values(row, width: int) -> list[str]:
    values = [""] * width
    position = 0
    for cell in getattr(row, "cells", []):
        text = cell_text(cell)
        span = max(1, getattr(cell, "colspan", 1))
        if position < width:
            values[position] = text
        position += span
    return values


def _near(values: list[str], index: int | None, match=None) -> str | None:
    """The first cell at or just after ``index`` that has content.

    ``match`` narrows it to cells of a given shape, so a numeric column skips
    the "$" that precedes its figure.
    """
    if index is None:
        return None
    for offset in range(_COLUMN_WINDOW):
        position = index + offset
        if position >= len(values):
            break
        text = values[position]
        if not text or text in "$€£":
            continue
        if match is None or match.match(text):
            return text
        if match is not None:
            continue
    return None


def _to_number(text: str | None) -> float | None:
    """Read a printed figure, which may carry a currency symbol or parentheses."""
    parsed = _to_number_and_step(text)
    return None if parsed is None else parsed[0]


def _to_number_and_step(text: str | None) -> tuple[float, float] | None:
    """The figure, and the unit it was printed to.

    A schedule stated in millions to one decimal prints an $11,847,000 holding
    as 11.8, so what the figure asserts is a range half a step wide. Matching it
    to a tagged value needs that step, not a fixed tolerance.
    """
    if not text:
        return None
    cleaned = text.strip().replace(",", "").lstrip("$€£").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    decimals = len(cleaned.partition(".")[2])
    return (-value if negative else value), 10.0 ** -decimals


def _is_section_heading(values: list[str]) -> str | None:
    """A lone text cell in an otherwise empty row names the section it opens."""
    filled = [v for v in values if v]
    if len(filled) != 1:
        return None
    text = filled[0]
    if _NUMERIC.match(text) or _TOTAL_ROW.match(text) or len(text) > 90:
        return None
    return _INDUSTRY_PREFIX.sub("", text).strip(" :–—-") or None


#: How many leading rows to consider as a header when the markup marks none.
_HEADER_SCAN = 6

#: Fewer rows than this and the table is something else that happened to match.
_MIN_SCHEDULE_ROWS = 3


def locate_header(table) -> tuple[list[str], dict[str, int], int]:
    """Find the header, whether the markup declares one or not.

    SEC filings rarely use ``<th>``: the header is usually the first row or two
    of ordinary cells, sometimes stacked. So fall back to scanning the leading
    rows and keeping whichever yields a usable schedule, which also tells us
    where the data starts.
    """
    declared = _flatten_headers(table)
    columns = _classify(declared)
    if is_schedule_of_investments(columns):
        return declared, columns, 0

    rows = list(getattr(table, "rows", []))
    best: tuple[list[str], dict[str, int], int] | None = None
    for index in range(min(_HEADER_SCAN, len(rows))):
        # Stacked headers: join this row with the one above it as well.
        for start in range(index + 1):
            merged = _merge_rows(rows[start : index + 1])
            candidate = _classify(merged)
            if not is_schedule_of_investments(candidate):
                continue
            if best is None or len(candidate) > len(best[1]):
                best = (merged, candidate, index + 1)
        if best:
            break
    return best or ([], {}, 0)


def _merge_rows(rows) -> list[str]:
    """Join a run of rows cell-wise, as stacked header lines."""
    width = 0
    for row in rows:
        width = max(width, sum(max(1, getattr(c, "colspan", 1)) for c in row.cells))
    merged = [""] * width
    for row in rows:
        position = 0
        for cell in row.cells:
            text = cell_text(cell)
            span = max(1, getattr(cell, "colspan", 1))
            for offset in range(span):
                if position + offset < width and text:
                    merged[position + offset] = f"{merged[position + offset]} {text}".strip()
            position += span
    return merged


def parse_table(table) -> list[SoiAttributes]:
    """Extract one schedule table, carrying section headings down its rows."""
    headers, columns, skip = locate_header(table)
    if not is_schedule_of_investments(columns):
        return []
    return read_rows(table, columns, skip, width=len(headers))


def _priced(text: str | None) -> dict:
    """The fair value and its printed unit, as keyword arguments."""
    parsed = _to_number_and_step(text)
    if parsed is None:
        return {}
    return {"fair_value": parsed[0], "fair_value_step": parsed[1]}


def read_rows(table, columns: dict[str, int], skip: int, width: int = 0) -> list[SoiAttributes]:
    """Read a table's rows against a known column layout."""
    width = max(width, max(columns.values()) + 1)
    out: list[SoiAttributes] = []
    section: str | None = None

    for row in list(getattr(table, "rows", []))[skip:]:
        values = _row_values(row, width)
        heading = _is_section_heading(values)
        if heading:
            section = heading
            continue

        issuer = _near(values, columns.get("issuer")) or ""
        if not looks_like_a_borrower(issuer):
            continue
        # A borrower row prices something; a stray label does not.
        if not any(
            _near(values, columns.get(field), _NUMERIC)
            for field in ("fair_value", "cost", "principal")
        ):
            continue

        def value(field: str) -> str | None:
            return _near(values, columns.get(field))

        industry = value("industry") or section
        out.append(
            SoiAttributes(
                issuer=issuer,
                industry=industry or None,
                country=normalize.canonical_country(value("country")),
                maturity_date=normalize.to_date(value("maturity"))
                or normalize.maturity_from_label(value("maturity")),
                acquisition_date=normalize.to_date(value("acquisition")),
                instrument=value("instrument") or issuer,
                **_priced(_near(values, columns.get("fair_value"), _NUMERIC)),
            )
        )

    # A schedule lists many holdings. A couple of rows scraped out of some other
    # table is a false positive, not a short schedule.
    return out if len(out) >= _MIN_SCHEDULE_ROWS else []


def parse_filing(filing) -> list[SoiAttributes]:
    """Parse every schedule table in a filing's primary document."""
    try:
        html = filing.html()
    except Exception as exc:
        log.warning("no HTML for %s: %s", getattr(filing, "accession_no", "?"), exc)
        return []
    if not html:
        return []

    from edgar.documents import parse_html

    try:
        document = parse_html(html)
    except Exception as exc:
        log.warning("could not parse HTML for %s: %s", getattr(filing, "accession_no", "?"), exc)
        return []

    return parse_tables(document.tables)


def parse_tables(tables) -> list[SoiAttributes]:
    """Parse a document's schedule, including the pages that carry no header.

    A schedule long enough to run over a page break is emitted as a run of
    tables, and only the first of them repeats the column headings — which is
    why Ares parses eleven hundred borrowers and not one acquisition date. So
    the layout found on a headed table is carried forward, and a later table is
    read against it when doing so yields a run of priced borrower rows. The
    guards that keep the balance sheet out do the work here too: a row must
    name something that reads as a borrower and put a figure under a priced
    column, and a table must yield several such rows to count as a schedule
    page at all.
    """
    rows: list[SoiAttributes] = []
    carried: dict[str, int] | None = None

    for table in tables:
        try:
            headers, columns, skip = locate_header(table)
            if is_schedule_of_investments(columns):
                carried = columns
                rows.extend(read_rows(table, columns, skip, width=len(headers)))
                continue
            if carried is None:
                continue
            continuation = read_rows(table, carried, skip=0)
            if _is_a_page_of_the_schedule(continuation, carried):
                rows.extend(continuation)
        except Exception as exc:
            log.debug("table skipped: %s", exc)
    return rows


#: A carried header describes holdings, so the rows under it must describe some.
#: Below this share carrying an attribute, the table is something else that
#: happens to name things and price them.
_DESCRIBED_SHARE = 0.5


def _is_a_page_of_the_schedule(rows: list[SoiAttributes], columns: dict[str, int]) -> bool:
    """Decide whether a headerless table is another page of the same schedule.

    A schedule page says something about each holding beyond its price. Main
    Street's fair-value rollforward does not: it names an asset class, prices
    it, and leaves every descriptive column empty — so read against a carried
    header it yields borrowers called "Debt" and "Equity". Requiring most rows
    to carry an attribute the header promised separates the two.
    """
    if len(rows) < _MIN_SCHEDULE_ROWS:
        return False
    promised = [f for f in ("maturity", "acquisition", "instrument", "industry")
                if f in columns]
    if not promised:
        return False

    def described(row: SoiAttributes) -> bool:
        return any((
            "maturity" in promised and row.maturity_date is not None,
            "acquisition" in promised and row.acquisition_date is not None,
            "industry" in promised and row.industry is not None,
            # instrument falls back to the issuer, so it only counts when it differs.
            "instrument" in promised and row.instrument not in (None, row.issuer),
        ))

    return sum(described(row) for row in rows) >= len(rows) * _DESCRIBED_SHARE


def build_index(rows: list[SoiAttributes]) -> dict:
    """Group parsed rows by borrower, which is how they are matched back.

    Industry and country belong to the borrower, so any row settles them.
    Maturity and acquisition date belong to one facility, so they are matched
    on the fair value printed beside them — see :func:`enrich`.
    """
    by_issuer: dict[str, list[SoiAttributes]] = {}
    for row in rows:
        if row.issuer:
            by_issuer.setdefault(row.issuer_id, []).append(row)
    return {"issuer": by_issuer}


#: A printed schedule states figures in thousands or millions; the XBRL states
#: them in dollars. The ratio between the two is one of these.
_SCALES = (1.0, 1e3, 1e6)

#: A floor under the tolerance, for the rounding the tagged side does too.
_VALUE_TOLERANCE = 0.001


def _choose_scale(pairs_by_issuer) -> float | None:
    """Pick the scale the whole schedule is stated in, by what it matches.

    A schedule states every figure in the same unit, so the scale is a property
    of the filing and inferring it per borrower is what broke Ares: comparing
    the largest printed figure for one borrower against the largest tagged one
    fails whenever the tagging carries a tranche the parse did not reach, and
    the borrower is then skipped entirely rather than matched.

    Each candidate is scored on how many positions it actually matches across
    the filing, and the best wins. That is the quantity the scale exists to
    serve, and a wrong scale matches almost nothing.
    """
    best, best_count = None, 0
    for scale in _SCALES:
        count = sum(
            len(_pair_on_value(rows, holdings, scale))
            for rows, holdings in pairs_by_issuer
        )
        if count > best_count:
            best, best_count = scale, count
    return best


def _within_reach(row: SoiAttributes, tagged: float, scale: float) -> float | None:
    """How far a printed row sits from a tagged value, if close enough to be it.

    The window is set by what the figure was printed to, not by a fixed
    percentage: Ares states millions to one decimal, so 11.8 asserts only that
    the holding lies between 11.75m and 11.85m, and demanding a tenth of a
    percent rejects every row it should match. Blackstone states thousands to
    the unit, where the same rule is far tighter.
    """
    printed = row.fair_value * scale
    window = max(0.5 * row.fair_value_step * scale, abs(tagged) * _VALUE_TOLERANCE)
    gap = abs(printed - tagged)
    return gap if gap <= window else None


def _disagree(left: SoiAttributes, right: SoiAttributes) -> bool:
    """Whether two rows would fill the dates differently."""
    return (left.maturity_date != right.maturity_date
            or left.acquisition_date != right.acquisition_date)


def _pair_on_value(rows: list[SoiAttributes], positions: list, scale: float) -> list[tuple]:
    """Match printed rows to tagged positions by the value each one carries.

    A borrower may hold several facilities and the printed schedule names them
    inconsistently, so the value is the key. Matching runs closest-first, so a
    clear pair is never displaced by a marginal one.

    Where a coarse printed scale leaves two rows equally able to be the same
    position and they would date it differently, neither is used. A blank date
    is a smaller error than a confident wrong one.
    """
    priced = [(index, row) for index, row in enumerate(rows) if row.fair_value is not None]
    candidates = []
    reachable: dict[int, list[int]] = {}
    for position_index, position in enumerate(positions):
        tagged = float(position.fair_value)
        if tagged <= 0:
            continue
        for row_index, row in priced:
            gap = _within_reach(row, tagged, scale)
            if gap is not None:
                candidates.append((gap, row_index, position_index))
                reachable.setdefault(position_index, []).append(row_index)

    candidates.sort()
    used_rows: set[int] = set()
    used_positions: set[int] = set()
    pairs = []
    for _, row_index, position_index in candidates:
        if row_index in used_rows or position_index in used_positions:
            continue
        rivals = reachable.get(position_index, ())
        if any(other != row_index and _disagree(rows[other], rows[row_index])
               for other in rivals):
            continue
        used_rows.add(row_index)
        used_positions.add(position_index)
        pairs.append((rows[row_index], positions[position_index]))
    return pairs


def enrich(positions, index: dict) -> int:
    """Fill blank attributes on positions from the printed schedule.

    Only ever fills blanks: a tagged value is authoritative, and the parse is
    the fallback for what the tagging omits.
    """
    by_issuer = index.get("issuer", {})
    if not by_issuer:
        return 0

    grouped: dict[str, list] = {}
    for position in positions:
        grouped.setdefault(position.issuer_id, []).append(position)

    # One scale for the whole schedule, chosen before any of it is applied.
    scale = _choose_scale([
        (by_issuer[issuer_id], [p for p in holdings if p.fair_value is not None])
        for issuer_id, holdings in grouped.items() if issuer_id in by_issuer
    ])

    filled = 0
    for issuer_id, holdings in grouped.items():
        rows = by_issuer.get(issuer_id)
        if not rows:
            continue

        # Sector and country describe the borrower, so any row that names them
        # applies to every holding in it.
        industry = next((r.industry for r in rows if r.industry), None)
        country = next((r.country for r in rows if r.country), None)
        touched = set()
        for position in holdings:
            if industry and position.industry is None:
                position.industry = industry
                touched.add(id(position))
            if country and position.country is None:
                position.country = country
                touched.add(id(position))

        if scale is not None:
            priced = [p for p in holdings if p.fair_value is not None]
            for row, position in _pair_on_value(rows, priced, scale):
                if position.maturity_date is None and row.maturity_date:
                    position.maturity_date = row.maturity_date
                    touched.add(id(position))
                if position.acquisition_date is None and row.acquisition_date:
                    position.acquisition_date = row.acquisition_date
                    touched.add(id(position))

        for position in holdings:
            if id(position) in touched:
                position.flags.append("enriched_from_schedule")
                filled += 1
    return filled
