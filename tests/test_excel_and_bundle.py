"""The workbook and the standalone page are deliverables — check what they carry."""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from bdctracker import db, excel, normalize, standalone
from bdctracker.models import Position
from bdctracker.universe import BDC

ARCC = BDC(ticker="ARCC", cik=1287750, name="Ares Capital", exchange="Nasdaq")
TSLX = BDC(ticker="TSLX", cik=1508655, name="Sixth Street", exchange="NYSE")
Q = date(2025, 12, 31)


def _position(cik, identifier, fv, par, cost=None, **kwargs):
    return normalize.finalize(
        Position(
            cik=cik, period_end=Q, identifier=identifier, source="dera",
            fair_value=Decimal(str(fv)), principal=Decimal(str(par)),
            cost=Decimal(str(cost if cost is not None else par)),
            accession="0001-25-000001", form="10-K", **kwargs,
        )
    )


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    db.upsert_bdcs(connection, [ARCC, TSLX])
    db.load_positions(connection, [
        _position(ARCC.cik, "Acme Holdings, LLC, First Lien Term Loan",
                  9_000_000, 10_000_000, interest_rate=11.25, spread=5.75),
        _position(TSLX.cik, "ACME Holdings Inc., First Lien Senior Secured Loan",
                  7_000_000, 10_000_000),
        _position(ARCC.cik, "Beta Industries, Second Lien Term Loan",
                  2_000_000, 5_000_000, is_non_accrual=True),
        # Equity: no par, so the mark basis must fall back to cost.
        _position(ARCC.cik, "Beta Industries, Common Equity", 100_000, 0, cost=500_000),
    ])
    connection.commit()
    yield connection
    connection.close()


def test_workbook_has_every_sheet_and_row(conn, tmp_path):
    target = tmp_path / "marks.xlsx"
    result = excel.export_workbook(conn, target)
    assert result["marks"] == 4
    assert result["synthetic"] is False

    book = load_workbook(target)
    assert book.sheetnames == ["Read me", "Marks", "BDC summary", "Disagreements"]
    assert book["Marks"].max_row == 5  # header + 4


def test_mark_is_a_live_formula_not_a_baked_number(conn, tmp_path):
    """Editing a fair value must move the mark, so it cannot be hardcoded."""
    target = tmp_path / "marks.xlsx"
    excel.export_workbook(conn, target)
    sheet = load_workbook(target)["Marks"]

    headers = [cell.value for cell in sheet[1]]
    mark_column = headers.index("Mark (% of par)") + 1
    formula = sheet.cell(row=2, column=mark_column).value
    assert isinstance(formula, str) and formula.startswith("=IFERROR(")
    assert "/" in formula and "*100" in formula


def test_mark_basis_is_par_for_debt_and_cost_for_equity(conn, tmp_path):
    target = tmp_path / "marks.xlsx"
    excel.export_workbook(conn, target)
    sheet = load_workbook(target)["Marks"]
    headers = [cell.value for cell in sheet[1]]
    basis = headers.index("Mark basis") + 1
    borrower = headers.index("Borrower") + 1
    instrument = headers.index("Instrument") + 1

    rows = {
        (sheet.cell(r, borrower).value, sheet.cell(r, instrument).value):
            sheet.cell(r, basis).value
        for r in range(2, sheet.max_row + 1)
    }
    assert rows[("ACME Holdings Inc.", "FIRST_LIEN")] == 10_000_000
    assert rows[("Beta Industries", "COMMON_EQUITY")] == 500_000


def test_rates_are_stored_as_fractions_so_excel_renders_them_as_percentages(conn, tmp_path):
    target = tmp_path / "marks.xlsx"
    excel.export_workbook(conn, target)
    sheet = load_workbook(target)["Marks"]
    headers = [cell.value for cell in sheet[1]]
    coupon = headers.index("Coupon") + 1

    values = [sheet.cell(r, coupon).value for r in range(2, sheet.max_row + 1)]
    assert pytest.approx(0.1125) in [v for v in values if v is not None]
    assert sheet.cell(2, coupon).number_format.endswith("%")


def test_undisclosed_non_accrual_is_labelled_not_left_blank(conn, tmp_path):
    """A blank cell reads as 'no'. The workbook has to say 'Not disclosed'."""
    target = tmp_path / "marks.xlsx"
    excel.export_workbook(conn, target)
    sheet = load_workbook(target)["Marks"]
    headers = [cell.value for cell in sheet[1]]
    column = headers.index("Non-accrual") + 1
    values = {sheet.cell(r, column).value for r in range(2, sheet.max_row + 1)}
    assert values == {"Yes", "Not disclosed"}


def test_readme_warns_loudly_when_the_data_is_synthetic(tmp_path):
    connection = db.connect(":memory:")
    db.init_schema(connection)
    db.upsert_bdcs(connection, [ARCC])
    fake = _position(ARCC.cik, "Demo Co, First Lien Term Loan", 1_000, 1_000)
    fake.source = "demo"
    db.load_positions(connection, [fake])

    target = tmp_path / "demo.xlsx"
    result = excel.export_workbook(connection, target)
    assert result["synthetic"] is True

    text = " ".join(
        str(cell.value)
        for row in load_workbook(target)["Read me"].iter_rows()
        for cell in row if cell.value
    )
    assert "THIS FILE CONTAINS SYNTHETIC DATA" in text
    assert "Nothing here was extracted from an SEC filing" in text
    assert "Do not use for analysis" in text
    connection.close()


def test_readme_stays_quiet_when_the_data_is_real(conn, tmp_path):
    target = tmp_path / "real.xlsx"
    excel.export_workbook(conn, target)
    text = " ".join(
        str(cell.value)
        for row in load_workbook(target)["Read me"].iter_rows()
        for cell in row if cell.value
    )
    assert "SYNTHETIC" not in text
    assert "dera" in text


def test_empty_database_is_refused_rather_than_shipping_a_blank_book(tmp_path):
    connection = db.connect(":memory:")
    db.init_schema(connection)
    with pytest.raises(ValueError, match="no marks"):
        excel.export_workbook(connection, tmp_path / "empty.xlsx")
    connection.close()


# ---------------------------------------------------------------------------
# Standalone page
# ---------------------------------------------------------------------------

def test_standalone_page_carries_its_data_and_no_document_scaffolding(conn, tmp_path):
    html = standalone.build_html(conn)
    assert "<title>BDC Tracker</title>" in html
    # An artifact host supplies the scaffolding; a second copy breaks the page.
    for tag in ("<!doctype", "<html", "<head>", "<body"):
        assert tag not in html.lower()
    assert "window.__BDC_BUNDLE__" in html
    assert 'src="app.js"' not in html and 'href="styles.css"' not in html


def test_standalone_bundle_holds_every_view_and_every_bdc(conn):
    bundle = standalone.build_bundle(conn)
    assert {"overview", "bdcs", "disagreements", "meta", "positions"} <= set(bundle)
    assert set(bundle["positions"]) == set(bundle["meta"]["tickers"])
    assert bundle["positions"]["ARCC"]


def test_standalone_escapes_a_closing_script_tag_in_the_data(conn):
    """A borrower name containing </script> would otherwise end the tag early."""
    db.load_positions(conn, [
        _position(ARCC.cik, "</script> Capital, First Lien Term Loan", 1_000, 1_000)
    ])
    conn.commit()
    html = standalone.build_html(conn)
    payload = re.search(r"window\.__BDC_BUNDLE__ = (.*?);</script>", html, re.S).group(1)
    assert "</script>" not in payload
    assert json.loads(payload.replace("<\\/", "</"))
