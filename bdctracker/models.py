"""Core records that flow through the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from decimal import Decimal


@dataclass(slots=True)
class Position:
    """One investment held by one BDC at one balance-sheet date — i.e. one mark."""

    cik: int
    period_end: date
    identifier: str

    issuer_name: str = ""
    investment_type: str = "UNKNOWN"
    lien: str = "NA"
    facility: str | None = None
    tranche_text: str | None = None
    industry: str | None = None
    country: str | None = None
    currency: str = "USD"

    fair_value: Decimal | None = None
    cost: Decimal | None = None
    principal: Decimal | None = None
    shares: Decimal | None = None

    #: ISO 4217 unit of each amount, as the filing tagged it. Filers commonly
    #: report principal in the loan's own currency and fair value in USD, so
    #: these are not always the same and must not be assumed.
    fair_value_currency: str | None = None
    cost_currency: str | None = None
    principal_currency: str | None = None

    #: Principal restated in USD, with the rate that did it. Filled by
    #: :mod:`bdctracker.fx`; None where no rate was available.
    principal_usd: float | None = None
    fx_rate: float | None = None
    fx_date: str | None = None

    interest_rate: float | None = None
    spread: float | None = None
    reference_rate: str | None = None
    pik_rate: float | None = None
    pct_net_assets: float | None = None

    maturity_date: date | None = None
    acquisition_date: date | None = None
    #: Where the schedule pins a position to a year but not to a row, the year
    #: is kept on its own rather than invented into a date. A cohort is built
    #: from the year anyway, and half a fact beats none.
    year_acquired: int | None = None
    year_matures: int | None = None
    fair_value_level: str | None = None
    is_non_accrual: bool | None = None

    accession: str | None = None
    form: str | None = None
    filed_date: date | None = None
    source: str = "unknown"

    # Derived keys, filled by normalize.finalize()
    loan_id: str = ""
    issuer_id: str = ""
    credit_id: str = ""
    is_debt: bool = False

    flags: list[str] = field(default_factory=list)

    @property
    def mark(self) -> float | None:
        """Fair value over principal, as a percentage.

        This is "the mark" the whole tracker is built on: 100 means the BDC
        carries the position at par, below 100 means it has written it down.

        Principal is the denominator whenever the filing reports one. Only when
        it does not — equity, and debt the filer left untagged — does this fall
        back to cost, which is what :attr:`mark_basis` records.
        """
        base = self.mark_basis
        if base is None or base == 0 or self.fair_value is None:
            return None
        return float(self.fair_value) / float(base) * 100.0

    @property
    def mark_basis(self) -> Decimal | None:
        """The denominator the mark used: principal, else cost — same currency.

        A mark is a ratio, so it is currency-free only when both sides are in
        one currency. Filers routinely report principal in the loan's own
        currency and fair value in USD; dividing one by the other returns the
        exchange rate, which is how a healthy sterling loan came out "marked" at
        130. Cost is reported alongside fair value in USD, so it is the correct
        denominator whenever principal is not in the numerator's currency.
        """
        # A converted principal is in the fair value's currency by construction.
        if self.principal_usd and self.fair_value_currency in (None, "USD"):
            return Decimal(str(self.principal_usd))
        if self.principal is not None and self.principal != 0 and self._matches(
            self.principal_currency
        ):
            return self.principal
        if self.cost is not None and self.cost != 0 and self._matches(self.cost_currency):
            return self.cost
        return None

    def _matches(self, currency: str | None) -> bool:
        """True when an amount shares the fair value's unit, or units are unknown."""
        if currency is None or self.fair_value_currency is None:
            return True  # nothing tagged; the filing's own consistency is all we have
        return currency == self.fair_value_currency

    @property
    def mark_basis_currency(self) -> str | None:
        basis = self.mark_basis
        if basis is None:
            return None
        if basis == self.principal:
            return self.principal_currency or self.fair_value_currency
        return self.cost_currency or self.fair_value_currency

    @property
    def vintage_year(self) -> int | None:
        """The year the BDC acquired the position, where the filing says so.

        The acquisition date where one was matched to this exact facility, and
        otherwise the year the schedule gives the borrower when all of its rows
        agree on one. Never the quarter a position first appears in this
        dataset — that is when our coverage began, not when the loan was made,
        and reporting one as the other would put every legacy loan in the wrong
        cohort.
        """
        if self.acquisition_date:
            return self.acquisition_date.year
        return self.year_acquired

    @property
    def maturity_year(self) -> int | None:
        if self.maturity_date:
            return self.maturity_date.year
        return self.year_matures

    @property
    def unrealized(self) -> Decimal | None:
        if self.fair_value is None or self.cost is None:
            return None
        return self.fair_value - self.cost

    def to_row(self) -> dict:
        row = asdict(self)
        row["flags"] = ",".join(self.flags)
        row["mark"] = self.mark
        unreal = self.unrealized
        row["unrealized"] = None if unreal is None else str(unreal)
        for key in ("fair_value", "cost", "principal", "shares"):
            value = row.get(key)
            row[key] = None if value is None else str(value)
        for key in ("period_end", "maturity_date", "acquisition_date", "filed_date"):
            value = row.get(key)
            row[key] = None if value is None else value.isoformat()
        return row


@dataclass(slots=True)
class FilingRef:
    """Provenance for a batch of positions."""

    accession: str
    cik: int
    form: str | None = None
    period_end: date | None = None
    filed_date: date | None = None
    source: str = "unknown"
    url: str | None = None
