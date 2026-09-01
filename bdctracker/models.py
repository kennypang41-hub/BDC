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

    interest_rate: float | None = None
    spread: float | None = None
    reference_rate: str | None = None
    pik_rate: float | None = None
    pct_net_assets: float | None = None

    maturity_date: date | None = None
    acquisition_date: date | None = None
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
        """The denominator the mark actually used: principal, else cost."""
        if self.principal is not None and self.principal != 0:
            return self.principal
        return self.cost

    @property
    def vintage_year(self) -> int | None:
        """The year the BDC acquired the position, where the filing says so.

        Only ever the tagged acquisition date. The quarter a position first
        appears in this dataset is not its vintage — it is the quarter our
        coverage began — and reporting one as the other would put every legacy
        loan in the wrong cohort.
        """
        return self.acquisition_date.year if self.acquisition_date else None

    @property
    def maturity_year(self) -> int | None:
        return self.maturity_date.year if self.maturity_date else None

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
