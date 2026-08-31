from datetime import date
from decimal import Decimal

import pytest

from bdctracker import analytics, db, normalize
from bdctracker.models import Position
from bdctracker.universe import BDC

ARCC = BDC(ticker="ARCC", cik=1287750, name="Ares Capital", exchange="Nasdaq")
TSLX = BDC(ticker="TSLX", cik=1508655, name="Sixth Street", exchange="NYSE")


def position(cik, period, identifier, fv, par, cost=None, **kwargs):  # noqa: D103
    return normalize.finalize(
        Position(
            cik=cik,
            period_end=period,
            identifier=identifier,
            fair_value=Decimal(str(fv)),
            principal=Decimal(str(par)),
            cost=Decimal(str(cost if cost is not None else par)),
            **kwargs,
        )
    )


Q1, Q2 = date(2025, 9, 30), date(2025, 12, 31)


def sample_positions():
    return [
        # Both BDCs lend to Acme first lien; TSLX marks it far lower.
        position(ARCC.cik, Q1, "Acme Holdings, LLC, First Lien Term Loan", 9_800_000, 10_000_000),
        position(ARCC.cik, Q2, "Acme Holdings, LLC, First Lien Term Loan", 9_000_000, 10_000_000),
        position(TSLX.cik, Q2, "ACME Holdings Inc., First Lien Senior Secured Loan", 7_000_000, 10_000_000, cost=9_900_000),
        # A non-accrual second lien, marked down hard.
        position(
            ARCC.cik, Q2, "Beta Industries, Second Lien Term Loan", 2_000_000, 5_000_000,
            cost=5_000_000, is_non_accrual=True, industry="Industrials",
            maturity_date=date(2027, 6, 30),
        ),
        # Equity, which must stay out of the debt-mark statistics.
        position(ARCC.cik, Q2, "Beta Industries, Common Equity", 100_000, 0, cost=500_000),
    ]


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    db.upsert_bdcs(connection, [ARCC, TSLX])
    db.load_positions(connection, sample_positions())
    connection.commit()
    yield connection
    connection.close()


def test_round_trip_counts(conn):
    summary = db.stats(conn)
    assert summary["bdcs"] == 2
    assert summary["marks"] == 5
    assert summary["loans"] == 4  # Acme@ARCC spans two quarters as one loan
    assert summary["latest_period"] == "2025-12-31"


def test_loading_twice_updates_rather_than_duplicates(conn):
    db.load_positions(conn, sample_positions())
    assert db.stats(conn)["marks"] == 5


def test_view_joins_through_to_the_ticker(conn):
    rows = analytics.bdc_positions(conn, "ARCC", "2025-12-31")
    # The display name is the deterministic pick across the two filers' spellings.
    assert {r["issuer_name"] for r in rows} == {"ACME Holdings Inc.", "Beta Industries"}


def test_bdc_summary_marks_and_quarter_over_quarter(conn):
    rows = {r["ticker"]: r for r in analytics.bdc_summary(conn, "2025-12-31")}
    ares = rows["ARCC"]
    # Debt only: (9.0m + 2.0m) / (10m + 5m)
    assert ares["portfolio_mark"] == pytest.approx(100 * 11 / 15)
    assert ares["nonaccrual_positions"] == 1
    assert ares["fv_change_pct"] == pytest.approx(100 * (11_100_000 - 9_800_000) / 9_800_000)


def test_equity_is_excluded_from_the_portfolio_mark(conn):
    ares = next(r for r in analytics.bdc_summary(conn, "2025-12-31") if r["ticker"] == "ARCC")
    assert ares["positions"] == 3
    assert ares["debt_positions"] == 2


def test_disagreement_pairs_the_two_bdcs_on_one_credit(conn):
    rows = analytics.disagreements(conn, "2025-12-31")
    assert len(rows) == 1
    acme = rows[0]
    assert acme["holders"] == 2
    assert acme["min_mark"] == pytest.approx(70.0)
    assert acme["max_mark"] == pytest.approx(90.0)
    assert acme["spread"] == pytest.approx(20.0)
    assert "ARCC:90.0" in acme["marks_by_bdc"] and "TSLX:70.0" in acme["marks_by_bdc"]


def test_deteriorating_finds_the_quarter_over_quarter_slide(conn):
    rows = analytics.deteriorating(conn, "2025-12-31", min_fair_value=0)
    assert rows[0]["issuer_name"] == "ACME Holdings Inc."
    assert rows[0]["mark_change"] == pytest.approx(-8.0)


def test_nonaccrual_views(conn):
    positions = analytics.nonaccrual_positions(conn, "2025-12-31")
    assert [p["issuer_name"] for p in positions] == ["Beta Industries"]
    assert positions[0]["quarters_nonaccrual"] == 1

    trend = {row["period_end"]: row for row in analytics.nonaccrual_trend(conn)}
    assert trend["2025-12-31"]["nonaccrual_positions"] == 1


def test_nonaccrual_is_unknown_not_zero_when_nothing_was_disclosed():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    db.upsert_bdcs(connection, [ARCC])
    db.load_positions(
        connection,
        [position(ARCC.cik, Q2, "Gamma Corp, First Lien Term Loan", 1_000, 1_000)],
    )
    row = analytics.bdc_summary(connection, "2025-12-31")[0]
    assert row["nonaccrual_coverage"] == 0
    assert row["nonaccrual_pct_fv"] is None
    connection.close()


def test_maturity_wall_buckets_by_year(conn):
    rows = analytics.maturity_wall(conn, "2025-12-31")
    assert rows == [
        {
            "maturity_year": 2027,
            "fair_value": 2_000_000.0,
            "principal": 5_000_000.0,
            "positions": 1,
            "stressed_fair_value": 2_000_000.0,
            "nonaccrual_fair_value": 2_000_000.0,
        }
    ]


def test_markdowns_rank_by_unrealised_loss(conn):
    rows = analytics.biggest_markdowns(conn, "2025-12-31")
    assert rows[0]["issuer_name"] == "Beta Industries"
    assert rows[0]["unrealized"] == pytest.approx(-3_000_000.0)


def test_histogram_covers_debt_only(conn):
    bins = analytics.mark_histogram(conn, "2025-12-31", bin_width=10)
    assert {b["bin_start"] for b in bins} == {40.0, 70.0, 90.0}


def test_loan_history_tracks_one_position_through_time(conn):
    loan_id = analytics.bdc_positions(conn, "ARCC", "2025-12-31")
    acme = next(r for r in loan_id if r["issuer_name"].upper().startswith("ACME"))["loan_id"]
    history = analytics.loan_history(conn, acme)
    assert [h["period_end"] for h in history] == ["2025-09-30", "2025-12-31"]
    assert [round(h["mark"], 1) for h in history] == [98.0, 90.0]


def test_overview(conn):
    summary = analytics.overview(conn, "2025-12-31")
    assert summary["bdcs"] == 2
    assert summary["total_marks"] == 5
    assert summary["portfolio_mark"] == pytest.approx(100 * 18 / 25)


# ---------------------------------------------------------------------------
# Quarterly series
# ---------------------------------------------------------------------------

def test_quarterly_marks_weight_by_principal_not_by_position_count(conn):
    rows = {r["ticker"]: r for r in analytics.quarterly_bdc_marks(conn, since="2025-01-01")
            if r["quarter"] == "2025Q4"}
    # ARCC debt at 2025-12-31: (9.0m + 2.0m) / (10m + 5m). A plain average of
    # the two marks (90 and 40) would give 65.
    assert rows["ARCC"]["weighted_mark"] == pytest.approx(100 * 11 / 15)


def test_quarterly_marks_align_filers_on_the_calendar_quarter(conn):
    """A November period end and a December one are the same quarter."""
    db.load_positions(conn, [
        position(TSLX.cik, date(2025, 11, 30), "Delta Corp, First Lien Term Loan",
                 950, 1_000),
    ])
    conn.commit()
    quarters = {r["quarter"] for r in analytics.quarterly_bdc_marks(conn, since="2025-01-01")}
    assert "2025Q4" in quarters


def test_quarterly_nonaccrual_mark_covers_only_the_nonaccrual_book(conn):
    rows = analytics.quarterly_nonaccrual_marks(conn, since="2025-01-01")
    ares = [r for r in rows if r["ticker"] == "ARCC" and r["quarter"] == "2025Q4"]
    assert len(ares) == 1
    # Beta alone: 2.0m fair value on 5.0m principal.
    assert ares[0]["weighted_mark"] == pytest.approx(40.0)
    assert ares[0]["positions"] == 1


def test_quarterly_nonaccrual_share_is_of_market_value(conn):
    rows = {r["ticker"]: r for r in
            analytics.quarterly_nonaccrual_share(conn, since="2025-01-01")
            if r["quarter"] == "2025Q4"}
    # ARCC: 2.0m non-accrual against 11.1m total fair value.
    assert rows["ARCC"]["nonaccrual_pct"] == pytest.approx(100 * 2.0 / 11.1, rel=1e-3)
    assert rows["ARCC"]["nonaccrual_fair_value"] == pytest.approx(2_000_000)


def test_quarterly_nonaccrual_share_is_null_where_nothing_was_disclosed(conn):
    rows = {r["ticker"]: r for r in
            analytics.quarterly_nonaccrual_share(conn, since="2025-01-01")
            if r["quarter"] == "2025Q4"}
    assert rows["TSLX"]["coverage"] == 0
    assert rows["TSLX"]["nonaccrual_pct"] is None
