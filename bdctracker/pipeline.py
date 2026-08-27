"""Orchestration: harvest -> normalise -> load."""
from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from bdctracker import db, normalize
from bdctracker.config import check_sec_reachable, configure_edgar
from bdctracker.models import Position
from bdctracker.sources import dera, xbrl
from bdctracker.universe import BDC, load_universe

log = logging.getLogger(__name__)


def clean(positions: Sequence[Position]) -> list[Position]:
    """Merge, rescale and flag a batch of raw positions.

    Order matters. Facilities are merged inside a filing first, then each
    filing's units are sanity-checked, then overlapping filings are collapsed —
    doing the scale check after cross-filing dedupe would mix a mis-scaled
    filing's rows with a correct one's and hide the problem.
    """
    merged = normalize.merge_within_filing(positions)

    by_filing: dict[tuple[int, str | None, date], list[Position]] = defaultdict(list)
    for position in merged:
        by_filing[(position.cik, position.accession, position.period_end)].append(position)

    for group in by_filing.values():
        factor = normalize.detect_scale_anomaly(group)
        if factor is not None:
            log.warning(
                "rescaling %s positions for cik=%s period=%s by %g (filer tagged the wrong units)",
                len(group), group[0].cik, group[0].period_end, factor,
            )
            normalize.apply_scale(group, factor)

    infer_accrual_status(by_filing.values())

    deduped = normalize.dedupe(merged)
    normalize.flag_quality(deduped)
    return deduped


def infer_accrual_status(filings: Iterable[Sequence[Position]]) -> None:
    """Resolve unknown non-accrual flags to False where the filing disclosed any.

    A filing that names even one non-accrual position is telling us it discloses
    them, so the rest of that schedule is accruing. A filing that names none
    tells us nothing, and those positions stay unknown rather than being
    reported as clean.
    """
    for group in filings:
        if any(p.is_non_accrual for p in group):
            for position in group:
                if position.is_non_accrual is None:
                    position.is_non_accrual = False


def harvest_dera(
    quarters: Sequence[dera.Quarter],
    ciks: Iterable[int],
    *,
    refresh: bool = False,
) -> tuple[list[Position], list[dera.Quarter]]:
    return dera.harvest(quarters, ciks, refresh=refresh)


def harvest_filings(
    bdcs: Sequence[BDC],
    *,
    since: date | None = None,
    forms: Sequence[str] = ("10-K", "10-Q"),
    limit_per_bdc: int | None = None,
    workers: int = 4,
) -> list[Position]:
    """Pull positions straight from filings, a few BDCs at a time.

    Kept to a handful of workers on purpose: the SEC asks for no more than ten
    requests a second and each filing costs several.
    """
    collected: list[Position] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                xbrl.harvest_company,
                bdc.cik,
                forms=forms,
                since=since,
                limit=limit_per_bdc,
            ): bdc
            for bdc in bdcs
        }
        for future in as_completed(futures):
            bdc = futures[future]
            try:
                positions = future.result()
            except Exception as exc:
                log.error("filing harvest failed for %s (%s): %s", bdc.ticker, bdc.cik, exc)
                continue
            log.info("%s: %s positions from filings", bdc.ticker, len(positions))
            collected.extend(positions)
    return collected


def run(
    *,
    start: dera.Quarter | None = None,
    end: dera.Quarter | None = None,
    tickers: Sequence[str] | None = None,
    db_path: str | Path | None = None,
    use_dera: bool = True,
    use_filings: bool = True,
    refresh: bool = False,
    workers: int = 4,
    limit_per_bdc: int | None = None,
    preflight: bool = True,
) -> dict:
    """Build (or extend) the mark dataset.

    The bulk data sets carry the history cheaply; filings cover whatever DERA
    has not published yet. Both write to the same tables, and marks are keyed
    on (loan, period), so the two paths reconcile instead of double-counting.
    """
    if preflight and (use_dera or use_filings):
        # One probe up front. Without it a blocked network is only discovered
        # twelve quarters and forty-three companies later, one retry at a time.
        configure_edgar()
        check_sec_reachable()

    universe = load_universe()
    if tickers:
        wanted = {t.upper() for t in tickers}
        universe = tuple(b for b in universe if b.ticker in wanted)
        if not universe:
            raise ValueError(f"no BDCs in the universe match {sorted(wanted)}")
    ciks = [b.cik for b in universe]

    end = end or dera.latest_published_quarter()
    start = start or dera.Quarter(end.year - 2, end.quarter)
    window = dera.quarters_between(start, end)

    raw: list[Position] = []
    missing: list[dera.Quarter] = []
    if use_dera:
        raw_dera, missing = harvest_dera(window, ciks, refresh=refresh)
        log.info("DERA: %s positions across %s quarters", len(raw_dera), len(window) - len(missing))
        raw.extend(raw_dera)

    if use_filings:
        # Only reach for filings to cover what the bulk sets did not.
        since = _fallback_since(window, missing, raw)
        if since is not None:
            raw.extend(
                harvest_filings(
                    universe,
                    since=since,
                    limit_per_bdc=limit_per_bdc,
                    workers=workers,
                )
            )

    positions = clean(raw)

    with db.session(db_path) as conn:
        run_id = db.start_run(
            conn,
            source="dera+filings" if (use_dera and use_filings) else ("dera" if use_dera else "filings"),
            scope=f"{start}..{end} n_bdcs={len(universe)}",
        )
        db.upsert_bdcs(conn, universe)
        counts = db.load_positions(conn, positions)
        db.finish_run(
            conn,
            run_id,
            len(positions),
            notes=f"missing_dera_quarters={[str(q) for q in missing]}",
        )
        summary = db.stats(conn)

    return {
        "quarters": [str(q) for q in window],
        "missing_dera_quarters": [str(q) for q in missing],
        "raw_positions": len(raw),
        "loaded": counts,
        "quality": normalize.summarize_quality(positions),
        "db": summary,
    }


def _fallback_since(
    window: Sequence[dera.Quarter],
    missing: Sequence[dera.Quarter],
    harvested: Sequence[Position],
) -> date | None:
    """Earliest filing date worth downloading to cover DERA's gaps.

    If DERA served the whole window there is nothing to fetch; otherwise start
    from the oldest missing quarter.
    """
    if not missing:
        # Still top up the current quarter, which DERA never has yet.
        latest = max((p.period_end for p in harvested), default=None)
        if latest is None:
            return None
        return latest
    oldest = min(missing, key=lambda q: (q.year, q.quarter))
    return date(oldest.year, 3 * (oldest.quarter - 1) + 1, 1)
