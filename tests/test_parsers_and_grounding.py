import datetime as dt

from scholarship_intel.extraction import parsers as P
from scholarship_intel.extraction.models import Claim, Extraction
from scholarship_intel.textproc import Haystack
from scholarship_intel.verification.grounding import ground_extraction, absence_check

PAGE = """Test Merit Scholarship 2026-27
Eligibility:
Students who have passed Class XII with at least 75% marks. Family income should not exceed Rs. 6 lakh per annum.
The scholarship amount is Rs. 50,000 per annum.
Last date to apply is 31st October 2026.
"""


def ground(field, value, quote, text=PAGE):
    ex = Extraction("t", {field: Claim(field, value, quote, "t")})
    return ground_extraction(ex, Haystack(text), [])


def test_date_parsing_formats():
    t = "Last date 31st October 2026; opening 01/09/2026; extended to Nov 15, 2026; iso 2026-12-01"
    got = {h.date.isoformat() for h in P.find_dates(t)}
    assert {"2026-10-31", "2026-09-01", "2026-11-15", "2026-12-01"} <= got


def test_money_and_age():
    assert P.find_money("Rs. 50,000 per annum")[0].value == 50000
    assert P.find_money("up to ₹2.5 lakh")[0].value == 250000
    assert P.parse_age("age should not exceed 25 years") == (None, 25)
    assert P.parse_age("between 18 and 25 years") == (18, 25)


def test_valid_claims_are_grounded_with_offsets():
    g, rej = ground("closing_date", "2026-10-31", "Last date to apply is 31st October 2026.")
    assert not rej and g["closing_date"].start is not None
    assert PAGE[g["closing_date"].start:g["closing_date"].end].startswith("Last date")


def test_fabricated_quote_is_rejected():
    g, rej = ground("closing_date", "2026-08-31", "Applications close on 31 August 2026 for all candidates.")
    assert "closing_date" not in g and "not found" in rej[0].reason


def test_value_must_match_quote():
    g, rej = ground("closing_date", "2026-11-30", "Last date to apply is 31st October 2026.")
    assert "closing_date" not in g and "not written in quote" in rej[0].reason
    g, rej = ground("income", {"max_inr": 500000}, "Family income should not exceed Rs. 6 lakh per annum.")
    assert "income" not in g            # invented ₹5 lakh is rejected
    g, rej = ground("income", {"max_inr": 600000}, "Family income should not exceed Rs. 6 lakh per annum.")
    assert g["income"].value["max_inr"] == 600000


def test_unrelated_money_on_official_page_is_not_a_scholarship_amount():
    quote = "We are a USD 85 million fund and the first SaaS-focused fund in India."
    g, rej = ground("amount", {"min": 85000000, "max": 85000000, "currency": "USD"}, quote, PAGE + quote)
    assert "amount" not in g
    assert "student benefit" in rej[0].reason
    valid = "The scholarship amount is Rs. 50,000 per annum."
    g, rej = ground("amount", {"min": 50000, "max": 50000, "currency": "INR"}, valid)
    assert not rej and g["amount"].value["max"] == 50000


def test_enum_values_need_justification():
    g, _ = ground("categories", ["SC", "OBC"], "Students who have passed Class XII with at least 75% marks.")
    assert "categories" not in g
    text = PAGE + "Reserved for SC and ST students.\n"
    g, _ = ground("categories", ["SC", "ST", "OBC"], "Reserved for SC and ST students.", text)
    assert g["categories"].value == ["SC", "ST"]      # OBC dropped: not in the quote


def test_fuzzy_match_survives_pdf_line_wrapping():
    wrapped = "Family income from all sources should not be\nmore than Rs. 8 lakh per annum during the\nfinancial year of the application."
    g, _ = ground("income", {"max_inr": 800000},
                  "Family income from all sources should not be more than Rs. 8 lakh per annum during the financial year", wrapped)
    assert g["income"].match_score >= 92


def test_absence_is_checked_not_assumed():
    assert absence_check("income", "A scholarship for toppers. Apply online.")[0] is True
    assert absence_check("income", "Parents' income below Rs 3 lakh is required")[0] is False


def test_deadline_note_must_state_an_open_window():
    q = "Applications will be invited once in a year through the portal."
    g, rej = ground("deadline_note", q, q, PAGE + q + "\n")
    assert "deadline_note" not in g and "rolling" in rej[0].reason       # not an open window => cannot make a record ACTIVE
    q2 = "Applications are accepted throughout the year on a rolling basis."
    g, _ = ground("deadline_note", q2, q2, PAGE + q2 + "\n")
    assert "deadline_note" in g


def test_text_field_quote_must_be_about_the_field():
    t = PAGE + "Following a competitive national selection process, I was selected to assist the professor at the UN.\n"
    g, rej = ground("selection_process", "x", "Following a competitive national selection process, I was selected to assist the professor at the UN.", t)
    assert "selection_process" not in g and "first-person" in rej[0].reason          # testimonial, not a rule
    g, _ = ground("selection_process", "x", "Selection will be based on merit in the qualifying examination.",
                  PAGE + "Selection will be based on merit in the qualifying examination.\n")
    assert "selection_process" in g
    g, rej = ground("documents_required", "x", "The campus has a lovely garden and a library.", PAGE + "The campus has a lovely garden and a library.\n")
    assert "documents_required" not in g
