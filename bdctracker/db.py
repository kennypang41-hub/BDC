"""SQLite storage for the mark dataset.

One row per (loan, quarter) in ``marks`` — that is the grain the whole tracker
reads from. Money is stored as REAL dollars: BDC portfolios top out around
$30bn, comfortably inside double precision, and keeping it numeric lets the
analytics run as plain SQL.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from bdctracker.config import SETTINGS
from bdctracker.identity import canonical_issuer
from bdctracker.models import Position
from bdctracker.universe import BDC, load_universe

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bdcs (
    cik        INTEGER PRIMARY KEY,
    ticker     TEXT NOT NULL,
    name       TEXT NOT NULL,
    exchange   TEXT
);

CREATE TABLE IF NOT EXISTS issuers (
    issuer_id      TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    display_name   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loans (
    loan_id         TEXT PRIMARY KEY,
    cik             INTEGER NOT NULL REFERENCES bdcs(cik),
    issuer_id       TEXT NOT NULL REFERENCES issuers(issuer_id),
    credit_id       TEXT NOT NULL,
    investment_type TEXT NOT NULL,
    lien            TEXT,
    facility        TEXT,
    currency        TEXT NOT NULL DEFAULT 'USD',
    is_debt         INTEGER NOT NULL DEFAULT 0,
    identifier      TEXT,
    first_period    TEXT,
    last_period     TEXT
);

CREATE TABLE IF NOT EXISTS marks (
    loan_id          TEXT NOT NULL REFERENCES loans(loan_id),
    period_end       TEXT NOT NULL,
    cik              INTEGER NOT NULL,
    issuer_id        TEXT NOT NULL,
    credit_id        TEXT NOT NULL,
    fair_value       REAL,
    cost             REAL,
    principal        REAL,
    shares           REAL,
    mark             REAL,
    unrealized       REAL,
    interest_rate    REAL,
    spread           REAL,
    reference_rate   TEXT,
    pik_rate         REAL,
    pct_net_assets   REAL,
    maturity_date    TEXT,
    acquisition_date TEXT,
    industry         TEXT,
    country          TEXT,
    principal_ccy    TEXT,
    fair_value_ccy   TEXT,
    fair_value_level TEXT,
    is_non_accrual   INTEGER,
    accession        TEXT,
    form             TEXT,
    filed_date       TEXT,
    source           TEXT,
    flags            TEXT,
    PRIMARY KEY (loan_id, period_end)
);

CREATE TABLE IF NOT EXISTS filings (
    accession  TEXT PRIMARY KEY,
    cik        INTEGER,
    form       TEXT,
    period_end TEXT,
    filed_date TEXT,
    source     TEXT,
    url        TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    source      TEXT,
    scope       TEXT,
    positions   INTEGER,
    notes       TEXT
);

CREATE INDEX IF NOT EXISTS idx_marks_period    ON marks(period_end);
CREATE INDEX IF NOT EXISTS idx_marks_cik       ON marks(cik, period_end);
CREATE INDEX IF NOT EXISTS idx_marks_issuer    ON marks(issuer_id, period_end);
CREATE INDEX IF NOT EXISTS idx_marks_credit    ON marks(credit_id, period_end);
CREATE INDEX IF NOT EXISTS idx_marks_industry  ON marks(industry);
CREATE INDEX IF NOT EXISTS idx_marks_country   ON marks(country);
CREATE INDEX IF NOT EXISTS idx_loans_cik       ON loans(cik);
CREATE INDEX IF NOT EXISTS idx_loans_issuer    ON loans(issuer_id);

-- Everything the UI reads, denormalised once here rather than in every query.
CREATE VIEW IF NOT EXISTS v_marks AS
SELECT
    m.*,
    b.ticker,
    b.name          AS bdc_name,
    l.investment_type,
    l.lien,
    l.facility,
    l.currency,
    l.is_debt,
    l.identifier,
    i.display_name  AS issuer_name,
    i.canonical_name
FROM marks m
JOIN loans   l ON l.loan_id  = m.loan_id
JOIN bdcs    b ON b.cik      = m.cik
JOIN issuers i ON i.issuer_id = m.issuer_id;
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path or SETTINGS.db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def session(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        init_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_bdcs(conn: sqlite3.Connection, bdcs: Sequence[BDC] | None = None) -> int:
    rows = [(b.cik, b.ticker, b.name, b.exchange) for b in (bdcs or load_universe())]
    conn.executemany(
        """
        INSERT INTO bdcs (cik, ticker, name, exchange) VALUES (?, ?, ?, ?)
        ON CONFLICT(cik) DO UPDATE SET ticker=excluded.ticker, name=excluded.name,
                                       exchange=excluded.exchange
        """,
        rows,
    )
    return len(rows)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _float(value) -> float | None:
    return None if value is None else float(value)


def load_positions(conn: sqlite3.Connection, positions: Iterable[Position]) -> dict:
    """Write positions into ``issuers``/``loans``/``marks``.

    Re-running is safe: marks are keyed on (loan, period) and the newer row
    wins, so a re-harvest of the same quarter overwrites rather than duplicates.
    """
    positions = list(positions)
    if not positions:
        return {"issuers": 0, "loans": 0, "marks": 0}

    issuers = {
        p.issuer_id: (p.issuer_id, canonical_issuer(p.issuer_name), p.issuer_name or "Unknown")
        for p in positions
    }
    conn.executemany(
        """
        INSERT INTO issuers (issuer_id, canonical_name, display_name) VALUES (?, ?, ?)
        -- Several BDCs spell the same borrower differently. Pick deterministically
        -- so the display name does not flip depending on load order.
        ON CONFLICT(issuer_id) DO UPDATE SET
            display_name = MIN(issuers.display_name, excluded.display_name)
        """,
        list(issuers.values()),
    )

    loans: dict[str, tuple] = {}
    for p in positions:
        period = _iso(p.period_end)
        existing = loans.get(p.loan_id)
        first_period = min(period, existing[9]) if existing and existing[9] else period
        last_period = max(period, existing[10]) if existing and existing[10] else period
        loans[p.loan_id] = (
            p.loan_id, p.cik, p.issuer_id, p.credit_id, p.investment_type, p.lien,
            p.facility, p.currency, int(p.is_debt), p.identifier, first_period, last_period,
        )
    conn.executemany(
        """
        INSERT INTO loans (loan_id, cik, issuer_id, credit_id, investment_type, lien,
                           facility, currency, is_debt, identifier, first_period, last_period)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(loan_id) DO UPDATE SET
            first_period = MIN(loans.first_period, excluded.first_period),
            last_period  = MAX(loans.last_period,  excluded.last_period),
            identifier   = COALESCE(excluded.identifier, loans.identifier)
        """,
        list(loans.values()),
    )

    mark_rows = []
    for p in positions:
        unrealized = p.unrealized
        mark_rows.append(
            (
                p.loan_id, _iso(p.period_end), p.cik, p.issuer_id, p.credit_id,
                _float(p.fair_value), _float(p.cost), _float(p.principal), _float(p.shares),
                p.mark, None if unrealized is None else float(unrealized),
                p.interest_rate, p.spread, p.reference_rate, p.pik_rate, p.pct_net_assets,
                _iso(p.maturity_date), _iso(p.acquisition_date), p.industry, p.country,
            p.principal_currency, p.fair_value_currency, p.fair_value_level,
                None if p.is_non_accrual is None else int(p.is_non_accrual),
                p.accession, p.form, _iso(p.filed_date), p.source, ",".join(p.flags),
            )
        )
    conn.executemany(
        """
        INSERT INTO marks (
            loan_id, period_end, cik, issuer_id, credit_id,
            fair_value, cost, principal, shares, mark, unrealized,
            interest_rate, spread, reference_rate, pik_rate, pct_net_assets,
            maturity_date, acquisition_date, industry, country,
            principal_ccy, fair_value_ccy, fair_value_level, is_non_accrual,
            accession, form, filed_date, source, flags
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(loan_id, period_end) DO UPDATE SET
            fair_value=excluded.fair_value, cost=excluded.cost, principal=excluded.principal,
            shares=excluded.shares, mark=excluded.mark, unrealized=excluded.unrealized,
            interest_rate=excluded.interest_rate, spread=excluded.spread,
            reference_rate=excluded.reference_rate, pik_rate=excluded.pik_rate,
            pct_net_assets=excluded.pct_net_assets, maturity_date=excluded.maturity_date,
            acquisition_date=excluded.acquisition_date, industry=excluded.industry,
            country=COALESCE(excluded.country, marks.country),
            principal_ccy=COALESCE(excluded.principal_ccy, marks.principal_ccy),
            fair_value_ccy=COALESCE(excluded.fair_value_ccy, marks.fair_value_ccy),
            fair_value_level=excluded.fair_value_level,
            is_non_accrual=COALESCE(excluded.is_non_accrual, marks.is_non_accrual),
            accession=excluded.accession, form=excluded.form, filed_date=excluded.filed_date,
            source=excluded.source, flags=excluded.flags
        """,
        mark_rows,
    )

    filing_rows = {
        p.accession: (p.accession, p.cik, p.form, _iso(p.period_end), _iso(p.filed_date), p.source, None)
        for p in positions
        if p.accession
    }
    if filing_rows:
        conn.executemany(
            """
            INSERT INTO filings (accession, cik, form, period_end, filed_date, source, url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(accession) DO NOTHING
            """,
            list(filing_rows.values()),
        )

    return {"issuers": len(issuers), "loans": len(loans), "marks": len(mark_rows)}


def start_run(conn: sqlite3.Connection, source: str, scope: str) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (started_at, source, scope) VALUES (?, ?, ?)",
        (datetime.utcnow().isoformat(timespec="seconds"), source, scope),
    )
    return int(cursor.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, positions: int, notes: str = "") -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, positions = ?, notes = ? WHERE run_id = ?",
        (datetime.utcnow().isoformat(timespec="seconds"), positions, notes, run_id),
    )


def stats(conn: sqlite3.Connection) -> dict:
    def scalar(sql: str):
        row = conn.execute(sql).fetchone()
        return None if row is None else row[0]

    return {
        "bdcs": scalar("SELECT COUNT(*) FROM bdcs"),
        "bdcs_with_marks": scalar("SELECT COUNT(DISTINCT cik) FROM marks"),
        "issuers": scalar("SELECT COUNT(*) FROM issuers"),
        "loans": scalar("SELECT COUNT(*) FROM loans"),
        "debt_loans": scalar("SELECT COUNT(*) FROM loans WHERE is_debt = 1"),
        "marks": scalar("SELECT COUNT(*) FROM marks"),
        "periods": scalar("SELECT COUNT(DISTINCT period_end) FROM marks"),
        "earliest_period": scalar("SELECT MIN(period_end) FROM marks"),
        "latest_period": scalar("SELECT MAX(period_end) FROM marks"),
        "total_fair_value": scalar(
            "SELECT SUM(fair_value) FROM marks WHERE period_end = (SELECT MAX(period_end) FROM marks)"
        ),
    }


def periods(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT period_end FROM marks ORDER BY period_end")]
