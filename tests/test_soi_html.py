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

def _position(identifier, **kwargs):
    return normalize.finalize(Position(
        cik=1396440, period_end=date(2026, 6, 30), identifier=identifier,
        fair_value=Decimal("9800"), principal=Decimal("10000"), source="xbrl", **kwargs,
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
    """Acme's revolver matures two years before its term loan."""
    index = soi_html.build_index(soi_html.parse_table(schedule()))
    revolver = _position("Acme Holdings, LLC, Revolver")
    soi_html.enrich([revolver], index)
    assert revolver.maturity_date == date(2028, 6, 30)


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
