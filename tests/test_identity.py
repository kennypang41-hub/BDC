from bdctracker import identity


def test_canonical_issuer_strips_structuring_layers():
    assert identity.canonical_issuer("Acme Intermediate Holdings, LLC") == "ACME"
    assert identity.canonical_issuer("ACME Corp.") == "ACME"
    assert identity.canonical_issuer("Acme & Sons, Inc. (dba Acme Plumbing)") == "ACME AND SONS"


def test_canonical_issuer_never_empties_a_name():
    # "Holdings" alone is all we have; keep it rather than key on "".
    assert identity.canonical_issuer("Holdings") == "HOLDINGS"
    assert identity.canonical_issuer(None) == ""


def test_two_bdcs_spelling_the_same_borrower_differently_agree():
    a = identity.canonical_issuer("Project Alpha Bidco Limited")
    b = identity.canonical_issuer("Project Alpha BidCo Ltd.")
    assert a == b == "PROJECT ALPHA"


def test_classify_picks_the_more_specific_lien():
    assert identity.classify_investment("Second Lien Senior Secured Loan")[0] == "SECOND_LIEN"
    assert identity.classify_investment("First Lien Senior Secured Loan")[0] == "FIRST_LIEN"
    assert identity.classify_investment("First Lien Last Out Term Loan")[0] == "FIRST_LIEN_LAST_OUT"
    assert identity.classify_investment("Preferred Equity")[0] == "PREFERRED_EQUITY"
    assert identity.classify_investment("Warrants to purchase common")[0] == "WARRANT"
    assert identity.classify_investment("")[0] == "UNKNOWN"


def test_debt_and_equity_split():
    assert identity.is_debt("FIRST_LIEN")
    assert not identity.is_debt("COMMON_EQUITY")
    assert not identity.is_debt("UNKNOWN")


def test_loan_key_survives_a_quarterly_rate_and_maturity_reset():
    q1 = identity.loan_key(
        1, "Acme Holdings, LLC", "FIRST_LIEN",
        "First Lien Senior Secured Term Loan, SOFR + 5.75%, 11.12%, due 6/30/2029",
    )
    q2 = identity.loan_key(
        1, "Acme Holdings LLC", "FIRST_LIEN",
        "First Lien Senior Secured Term Loan, SOFR + 5.75%, 10.48%, due 6/30/2029",
    )
    assert q1 == q2


def test_loan_key_separates_a_revolver_from_a_term_loan():
    term = identity.loan_key(1, "Acme", "FIRST_LIEN", "First Lien Term Loan")
    revolver = identity.loan_key(1, "Acme", "FIRST_LIEN", "First Lien Revolver")
    assert term != revolver


def test_loan_key_separates_tranche_letters():
    a = identity.loan_key(1, "Acme", "FIRST_LIEN", "First Lien Term Loan A")
    b = identity.loan_key(1, "Acme", "FIRST_LIEN", "First Lien Term Loan B")
    assert a != b


def test_loan_key_is_scoped_to_the_bdc():
    assert identity.loan_key(1, "Acme", "FIRST_LIEN", "Term Loan") != identity.loan_key(
        2, "Acme", "FIRST_LIEN", "Term Loan"
    )


def test_credit_key_matches_the_same_credit_across_bdcs():
    # Two filers, two wordings, same borrower and same point in the stack.
    one = identity.credit_key("Acme Holdings, LLC", "FIRST_LIEN")
    two = identity.credit_key("ACME Holdings LLC", "SENIOR_SECURED")
    assert one == two
    assert one != identity.credit_key("Acme Holdings, LLC", "SECOND_LIEN")


def test_split_identifier():
    issuer, rest = identity.split_identifier("Acme Corp, First Lien Term Loan, due 2029")
    assert issuer == "Acme Corp"
    assert rest.startswith("First Lien")
    assert identity.split_identifier("Acme Corp") == ("Acme Corp", "")
