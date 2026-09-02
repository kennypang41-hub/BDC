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
    schedule_parse: bool = typer.Option(
        True, "--schedule/--no-schedule",
        help="Also parse the printed Schedule of Investments for sector, "
             "country, maturity and acquisition date.",
    ),
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
            with_schedule=schedule_parse,
        )
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
def inspect(
    ticker: str = typer.Option(..., help="BDC to pull, e.g. MAIN."),
    issuer: str = typer.Option(None, help="Only show borrowers whose name contains this."),
    limit: int = typer.Option(1, help="How many recent filings to read."),
    period: str = typer.Option(None, help="Only show this balance-sheet date."),
) -> None:
    """Print raw positions straight from a filing, before any merging.

    The merge step sums positions that share a loan key, so when a total looks
    wrong the question is always which raw rows went into it. This shows them
    with their full XBRL identifiers and the key each one derived.
    """
    from bdctracker.sources import xbrl as xbrl_source
    from bdctracker.universe import resolve

    bdc = resolve(ticker)
    try:
        raw = xbrl_source.harvest_company(bdc.cik, limit=limit)
    except SecUnreachable as exc:
        console.print(f"[red]Cannot reach the SEC.[/red] {exc}")
        raise typer.Exit(3) from None

    rows = raw
    if issuer:
        needle = issuer.upper()
        rows = [p for p in rows if needle in (p.issuer_name or "").upper()
                or needle in (p.identifier or "").upper()]
    if period:
        rows = [p for p in rows if p.period_end.isoformat() == period]

    console.print(f"[bold]{bdc.ticker}[/bold] — {len(rows)} raw positions "
                  f"of {len(raw)} in the last {limit} filing(s)")

    groups: dict[str, list] = {}
    for position in rows:
        groups.setdefault(position.loan_id, []).append(position)

    for loan_id, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        head = members[0]
        console.print(
            f"\n[cyan]loan {loan_id}[/cyan]  {head.investment_type}"
            f"  facility={head.facility}  ({len(members)} raw row(s))"
        )
        for m in sorted(members, key=lambda p: (p.period_end, p.identifier or "")):
            console.print(
                f"    {m.period_end}  par={_fmt(float(m.principal) if m.principal else None)}"
                f"  cost={_fmt(float(m.cost) if m.cost else None)}"
                f"  fv={_fmt(float(m.fair_value) if m.fair_value else None)}"
            )
            console.print(f"      id: {m.identifier!r}")


@app.command()
def concepts(
    ticker: str = typer.Option(..., help="BDC to read, e.g. MAIN."),
    limit: int = typer.Option(1, help="How many recent filings to scan."),
    top: int = typer.Option(40, help="How many concepts and axes to list."),
) -> None:
    """List the XBRL concepts and axes a filing actually tags per investment.

    Extraction maps concept names to fields, and a name that is merely plausible
    yields an empty column with no error. This reports what the filings really
    carry, so the map is built from evidence rather than from guesses.
    """
    from collections import Counter

    from bdctracker.config import SecUnreachable, configure_edgar
    from bdctracker.sources.xbrl import IDENTIFIER_AXIS
    from bdctracker.universe import resolve

    bdc = resolve(ticker)
    configure_edgar()
    from edgar import Company

    try:
        filings = Company(bdc.cik).get_filings(form=["10-K", "10-Q"])
    except Exception as exc:
        console.print(f"[red]Cannot reach the SEC.[/red] {exc}")
        raise typer.Exit(3) from None

    concept_counts: Counter = Counter()
    axis_counts: Counter = Counter()
    scanned = 0
    for filing in filings:
        if scanned >= limit:
            break
        try:
            xbrl = filing.xbrl()
        except Exception:
            continue
        if xbrl is None:
            continue
        scanned += 1
        console.print(f"[dim]scanning {filing.form} {filing.accession_no}[/dim]")
        for fact in xbrl.facts.get_facts():
            if not fact.get(IDENTIFIER_AXIS):
                continue
            concept_counts[fact.get("concept")] += 1
            for key in fact:
                if key.startswith("dim_") and key != IDENTIFIER_AXIS and fact[key]:
                    axis_counts[key[4:]] += 1

    table = Table(title=f"{bdc.ticker}: concepts on investment-dimensioned facts")
    table.add_column("Concept"); table.add_column("Facts", justify="right")
    for name, count in concept_counts.most_common(top):
        table.add_row(str(name), f"{count:,}")
    console.print(table)

    table = Table(title=f"{bdc.ticker}: other axes on those facts")
    table.add_column("Axis"); table.add_column("Facts", justify="right")
    for name, count in axis_counts.most_common(top):
        table.add_row(str(name), f"{count:,}")
    console.print(table)


@app.command()
def schedule(
    ticker: str = typer.Option(..., help="BDC to read, e.g. MAIN."),
    limit: int = typer.Option(1, help="How many recent filings to parse."),
    show: int = typer.Option(12, help="How many parsed rows to print."),
    debug: bool = typer.Option(False, help="Dump every table's header and classification."),
    match: bool = typer.Option(False, help="Also pull the tagged positions and "
                                           "report where the join loses them."),
) -> None:
    """Parse the printed Schedule of Investments and report what it yields.

    The XBRL of most filers carries no sector, country, maturity or acquisition
    date. This reads them from the rendered table, and reports coverage so the
    parse can be judged against a real filing rather than a fixture.
    """
    from bdctracker.config import SecUnreachable, configure_edgar
    from bdctracker.sources import soi_html
    from bdctracker.universe import resolve

    bdc = resolve(ticker)
    configure_edgar()
    from edgar import Company

    try:
        filings = Company(bdc.cik).get_filings(form=["10-K", "10-Q"])
    except Exception as exc:
        console.print(f"[red]Cannot reach the SEC.[/red] {exc}")
        raise typer.Exit(3) from None

    scanned = 0
    for filing in filings:
        if scanned >= limit:
            break
        scanned += 1
        console.print(f"\n[bold]{bdc.ticker}[/bold] {filing.form} {filing.accession_no}")
        if debug:
            _dump_tables(filing, soi_html)

        rows = soi_html.parse_filing(filing)
        if not rows:
            console.print("  [yellow]no schedule table recognised[/yellow]")
            continue

        if match:
            _report_match(filing, bdc, rows, soi_html)

        total = len(rows)
        have = {
            field: sum(1 for r in rows if getattr(r, field) is not None)
            for field in ("industry", "country", "maturity_date", "acquisition_date")
        }
        console.print(f"  parsed {total:,} rows")
        for field, count in have.items():
            console.print(f"    {field:<18} {count:>6,}  ({count / total * 100:5.1f}%)")

        table = Table(title="sample")
        for column in ("Borrower", "Instrument", "Sector", "Country", "Maturity", "Acquired"):
            table.add_column(column)
        for row in rows[:show]:
            table.add_row(
                row.issuer[:34], (row.instrument or "")[:26], (row.industry or "-")[:22],
                row.country or "-", str(row.maturity_date or "-"), str(row.acquisition_date or "-"),
            )
        console.print(table)


def _report_match(filing, bdc, rows, soi_html) -> None:
    """Show where the parsed rows and the tagged positions fail to meet."""
    from bdctracker.sources import xbrl as xbrl_source

    from bdctracker import normalize

    raw = xbrl_source.positions_from_xbrl(
        filing.xbrl(), bdc.cik, accession=getattr(filing, "accession_no", None),
    )
    if not raw:
        console.print("  [yellow]no tagged positions to match against[/yellow]")
        return

    # Merged, because that is what the harvest matches against: reporting the
    # raw count made the parse look like it covered a third of the schedule.
    positions = normalize.merge_within_filing(raw)
    report = soi_html.match_report(positions, soi_html.build_index(rows))
    total = report["positions"] or 1
    console.print(f"  [bold]join[/bold] scale={report['scale']!r} "
                  f"parsed_rows={report['parsed_rows']:,} positions={total:,} "
                  f"(merged from {len(raw):,} raw)")
    for key in ("matched", "borrower_not_in_schedule", "position_unpriced",
                "no_row_within_reach", "ambiguous"):
        console.print(f"    {key:26} {report[key]:6,}  ({100 * report[key] / total:5.1f}%)")


def _dump_tables(filing, soi_html, limit: int = 4) -> None:
    """Show the tables the parse actually reads, and what it found in them.

    Ordered by rows produced rather than rows present: a filing's biggest table
    is often the cash flow statement, and the question worth answering is which
    table the borrowers came out of and which columns were located in it.
    """
    from edgar.documents import parse_html

    document = parse_html(filing.html())
    tables = list(document.tables)
    console.print(f"  [dim]{len(tables)} tables in the document[/dim]")

    seen = []
    carried = None
    for index, table in enumerate(tables):
        try:
            headers, columns, skip = soi_html.locate_header(table)
            headed = soi_html.is_schedule_of_investments(columns)
            if headed:
                carried = columns
                produced = soi_html.read_rows(table, columns, skip, width=len(headers))
            elif carried:
                columns, skip = carried, 0
                produced = soi_html.read_rows(table, carried, skip=0)
                produced = produced if len(produced) >= soi_html._MIN_SCHEDULE_ROWS else []
            else:
                produced = []
        except Exception as exc:
            console.print(f"  [red]table {index} raised {exc}[/red]")
            continue
        if produced:
            seen.append((len(produced), index, headers, columns, headed, skip, table, produced))

    if not seen:
        console.print("  [yellow]no table produced any rows[/yellow]")
        return

    console.print(f"  [dim]{len(seen)} tables produced rows[/dim]")
    for count, index, headers, columns, headed, skip, table, produced in sorted(
        seen, reverse=True, key=lambda entry: entry[0]
    )[:limit]:
        origin = "own header" if headed else "carried header"
        console.print(
            f"  [cyan]table {index}[/cyan] produced={count} ({origin}) skip={skip}"
        )
        console.print(f"    columns: {columns}")
        missing = [f for f in ("acquisition", "maturity", "industry", "country")
                   if f not in columns]
        if missing:
            console.print(f"    [yellow]no column found for: {', '.join(missing)}[/yellow]")
        if headers:
            console.print(f"    header: {[h for h in headers if h][:16]}")
        rows = list(getattr(table, "rows", []))
        for offset, row in enumerate(rows[skip : skip + 3]):
            expanded: list[str] = []
            for cell in row.cells:
                expanded.append(soi_html.cell_text(cell))
                expanded.extend([""] * (max(1, getattr(cell, "colspan", 1)) - 1))
            console.print(f"      row {offset}: {[(i, t) for i, t in enumerate(expanded) if t][:16]}")
        sample = produced[0]
        console.print(
            f"    first parsed: issuer={sample.issuer!r} sector={sample.industry!r} "
            f"maturity={sample.maturity_date} acquired={sample.acquisition_date} "
            f"fv={sample.fair_value}"
        )


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
def bundle(
    out: Path = typer.Option(None, help="Output .html (default data/bdc-tracker.html)."),
    period: str = typer.Option(None, help="Period to freeze. Defaults to latest."),
    db_path: Path = typer.Option(None),
) -> None:
    """Build a single self-contained HTML file: styles, scripts and data inlined."""
    from bdctracker import standalone

    SETTINGS.ensure_dirs()
    target = out or (SETTINGS.root / "bdc-tracker.html")
    with db.session(db_path) as conn:
        try:
            result = standalone.write_html(conn, target, period)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None
    console.print(f"[green]wrote[/green] {result['path']} ({result['bytes'] / 1e6:.1f} MB)")


@app.command()
def excel(
    out: Path = typer.Option(None, help="Output .xlsx (default data/bdc-marks-<period>.xlsx)."),
    period: str = typer.Option(None, help="Period for the summary sheets. Defaults to latest."),
    include_unpriced: bool = typer.Option(
        False, "--include-unpriced/--priced-only",
        help="Keep positions the filing reported without a fair value.",
    ),
    db_path: Path = typer.Option(None),
) -> None:
    """Export the marks to a workbook: Marks, summaries, cohorts, Read me."""
    from bdctracker import excel as excel_mod

    with db.session(db_path) as conn:
        target = out
        if target is None:
            latest = period or analytics.latest_period(conn)
            SETTINGS.ensure_dirs()
            target = SETTINGS.root / f"bdc-marks-{latest or 'empty'}.xlsx"
        try:
            result = excel_mod.export_workbook(conn, target, period, include_unpriced)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None

    console.print(f"[green]wrote[/green] {result['marks']:,} marks to {result['path']}")
    if result["synthetic"]:
        console.print(
            "[yellow]This workbook contains SYNTHETIC data from `bdc demo`, "
            "not marks extracted from SEC filings.[/yellow]"
        )


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
