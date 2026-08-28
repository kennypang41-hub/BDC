from datetime import date
from decimal import Decimal

from bdctracker import normalize, pipeline
from bdctracker.models import Position


def make(identifier, accession="0001", fv=1_000, par=1_000, **kwargs):
    return normalize.finalize(
        Position(
            cik=1, period_end=date(2025, 12, 31), identifier=identifier, accession=accession,
            fair_value=Decimal(str(fv)), principal=Decimal(str(par)), **kwargs,
        )
    )


def test_a_filing_that_discloses_one_nonaccrual_resolves_the_rest_to_accruing():
    filing = [
        make("Acme Corp, First Lien Term Loan (non-accrual)"),
        make("Beta Corp, First Lien Term Loan"),
    ]
    assert filing[0].is_non_accrual is True
    assert filing[1].is_non_accrual is None

    pipeline.infer_accrual_status([filing])
    assert filing[1].is_non_accrual is False


def test_a_filing_that_discloses_nothing_leaves_status_unknown():
    filing = [make("Acme Corp, First Lien Term Loan"), make("Beta Corp, First Lien Term Loan")]
    pipeline.infer_accrual_status([filing])
    assert all(p.is_non_accrual is None for p in filing)


def test_clean_merges_then_dedupes_then_flags():
    positions = [
        # Two draws on one delayed-draw facility inside one filing: summed.
        make("Acme Corp, First Lien Delayed Draw Term Loan", fv=600, par=600),
        make("Acme Corp, First Lien Delayed Draw Term Loan", fv=300, par=400),
        # The same position echoed by a second filing: kept once.
        make("Beta Corp, First Lien Term Loan", accession="0002", fv=950, par=1_000),
        make("Beta Corp, First Lien Term Loan", accession="0003", fv=950, par=1_000),
    ]
    cleaned = pipeline.clean(positions)
    assert len(cleaned) == 2

    acme = next(p for p in cleaned if p.issuer_name.startswith("Acme"))
    assert acme.fair_value == Decimal("900")
    assert acme.principal == Decimal("1000")

    beta = next(p for p in cleaned if p.issuer_name.startswith("Beta"))
    assert beta.fair_value == Decimal("950")


def test_clean_rescales_a_filing_that_tagged_the_wrong_units():
    positions = [
        make(f"Borrower {i}, First Lien Term Loan", fv=980_000, par=1_000_000_000)
        for i in range(15)
    ]
    cleaned = pipeline.clean(positions)
    assert all(abs(p.mark - 98.0) < 0.01 for p in cleaned)
    assert all("rescaled_x1000" in p.flags for p in cleaned)


def test_clean_scores_each_filing_separately():
    good = [
        make(f"Good {i}, First Lien Term Loan", accession="0001", fv=980, par=1_000)
        for i in range(15)
    ]
    bad = [
        make(f"Bad {i}, First Lien Term Loan", accession="0002", fv=980_000, par=1_000_000_000)
        for i in range(15)
    ]
    cleaned = pipeline.clean(good + bad)
    assert all(abs(p.mark - 98.0) < 0.01 for p in cleaned)
    assert not any("rescaled" in f for p in cleaned if p.identifier.startswith("Good") for f in p.flags)


# ---------------------------------------------------------------------------
# Which filings the fallback reaches for
# ---------------------------------------------------------------------------

from bdctracker.sources.dera import Quarter  # noqa: E402

WINDOW = [Quarter(2024, 1), Quarter(2024, 2), Quarter(2024, 3)]


def test_filings_cover_the_whole_window_when_the_bulk_sets_are_skipped():
    """--no-dera once harvested nothing at all: filings were treated as a gap-filler."""
    since = pipeline._fallback_since(WINDOW, [], [], dera_ran=False)
    assert since == date(2024, 1, 1)


def test_filings_start_at_the_oldest_quarter_the_bulk_sets_missed():
    since = pipeline._fallback_since(WINDOW, [Quarter(2024, 2)], [], dera_ran=True)
    assert since == date(2024, 4, 1)


def test_filings_only_top_up_when_the_bulk_sets_covered_everything():
    harvested = [make("Acme Corp, First Lien Term Loan")]
    since = pipeline._fallback_since(WINDOW, [], harvested, dera_ran=True)
    assert since == date(2025, 12, 31)


def test_nothing_to_fetch_when_there_is_nothing_to_go_on():
    assert pipeline._fallback_since([], [], [], dera_ran=True) is None
    assert pipeline._fallback_since([], [], [], dera_ran=False) is None
