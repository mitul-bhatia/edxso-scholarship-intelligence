"""Second-hop corroboration: a deadline stated on a same-domain FAQ page can support the main page – with exact evidence."""
import datetime as dt
import hashlib

import pytest

from scholarship_intel import config
from scholarship_intel.db import connect, rows
from scholarship_intel.extraction.llm import LLMRouter
from scholarship_intel.fetcher import Fetcher, FetchResult
from scholarship_intel.pipeline import Pipeline
from scholarship_intel.textproc import Link

MAIN = "https://scholarships.example.gov.in/merit-award"
FAQ = "https://scholarships.example.gov.in/merit-award-faq"
OTHER = "https://scholarships.example.gov.in/other-notice"

MAIN_TEXT = """Merit Award Scholarship 2026-27
Ministry of Education
Eligibility:
Students who have passed Class XII with at least 75% marks are eligible. Family income should not exceed Rs. 6 lakh per annum.
Benefit:
The Merit Award scholarship amount is Rs. 50,000 per annum.
Documents required:
Income certificate and marksheet.
Selection process:
Selection will be based on merit in the qualifying examination.
"""


class Fake(Fetcher):
    def __init__(self, faq_text):
        super().__init__(mode="live")
        self.faq_text = faq_text
        self.stats = {"fetched": 0}

    def _r(self, url, text, links=()):
        return FetchResult(url=url, final_url=url, status=200, content_type="text/html", text=text, title=text.splitlines()[0],
                           headings=[text.splitlines()[0]], links=list(links), content_hash=hashlib.sha1(text.encode()).hexdigest()[:20],
                           fetched_at=config.now_iso())

    def fetch(self, url):
        if url == MAIN:
            return self._r(url, MAIN_TEXT, [Link(FAQ, "Frequently Asked Questions"), Link(OTHER, "Latest notice")])
        if url == FAQ:
            return self._r(url, self.faq_text)
        return self._r(url, "Unrelated notice about hostel allotment. Last date: 31 December 2026.")

    def probe(self, url):
        return {"status": 200}


def run(tmp_path, faq_text):
    config.set_as_of(dt.date(2026, 10, 5))
    conn = connect(tmp_path / "t.db")
    ff = Fake(faq_text)
    p = Pipeline(conn, ff, LLMRouter(enabled=False), log=lambda *_: None, discover=False)
    p.run_id = p.repo.start_run("test")
    p._process(ff.fetch(MAIN), existing=None, via="test", kscore=0.9)
    return conn


def test_deadline_from_supporting_faq_page_is_traced_to_that_page(tmp_path):
    try:
        conn = run(tmp_path, "Merit Award Scholarship FAQ\nQ. What is the last date? The Merit Award Scholarship last date to apply is 20 November 2026.\n")
        s = rows(conn, "SELECT * FROM scholarships")[0]
        assert s["closing_date"] == "2026-11-20"
        ev = rows(conn, "SELECT * FROM field_evidence WHERE scholarship_id=? AND field='closing_date' AND is_current=1", (s["id"],))[0]
        assert ev["source_url"] == FAQ and ev["page_id"] != s["primary_page_id"]
        text = rows(conn, "SELECT text FROM pages WHERE id=?", (ev["page_id"],))[0]["text"]
        assert text[ev["char_start"]:ev["char_end"]].strip() == ev["quote"]        # exact offsets into the SUPPORTING page
    finally:
        config.set_as_of(None)


def test_old_cycle_date_on_supporting_page_is_not_used(tmp_path):
    try:
        conn = run(tmp_path, "Merit Award Scholarship FAQ\nThe Merit Award Scholarship last date to apply was 20 November 2024.\n")
        assert rows(conn, "SELECT closing_date FROM scholarships")[0]["closing_date"] is None
    finally:
        config.set_as_of(None)


def test_unrelated_page_date_is_not_used(tmp_path):
    try:
        conn = run(tmp_path, "Hostel allotment FAQ\nLast date: 20 November 2026.\n")      # does not mention the scholarship
        assert rows(conn, "SELECT closing_date FROM scholarships")[0]["closing_date"] is None
    finally:
        config.set_as_of(None)
