"""Converting local-currency principal to USD."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bdctracker import fx, normalize
from bdctracker.models import Position


class StubRates(fx.FxRates):
    """Rates without a network, quoted currency-per-USD as the ECB does."""

    def __init__(self, table=None):
        super().__init__(offline=True)
        self._table = table if table is not None else {
            "GBP": 0.79, "EUR": 0.92, "CAD": 1.36, "_date": "2026-06-30",
        }

    def rates_on(self, on: date):
        return dict(self._table)


def _position(principal, currency, fair_value=None, cost=None):
    return normalize.finalize(Position(
        cik=1, period_end=date(2026, 6, 30),
        identifier="Acme Holdings, First Lien Term Loan",
        principal=Decimal(str(principal)), principal_currency=currency,
        fair_value=None if fair_value is None else Decimal(str(fair_value)),
        fair_value_currency="USD" if fair_value is not None else None,
        cost=None if cost is None else Decimal(str(cost)), cost_currency="USD",
    ))


def test_sterling_principal_is_restated_in_dollars():
    position = _position(25_885_000, "GBP")
    assert fx.convert_positions([position], StubRates()) == 1
    # 25.885m GBP at 0.79 per USD.
    assert position.principal_usd == pytest.approx(25_885_000 / 0.79)
    assert position.fx_rate == pytest.approx(0.79)
    assert position.fx_date == "2026-06-30"


def test_a_dollar_principal_is_passed_through_untouched():
    position = _position(10_000, "USD")
    fx.convert_positions([position], StubRates())
    assert position.principal_usd == 10_000
    assert position.fx_rate == 1.0


def test_an_untagged_currency_is_treated_as_dollars():
    """Most filings tag no unit; the filing's own consistency is all we have."""
    position = _position(10_000, None)
    fx.convert_positions([position], StubRates())
    assert position.principal_usd == 10_000


def test_a_missing_rate_leaves_the_figure_out_rather_than_passing_it_through():
    """A local number in a USD column is the bug this exists to prevent."""
    position = _position(1_000, "ZWL")
    assert fx.convert_positions([position], StubRates()) == 0
    assert position.principal_usd is None
    assert "no_fx_rate_zwl" in position.flags


def test_the_converted_principal_becomes_the_mark_denominator():
    """FV / principal, as asked — no longer a fallback to cost."""
    position = _position(25_885_000, "GBP", fair_value=33_755_000, cost=34_100_000)
    fx.convert_positions([position], StubRates())

    expected_usd = 25_885_000 / 0.79
    assert position.mark_basis == pytest.approx(Decimal(str(expected_usd)))
    assert position.mark == pytest.approx(33_755_000 / expected_usd * 100)
    # Around par, not the 130 that dividing USD by GBP produced.
    assert 95 < position.mark < 108


def test_without_a_rate_the_mark_still_falls_back_to_cost():
    position = _position(1_000, "ZWL", fair_value=900, cost=1_000)
    fx.convert_positions([position], StubRates())
    assert position.mark_basis == Decimal("1000")
    assert position.mark == pytest.approx(90.0)


def test_rates_are_only_fetched_once_per_date(monkeypatch, tmp_path):
    """One request per balance-sheet date, however many positions need it."""
    from bdctracker.config import Settings

    monkeypatch.setattr(fx, "SETTINGS", Settings(root=tmp_path, identity="test"))
    calls = []

    class Counting(fx.FxRates):
        def _fetch(self, on):
            calls.append(on)
            return {"EUR": 0.92, "USD": 1.0, "_date": on.isoformat()}

    rates = Counting()
    assert rates.rates_on(date(2026, 6, 30))["EUR"] == 0.92
    rates.rates_on(date(2026, 6, 30))
    assert len(calls) == 1


def test_a_cached_date_is_read_back_from_disk(monkeypatch, tmp_path):
    from bdctracker.config import Settings

    monkeypatch.setattr(fx, "SETTINGS", Settings(root=tmp_path, identity="test"))

    class Once(fx.FxRates):
        def _fetch(self, on):
            return {"EUR": 0.92, "_date": on.isoformat()}

    Once().rates_on(date(2026, 6, 30))
    # A second instance must not need the network at all.
    offline = fx.FxRates(offline=True)
    assert offline.rates_on(date(2026, 6, 30))["EUR"] == 0.92
