"""Fold the whole tracker into one self-contained HTML file.

Useful for handing the site to someone without a server: the stylesheet, the
scripts and every view's JSON are inlined, so the page needs no network at all.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from bdctracker import analytics, export

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def build_bundle(conn: sqlite3.Connection, period: str | None = None) -> dict:
    """Every view the front end can ask for, as one object."""
    period = period or analytics.latest_period(conn)
    if period is None:
        raise ValueError("the database has no marks; run `bdc harvest` first")

    bundle = {name: export.build(conn, name, period) for name in export.BUNDLE}
    bundle["meta"] = export.meta(conn, period)
    bundle["positions"] = {
        ticker: analytics.bdc_positions(conn, ticker, period)
        for ticker in bundle["meta"]["tickers"]
    }
    return bundle


def _body(html: str) -> str:
    """Strip the document scaffolding; an artifact host supplies its own."""
    match = re.search(r"<body[^>]*>(.*)</body>", html, re.S)
    return (match.group(1) if match else html).strip()


def _inline_modules() -> str:
    """Concatenate the ES modules, dropping the import/export that joined them."""
    charts = (WEB_DIR / "charts.js").read_text()
    app = (WEB_DIR / "app.js").read_text()
    charts = re.sub(r"^export (?=const|function)", "", charts, flags=re.M)
    app = re.sub(r'^import .*?from "\./charts\.js";\n', "", app, flags=re.M | re.S)
    return f"{charts}\n\n{app}"


def build_html(conn: sqlite3.Connection, period: str | None = None,
               title: str = "BDC Tracker") -> str:
    bundle = build_bundle(conn, period)
    payload = json.dumps(bundle, separators=(",", ":"))
    # </script> inside the data would close the tag early.
    payload = payload.replace("</", "<\\/")

    index = (WEB_DIR / "index.html").read_text()
    styles = (WEB_DIR / "styles.css").read_text()

    return "\n".join([
        f"<title>{title}</title>",
        "<style>",
        styles,
        "</style>",
        _body(index).replace('<script type="module" src="app.js"></script>', ""),
        f"<script>window.__BDC_BUNDLE__ = {payload};</script>",
        "<script>",
        _inline_modules(),
        "</script>",
    ])


def write_html(conn: sqlite3.Connection, path: str | Path,
               period: str | None = None) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    html = build_html(conn, period)
    path.write_text(html)
    return {"path": str(path), "bytes": len(html.encode("utf-8"))}
