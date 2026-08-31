"""JSON API for the tracker front end."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from bdctracker import analytics, db, export

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="BDC Tracker", version="0.1.0")


def get_conn() -> sqlite3.Connection:
    path = os.environ.get("BDC_DB_PATH")
    conn = db.connect(path)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/api/bundle/meta")
def api_meta(period: str | None = None, conn=Depends(get_conn)):
    """Same payload as the static export's meta.json."""
    return export.meta(conn, period)


@app.get("/api/bundle/positions/{ticker}")
def api_bundle_positions(ticker: str, period: str | None = None, conn=Depends(get_conn)):
    return analytics.bdc_positions(conn, ticker, period)


@app.get("/api/bundle/{name}")
def api_bundle(name: str, period: str | None = None, conn=Depends(get_conn)):
    """Serve a named view so the front end reads the API and the static bundle alike."""
    try:
        return export.build(conn, name, period)
    except KeyError:
        raise HTTPException(404, f"unknown view {name!r}; try one of {sorted(export.BUNDLE)}")


@app.get("/api/overview")
def api_overview(period: str | None = None, conn=Depends(get_conn)):
    return analytics.overview(conn, period)


@app.get("/api/periods")
def api_periods(conn=Depends(get_conn)):
    return analytics.periods(conn)


@app.get("/api/bdcs")
def api_bdcs(period: str | None = None, conn=Depends(get_conn)):
    return analytics.bdc_summary(conn, period)


@app.get("/api/bdcs/{ticker}/positions")
def api_positions(ticker: str, period: str | None = None, conn=Depends(get_conn)):
    rows = analytics.bdc_positions(conn, ticker, period)
    if not rows:
        raise HTTPException(404, f"no positions for {ticker}")
    return rows


@app.get("/api/nonaccruals")
def api_nonaccruals(period: str | None = None, conn=Depends(get_conn)):
    return {
        "trend": analytics.nonaccrual_trend(conn),
        "positions": analytics.nonaccrual_positions(conn, period),
        "by_industry": analytics.nonaccrual_by(conn, "industry", period),
        "by_bdc": analytics.nonaccrual_by(conn, "ticker", period),
    }


@app.get("/api/marks/histogram")
def api_histogram(period: str | None = None, bin_width: float = 2.5, conn=Depends(get_conn)):
    return analytics.mark_histogram(conn, period, bin_width)


@app.get("/api/marks/sectors")
def api_sectors(quarters: int = 8, conn=Depends(get_conn)):
    return analytics.sector_marks(conn, quarters)


@app.get("/api/quarterly/marks")
def api_quarterly_marks(since: str = analytics.DEFAULT_SINCE, conn=Depends(get_conn)):
    return analytics.quarterly_bdc_marks(conn, since)


@app.get("/api/quarterly/nonaccrual-marks")
def api_quarterly_na_marks(since: str = analytics.DEFAULT_SINCE, conn=Depends(get_conn)):
    return analytics.quarterly_nonaccrual_marks(conn, since)


@app.get("/api/quarterly/nonaccrual-share")
def api_quarterly_na_share(since: str = analytics.DEFAULT_SINCE, conn=Depends(get_conn)):
    return analytics.quarterly_nonaccrual_share(conn, since)


@app.get("/api/maturities")
def api_maturities(period: str | None = None, conn=Depends(get_conn)):
    return analytics.maturity_wall(conn, period)


@app.get("/api/markdowns")
def api_markdowns(period: str | None = None, limit: int = 100, conn=Depends(get_conn)):
    return analytics.biggest_markdowns(conn, period, limit)


@app.get("/api/deteriorating")
def api_deteriorating(period: str | None = None, limit: int = 100, conn=Depends(get_conn)):
    return analytics.deteriorating(conn, period, limit)


@app.get("/api/disagreements")
def api_disagreements(
    period: str | None = None,
    min_holders: int = Query(2, ge=2),
    limit: int = 200,
    conn=Depends(get_conn),
):
    return analytics.disagreements(conn, period, min_holders, limit)


@app.get("/api/loans/{loan_id}")
def api_loan(loan_id: str, conn=Depends(get_conn)):
    rows = analytics.loan_history(conn, loan_id)
    if not rows:
        raise HTTPException(404, "unknown loan")
    return rows


@app.get("/api/issuers/{issuer_id}")
def api_issuer(issuer_id: str, conn=Depends(get_conn)):
    rows = analytics.issuer_detail(conn, issuer_id)
    if not rows:
        raise HTTPException(404, "unknown issuer")
    return rows


@app.get("/api/search")
def api_search(q: str, limit: int = 50, conn=Depends(get_conn)):
    return analytics.search_issuers(conn, q, limit)


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")
