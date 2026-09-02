"""Parsing the printed Schedule of Investments.

The fixture reproduces how BDCs actually lay the schedule out: stacked header
rows, industry as a section heading spanning the table rather than a column,
subtotal rows between sections, and a mix of facilities per borrower.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest

from bdctracker import normalize
from bdctracker.models import Position
from bdctracker.sources import soi_html


@dataclass
class FakeCell:
    content: str
    colspan: int = 1


@dataclass
class FakeRow:
    cells: list


@dataclass
class FakeTable:
    headers: list = field(default_factory=list)
    rows: list = field(default_factory=list)


def _row(*values, spans=None):
    spans = spans or [1] * len(values)
    return FakeRow([FakeCell(v, s) for v, s in zip(values, spans)])


HEADERS = [
    [FakeCell("Portfolio Company"), FakeCell("Investment Type"), FakeCell("Acquisition"),
     FakeCell("Maturity"), FakeCell("Principal"), FakeCell("Amortized"), FakeCell("Fair")],
    [FakeCell(""), FakeCell(""), FakeCell("Date"), FakeCell("Date"),
     FakeCell(""), FakeCell("Cost"), FakeCell("Value")],
]


def schedule() -> FakeTable:
    return FakeTable(
        headers=HEADERS,
        rows=[
            # Industry as a section heading spanning the row — the common layout.
            _row("Software", "", "", "", "", "", "", spans=[7, 1, 1, 1, 1, 1, 1]),
            _row("Acme Holdings, LLC", "First Lien Term Loan", "3/15/2021",
                 "6/30/2029", "10,000", "9,950", "9,800"),
            _row("Acme Holdings, LLC", "Revolver", "3/15/2021",
                 "6/30/2028", "1,000", "990", "980"),
            _row("Total Software", "", "", "", "11,000", "10,940", "10,780"),
            _row("Health Care Providers", "", "", "", "", "", "", spans=[7, 1, 1, 1, 1, 1, 1]),
            _row("Beta Industries Inc.", "Second Lien Term Loan", "9/1/2019",
                 "3/31/2027", "5,000", "5,000", "2,000"),
            _row("Total investments", "", "", "", "16,000", "15,940", "12,780"),
        ],
    )


def test_the_schedule_table_is_recognised_and_others_are_not():
    columns = {"issuer": 0, "instrument": 1, "fair_value": 6, "cost": 5}
    assert soi_html.is_schedule_of_investments(columns)
    # A balance sheet names no borrower.
    assert not soi_html.is_schedule_of_investments({"fair_value": 1, "cost": 2})
    # A commitments table prices nothing.
    assert not soi_html.is_schedule_of_investments({"issuer": 0, "maturity": 1})


def test_rows_carry_the_industry_heading_that_opened_their_section():
    rows = soi_html.parse_table(schedule())
    assert len(rows) == 3
    by_issuer = {r.issuer: r for r in rows}
    assert by_issuer["Acme Holdings, LLC"].industry == "Software"
    assert by_issuer["Beta Industries Inc."].industry == "Health Care Providers"


def test_totals_and_subtotals_are_not_positions():
    issuers = {r.issuer for r in soi_html.parse_table(schedule())}
    assert not any(i.lower().startswith("total") for i in issuers)


def test_dates_are_read_from_their_own_columns():
    rows = soi_html.parse_table(schedule())
    loan = next(r for r in rows if r.instrument == "First Lien Term Loan")
    assert loan.acquisition_date == date(2021, 3, 15)
    assert loan.maturity_date == date(2029, 6, 30)


def test_stacked_header_rows_are_joined_before_matching():
    """"Acquisition" over "Date" is one column, and must not read as maturity."""
    rows = soi_html.parse_table(schedule())
    revolver = next(r for r in rows if r.instrument == "Revolver")
    assert revolver.maturity_date == date(2028, 6, 30)
    assert revolver.acquisition_date == date(2021, 3, 15)


def test_a_table_that_is_not_a_schedule_yields_nothing():
    other = FakeTable(
        headers=[[FakeCell("Assets"), FakeCell("2026"), FakeCell("2025")]],
        rows=[_row("Cash", "1,000", "900")],
    )
    assert soi_html.parse_table(other) == []


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def _position(identifier, fair_value="9800000", principal="10000000", **kwargs):
    """A tagged position, priced in dollars against a schedule in thousands."""
    return normalize.finalize(Position(
        cik=1396440, period_end=date(2026, 6, 30), identifier=identifier,
        fair_value=Decimal(fair_value), principal=Decimal(principal),
        source="xbrl", **kwargs,
    ))


def test_enrichment_fills_the_fields_the_xbrl_omits():
    index = soi_html.build_index(soi_html.parse_table(schedule()))
    position = _position("Acme Holdings, LLC, First Lien Term Loan")
    assert position.industry is None and position.acquisition_date is None

    assert soi_html.enrich([position], index) == 1
    assert position.industry == "Software"
    assert position.acquisition_date == date(2021, 3, 15)
    assert position.maturity_date == date(2029, 6, 30)
    assert "enriched_from_schedule" in position.flags


def test_enrichment_never_overwrites_a_tagged_value():
    """A tagged value is authoritative; the parse only fills blanks."""
    index = soi_html.build_index(soi_html.parse_table(schedule()))
    position = _position(
        "Acme Holdings, LLC, First Lien Term Loan",
        industry="Tagged Sector", maturity_date=date(2030, 1, 1),
    )
    soi_html.enrich([position], index)
    assert position.industry == "Tagged Sector"
    assert position.maturity_date == date(2030, 1, 1)


def test_maturity_follows_the_facility_not_just_the_borrower():
    """Acme's revolver matures two years before its term loan.

    Both are tagged under the same borrower and the schedule names facilities
    inconsistently, so the fair value beside each printed row is what tells the
    revolver from the term loan.
    """
    index = soi_html.build_index(soi_html.parse_table(schedule()))
    revolver = _position("Acme Holdings, LLC, Revolver",
                         fair_value="980000", principal="1000000")
    term_loan = _position("Acme Holdings, LLC, First Lien Term Loan")

    soi_html.enrich([revolver, term_loan], index)
    assert revolver.maturity_date == date(2028, 6, 30)
    assert term_loan.maturity_date == date(2029, 6, 30)


def test_a_facility_the_schedule_does_not_price_keeps_its_sector():
    """No value to match on costs the dates, not the borrower's attributes."""
    index = soi_html.build_index(soi_html.parse_table(schedule()))
    position = _position("Acme Holdings, LLC, Delayed Draw", fair_value="4242424")

    assert soi_html.enrich([position], index) == 1
    assert position.industry == "Software"
    assert position.maturity_date is None
    assert position.acquisition_date is None


def test_a_schedule_stated_in_dollars_matches_too():
    """Scale is inferred from the two sets, not assumed to be thousands."""
    index = soi_html.build_index(soi_html.parse_table(schedule()))
    position = _position("Acme Holdings, LLC, First Lien Term Loan",
                         fair_value="9800", principal="10000")

    soi_html.enrich([position], index)
    assert position.maturity_date == date(2029, 6, 30)


def test_an_unmatched_borrower_is_left_alone():
    index = soi_html.build_index(soi_html.parse_table(schedule()))
    position = _position("Unrelated Corp, First Lien Term Loan")
    assert soi_html.enrich([position], index) == 0
    assert position.industry is None
    assert position.flags == []


# ---------------------------------------------------------------------------
# Headers the markup does not declare
# ---------------------------------------------------------------------------

def headerless_schedule() -> FakeTable:
    """SEC filings rarely use <th>; the header is just the first rows of cells."""
    return FakeTable(
        headers=[],
        rows=[
            _row("Portfolio Company", "Investment Type", "Acquisition",
                 "Maturity", "Principal", "Amortized", "Fair"),
            _row("", "", "Date", "Date", "", "Cost", "Value"),
            _row("Software", "", "", "", "", "", "", spans=[7, 1, 1, 1, 1, 1, 1]),
            _row("Acme Holdings, LLC", "First Lien Term Loan", "3/15/2021",
                 "6/30/2029", "10,000", "9,950", "9,800"),
            _row("Acme Holdings, LLC", "Revolver", "3/15/2021",
                 "6/30/2028", "1,000", "990", "980"),
            _row("Beta Industries Inc.", "Second Lien Term Loan", "9/1/2019",
                 "3/31/2027", "5,000", "5,000", "2,000"),
        ],
    )


def test_a_header_in_ordinary_cells_is_still_found():
    headers, columns, skip = soi_html.locate_header(headerless_schedule())
    assert soi_html.is_schedule_of_investments(columns)
    assert skip == 2  # both header lines consumed


def test_headerless_tables_parse_and_keep_their_dates_apart():
    rows = soi_html.parse_table(headerless_schedule())
    assert len(rows) == 3
    first = rows[0]
    assert first.issuer == "Acme Holdings, LLC"
    assert first.industry == "Software"
    assert first.acquisition_date == date(2021, 3, 15)
    assert first.maturity_date == date(2029, 6, 30)


def test_the_header_row_is_not_emitted_as_a_position():
    issuers = {r.issuer for r in soi_html.parse_table(headerless_schedule())}
    assert "Portfolio Company" not in issuers


# ---------------------------------------------------------------------------
# Columns split across cells
# ---------------------------------------------------------------------------

def split_cell_schedule() -> FakeTable:
    """Main Street's layout: every logical column spans three physical ones,
    the currency symbol is its own cell, and footnote markers trail the name."""
    return FakeTable(
        headers=[],
        rows=[
            FakeRow([
                FakeCell("Portfolio Company (1) (20)", 3), FakeCell("", 3),
                FakeCell("Type of Investment (2) (3)", 3), FakeCell("Maturity", 3),
                FakeCell("Principal", 3), FakeCell("Cost", 3), FakeCell("Fair Value", 3),
            ]),
            FakeRow([FakeCell("Software", 21)]),
            FakeRow([
                FakeCell("Acme Holdings, LLC", 3), FakeCell("", 3),
                FakeCell("First Lien Term Loan", 3), FakeCell("6/30/2029", 3),
                # "$" in its own cell, the figure in the next.
                FakeCell("$", 1), FakeCell("10,000", 1), FakeCell("", 1),
                FakeCell("$", 1), FakeCell("9,950", 1), FakeCell("", 1),
                FakeCell("$", 1), FakeCell("9,800", 1), FakeCell("", 1),
            ]),
            FakeRow([
                FakeCell("Beta Industries Inc.", 3), FakeCell("", 3),
                FakeCell("Second Lien Term Loan", 3), FakeCell("3/31/2027", 3),
                FakeCell("$", 1), FakeCell("5,000", 1), FakeCell("", 1),
                FakeCell("$", 1), FakeCell("5,000", 1), FakeCell("", 1),
                FakeCell("$", 1), FakeCell("2,000", 1), FakeCell("", 1),
            ]),
            FakeRow([
                FakeCell("Gamma Corp", 3), FakeCell("", 3),
                FakeCell("First Lien Term Loan", 3), FakeCell("12/31/2030", 3),
                FakeCell("$", 1), FakeCell("2,500", 1), FakeCell("", 1),
                FakeCell("$", 1), FakeCell("2,480", 1), FakeCell("", 1),
                FakeCell("$", 1), FakeCell("2,450", 1), FakeCell("", 1),
            ]),
        ],
    )


def test_a_value_is_found_when_the_currency_symbol_owns_its_own_cell():
    """The header lands on the first of three columns; the figure does not."""
    rows = soi_html.parse_table(split_cell_schedule())
    assert len(rows) == 3
    assert rows[0].issuer == "Acme Holdings, LLC"
    assert rows[0].industry == "Software"
    assert rows[0].maturity_date == date(2029, 6, 30)


def test_a_spanning_heading_still_reads_as_a_section():
    """The industry heading spans the table as one wide cell."""
    heading = soi_html._is_section_heading(["Software"] + [""] * 20)
    assert heading == "Software"


# ---------------------------------------------------------------------------
# Keeping other tables out
# ---------------------------------------------------------------------------

def test_a_balance_sheet_is_not_a_schedule():
    """It names things and carries figures, but describes no holding."""
    assert not soi_html.is_schedule_of_investments(
        {"issuer": 0, "fair_value": 3, "cost": 4}
    )
    assert soi_html.is_schedule_of_investments(
        {"issuer": 0, "fair_value": 3, "cost": 4, "maturity": 2}
    )


def test_a_figure_or_a_shouted_caption_is_not_a_borrower():
    assert not soi_html.looks_like_a_borrower("550,612")
    assert not soi_html.looks_like_a_borrower("LIABILITIES")
    assert not soi_html.looks_like_a_borrower("Total investments")
    assert not soi_html.looks_like_a_borrower("$")
    assert soi_html.looks_like_a_borrower("Acme Holdings, LLC")
    assert soi_html.looks_like_a_borrower("RA Outdoors LLC")


def test_a_table_yielding_only_a_row_or_two_is_discarded():
    """A real schedule lists many holdings; two rows is a false positive."""
    table = FakeTable(
        headers=[],
        rows=[
            _row("Portfolio Company", "Type of Investment", "Maturity", "Cost", "Fair Value"),
            _row("Acme Holdings, LLC", "First Lien", "6/30/2029", "9,950", "9,800"),
            _row("Beta Inc.", "Second Lien", "3/31/2027", "5,000", "2,000"),
        ],
    )
    assert soi_html.parse_table(table) == []


# ---------------------------------------------------------------------------
# A schedule that runs over a page break
# ---------------------------------------------------------------------------

def continuation_page() -> FakeTable:
    """The next page of the same schedule: rows, no headings, same columns."""
    return FakeTable(
        headers=[],
        rows=[
            _row("Gamma Systems Corp.", "First Lien Term Loan", "7/1/2022",
                 "9/30/2030", "8,000", "7,900", "7,750"),
            _row("Delta Freight LLC", "First Lien Term Loan", "2/14/2023",
                 "2/14/2031", "3,000", "2,980", "2,900"),
            _row("Epsilon Foods Inc.", "Second Lien Term Loan", "11/2/2020",
                 "11/2/2028", "6,000", "5,900", "4,100"),
        ],
    )


def unrelated_table() -> FakeTable:
    """The exhibit index, which follows the schedule and names things too."""
    return FakeTable(
        headers=[],
        rows=[
            _row("3.1", "Articles of Amendment and Restatement", "", "", "", "", ""),
            _row("4.1", "Sixth Supplemental Indenture, dated May 2026", "", "", "", "", ""),
            _row("10.2", "Amendment No. 4 to the Credit Agreement", "", "", "", "", ""),
        ],
    )


def test_a_second_page_is_read_against_the_first_pages_header():
    """Only the first page repeats the headings; the rest are the same table."""
    rows = soi_html.parse_tables([schedule(), continuation_page()])
    issuers = [r.issuer for r in rows]
    assert "Gamma Systems Corp." in issuers
    assert "Epsilon Foods Inc." in issuers

    gamma = next(r for r in rows if r.issuer == "Gamma Systems Corp.")
    assert gamma.acquisition_date == date(2022, 7, 1)
    assert gamma.maturity_date == date(2030, 9, 30)
    assert gamma.fair_value == 7750


def test_a_continuation_page_is_not_carried_into_the_exhibit_index():
    """Carrying a header forward must not turn every later table into holdings."""
    rows = soi_html.parse_tables([schedule(), unrelated_table()])
    assert not any(r.issuer.startswith(("3.1", "4.1", "10.2")) for r in rows)
    assert all("Indenture" not in r.issuer for r in rows)


def test_nothing_is_carried_before_a_header_is_ever_found():
    assert soi_html.parse_tables([continuation_page()]) == []
