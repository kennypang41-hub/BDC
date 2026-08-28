"""Fallback source: pull the Schedule of Investments straight out of a filing.

Used for quarters DERA has not published yet (the newest one or two) and for
any BDC missing from a bulk data set. Slower — one XBRL download per filing —
but it reaches the same facts, because DERA builds its data sets from exactly
these tags.

The tags that matter, all dimensioned by ``us-gaap:InvestmentIdentifierAxis``
so there is one set per position:

    InvestmentOwnedAtFairValue          the mark
    InvestmentOwnedAtCost               what the BDC paid
    InvestmentOwnedBalancePrincipalAmount   par, the denominator of the mark
    InvestmentInterestRate / BasisSpreadVariableRate / InterestRatePaidInKind
    InvestmentMaturityDate
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from typing import Iterable, Sequence

from bdctracker import normalize
from bdctracker.config import configure_edgar
from bdctracker.models import Position

log = logging.getLogger(__name__)

IDENTIFIER_AXIS = "dim_us-gaap_InvestmentIdentifierAxis"

#: XBRL concept -> Position field. Values arrive as strings or numerics.
VALUE_CONCEPTS = {
    "us-gaap:InvestmentOwnedAtFairValue": "fair_value",
    "us-gaap:InvestmentOwnedAtCost": "cost",
    "us-gaap:InvestmentOwnedBalancePrincipalAmount": "principal",
    "us-gaap:InvestmentOwnedBalanceShares": "shares",
    "us-gaap:InvestmentOwnedPercentOfNetAssets": "pct_net_assets",
    "us-gaap:InvestmentInterestRate": "interest_rate",
    "us-gaap:InvestmentBasisSpreadVariableRate": "spread",
    "us-gaap:InvestmentInterestRatePaidInKind": "pik_rate",
    "us-gaap:InvestmentMaturityDate": "maturity_date",
    "us-gaap:InvestmentAcquisitionDate": "acquisition_date",
}

RATE_FIELDS = {"interest_rate", "spread", "pik_rate", "pct_net_assets"}
DATE_FIELDS = {"maturity_date", "acquisition_date"}
MONEY_FIELDS = {"fair_value", "cost", "principal", "shares"}

#: Dimension suffix -> descriptive field we care about.
DIMENSION_FIELDS = {
    "InvestmentIssuerNameAxis": "issuer",
    "InvestmentTypeAxis": "investment_type",
    "IndustrySectorAxis": "industry",
    "InvestmentIndustrySectorAxis": "industry",
    "LienCategoryAxis": "lien_category",
    "AssetClassAxis": "asset_class",
    "FinancialInstrumentAxis": "instrument",
    "FairValueByFairValueHierarchyLevelAxis": "fair_value_level",
    "InvestmentIssuerAffiliationAxis": "affiliation",
    "InvestmentNameAxis": "investment_name",
}


def _clean_member(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    return text.replace("[Member]", "").replace("Member", "").strip(" :")


def _describe_dimensions(fact: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in fact.items():
        if not key.startswith("dim_") or key == IDENTIFIER_AXIS:
            continue
        cleaned = _clean_member(value)
        if cleaned is None:
            continue
        axis = key.rsplit("_", 1)[-1]
        out[DIMENSION_FIELDS.get(axis, axis)] = cleaned
    return out


def positions_from_xbrl(
    xbrl,
    cik: int,
    *,
    accession: str | None = None,
    form: str | None = None,
    filed_date: date | None = None,
    periods: Iterable[str] | None = None,
    nonaccrual_by_period: dict[str, set[str]] | None = None,
    all_facts: list[dict] | None = None,
) -> list[Position]:
    """Extract every tagged position, for every balance-sheet date in the filing.

    Taking all instant periods (not just the latest) is deliberate: a 10-K
    carries the prior year-end schedule too, so one download yields two
    quarters of marks.

    ``all_facts`` lets a caller that has already built the fact list pass it in.
    Rebuilding it is the single most expensive step in the whole harvest, so it
    should happen once per filing, never once per consumer.
    """
    facts = all_facts if all_facts is not None else xbrl.facts.get_facts()
    wanted = set(periods) if periods else None

    grouped: dict[tuple[str, str], dict] = defaultdict(dict)
    for fact in facts:
        field = VALUE_CONCEPTS.get(fact.get("concept"))
        if field is None:
            continue
        identifier = fact.get(IDENTIFIER_AXIS)
        if not identifier:
            continue
        period = fact.get("period_instant")
        if not period or (wanted is not None and period not in wanted):
            continue

        bucket = grouped[(str(identifier), str(period))]
        value = fact.get("numeric_value")
        if value is None:
            value = fact.get("value")
        if value is not None and field not in bucket:
            bucket[field] = value
        bucket.setdefault("_dims", {}).update(_describe_dimensions(fact))

    non_accrual = nonaccrual_by_period or {}
    positions: list[Position] = []
    for (identifier, period), bucket in grouped.items():
        period_end = normalize.to_date(period)
        if period_end is None:
            continue
        dims = bucket.get("_dims", {})
        cleaned_identifier = _clean_member(identifier) or identifier
        descriptor = " ".join(
            str(v)
            for v in (
                dims.get("investment_name"),
                dims.get("investment_type"),
                dims.get("lien_category"),
                dims.get("asset_class"),
                dims.get("instrument"),
                cleaned_identifier,
            )
            if v
        )

        position = Position(
            cik=cik,
            period_end=period_end,
            identifier=cleaned_identifier,
            issuer_name=dims.get("issuer", "") or "",
            tranche_text=descriptor or None,
            industry=dims.get("industry"),
            fair_value=normalize.to_decimal(bucket.get("fair_value")),
            cost=normalize.to_decimal(bucket.get("cost")),
            principal=normalize.to_decimal(bucket.get("principal")),
            shares=normalize.to_decimal(bucket.get("shares")),
            interest_rate=normalize.to_rate_pct(bucket.get("interest_rate")),
            spread=normalize.to_rate_pct(bucket.get("spread")),
            pik_rate=normalize.to_rate_pct(bucket.get("pik_rate")),
            pct_net_assets=normalize.to_rate_pct(bucket.get("pct_net_assets")),
            maturity_date=normalize.to_date(bucket.get("maturity_date")),
            acquisition_date=normalize.to_date(bucket.get("acquisition_date")),
            fair_value_level=dims.get("fair_value_level"),
            is_non_accrual=True if identifier in non_accrual.get(period, set()) else None,
            accession=accession,
            form=form,
            filed_date=filed_date,
            source="xbrl",
        )
        positions.append(normalize.finalize(position))
    return positions


def nonaccrual_identifiers(xbrl, period: str | None = None,
                           all_facts: list[dict] | None = None) -> set[str]:
    """Identifiers the filing footnotes mark as non-accrual, for one period.

    Non-accrual status lives in footnotes rather than a tagged flag, so this is
    best-effort; a miss leaves the flag unset rather than asserting "accruing".

    Takes a parsed XBRL object rather than a filing on purpose. The public
    ``extract_nonaccrual`` re-downloads and re-parses the filing and rebuilds
    the fact list, which triples the cost of the most expensive step in the
    harvest — measured at roughly a minute a filing across forty-three BDCs.
    """
    try:
        from edgar.bdc.nonaccrual import _extract_nonaccrual_from_xbrl

        result = _extract_nonaccrual_from_xbrl(xbrl, period=period, all_facts=all_facts)
    except Exception as exc:
        log.debug("non-accrual extraction failed: %s", exc)
        return set()
    if result is None:
        return set()
    return {inv.identifier for inv in getattr(result, "investments", []) if inv.identifier}


def harvest_company(
    cik: int,
    *,
    forms: Sequence[str] = ("10-K", "10-Q"),
    since: date | None = None,
    limit: int | None = None,
    with_nonaccrual: bool = True,
) -> list[Position]:
    """Walk a BDC's periodic filings and extract every tagged position."""
    configure_edgar()
    from edgar import Company

    company = Company(cik)
    filings = company.get_filings(form=list(forms))
    if filings is None:
        return []

    collected: list[Position] = []
    seen = 0
    for filing in filings:
        filed = getattr(filing, "filing_date", None)
        filed = normalize.to_date(filed)
        if since and filed and filed < since:
            break
        if limit is not None and seen >= limit:
            break
        seen += 1
        try:
            xbrl = filing.xbrl()
        except Exception as exc:
            log.warning("no XBRL for %s %s: %s", cik, filing.accession_no, exc)
            continue
        if xbrl is None:
            continue

        try:
            # Built once and shared: the fact list is the expensive artefact,
            # not the download.
            facts = xbrl.facts.get_facts()
            by_period: dict[str, set[str]] = {}
            if with_nonaccrual:
                instants = {
                    f.get("period_instant") for f in facts
                    if f.get("concept") == "us-gaap:InvestmentOwnedAtFairValue"
                    and f.get("period_instant")
                }
                for instant in instants:
                    found = nonaccrual_identifiers(xbrl, period=instant, all_facts=facts)
                    if found:
                        by_period[instant] = found

            collected.extend(
                positions_from_xbrl(
                    xbrl,
                    cik,
                    accession=getattr(filing, "accession_no", None),
                    form=getattr(filing, "form", None),
                    filed_date=filed,
                    nonaccrual_by_period=by_period,
                    all_facts=facts,
                )
            )
        except Exception as exc:
            log.warning("extraction failed for %s %s: %s", cik, filing.accession_no, exc)
    return collected
