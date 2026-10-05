"""End-to-end (no network, no LLM): run 1 discovers, run 2 detects a deadline change, removal and expiry – history retained."""
import datetime as dt

import pytest

from scholarship_intel import config
from scholarship_intel.db import connect, rows
from scholarship_intel.extraction.llm import LLMRouter
from scholarship_intel.fetcher import Fetcher, FetchResult
from scholarship_intel.pipeline import Pipeline

URL = "https://scholarships.example.gov.in/test-merit-scholarship"
PAGE = """Test Merit Scholarship 2026-27
Ministry of Education
About the scheme
The Test Merit Scholarship supports meritorious undergraduate students.
Eligibility:
Students who have passed Class XII with at least 75% marks are eligible. Family income should not exceed Rs. 6 lakh per annum.
Benefit:
The scholarship amount is Rs. 50,000 per annum.
Documents required:
Income certificate and marksheet.
Selection process:
Selection will be based on merit in the qualifying examination.
Important dates:
Last date to apply is {deadline}.
Apply online through the portal.
"""


class FakeFetcher(Fetcher):
    def __init__(self):
        super().__init__(mode="live")
        self.pages = {}
        self.stats = {"fetched": 0}

    def fetch(self, url):
        spec = self.pages.get(url)
        if spec is None or isinstance(spec, int):
            return FetchResult(url=url, final_url=url, status=spec or 0, error=f"HTTP {spec}", fetched_at=config.now_iso())
        from scholarship_intel.textproc import html_to_doc
        text = spec
        import hashlib
        return FetchResult(url=url, final_url=url, status=200, content_type="text/html", text=text, title=text.splitlines()[0],
                           headings=[text.splitlines()[0]], content_hash=hashlib.sha1(text.encode()).hexdigest()[:20], fetched_at=config.now_iso())

    def probe(self, url):
        return {"status": 200}


@pytest.fixture()
def env(tmp_path):
    config.set_as_of(dt.date(2026, 10, 5))
    conn = connect(tmp_path / "t.db")
    ff = FakeFetcher()
    yield conn, ff
    config.set_as_of(None)


def run(conn, ff, **kw):
    p = Pipeline(conn, ff, LLMRouter(enabled=False), log=lambda *_: None, discover=False, **kw)
    p.run_id = p.repo.start_run("test")
    return p


def test_change_detection_and_lifecycle(env):
    conn, ff = env
    ff.pages[URL] = PAGE.format(deadline="31 December 2026")
    p = run(conn, ff)
    from scholarship_intel.fetcher import FetchResult
    res = ff.fetch(URL)
    assert p._process(res, existing=None, via="test", kscore=0.9) == "new"
    s = rows(conn, "SELECT * FROM scholarships")[0]
    assert s["closing_date"] == "2026-12-31" and s["status"] == "ACTIVE" and s["income_max_inr"] == 600000
    assert s["age_max"] is None                      # never invented
    ev = rows(conn, "SELECT * FROM field_evidence WHERE scholarship_id=? AND field='closing_date' AND is_current=1", (s["id"],))[0]
    page = rows(conn, "SELECT text FROM pages WHERE id=?", (ev["page_id"],))[0]["text"]
    assert page[ev["char_start"]:ev["char_end"]].strip() == ev["quote"]      # DB -> source -> evidence offsets are exact

    # Run 2: official source changes the deadline
    ff.pages[URL] = PAGE.format(deadline="15 January 2027")
    run(conn, ff).run() if False else None
    p2 = Pipeline(conn, ff, LLMRouter(enabled=False), log=lambda *_: None, discover=False)
    p2.run()
    ch = rows(conn, "SELECT * FROM changes WHERE change_type='FIELD_CHANGED' AND field='closing_date'")
    assert len(ch) == 1 and ch[0]["old_value"] == "2026-12-31" and ch[0]["new_value"] == "2027-01-15"
    assert ch[0]["source_url"] == URL and "15 January 2027" in ch[0]["new_evidence"] and "31 December 2026" in ch[0]["old_evidence"]
    assert rows(conn, "SELECT closing_date FROM scholarships")[0]["closing_date"] == "2027-01-15"

    # Run 3: the clock passes the deadline -> EXPIRED, with no re-extraction needed (page unchanged)
    config.set_as_of(dt.date(2027, 2, 1))
    Pipeline(conn, ff, LLMRouter(enabled=False), log=lambda *_: None, discover=False).run()
    assert rows(conn, "SELECT status FROM scholarships")[0]["status"] == "EXPIRED"

    # Run 4: removed from the official source
    ff.pages[URL] = 404
    Pipeline(conn, ff, LLMRouter(enabled=False), log=lambda *_: None, discover=False).run()
    s = rows(conn, "SELECT * FROM scholarships")[0]
    assert s["status"] == "NO_LONGER_VERIFIABLE" and s["closing_date"] == "2027-01-15"      # old value retained, not blanked
    hist = [r["new_status"] for r in rows(conn, "SELECT * FROM status_history ORDER BY id")]
    assert hist == ["ACTIVE", "EXPIRED", "NO_LONGER_VERIFIABLE"]
