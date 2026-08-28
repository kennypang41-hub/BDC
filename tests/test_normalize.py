from datetime import date
from decimal import Decimal

import pytest

from bdctracker import normalize
from bdctracker.models import Position


def make(**kwargs) -> Position:
    base = dict(cik=1, period_end=date(2025, 6, 30), identifier="Acme Corp, First Lien Term Loan")
    base.update(kwargs)
    return normalize.finalize(Position(**base))


def test_to_decimal_handles_filer_formatting():
    assert normalize.to_decimal("$1,234.50") == Decimal("1234.50")
    assert normalize.to_decimal("(500)") == Decimal("-500")
    assert normalize.to_decimal("") is None
    assert normalize.to_decimal(None) is None
    assert normalize.to_decimal("n/a") is None


def test_to_date_accepts_the_formats_filers_actually_use():
    assert normalize.to_date("2025-06-30") == date(2025, 6, 30)
    assert normalize.to_date("6/30/2025") == date(2025, 6, 30)
    assert normalize.to_date(20250630) == date(2025, 6, 30)
    assert normalize.to_date("garbage") is None


def test_rates_normalise_to_percentage_points_either_way_they_were_tagged():
    assert normalize.to_rate_pct(0.1125) == pytest.approx(11.25)
    assert normalize.to_rate_pct(11.25) == pytest.approx(11.25)
    assert normalize.to_rate_pct(None) is None


def test_mark_is_fair_value_over_par_for_debt():
    position = make(fair_value=Decimal("980000"), principal=Decimal("1000000"), cost=Decimal("995000"))
    assert position.mark == pytest.approx(98.0)


def test_mark_falls_back_to_cost_for_equity():
    position = make(
        identifier="Acme Corp, Common Equity",
        fair_value=Decimal("1200"), cost=Decimal("1000"),
    )
    assert not position.is_debt
    assert position.mark == pytest.approx(120.0)


def test_mark_is_none_without_a_denominator():
    assert make(fair_value=Decimal("100")).mark is None


def test_finalize_reads_currency_and_reference_rate_off_the_label():
    position = make(identifier="Acme GmbH, First Lien Term Loan, EURIBOR + 6.00%, EUR")
    assert position.currency == "EUR"
    assert position.reference_rate == "EURIBOR"


def test_merge_sums_two_facilities_that_share_a_key():
    common = dict(
        cik=1, period_end=date(2025, 6, 30), accession="0001",
        identifier="Acme Corp, First Lien Delayed Draw Term Loan",
    )
    a = normalize.finalize(Position(**common, fair_value=Decimal("100"), principal=Decimal("100"), interest_rate=10.0))
    b = normalize.finalize(Position(**common, fair_value=Decimal("300"), principal=Decimal("400"), interest_rate=12.0))
    merged = normalize.merge_within_filing([a, b])

    assert len(merged) == 1
    assert merged[0].fair_value == Decimal("400")
    assert merged[0].principal == Decimal("500")
    # Weighted by fair value: (10*100 + 12*300) / 400
    assert merged[0].interest_rate == pytest.approx(11.5)
    assert any(f.startswith("merged_") for f in merged[0].flags)


def test_merge_does_not_combine_across_filings():
    # The same position reported by a 10-Q and the following 10-K is one mark,
    # not two, and must never be added together.
    kwargs = dict(cik=1, period_end=date(2025, 6, 30), identifier="Acme Corp, First Lien Term Loan")
    a = normalize.finalize(Position(**kwargs, accession="0001", fair_value=Decimal("100"), principal=Decimal("100")))
    b = normalize.finalize(Position(**kwargs, accession="0002", fair_value=Decimal("100"), principal=Decimal("100")))
    merged = normalize.merge_within_filing([a, b])
    assert len(merged) == 2

    deduped = normalize.dedupe(merged)
    assert len(deduped) == 1
    assert deduped[0].fair_value == Decimal("100")


def test_dedupe_prefers_the_more_complete_row():
    kwargs = dict(cik=1, period_end=date(2025, 6, 30), identifier="Acme Corp, First Lien Term Loan")
    sparse = normalize.finalize(Position(**kwargs, accession="a", fair_value=Decimal("100")))
    rich = normalize.finalize(
        Position(**kwargs, accession="b", fair_value=Decimal("100"), cost=Decimal("99"),
                 principal=Decimal("100"), interest_rate=11.0, maturity_date=date(2029, 1, 1))
    )
    kept = normalize.dedupe([sparse, rich])
    assert len(kept) == 1
    assert kept[0].accession == "b"


def _thousand_scaled_filing():
    positions = []
    for i in range(20):
        positions.append(
            make(
                identifier=f"Borrower {i}, First Lien Term Loan",
                fair_value=Decimal("980000"),
                principal=Decimal("1000000000"),  # tagged in units, should be thousands
            )
        )
    return positions


def test_scale_anomaly_is_detected_and_corrected():
    positions = _thousand_scaled_filing()
    factor = normalize.detect_scale_anomaly(positions)
    assert factor == pytest.approx(1000.0)

    normalize.apply_scale(positions, factor)
    assert positions[0].mark == pytest.approx(98.0)
    assert "rescaled_x1000" in positions[0].flags


def test_a_normal_filing_is_left_alone():
    positions = [
        make(identifier=f"Borrower {i}, First Lien Term Loan",
             fair_value=Decimal("980"), principal=Decimal("1000"))
        for i in range(20)
    ]
    assert normalize.detect_scale_anomaly(positions) is None


def test_quality_flags():
    positions = [
        make(fair_value=Decimal("100"), principal=Decimal("100")),
        make(identifier="Other Corp, First Lien Term Loan", fair_value=Decimal("100")),
        make(identifier="Third Corp, Mystery Instrument"),
    ]
    normalize.flag_quality(positions)
    assert positions[0].flags == []
    assert "no_principal" in positions[1].flags
    assert "no_fair_value" in positions[2].flags
    assert "unclassified" in positions[2].flags


# ---------------------------------------------------------------------------
# Restatement rows
# ---------------------------------------------------------------------------

def _facility(identifier, par, cost, fv, accession="0001"):
    """A row as Main Street tags it: some priced, some fair-value only."""
    return normalize.finalize(
        Position(
            cik=1396440, period_end=date(2026, 6, 30), accession=accession,
            identifier=identifier,
            principal=None if par is None else Decimal(str(par)),
            cost=None if cost is None else Decimal(str(cost)),
            fair_value=None if fv is None else Decimal(str(fv)),
        )
    )


def _ra_outdoors_as_filed():
    """Main Street's RA Outdoors rows, 2026-06-30, verbatim from the filing.

    Four real facilities, each priced; then the same four fair values repeated
    under 1.1-1.4 with no par and no cost. Summing all eight doubles the mark.
    """
    return [
        _facility("RA Outdoors LLC | Secured Debt 1",   1_510_000, 1_510_000,  1_258_000),
        _facility("RA Outdoors LLC | Secured Debt 1.1",      None,      None,  1_258_000),
        _facility("RA Outdoors LLC | Secured Debt 1.2",      None,      None, 13_160_000),
        _facility("RA Outdoors LLC | Secured Debt 1.3",      None,      None,    399_000),
        _facility("RA Outdoors LLC | Secured Debt 1.4",      None,      None,    389_000),
        _facility("RA Outdoors LLC | Secured Debt 2",     478_000,   478_000,    399_000),
        _facility("RA Outdoors LLC | Secured Debt 3",     467_000,   467_000,    389_000),
        _facility("RA Outdoors LLC | Secured Debt 4",  15_794_000, 15_788_000, 13_160_000),
    ]


def test_ra_outdoors_totals_match_the_filing():
    merged = normalize.merge_within_filing(_ra_outdoors_as_filed())
    assert len(merged) == 1
    position = merged[0]

    assert position.principal == Decimal("18249000")
    assert position.cost == Decimal("18243000")
    # The reported figure. Summing the restatement rows gave 30,412,000.
    assert position.fair_value == Decimal("15206000")
    assert position.mark == pytest.approx(100 * 15_206 / 18_249)


def test_restatement_rows_are_dropped_and_flagged():
    kept = normalize.drop_restatements(_ra_outdoors_as_filed())
    assert [p.identifier.rsplit(" ", 1)[-1] for p in kept] == ["1", "2", "3", "4"]


def test_a_group_with_no_priced_row_is_left_intact():
    """Nothing distinguishes a restatement when no sibling carries par or cost."""
    rows = [
        _facility("Acme | Secured Debt 1", None, None, 100),
        _facility("Acme | Secured Debt 2", None, None, 200),
    ]
    assert len(normalize.drop_restatements(rows)) == 2


def test_genuinely_distinct_facilities_still_sum():
    rows = [
        _facility("Acme | Secured Debt 1", 1_000, 1_000, 900),
        _facility("Acme | Secured Debt 2", 2_000, 2_000, 1_800),
    ]
    merged = normalize.merge_within_filing(rows)
    assert len(merged) == 1
    assert merged[0].principal == Decimal("3000")
    assert merged[0].fair_value == Decimal("2700")
