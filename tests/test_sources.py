"""Parser tests against fixtures shaped like the real SEC artefacts.

The DERA fixture reproduces the tab-separated ``soi.tsv`` layout, taxonomy
label column names and ``[Member]`` decoration; the XBRL fixture reproduces the
fact dicts edgartools yields, including the ``dim_us-gaap_InvestmentIdentifierAxis``
key that makes one position addressable.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from bdctracker.sources import dera, xbrl

SOI_COLUMNS = [
    "adsh", "cik", "name", "form", "filed", "ddate",
    "Investment, Identifier Axis", "Investment, Issuer Name Axis",
    "Investment Type Axis", "Industry Sector Axis", "Lien Category Axis",
    "Investment Owned, Fair Value", "Investment Owned, Cost",
    "Investment Owned, Balance, Principal Amount",
    "Investment Interest Rate", "Investment, Basis Spread, Variable Rate",
    "Investment, Interest Rate, Paid in Kind", "Investment Maturity Date",
    "Investment Owned, Net Assets, Percentage",
]

SOI_ROWS = [
    [
        "0001-24-000001", 1287750, "ARES CAPITAL CORP", "10-Q", "2025-08-05", 20250630,
        "Acme Holdings, LLC, First Lien Senior Secured Loan, SOFR + 5.75%, due 6/30/2029",
        "Acme Holdings, LLC", "First Lien Senior Secured Loan [Member]",
        "Software and Computer Services [Member]", "First Lien [Member]",
        9_800_000, 9_950_000, 10_000_000, 0.1112, 0.0575, 0.0, "2029-06-30", 0.0042,
    ],
    [
        "0001-24-000001", 1287750, "ARES CAPITAL CORP", "10-Q", "2025-08-05", 20250630,
        "Beta Industries Inc., Second Lien Term Loan",
        "Beta Industries Inc.", "Second Lien [Member]", "Industrials [Member]", "Second Lien [Member]",
        4_200_000, 5_000_000, 5_000_000, 0.1425, 0.0875, 0.0200, "2028-03-31", 0.0018,
    ],
    [
        "0001-24-000001", 1287750, "ARES CAPITAL CORP", "10-Q", "2025-08-05", 20250630,
        "Beta Industries Inc., Common Equity",
        "Beta Industries Inc.", "Common Equity [Member]", "Industrials [Member]", "",
        150_000, 500_000, "", "", "", "", "", 0.0001,
    ],
]


@pytest.fixture
def dera_zip(tmp_path: Path) -> Path:
    header = "\t".join(SOI_COLUMNS)
    lines = ["\t".join("" if v is None else str(v) for v in row) for row in SOI_ROWS]
    soi = "\n".join([header, *lines])
    sub = "adsh\tcik\tname\tform\tfiled\n0001-24-000001\t1287750\tARES CAPITAL CORP\t10-Q\t20250805"

    target = tmp_path / "2025q3_bdc.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("datasets/soi.tsv", soi)
        archive.writestr("datasets/sub.tsv", sub)
    return target


def test_dera_reads_the_zip_layout(dera_zip: Path):
    soi, subs = dera.read_quarter(dera_zip)
    assert len(soi) == 3
    assert len(subs) == 1


def test_dera_positions_carry_the_mark(dera_zip: Path):
    soi, _ = dera.read_quarter(dera_zip)
    positions = dera.to_positions(dera.tidy(soi), ciks=[1287750])
    assert len(positions) == 3

    by_issuer = {p.issuer_name: p for p in positions if p.is_debt}
    acme = by_issuer["Acme Holdings, LLC"]
    assert acme.period_end == date(2025, 6, 30)
    assert acme.investment_type == "FIRST_LIEN"
    assert acme.mark == pytest.approx(98.0)
    assert acme.interest_rate == pytest.approx(11.12)
    assert acme.spread == pytest.approx(5.75)
    assert acme.reference_rate == "SOFR"
    assert acme.maturity_date == date(2029, 6, 30)
    assert acme.industry == "Software and Computer Services"
    assert acme.source == "dera"


def test_dera_marks_a_written_down_second_lien(dera_zip: Path):
    soi, _ = dera.read_quarter(dera_zip)
    positions = dera.to_positions(dera.tidy(soi), ciks=[1287750])
    beta = next(p for p in positions if p.investment_type == "SECOND_LIEN")
    assert beta.mark == pytest.approx(84.0)
    assert beta.pik_rate == pytest.approx(2.0)


def test_dera_classifies_equity_and_excludes_it_from_debt(dera_zip: Path):
    soi, _ = dera.read_quarter(dera_zip)
    positions = dera.to_positions(dera.tidy(soi), ciks=[1287750])
    equity = next(p for p in positions if not p.is_debt)
    assert equity.investment_type == "COMMON_EQUITY"
    # Equity marks against cost, not par.
    assert equity.mark == pytest.approx(30.0)


def test_dera_filters_to_the_universe(dera_zip: Path):
    soi, _ = dera.read_quarter(dera_zip)
    assert dera.to_positions(dera.tidy(soi), ciks=[999]) == []


def test_quarter_parsing_and_urls():
    quarter = dera.Quarter.parse("2025Q3")
    assert (quarter.year, quarter.quarter) == (2025, 3)
    assert quarter.url.endswith("/2025q3_bdc.zip")
    assert str(dera.latest_published_quarter(date(2026, 2, 14))) == "2025Q4"


# ---------------------------------------------------------------------------
# XBRL
# ---------------------------------------------------------------------------

class _Facts:
    def __init__(self, facts):
        self._facts = facts

    def get_facts(self):
        return self._facts


class _XBRL:
    def __init__(self, facts):
        self.facts = _Facts(facts)


def _fact(concept, identifier, period, value, **dims):
    fact = {
        "concept": concept,
        "period_type": "instant",
        "period_instant": period,
        "numeric_value": value if isinstance(value, (int, float)) else None,
        "value": value,
        xbrl.IDENTIFIER_AXIS: identifier,
    }
    fact.update(dims)
    return fact


ACME = "Acme Holdings, LLC, First Lien Senior Secured Loan"
INDUSTRY_DIM = {"dim_us-gaap_IndustrySectorAxis": "Software [Member]"}


@pytest.fixture
def xbrl_doc():
    facts = [
        _fact("us-gaap:InvestmentOwnedAtFairValue", ACME, "2025-06-30", 9_800_000, **INDUSTRY_DIM),
        _fact("us-gaap:InvestmentOwnedAtCost", ACME, "2025-06-30", 9_950_000),
        _fact("us-gaap:InvestmentOwnedBalancePrincipalAmount", ACME, "2025-06-30", 10_000_000),
        _fact("us-gaap:InvestmentInterestRate", ACME, "2025-06-30", 0.1112),
        _fact("us-gaap:InvestmentMaturityDate", ACME, "2025-06-30", "2029-06-30"),
        # The prior year-end that a 10-K carries alongside the current schedule.
        _fact("us-gaap:InvestmentOwnedAtFairValue", ACME, "2024-12-31", 9_900_000),
        _fact("us-gaap:InvestmentOwnedBalancePrincipalAmount", ACME, "2024-12-31", 10_000_000),
        # A fact with no investment dimension: a portfolio total, not a position.
        {"concept": "us-gaap:InvestmentOwnedAtFairValue", "period_instant": "2025-06-30",
         "numeric_value": 25_000_000_000, "period_type": "instant"},
        # A concept we do not track.
        _fact("us-gaap:Assets", ACME, "2025-06-30", 1),
    ]
    return _XBRL(facts)


def test_xbrl_builds_the_fact_list_once_when_it_is_handed_one(xbrl_doc):
    """Rebuilding facts per consumer tripled the cost of the slowest step."""
    calls = {"n": 0}
    original = xbrl_doc.facts.get_facts

    def counting():
        calls["n"] += 1
        return original()

    xbrl_doc.facts.get_facts = counting
    facts = counting()
    xbrl.positions_from_xbrl(xbrl_doc, cik=1287750, all_facts=facts)
    assert calls["n"] == 1


def test_nonaccrual_flags_apply_only_to_their_own_period(xbrl_doc):
    positions = xbrl.positions_from_xbrl(
        xbrl_doc, cik=1287750,
        nonaccrual_by_period={"2025-06-30": {ACME}},
    )
    by_period = {p.period_end: p.is_non_accrual for p in positions}
    assert by_period[date(2025, 6, 30)] is True
    # The prior year-end was not flagged, and must not inherit the flag.
    assert by_period[date(2024, 12, 31)] is None


def test_xbrl_extracts_one_position_per_identifier_and_period(xbrl_doc):
    positions = xbrl.positions_from_xbrl(xbrl_doc, cik=1287750, accession="0001", form="10-K")
    assert len(positions) == 2
    assert {p.period_end for p in positions} == {date(2025, 6, 30), date(2024, 12, 31)}


def test_xbrl_ignores_undimensioned_totals(xbrl_doc):
    positions = xbrl.positions_from_xbrl(xbrl_doc, cik=1287750)
    assert all(float(p.fair_value) < 1e9 for p in positions)


def test_xbrl_position_fields(xbrl_doc):
    positions = xbrl.positions_from_xbrl(xbrl_doc, cik=1287750, accession="0001", form="10-K")
    current = next(p for p in positions if p.period_end == date(2025, 6, 30))
    assert current.issuer_name == "Acme Holdings"
    assert current.investment_type == "FIRST_LIEN"
    assert current.mark == pytest.approx(98.0)
    assert current.interest_rate == pytest.approx(11.12)
    assert current.maturity_date == date(2029, 6, 30)
    assert current.industry == "Software"
    assert current.source == "xbrl"


def test_xbrl_period_filter(xbrl_doc):
    positions = xbrl.positions_from_xbrl(xbrl_doc, cik=1287750, periods=["2025-06-30"])
    assert [p.period_end for p in positions] == [date(2025, 6, 30)]


def test_xbrl_and_dera_agree_on_the_loan_key(dera_zip: Path, xbrl_doc):
    """The two sources must produce the same key or the fallback would duplicate."""
    soi, _ = dera.read_quarter(dera_zip)
    from_dera = next(
        p for p in dera.to_positions(dera.tidy(soi), ciks=[1287750])
        if p.issuer_name.startswith("Acme")
    )
    from_xbrl = next(
        p for p in xbrl.positions_from_xbrl(xbrl_doc, cik=1287750)
        if p.period_end == date(2025, 6, 30)
    )
    assert from_dera.loan_id == from_xbrl.loan_id
