"""Regression tests built from REAL mistakes found in the crawl: a date inside a sentence is not necessarily the application deadline."""
from scholarship_intel.extraction import rules
from scholarship_intel.extraction.models import Claim, Extraction
from scholarship_intel.textproc import Haystack
from scholarship_intel.verification.grounding import ground_extraction

RELIANCE_FAQ = """Frequently Asked Questions
1. When is the deadline to submit my application?
The deadline for all applications is 11.59pm IST on Monday the 5th October 2026.
2. When is the deadline to submit my reference letters?
The deadline for referees to submit their references online is one week after the application
deadline, 11.59pm IST on Monday, the 12th October 2026.
3. When do I submit bank details?
The deadline to submit your bank account and PAN details is 18 October 2026.
"""


def closing(text):
    c = rules.extract(text, "", [], [], "https://x.org/y").claims.get("closing_date")
    return c.value if c else None


def test_application_deadline_beats_referee_and_bank_dates():
    assert closing(RELIANCE_FAQ) == "2026-10-05"


def test_notification_programme_and_communication_dates_are_not_deadlines():
    assert closing("Applicants will be notified latest by March 31st, 2027.") is None
    assert closing("Proposed programme dates: 01/03/2027 to 28/05/2027") is None
    assert closing("Candidates who do not receive any communication from the Foundation by 30th April 2026 must assume they were unsuccessful.") is None


def test_genuine_deadline_wordings_are_accepted():
    assert closing("Application Deadline: 30-09-2026") == "2026-09-30"
    assert closing("Open for applications until 6 October 2026, at 11:00 (UTC)") == "2026-10-06"
    assert closing("The online DAAD application portal is open from September 01 till October 30, 2026.") == "2026-10-30"
    assert closing("Last date to submit the scholarship application form is 30/09/2026.") == "2026-09-30"


def test_llm_claim_with_wrong_kind_of_date_is_rejected_by_grounding():
    text = "Selection. Applicants will be notified latest by March 31st, 2027.\n"
    ex = Extraction("llm", {"closing_date": Claim("closing_date", "2027-03-31", "Applicants will be notified latest by March 31st, 2027.", "llm")})
    g, rej = ground_extraction(ex, Haystack(text), [])
    assert "closing_date" not in g and "different date" in rej[0].reason
