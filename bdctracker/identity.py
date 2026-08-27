"""Turning free-text Schedule of Investments labels into stable keys.

Every quarter each BDC re-tags the same position with a slightly different
string: the rate resets, the maturity rolls, the borrower picks up a "(dba ...)".
Marks are only comparable across quarters — and across BDCs — once those labels
collapse onto a stable key, so this module is the backbone of the whole dataset.
"""
from __future__ import annotations

import hashlib
import re

# Legal / structuring suffixes that carry no identity. Stripped repeatedly from
# the tail of a name so "Acme Intermediate Holdings, LLC" -> "ACME".
_LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "LLC", "LC", "LLP", "LP", "PLLC", "CORP", "CORPORATION",
    "CO", "COMPANY", "COMPANIES", "LTD", "LIMITED", "PLC", "SA", "SAS", "SARL",
    "GMBH", "NV", "BV", "AB", "AS", "OY", "PTY", "ULC", "SPA", "SRL", "AG",
    "HOLDING", "HOLDINGS", "HOLDCO", "TOPCO", "BIDCO", "MIDCO", "INTERMEDIATE",
    "PARENT", "GROUP", "TRUST", "PARTNERS", "PARTNERSHIP", "ENTERPRISES",
    "INVESTMENTS", "ACQUISITION", "ACQUISITIONS", "BORROWER", "OPCO", "NEWCO",
}

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")

# Rate/date/size noise that changes every quarter and must never reach a key.
_NOISE_PATTERNS = [
    re.compile(r"\b(?:sofr|libor|euribor|sonia|bsby|prime|base rate|cdor|term sofr)\b", re.I),
    re.compile(r"\b[lspe]\s*\+\s*[\d.]+\s*%?", re.I),          # "L + 5.75%", "S+575"
    re.compile(r"[\d.]+\s*%"),                                  # "11.25%"
    re.compile(r"\b(?:pik|cash)\b", re.I),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),                 # 6/30/2027
    re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*\d{1,2}?,?\s*\d{4}\b", re.I),
    re.compile(r"\b(?:19|20)\d{2}\b"),                          # bare years
    re.compile(r"\bdue\b", re.I),
    re.compile(r"\bmatur(?:es|ity|ing)\b", re.I),
    re.compile(r"[\d,]+(?:\.\d+)?\s*(?:x|shares|units|warrants?)\b", re.I),
]

# Ordered longest-first: "second lien" must win before a bare "lien" heuristic,
# and "first lien last out" before "first lien".
_TYPE_RULES: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"first[\s-]*lien[\s-]*last[\s-]*out|flfo|last[\s-]*out", re.I), "FIRST_LIEN_LAST_OUT", "FIRST"),
    (re.compile(r"unitranche", re.I), "UNITRANCHE", "FIRST"),
    (re.compile(r"(first|1st|senior)[\s-]*lien", re.I), "FIRST_LIEN", "FIRST"),
    (re.compile(r"(second|2nd)[\s-]*lien", re.I), "SECOND_LIEN", "SECOND"),
    (re.compile(r"(third|3rd)[\s-]*lien", re.I), "THIRD_LIEN", "THIRD"),
    (re.compile(r"\bmezzanine\b|\bmezz\b", re.I), "MEZZANINE", "SUBORDINATED"),
    (re.compile(r"subordinated|\bsub(?:\s|-)?debt\b|junior[\s-]*secured", re.I), "SUBORDINATED", "SUBORDINATED"),
    (re.compile(r"senior[\s-]*secured", re.I), "SENIOR_SECURED", "FIRST"),
    (re.compile(r"unsecured", re.I), "UNSECURED", "UNSECURED"),
    (re.compile(r"\bclo\b|collateralized loan obligation|structured (?:product|note)|joint venture|\bjv\b", re.I), "STRUCTURED", "NA"),
    (re.compile(r"preferred[\s-]*(?:stock|equity|shares|units|interest)|\bpreferred\b", re.I), "PREFERRED_EQUITY", "EQUITY"),
    (re.compile(r"warrant", re.I), "WARRANT", "EQUITY"),
    (re.compile(r"common[\s-]*(?:stock|equity|shares|units)|\bequity\b|llc[\s-]*(?:interest|unit)|membership[\s-]*interest|\bunits?\b|\bshares?\b", re.I), "COMMON_EQUITY", "EQUITY"),
    (re.compile(r"equipment[\s-]*(?:financing|loan|lease)", re.I), "EQUIPMENT", "FIRST"),
    (re.compile(r"revolv|\bdelayed[\s-]*draw\b|\bddtl\b|term[\s-]*loan|\bnotes?\b|\bbonds?\b|\bloan\b|\bdebt\b", re.I), "OTHER_DEBT", "NA"),
)

#: Types that represent lending, i.e. the ones that carry a loan "mark".
DEBT_TYPES = frozenset({
    "FIRST_LIEN", "FIRST_LIEN_LAST_OUT", "UNITRANCHE", "SECOND_LIEN", "THIRD_LIEN",
    "MEZZANINE", "SUBORDINATED", "SENIOR_SECURED", "UNSECURED", "EQUIPMENT", "OTHER_DEBT",
})

EQUITY_TYPES = frozenset({"COMMON_EQUITY", "PREFERRED_EQUITY", "WARRANT"})

# Facility flavours, kept separately so a revolver and a term loan to the same
# borrower stay distinct positions.
_FACILITY_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bdelayed[\s-]*draw\b|\bddtl\b", re.I), "DDTL"),
    (re.compile(r"revolv", re.I), "REVOLVER"),
    (re.compile(r"\bterm[\s-]*loan\b|\btl\b", re.I), "TERM_LOAN"),
    (re.compile(r"\bnotes?\b|\bbonds?\b", re.I), "NOTES"),
)

# Tranche letters/numbers ("Term Loan B", "Tranche A-2") distinguish facilities
# that are otherwise described identically.
_TRANCHE_ORDINAL = re.compile(
    r"\b(?:term\s*loan|tranche|facility|note|series|class)\s*([A-H](?:-?\d)?|\d{1,2})\b", re.I
)
_FIRST_LAST_OUT = re.compile(r"\b(first|last)[\s-]*out\b", re.I)


def canonical_issuer(name: str | None) -> str:
    """Collapse a portfolio-company name to a cross-BDC matching key.

    Two BDCs lending to the same borrower rarely spell it the same way, so the
    key drops punctuation, parentheticals and structuring suffixes.
    """
    if not name:
        return ""
    text = _PARENTHETICAL.sub(" ", str(name)).upper()
    text = text.replace("&", " AND ")
    text = _NON_ALNUM.sub(" ", text)
    tokens = _WS.sub(" ", text).strip().split()
    # Strip trailing legal noise, but never strip a name down to nothing.
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def strip_noise(text: str | None) -> str:
    """Remove rates, dates and sizes — anything that moves quarter to quarter."""
    if not text:
        return ""
    out = str(text)
    for pattern in _NOISE_PATTERNS:
        out = pattern.sub(" ", out)
    out = _NON_ALNUM.sub(" ", out.upper())
    return _WS.sub(" ", out).strip()


def classify_investment(*texts: str | None) -> tuple[str, str]:
    """Return ``(investment_type, lien)`` from any label text available.

    Texts are searched in order, so pass the most specific field first.
    """
    joined = " ".join(t for t in texts if t)
    if not joined.strip():
        return "UNKNOWN", "NA"
    for pattern, inv_type, lien in _TYPE_RULES:
        if pattern.search(joined):
            return inv_type, lien
    return "UNKNOWN", "NA"


def facility_kind(*texts: str | None) -> str | None:
    joined = " ".join(t for t in texts if t)
    for pattern, kind in _FACILITY_RULES:
        if pattern.search(joined):
            return kind
    return None


def tranche_signature(*texts: str | None) -> str:
    """A short, quarter-stable descriptor of *which* facility this is.

    Deliberately narrow: only the structural markers a filer keeps constant
    (facility kind, tranche letter, first/last out). Using the full label would
    break the key every time a filer reworded a description or the rate reset.
    """
    joined = " ".join(t for t in texts if t)
    if not joined.strip():
        return ""
    parts: list[str] = []
    kind = facility_kind(joined)
    if kind:
        parts.append(kind)
    ordinal = _TRANCHE_ORDINAL.search(joined)
    if ordinal:
        parts.append(ordinal.group(1).upper().replace("-", ""))
    out = _FIRST_LAST_OUT.search(joined)
    if out:
        parts.append(f"{out.group(1).upper()}_OUT")
    return "/".join(parts)


def is_debt(investment_type: str) -> bool:
    return investment_type in DEBT_TYPES


def _digest(*parts: str) -> str:
    payload = "|".join(p or "" for p in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def issuer_key(name: str | None) -> str:
    """Stable id for a portfolio company, shared across every BDC."""
    return _digest("issuer", canonical_issuer(name))


def loan_key(
    cik: int,
    issuer_name: str | None,
    investment_type: str,
    tranche_text: str | None = None,
    currency: str = "USD",
) -> str:
    """Stable id for one position held by one BDC, across quarters.

    Scoped to the BDC because the same borrower held by two BDCs is two
    separate positions with two separate marks — comparing them is the point.

    Two facilities of the same kind to the same borrower collapse onto one key
    and are summed by :func:`bdctracker.normalize.merge_within_filing`. That is
    intentional: the alternative is a key that depends on row order, which is
    not stable from one quarter to the next.
    """
    return _digest(
        "loan",
        str(cik),
        canonical_issuer(issuer_name),
        investment_type,
        tranche_signature(tranche_text),
        currency or "USD",
    )


def credit_key(issuer_name: str | None, investment_type: str) -> str:
    """Stable id for "this borrower, at this point in the capital structure".

    Used to line up the same credit across different BDCs so their marks can be
    compared. Lien family rather than exact type, since one BDC's "First Lien
    Senior Secured Loan" is another's "Senior Secured First Lien Term Loan".
    """
    lien = "NA"
    for _pattern, inv_type, lien_family in _TYPE_RULES:
        if inv_type == investment_type:
            lien = lien_family
            break
    return _digest("credit", canonical_issuer(issuer_name), lien)


def split_identifier(identifier: str | None) -> tuple[str, str]:
    """Split an SOI identifier into ``(issuer, remainder)``.

    Identifiers are conventionally "Borrower Name, Instrument description ...",
    but plenty of filers use a dash or nothing at all, so fall back gracefully.
    """
    if not identifier:
        return "", ""
    text = str(identifier).strip()
    for sep in (",", " - ", " – ", " — ", ";", "|"):
        if sep in text:
            head, _, tail = text.partition(sep)
            head, tail = head.strip(), tail.strip()
            # A leading fragment that is itself an instrument description means
            # the filer led with the instrument; keep the whole string as issuer.
            if head and len(head) > 1:
                return head, tail
    return text, ""
