"""The questions the tracker exists to answer, expressed as SQL over ``marks``.

Every function takes an open connection and returns plain dicts, so the same
code backs the JSON API, the static export and the CLI.
"""
from __future__ import annotations

import sqlite3
from typing import Sequence

#: The denominator every mark divides by: principal where the filing reports
#: one, cost otherwise — the same precedence as Position.mark_basis. Insisting
#: on principal alone leaves whole quarters blank, because the bulk data sets
#: carry it on barely half of positions.
BASIS = "COALESCE(NULLIF(principal, 0), cost)"

#: A debt position below this mark is treated as stressed.
STRESS_MARK = 90.0

#: ...and below this, distressed.
DISTRESS_MARK = 80.0


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def latest_period(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(period_end) FROM marks").fetchone()
    return row[0] if row else None


def prior_period(conn: sqlite3.Connection, period: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(period_end) FROM marks WHERE period_end < ?", (period,)
    ).fetchone()
    return row[0] if row else None


def periods(conn: sqlite3.Connection, limit: int | None = None) -> list[str]:
    sql = "SELECT DISTINCT period_end FROM marks ORDER BY period_end DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [r[0] for r in conn.execute(sql)][::-1]


# ---------------------------------------------------------------------------
# Every BDC side by side
# ---------------------------------------------------------------------------

_BDC_SUMMARY = """
WITH cur AS (
    SELECT * FROM v_marks WHERE period_end = :period
), prev AS (
    SELECT cik, SUM(fair_value) AS fv FROM v_marks WHERE period_end = :prior GROUP BY cik
)
SELECT
    c.cik,
    c.ticker,
    c.bdc_name,
    COUNT(*)                                                   AS positions,
    SUM(c.is_debt)                                             AS debt_positions,
    SUM(c.fair_value)                                          AS fair_value,
    SUM(c.cost)                                                AS cost,
    SUM(CASE WHEN c.is_debt THEN c.principal END)              AS principal,
    100.0 * SUM(CASE WHEN c.is_debt THEN c.fair_value END)
          / NULLIF(SUM(CASE WHEN c.is_debt
                           THEN COALESCE(NULLIF(c.principal, 0), c.cost) END), 0)
                                                                      AS portfolio_mark,
    100.0 * SUM(c.fair_value) / NULLIF(SUM(c.cost), 0)          AS fv_over_cost,
    -- Null, not zero, when the filing never disclosed non-accrual status:
    -- "we could not tell" and "nothing is on non-accrual" are different claims.
    CASE WHEN SUM(c.is_non_accrual IS NOT NULL) = 0 THEN NULL
         ELSE 100.0 * COALESCE(SUM(CASE WHEN c.is_non_accrual = 1 THEN c.fair_value END), 0)
              / NULLIF(SUM(c.fair_value), 0) END                 AS nonaccrual_pct_fv,
    SUM(CASE WHEN c.is_non_accrual = 1 THEN 1 ELSE 0 END)        AS nonaccrual_positions,
    SUM(c.is_non_accrual IS NOT NULL)                            AS nonaccrual_coverage,
    100.0 * COALESCE(SUM(CASE WHEN COALESCE(c.pik_rate, 0) > 0 THEN c.fair_value END), 0)
          / NULLIF(SUM(c.fair_value), 0)                         AS pik_pct_fv,
    100.0 * COALESCE(SUM(CASE WHEN c.is_debt AND c.mark < :stress THEN c.fair_value END), 0)
          / NULLIF(SUM(CASE WHEN c.is_debt THEN c.fair_value END), 0) AS stressed_pct_fv,
    AVG(c.interest_rate)                                        AS avg_coupon,
    COUNT(DISTINCT c.issuer_id)                                 AS issuers,
    p.fv                                                        AS prior_fair_value,
    100.0 * (SUM(c.fair_value) - p.fv) / NULLIF(p.fv, 0)         AS fv_change_pct
FROM cur c
LEFT JOIN prev p ON p.cik = c.cik
GROUP BY c.cik, c.ticker, c.bdc_name, p.fv
ORDER BY fair_value DESC
"""


def bdc_summary(conn: sqlite3.Connection, period: str | None = None) -> list[dict]:
    """One row per BDC: size, mark, non-accrual, PIK, and quarter-over-quarter drift."""
    period = period or latest_period(conn)
    if period is None:
        return []
    rows = _rows(
        conn,
        _BDC_SUMMARY,
        {"period": period, "prior": prior_period(conn, period) or "", "stress": STRESS_MARK},
    )
    concentration = _top_holdings_share(conn, period)
    for row in rows:
        row["top10_pct_fv"] = concentration.get(row["cik"])
        row["period_end"] = period
    return rows


def _top_holdings_share(conn: sqlite3.Connection, period: str, top: int = 10) -> dict[int, float]:
    """Share of each BDC's portfolio in its ten largest issuers."""
    sql = """
    WITH by_issuer AS (
        SELECT cik, issuer_id, SUM(fair_value) AS fv
        FROM marks WHERE period_end = ?
        GROUP BY cik, issuer_id
    ), ranked AS (
        SELECT cik, fv, ROW_NUMBER() OVER (PARTITION BY cik ORDER BY fv DESC) AS rn,
               SUM(fv) OVER (PARTITION BY cik) AS total
        FROM by_issuer
    )
    SELECT cik, 100.0 * SUM(fv) / NULLIF(MAX(total), 0) AS pct
    FROM ranked WHERE rn <= ? GROUP BY cik
    """
    return {r["cik"]: r["pct"] for r in _rows(conn, sql, (period, top))}


# ---------------------------------------------------------------------------
# Non-accruals
# ---------------------------------------------------------------------------

def nonaccrual_trend(conn: sqlite3.Connection) -> list[dict]:
    """Portfolio-wide non-accrual share by quarter."""
    return _rows(
        conn,
        """
        SELECT period_end,
               COALESCE(SUM(CASE WHEN is_non_accrual = 1 THEN fair_value END), 0) AS nonaccrual_fv,
               SUM(fair_value)                                              AS total_fv,
               CASE WHEN SUM(is_non_accrual IS NOT NULL) = 0 THEN NULL
                    ELSE 100.0 * COALESCE(SUM(CASE WHEN is_non_accrual = 1 THEN fair_value END), 0)
                         / NULLIF(SUM(fair_value), 0) END                   AS nonaccrual_pct,
               SUM(CASE WHEN is_non_accrual = 1 THEN 1 ELSE 0 END)          AS nonaccrual_positions,
               SUM(is_non_accrual IS NOT NULL)                              AS coverage,
               COUNT(*)                                                     AS positions
        FROM marks GROUP BY period_end ORDER BY period_end
        """,
    )


def nonaccrual_positions(conn: sqlite3.Connection, period: str | None = None) -> list[dict]:
    """Currently non-accrual positions, with how long they have been that way."""
    period = period or latest_period(conn)
    if period is None:
        return []
    return _rows(
        conn,
        """
        WITH streak AS (
            SELECT loan_id, COUNT(*) AS quarters_nonaccrual, MIN(period_end) AS since
            FROM marks WHERE is_non_accrual = 1 GROUP BY loan_id
        )
        SELECT v.loan_id, v.ticker, v.issuer_name, v.investment_type, v.industry,
               v.fair_value, v.cost, v.principal, v.mark, v.maturity_date,
               s.quarters_nonaccrual, s.since
        FROM v_marks v JOIN streak s ON s.loan_id = v.loan_id
        WHERE v.period_end = ? AND v.is_non_accrual = 1
        ORDER BY v.fair_value DESC
        """,
        (period,),
    )


def nonaccrual_by(conn: sqlite3.Connection, dimension: str = "industry",
                  period: str | None = None) -> list[dict]:
    if dimension not in {"industry", "ticker"}:
        raise ValueError("dimension must be 'industry' or 'ticker'")
    period = period or latest_period(conn)
    if period is None:
        return []
    return _rows(
        conn,
        f"""
        SELECT {dimension} AS bucket,
               SUM(CASE WHEN is_non_accrual = 1 THEN fair_value END) AS nonaccrual_fv,
               SUM(fair_value) AS total_fv,
               100.0 * SUM(CASE WHEN is_non_accrual = 1 THEN fair_value END)
                     / NULLIF(SUM(fair_value), 0) AS nonaccrual_pct
        FROM v_marks WHERE period_end = ? AND {dimension} IS NOT NULL
        GROUP BY bucket HAVING nonaccrual_fv > 0 ORDER BY nonaccrual_fv DESC
        """,
        (period,),
    )


# ---------------------------------------------------------------------------
# Shape of credit quality
# ---------------------------------------------------------------------------

def mark_histogram(conn: sqlite3.Connection, period: str | None = None,
                   bin_width: float = 2.5) -> list[dict]:
    """Distribution of debt marks — the shape of credit quality in one chart."""
    period = period or latest_period(conn)
    if period is None:
        return []
    return _rows(
        conn,
        """
        SELECT CAST(mark / ? AS INTEGER) * ?              AS bin_start,
               COUNT(*)                                    AS positions,
               SUM(fair_value)                             AS fair_value
        FROM marks
        WHERE period_end = ? AND mark IS NOT NULL AND mark BETWEEN 0 AND 150
          AND loan_id IN (SELECT loan_id FROM loans WHERE is_debt = 1)
        GROUP BY bin_start ORDER BY bin_start
        """,
        (bin_width, bin_width, period),
    )


def sector_marks(conn: sqlite3.Connection, quarters: int = 8) -> list[dict]:
    """Weighted average mark by industry per quarter — which sectors are rolling over."""
    window = periods(conn, limit=quarters)
    if not window:
        return []
    placeholders = ",".join("?" * len(window))
    return _rows(
        conn,
        f"""
        SELECT industry, period_end,
               100.0 * SUM(fair_value) / NULLIF(SUM(COALESCE(NULLIF(principal, 0), cost)), 0) AS weighted_mark,
               SUM(fair_value) AS fair_value, COUNT(*) AS positions
        FROM v_marks
        WHERE period_end IN ({placeholders}) AND is_debt = 1 AND industry IS NOT NULL
              AND COALESCE(NULLIF(principal, 0), cost) > 0
        GROUP BY industry, period_end
        HAVING positions >= 5
        ORDER BY industry, period_end
        """,
        window,
    )


def maturity_wall(conn: sqlite3.Connection, period: str | None = None) -> list[dict]:
    """Debt fair value grouped by maturity year, with the stressed slice split out."""
    period = period or latest_period(conn)
    if period is None:
        return []
    return _rows(
        conn,
        """
        SELECT CAST(substr(maturity_date, 1, 4) AS INTEGER)  AS maturity_year,
               SUM(fair_value)                                AS fair_value,
               SUM(principal)                                 AS principal,
               COUNT(*)                                       AS positions,
               SUM(CASE WHEN mark < ? THEN fair_value END)    AS stressed_fair_value,
               SUM(CASE WHEN is_non_accrual = 1 THEN fair_value END) AS nonaccrual_fair_value
        FROM v_marks
        WHERE period_end = ? AND is_debt = 1 AND maturity_date IS NOT NULL
        GROUP BY maturity_year HAVING maturity_year BETWEEN 2000 AND 2100
        ORDER BY maturity_year
        """,
        (STRESS_MARK, period),
    )


def biggest_markdowns(conn: sqlite3.Connection, period: str | None = None,
                      limit: int = 50) -> list[dict]:
    """Positions whose fair value has fallen furthest below the BDC's cost."""
    period = period or latest_period(conn)
    if period is None:
        return []
    return _rows(
        conn,
        """
        SELECT loan_id, ticker, issuer_name, investment_type, industry,
               fair_value, cost, principal, mark, unrealized,
               100.0 * unrealized / NULLIF(cost, 0) AS unrealized_pct, is_non_accrual
        FROM v_marks
        WHERE period_end = ? AND cost > 0 AND unrealized < 0
        ORDER BY unrealized ASC LIMIT ?
        """,
        (period, limit),
    )


def deteriorating(conn: sqlite3.Connection, period: str | None = None,
                  limit: int = 50, min_fair_value: float = 1_000_000) -> list[dict]:
    """Largest quarter-over-quarter mark declines — what is going wrong right now."""
    period = period or latest_period(conn)
    prior = prior_period(conn, period) if period else None
    if not period or not prior:
        return []
    return _rows(
        conn,
        """
        SELECT c.loan_id, c.ticker, c.issuer_name, c.investment_type, c.industry,
               c.fair_value, c.mark AS mark_now, p.mark AS mark_prior,
               c.mark - p.mark AS mark_change, c.is_non_accrual
        FROM v_marks c JOIN marks p ON p.loan_id = c.loan_id AND p.period_end = ?
        WHERE c.period_end = ? AND c.mark IS NOT NULL AND p.mark IS NOT NULL
              AND c.fair_value >= ? AND c.is_debt = 1
        ORDER BY mark_change ASC LIMIT ?
        """,
        (prior, period, min_fair_value, limit),
    )


# ---------------------------------------------------------------------------
# Where BDCs disagree
# ---------------------------------------------------------------------------

def disagreements(conn: sqlite3.Connection, period: str | None = None,
                  min_holders: int = 2, limit: int = 100) -> list[dict]:
    """Credits held by more than one BDC, ranked by how far apart the marks are.

    Two lenders looking at the same borrower and the same lien should land in
    the same place. When they do not, one of them is early.
    """
    period = period or latest_period(conn)
    if period is None:
        return []
    return _rows(
        conn,
        """
        SELECT credit_id,
               MIN(issuer_name)                        AS issuer_name,
               COUNT(DISTINCT cik)                     AS holders,
               MIN(mark)                               AS min_mark,
               MAX(mark)                               AS max_mark,
               MAX(mark) - MIN(mark)                   AS spread,
               100.0 * SUM(fair_value) / NULLIF(SUM(principal), 0) AS weighted_mark,
               SUM(fair_value)                         AS fair_value,
               GROUP_CONCAT(ticker || ':' || CAST(ROUND(mark, 1) AS TEXT), ', ') AS marks_by_bdc
        FROM v_marks
        WHERE period_end = ? AND is_debt = 1 AND mark IS NOT NULL AND principal > 0
        GROUP BY credit_id
        HAVING holders >= ?
        ORDER BY spread DESC LIMIT ?
        """,
        (period, min_holders, limit),
    )


def shared_credits(conn: sqlite3.Connection, period: str | None = None,
                   min_holders: int = 2) -> list[dict]:
    """Every credit held by two or more BDCs, largest first."""
    period = period or latest_period(conn)
    if period is None:
        return []
    return _rows(
        conn,
        """
        SELECT credit_id, MIN(issuer_name) AS issuer_name, COUNT(DISTINCT cik) AS holders,
               SUM(fair_value) AS fair_value,
               100.0 * SUM(fair_value) / NULLIF(SUM(principal), 0) AS weighted_mark,
               GROUP_CONCAT(DISTINCT ticker) AS bdcs
        FROM v_marks
        WHERE period_end = ? AND is_debt = 1
        GROUP BY credit_id HAVING holders >= ?
        ORDER BY fair_value DESC
        """,
        (period, min_holders),
    )


# ---------------------------------------------------------------------------
# Quarterly series, one row per BDC per quarter
# ---------------------------------------------------------------------------

#: BDCs keep different fiscal calendars — Golub reports to September, most to
#: December — so a February period end and a March one are the same quarter.
#: Aligning on the calendar quarter is what makes them comparable side by side.
_QUARTER = ("substr(period_end, 1, 4) || 'Q' || "
            "CAST((CAST(substr(period_end, 6, 2) AS INTEGER) + 2) / 3 AS INTEGER)")

#: One period end per BDC per quarter: the latest, should a filer report twice.
_LATEST_PER_QUARTER = f"""
WITH labelled AS (
    SELECT *, {_QUARTER} AS quarter FROM v_marks WHERE period_end >= :since
), chosen AS (
    SELECT cik, quarter, MAX(period_end) AS period_end
    FROM labelled GROUP BY cik, quarter
), scoped AS (
    SELECT l.* FROM labelled l
    JOIN chosen c ON c.cik = l.cik AND c.quarter = l.quarter
                 AND c.period_end = l.period_end
)
"""

DEFAULT_SINCE = "2024-01-01"


def quarterly_bdc_marks(conn: sqlite3.Connection, since: str = DEFAULT_SINCE) -> list[dict]:
    """Weighted average mark per BDC per quarter.

    Weighted by principal — the sum of fair value over the sum of principal —
    so a large position moves the number in proportion to its size, which a
    simple average of marks would not do.
    """
    return _rows(
        conn,
        _LATEST_PER_QUARTER + """
        SELECT quarter, ticker, bdc_name, MIN(period_end) AS period_end,
               100.0 * SUM(fair_value) / NULLIF(SUM(COALESCE(NULLIF(principal, 0), cost)), 0) AS weighted_mark,
               SUM(fair_value) AS fair_value,
               SUM(COALESCE(NULLIF(principal, 0), cost))        AS principal,
               COUNT(*)        AS positions
        FROM scoped
        WHERE is_debt = 1 AND COALESCE(NULLIF(principal, 0), cost) > 0 AND fair_value IS NOT NULL
        GROUP BY quarter, ticker, bdc_name
        ORDER BY quarter, ticker
        """,
        {"since": since},
    )


def quarterly_nonaccrual_marks(conn: sqlite3.Connection, since: str = DEFAULT_SINCE) -> list[dict]:
    """Weighted average mark of the non-accrual book, per BDC per quarter.

    How hard a lender has written down the loans it has stopped accruing —
    distinct from how much of the book is on non-accrual.
    """
    return _rows(
        conn,
        _LATEST_PER_QUARTER + """
        SELECT quarter, ticker, bdc_name, MIN(period_end) AS period_end,
               100.0 * SUM(fair_value) / NULLIF(SUM(COALESCE(NULLIF(principal, 0), cost)), 0) AS weighted_mark,
               SUM(fair_value) AS fair_value,
               SUM(COALESCE(NULLIF(principal, 0), cost))        AS principal,
               COUNT(*)        AS positions
        FROM scoped
        WHERE is_non_accrual = 1 AND COALESCE(NULLIF(principal, 0), cost) > 0 AND fair_value IS NOT NULL
        GROUP BY quarter, ticker, bdc_name
        ORDER BY quarter, ticker
        """,
        {"since": since},
    )


def quarterly_nonaccrual_share(conn: sqlite3.Connection,
                               since: str = DEFAULT_SINCE) -> list[dict]:
    """Non-accrual share of each BDC's portfolio by market value, per quarter.

    Market value means fair value: the share of what the book is currently
    worth that sits on non-accrual, not the share of what it cost.
    """
    return _rows(
        conn,
        _LATEST_PER_QUARTER + """
        SELECT quarter, ticker, bdc_name, MIN(period_end) AS period_end,
               SUM(fair_value) AS total_fair_value,
               COALESCE(SUM(CASE WHEN is_non_accrual = 1 THEN fair_value END), 0)
                   AS nonaccrual_fair_value,
               CASE WHEN SUM(is_non_accrual IS NOT NULL) = 0 THEN NULL
                    ELSE 100.0
                         * COALESCE(SUM(CASE WHEN is_non_accrual = 1 THEN fair_value END), 0)
                         / NULLIF(SUM(fair_value), 0) END AS nonaccrual_pct,
               SUM(CASE WHEN is_non_accrual = 1 THEN 1 ELSE 0 END) AS nonaccrual_positions,
               SUM(is_non_accrual IS NOT NULL) AS coverage,
               COUNT(*) AS positions
        FROM scoped
        GROUP BY quarter, ticker, bdc_name
        ORDER BY quarter, ticker
        """,
        {"since": since},
    )


def vintage_profile(conn: sqlite3.Connection, period: str | None = None) -> list[dict]:  # noqa: ARG001
    """Fair value and weighted mark by the year each position was acquired.

    Vintage is the tagged acquisition year and nothing else. Positions the
    filing left untagged are reported as a single unknown row rather than
    assigned a year, because a cohort analysis built on a guessed vintage is
    worse than one that admits its coverage.

    Built from each loan's most recent observation rather than from a single
    quarter. Acquisition dates come from a minority of filers, and confining the
    cohorts to one period would show nothing at all whenever that period's data
    happens to come from filers who do not tag them.
    """
    return _rows(
        conn,
        f"""
        WITH latest AS (
            SELECT loan_id, MAX(period_end) AS period_end FROM marks GROUP BY loan_id
        ), current AS (
            SELECT v.* FROM v_marks v
            JOIN latest l ON l.loan_id = v.loan_id AND l.period_end = v.period_end
        ), vintages AS (
            -- A loan tagged in any quarter keeps that vintage in every other.
            SELECT loan_id, MIN(acquisition_date) AS acquisition_date
            FROM marks WHERE acquisition_date IS NOT NULL GROUP BY loan_id
        )
        SELECT CAST(substr(vintages.acquisition_date, 1, 4) AS INTEGER) AS vintage_year,
               COUNT(*)        AS positions,
               SUM(fair_value) AS fair_value,
               SUM({BASIS})    AS basis,
               100.0 * SUM(fair_value) / NULLIF(SUM({BASIS}), 0) AS weighted_mark,
               SUM(CASE WHEN is_non_accrual = 1 THEN fair_value END) AS nonaccrual_fair_value
        FROM current LEFT JOIN vintages ON vintages.loan_id = current.loan_id
        -- Both sides of the ratio must come from the same rows. The bulk data
        -- sets carry a basis without a fair value, and counting those in the
        -- denominator alone drove whole cohorts to single-digit marks.
        WHERE fair_value IS NOT NULL AND COALESCE(NULLIF(principal, 0), cost) > 0
        GROUP BY vintage_year
        ORDER BY vintage_year IS NULL, vintage_year
        """,
    )


def maturity_profile(conn: sqlite3.Connection, period: str | None = None) -> list[dict]:
    """Fair value and weighted mark by the year each loan matures."""
    period = period or latest_period(conn)
    if period is None:
        return []
    return _rows(
        conn,
        f"""
        SELECT CAST(substr(maturity_date, 1, 4) AS INTEGER) AS maturity_year,
               COUNT(*)        AS positions,
               SUM(fair_value) AS fair_value,
               SUM({BASIS})    AS basis,
               100.0 * SUM(fair_value) / NULLIF(SUM({BASIS}), 0) AS weighted_mark,
               SUM(CASE WHEN is_non_accrual = 1 THEN fair_value END) AS nonaccrual_fair_value
        FROM v_marks
        WHERE period_end = ? AND is_debt = 1
              AND fair_value IS NOT NULL AND COALESCE(NULLIF(principal, 0), cost) > 0
        GROUP BY maturity_year
        ORDER BY maturity_year IS NULL, maturity_year
        """,
        (period,),
    )


def country_exposure(conn: sqlite3.Connection, period: str | None = None) -> list[dict]:
    """Fair value and weighted mark by country of the borrower."""
    period = period or latest_period(conn)
    if period is None:
        return []
    return _rows(
        conn,
        """
        SELECT country, SUM(fair_value) AS fair_value, COUNT(*) AS positions,
               100.0 * SUM(CASE WHEN is_debt THEN fair_value END)
                     / NULLIF(SUM(CASE WHEN is_debt THEN principal END), 0) AS weighted_mark
        FROM v_marks
        WHERE period_end = ? AND country IS NOT NULL AND fair_value IS NOT NULL
        GROUP BY country ORDER BY fair_value DESC
        """,
        (period,),
    )


# ---------------------------------------------------------------------------
# Drill-downs
# ---------------------------------------------------------------------------

def bdc_positions(conn: sqlite3.Connection, ticker: str, period: str | None = None) -> list[dict]:
    period = period or latest_period(conn)
    return _rows(
        conn,
        """
        SELECT loan_id, issuer_name, investment_type, lien, facility, industry, country, currency,
               fair_value, cost, principal, mark, unrealized, interest_rate, spread,
               reference_rate, pik_rate, maturity_date, is_non_accrual, fair_value_level, flags
        FROM v_marks WHERE ticker = ? AND period_end = ?
        ORDER BY fair_value DESC
        """,
        (ticker.upper(), period),
    )


def loan_history(conn: sqlite3.Connection, loan_id: str) -> list[dict]:
    """Every mark ever recorded for one position — the quarter-by-quarter track."""
    return _rows(
        conn,
        """
        SELECT period_end, ticker, issuer_name, investment_type, fair_value, cost, principal,
               mark, interest_rate, spread, pik_rate, maturity_date, is_non_accrual,
               accession, form, source
        FROM v_marks WHERE loan_id = ? ORDER BY period_end
        """,
        (loan_id,),
    )


def issuer_detail(conn: sqlite3.Connection, issuer_id: str) -> list[dict]:
    """Every BDC's view of one borrower, across quarters."""
    return _rows(
        conn,
        """
        SELECT period_end, ticker, investment_type, lien, fair_value, cost, principal, mark,
               interest_rate, pik_rate, maturity_date, is_non_accrual, loan_id
        FROM v_marks WHERE issuer_id = ? ORDER BY period_end DESC, fair_value DESC
        """,
        (issuer_id,),
    )


def search_issuers(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT i.issuer_id, i.display_name, COUNT(DISTINCT m.cik) AS holders,
               SUM(CASE WHEN m.period_end = (SELECT MAX(period_end) FROM marks)
                        THEN m.fair_value END) AS latest_fair_value
        FROM issuers i JOIN marks m ON m.issuer_id = i.issuer_id
        WHERE i.canonical_name LIKE ?
        GROUP BY i.issuer_id, i.display_name
        ORDER BY latest_fair_value DESC NULLS LAST LIMIT ?
        """,
        (f"%{query.upper()}%", limit),
    )


def overview(conn: sqlite3.Connection, period: str | None = None) -> dict:
    """Headline numbers for the landing page."""
    period = period or latest_period(conn)
    if period is None:
        return {}
    row = conn.execute(
        """
        SELECT COUNT(*) AS positions,
               COUNT(DISTINCT cik) AS bdcs,
               COUNT(DISTINCT issuer_id) AS issuers,
               SUM(fair_value) AS fair_value,
               SUM(CASE WHEN is_debt THEN principal END) AS principal,
               100.0 * SUM(CASE WHEN is_debt THEN fair_value END)
                     / NULLIF(SUM(CASE WHEN is_debt THEN principal END), 0) AS portfolio_mark,
               100.0 * SUM(CASE WHEN is_non_accrual = 1 THEN fair_value END)
                     / NULLIF(SUM(fair_value), 0) AS nonaccrual_pct
        FROM v_marks WHERE period_end = ?
        """,
        (period,),
    ).fetchone()
    totals = conn.execute(
        "SELECT COUNT(*) AS marks, COUNT(DISTINCT loan_id) AS loans FROM marks"
    ).fetchone()
    return {
        "period_end": period,
        "periods": periods(conn),
        **dict(row),
        "total_marks": totals["marks"],
        "total_loans": totals["loans"],
    }
