"""Convert local-currency amounts to USD at the period end.

Filers report fair value in USD but principal in the loan's own currency, so a
Principal column that mixes the two cannot be summed and cannot serve as the
denominator of a mark. Converting it needs a rate for the balance-sheet date —
the date the amount is *as of*, not the date the harvest ran.

Rates come from the ECB reference set, published daily and freely available. The
rate used and its date are stored on every converted row, so a figure can always
be traced back to the number it was multiplied by.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from bdctracker.config import SETTINGS

log = logging.getLogger(__name__)

#: ECB reference rates. No key, no quota, and a citable publisher.
BASE_URL = "https://api.frankfurter.dev/v1"

#: The ECB publishes on business days; a quarter end can fall on a weekend.
_MAX_LOOKBACK = 6


class FxRates:
    """Period-end USD rates, fetched once per date and cached on disk."""

    def __init__(self, offline: bool = False):
        self._memory: dict[date, dict[str, float]] = {}
        self.offline = offline

    @property
    def _cache_dir(self) -> Path:
        return SETTINGS.cache_dir / "fx"

    def _cache_path(self, on: date) -> Path:
        return self._cache_dir / f"{on.isoformat()}.json"

    def rates_on(self, on: date) -> dict[str, float]:
        """Units of each currency per USD, for a balance-sheet date."""
        if on in self._memory:
            return self._memory[on]

        cached = self._cache_path(on)
        if cached.exists():
            rates = json.loads(cached.read_text())
            self._memory[on] = rates
            return rates

        rates = {} if self.offline else self._fetch(on)
        self._memory[on] = rates
        if rates:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(rates))
        return rates

    def _fetch(self, on: date) -> dict[str, float]:
        """Ask for the date, stepping back over weekends and holidays."""
        import httpx

        for back in range(_MAX_LOOKBACK):
            asked = on - timedelta(days=back)
            try:
                response = httpx.get(
                    f"{BASE_URL}/{asked.isoformat()}",
                    params={"base": "USD"},
                    timeout=20.0,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                log.warning("FX rates unavailable for %s: %s", asked, exc)
                return {}
            rates = payload.get("rates") or {}
            if rates:
                rates["USD"] = 1.0
                rates["_date"] = payload.get("date", asked.isoformat())
                return rates
        return {}

    def to_usd(self, amount, currency: str | None, on: date) -> tuple[float | None, float | None, str | None]:
        """Return ``(usd_amount, rate_used, rate_date)``.

        A missing rate returns ``None`` rather than the unconverted figure: a
        local-currency number sitting in a USD column is the bug this exists to
        prevent, and passing it through silently would recreate it.
        """
        if amount is None:
            return None, None, None
        if not currency or currency == "USD":
            return float(amount), 1.0, None

        rates = self.rates_on(on)
        rate = rates.get(currency)
        if not rate:
            return None, None, None
        # The API quotes currency-per-USD, so dividing converts back to USD.
        return float(amount) / float(rate), float(rate), rates.get("_date")


def convert_positions(positions, rates: FxRates | None = None) -> int:
    """Fill ``principal_usd`` on every position whose principal is not in USD."""
    rates = rates or FxRates()
    converted = 0
    for position in positions:
        currency = position.principal_currency
        if position.principal is None:
            continue
        if not currency or currency == "USD":
            position.principal_usd = float(position.principal)
            position.fx_rate = 1.0
            continue

        usd, rate, rate_date = rates.to_usd(
            position.principal, currency, position.period_end
        )
        position.principal_usd = usd
        position.fx_rate = rate
        position.fx_date = rate_date
        if usd is None:
            position.flags.append(f"no_fx_rate_{currency.lower()}")
        else:
            converted += 1
    return converted
