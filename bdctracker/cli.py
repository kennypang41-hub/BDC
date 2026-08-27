"""Command line entry point: ``bdc``."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from bdctracker import analytics, db, export as export_mod, pipeline
from bdctracker.config import SETTINGS, SecUnreachable, check_sec_reachable
from bdctracker.sources.dera import Quarter, latest_published_quarter
from bdctracker.universe import UNIVERSE_PATH, load_universe, sync_universe

app = typer.Typer(add_completion=False, help="Extract and serve BDC loan valuation marks from SEC EDGAR.")
universe_app = typer.Typer(help="Inspect and refresh the BDC coverage universe.")
app.add_typer(universe_app, name="universe")

console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


@universe_app.command("list")
def universe_list() -> None:
    """Show the BDCs we pull marks for."""
    table = Table(title=f"BDC universe ({len(load_universe())})")
    for column in ("Ticker", "CIK", "Name", "Exchange"):
        table.add_column(column)
    for bdc in load_universe():
        table.add_row(bdc.ticker, str(bdc.cik), bdc.name, bdc.exchange or "")
    console.print(table)


@universe_app.command("sync")
def universe_sync(
    write: bool = typer.Option(False, "--write", help="Write the refreshed universe back to disk."),
) -> None:
    """Verify CIKs and names against the SEC BDC Report (needs network)."""
    report = sync_universe()
    document = report.pop("document")
    console.print_json(json.dumps(report, indent=2, default=str))
    if write:
        UNIVERSE_PATH.write_text(json.dumps(document, indent=2) + "\n")
        console.print(f"[green]wrote[/green] {UNIVERSE_PATH}")


@app.command()
def harvest(
    start: str = typer.Option(None, help="First quarter of DERA data, e.g. 2023Q4."),
    end: str = typer.Option(None, help="Last quarter, e.g. 2025Q4. Defaults to newest published."),
    tickers: str = typer.Option(None, help="Comma-separated subset, e.g. ARCC,TSLX."),
    dera: bool = typer.Option(True, "--dera/--no-dera", help="Use the SEC bulk BDC data sets."),
    filings: bool = typer.Option(True, "--filings/--no-filings", help="Fall back to per-filing XBRL."),
    refresh: bool = typer.Option(False, help="Re-download cached data sets."),
    workers: int = typer.Option(4, help="Parallel filing downloads (SEC allows ~10 req/s)."),
    limit_per_bdc: int = typer.Option(None, help="Cap filings per BDC; useful for a smoke test."),
    preflight: bool = typer.Option(True, "--preflight/--no-preflight",
                                   help="Probe sec.gov once before starting."),
    db_path: Path = typer.Option(None, help="SQLite file to write (default data/bdc.db)."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Extract marks from EDGAR and load them into the database."""
    _setup_logging(verbose)
    try:
        result = pipeline.run(
            start=Quarter.parse(start) if start else None,
            end=Quarter.parse(end) if end else None,
            tickers=[t.strip() for t in tickers.split(",")] if tickers else None,
            db_path=db_path,
            use_dera=dera,
            use_filings=filings,
            refresh=refresh,
            workers=workers,
            limit_per_bdc=limit_per_bdc,
            preflight=preflight,
        )
    except SecUnreachable as exc:
        console.print(f"[red]Cannot reach the SEC.[/red] {exc}")
        raise typer.Exit(3) from None
    except SecUnreachable as exc:
        console.print(f"[red]Cannot reach the SEC.[/red] {exc}")
        raise typer.Exit(3) from None
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    console.print_json(json.dumps(result, indent=2, default=str))
    if not result["raw_positions"]:
        console.print(
            "[red]No positions extracted.[/red] Every requested quarter came back empty — "
            "usually no network route to sec.gov, or a window with no published data sets. "
            "Check `missing_dera_quarters` above and re-run with -v."
        )
        raise typer.Exit(1)


@app.command()
def backfill(
    tickers: str = typer.Option(..., help="Comma-separated BDCs to re-pull from filings."),
    since: str = typer.Option(None, help="Only filings on or after this date (YYYY-MM-DD)."),
    db_path: Path = typer.Option(None),
    workers: int = typer.Option(4),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Re-extract specific BDCs straight from their filings, skipping the bulk sets."""
    _setup_logging(verbose)
    from datetime import date as _date

    wanted = {t.strip().upper() for t in tickers.split(",")}
    bdcs = [b for b in load_universe() if b.ticker in wanted]
    if not bdcs:
        raise typer.BadParameter(f"none of {sorted(wanted)} are in the universe")

    try:
        raw = pipeline.harvest_filings(
            bdcs,
            since=_date.fromisoformat(since) if since else None,
            workers=workers,
        )
    except SecUnreachable as exc:
        console.print(f"[red]Cannot reach the SEC.[/red] {exc}")
        raise typer.Exit(3) from None
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    positions = pipeline.clean(raw)
    with db.session(db_path) as conn:
        db.upsert_bdcs(conn, bdcs)
        counts = db.load_positions(conn, positions)
        summary = db.stats(conn)
    console.print_json(json.dumps({"loaded": counts, "db": summary}, indent=2, default=str))


@app.command()
def stats(db_path: Path = typer.Option(None)) -> None:
    """Show what is in the database."""
    with db.session(db_path) as conn:
        summary = db.stats(conn)
        table = Table(title="BDC mark dataset")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        for key, value in summary.items():
            table.add_row(key, f"{value:,}" if isinstance(value, (int, float)) and value else str(value))
        console.print(table)


@app.command()
def report(
    what: str = typer.Argument("bdcs", help="bdcs | nonaccruals | disagreements | markdowns | maturities"),
    period: str = typer.Option(None, help="Period end, e.g. 2025-09-30. Defaults to latest."),
    limit: int = typer.Option(25),
    db_path: Path = typer.Option(None),
) -> None:
    """Print one of the tracker views to the terminal."""
    with db.session(db_path) as conn:
        views = {
            "bdcs": lambda: analytics.bdc_summary(conn, period),
            "nonaccruals": lambda: analytics.nonaccrual_positions(conn, period),
            "disagreements": lambda: analytics.disagreements(conn, period, limit=limit),
            "markdowns": lambda: analytics.biggest_markdowns(conn, period, limit=limit),
            "maturities": lambda: analytics.maturity_wall(conn, period),
            "deteriorating": lambda: analytics.deteriorating(conn, period, limit=limit),
        }
        if what not in views:
            raise typer.BadParameter(f"unknown report {what!r}; choose from {sorted(views)}")
        rows = views[what]()[:limit]

    if not rows:
        console.print("[yellow]no data — run `bdc harvest` first[/yellow]")
        raise typer.Exit(1)

    table = Table(title=what)
    for column in rows[0]:
        table.add_column(column)
    for row in rows:
        table.add_row(*[_fmt(v) for v in row.values()])
    console.print(table)


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


@app.command()
def export(
    out: Path = typer.Option(None, help="Directory for the JSON bundle (default data/export)."),
    db_path: Path = typer.Option(None),
) -> None:
    """Write the JSON bundle the static front end reads."""
    target = out or SETTINGS.export_dir
    with db.session(db_path) as conn:
        written = export_mod.export_all(conn, target)
    console.print(f"[green]wrote[/green] {len(written)} files to {target}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    db_path: Path = typer.Option(None),
) -> None:
    """Run the tracker web app."""
    import os

    import uvicorn

    if db_path:
        os.environ["BDC_DB_PATH"] = str(db_path)
    uvicorn.run("bdctracker.api:app", host=host, port=port, reload=False)


@app.command()
def demo(
    db_path: Path = typer.Option(None),
    quarters: int = typer.Option(8, help="How many quarters of synthetic history."),
) -> None:
    """Load a synthetic dataset for UI work. Not SEC data — labelled as such everywhere."""
    from bdctracker import demo as demo_mod

    console.print("[yellow]Generating SYNTHETIC data — not extracted from EDGAR.[/yellow]")
    result = demo_mod.build(db_path, n_quarters=quarters)
    console.print_json(json.dumps(result, indent=2, default=str))


@app.command()
def quarters() -> None:
    """Show the newest quarter the SEC is likely to have published."""
    console.print(str(latest_published_quarter()))


@app.command()
def doctor() -> None:
    """Check that this machine can actually reach EDGAR."""
    from bdctracker.config import IDENTITY_ENV, configure_edgar

    try:
        identity = configure_edgar()
    except RuntimeError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(2) from None
    console.print(f"[green]✓[/green] {IDENTITY_ENV} = {identity}")

    try:
        check_sec_reachable()
    except SecUnreachable as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(3) from None
    console.print("[green]✓[/green] sec.gov is reachable")


if __name__ == "__main__":
    app()
