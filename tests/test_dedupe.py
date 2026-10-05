import datetime as dt

from scholarship_intel.db import connect, rows
from scholarship_intel.storage.repo import Repo


def _ins(conn, key, name, provider, conf, tier="T1"):
    conn.execute("INSERT INTO scholarships(key,name,provider,confidence,source_tier,official_url) VALUES (?,?,?,?,?,?)",
                 (key, name, provider, conf, tier, "https://x/" + key))


def test_same_programme_on_two_official_domains_is_merged(tmp_path):
    conn = connect(tmp_path / "t.db")
    _ins(conn, "a", "AICTE – SAKSHAM SCHOLARSHIP SCHEME FOR SPECIALLY-ABLED STUDENTS", "All India Council for Technical Education", 82.0)
    _ins(conn, "b", "AICTE Saksham Scholarship Scheme for Specially-Abled Students", "AICTE", 90.0)
    _ins(conn, "c", "AICTE – PRAGATI SCHOLARSHIP SCHEME FOR GIRL STUDENTS (DEGREE)", "All India Council for Technical Education", 80.0)
    conn.commit()
    out = Repo(conn).dedupe(1)
    assert out == [(1, 2)]                                   # lower-confidence copy retired in favour of the better one
    assert {r["key"] for r in rows(conn, "SELECT key FROM scholarships")} == {"b", "c"}
    assert "duplicate of #2" in rows(conn, "SELECT reason FROM rejected_extractions")[0]["reason"]


def test_different_programmes_with_similar_names_are_kept(tmp_path):
    conn = connect(tmp_path / "t.db")
    _ins(conn, "a", "Pragati Scholarship (Degree)", "AICTE", 80.0, "T3")
    _ins(conn, "b", "Pragati Scholarship (Diploma)", "AICTE", 80.0, "T3")
    conn.commit()
    assert Repo(conn).dedupe(1) == []
