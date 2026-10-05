"""Change-detection / stale-data demonstration.

Real official pages rarely change within the minutes between two crawls, so this builds a SEPARATE demo database:
  1. copy the real database (run #1 data is real),
  2. delete a few records (they will be *rediscovered* by the real discovery code = "newly discovered"),
  3. replay the crawl from the on-disk page cache with a handful of clearly-labelled source edits injected at the
     fetch layer (a deadline text changed, an amount changed, a deadline moved into the past, a page turned into 404),
  4. run the normal pipeline – extraction, grounding, scoring, diffing and lifecycle are all the real code.

Every record touched by an edit is flagged `simulated=1` and the dashboard shows a SIMULATED badge. The real database
is never modified.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

from . import config
from .db import connect, rows
from .extraction import parsers as P
from .extraction.llm import LLMRouter
from .fetcher import Fetcher, Overlay
from .pipeline import Pipeline
from .util import canonical_url

_ORDINAL = re.compile(r"(st|nd|rd|th)\b")


def _fmt_like(original: str, new: dt.date) -> str:
    """Render `new` in the same style as the original date text (so the page still reads naturally)."""
    if re.match(r"\d{4}-\d{2}-\d{2}", original):
        return new.isoformat()
    if re.match(r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}", original):
        sep = re.search(r"[/.\-]", original).group(0)
        return f"{new.day:02d}{sep}{new.month:02d}{sep}{new.year}"
    if re.match(r"[A-Za-z]", original):
        return new.strftime("%B %d, %Y")
    ordinal = ""
    if _ORDINAL.search(original):
        ordinal = "th" if 11 <= new.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(new.day % 10, "th")
    return f"{new.day}{ordinal} {new.strftime('%B %Y')}"


def _page_text(conn: sqlite3.Connection, c: dict) -> str:
    r = conn.execute("SELECT text FROM pages WHERE id=?", (c["primary_page_id"],)).fetchone()
    return r["text"] if r else ""


def _effective_date_edit(conn: sqlite3.Connection, c: dict, orig: str, new_txt: str, want_iso: str) -> bool:
    """True only if replaying the edit on the stored page really makes the rule extractor report the new date."""
    from .extraction import rules
    text = re.sub(re.escape(orig), new_txt, _page_text(conn, c), count=1)
    if not text:
        return False
    ex = rules.extract(text, "", [], [], c["official_url"])
    got = ex.claims.get("closing_date")
    return bool(got and got.value == want_iso)


class _Plan:
    """Collects the labelled source edits that will be injected at the fetch layer."""

    def __init__(self) -> None:
        self.overlays: dict[str, Overlay] = {}
        self.items: list[dict] = []
        self.used: set[int] = set()

    def add(self, c: dict, overlay: Overlay | None, kind: str, edit: str) -> None:
        if overlay is not None:
            self.overlays[canonical_url(c["official_url"])] = overlay
        self.items.append({"scholarship": c["name"], "kind": kind, "edit": edit, "url": c["official_url"]})
        self.used.add(c["id"])

    def shift_deadline(self, c: dict, new_date: dt.date, kind: str, conn: sqlite3.Connection | None = None) -> bool:
        want = dt.date.fromisoformat(c["closing_date"])
        hit = next((h for h in P.find_dates(c["cq"]) if h.date == want), None)
        if not hit:
            return False
        orig = c["cq"][hit.start:hit.end]
        new_txt = _fmt_like(orig, new_date)
        if conn is not None and not _effective_date_edit(conn, c, orig, new_txt, new_date.isoformat()):
            return False                      # e.g. the page carries a later date that would still win
        self.add(c, Overlay(replacements=[(re.escape(orig), new_txt)], note=kind), kind, f"“{orig}” → “{new_txt}”")
        return True

    def bump_amount(self, c: dict) -> bool:
        money = P.find_money(c["aq"])
        digits = re.search(r"[\d,]+(?:\.\d+)?", c["aq"][money[0].start:money[0].end]) if money else None
        if not digits:
            return False
        orig = c["aq"][money[0].start:money[0].end]
        raw = float(digits.group(0).replace(",", ""))
        if raw >= 1000:
            bumped = int(round(raw * 1.2, -2))
        elif raw == int(raw):
            bumped = max(int(raw) + 1, int(round(raw * 1.2)))      # small figures (e.g. "2 Lakhs") must still visibly change
        else:
            bumped = round(raw * 1.2, 1)
        new_orig = orig.replace(digits.group(0), f"{bumped:,}" if "," in digits.group(0) else str(bumped), 1)
        if new_orig == orig:
            return False
        self.add(c, Overlay(replacements=[(re.escape(orig), new_orig)], note="amount revised"), "benefit amount revised (+20%)",
                 f"“{orig}” → “{new_orig}”")
        return True


def _candidates(conn: sqlite3.Connection) -> list[dict]:
    return rows(conn, """SELECT s.*,
        (SELECT quote FROM field_evidence e WHERE e.scholarship_id=s.id AND e.field='closing_date' AND e.is_current=1 LIMIT 1) cq,
        (SELECT quote FROM field_evidence e WHERE e.scholarship_id=s.id AND e.field='amount' AND e.is_current=1 LIMIT 1) aq
        FROM scholarships s WHERE s.status != 'NO_LONGER_VERIFIABLE' ORDER BY s.confidence DESC, s.id""")


def _build_plan(conn: sqlite3.Connection) -> tuple[_Plan, list[dict]]:
    cands = _candidates(conn)
    today = config.today()
    plan = _Plan()
    with_deadline = [c for c in cands if c["closing_date"] and c["cq"]]

    shifted = 0
    for c in with_deadline:
        if shifted >= 2:
            break
        base = dt.date.fromisoformat(c["closing_date"])
        if plan.shift_deadline(c, base + dt.timedelta(days=21), "deadline extended by 21 days", conn):
            shifted += 1
    for c in with_deadline:                 # an OPEN scholarship whose deadline is brought into the past => ACTIVE -> EXPIRED
        if c["id"] in plan.used or c["status"] not in ("ACTIVE", "EXPIRING_SOON"):
            continue
        if plan.shift_deadline(c, today - dt.timedelta(days=9), "deadline moved into the past (expiry)", conn):
            break
    for c in cands:
        if c["id"] not in plan.used and c["amount_max"] and c["aq"] and plan.bump_amount(c):
            break
    for c in [x for x in cands if x["id"] not in plan.used][-2:]:
        plan.add(c, Overlay(status=404, note="page removed"), "page removed from official source (HTTP 404)", "HTTP 404")

    rediscover = [c for c in cands if c["id"] not in plan.used and (c["discovered_via"] or "").startswith(("link", "hub"))][:2]
    return plan, rediscover


def run_demo(args) -> int:
    src = Path(args.source_db) if args.source_db else config.db_path()
    out = Path(args.out)
    if not src.exists():
        raise SystemExit(f"source DB {src} not found – run a real crawl first (python -m scholarship_intel run)")
    out.parent.mkdir(parents=True, exist_ok=True)
    for p in (out, Path(str(out) + "-wal"), Path(str(out) + "-shm")):
        p.unlink(missing_ok=True)
    s_conn, d_raw = sqlite3.connect(src), sqlite3.connect(out)
    s_conn.backup(d_raw)
    s_conn.close()
    d_raw.close()
    conn = connect(out)

    plan, rediscover = _build_plan(conn)
    for c in rediscover:                                  # forgotten here, re-found by real discovery => NEW
        conn.execute("DELETE FROM scholarships WHERE id=?", (c["id"],))
        plan.items.append({"scholarship": c["name"], "kind": "removed from DB copy to be re-found by discovery (NEW)", "edit": "-",
                           "url": c["official_url"]})
    notice = (f"DEMO DATABASE – a copy of the real crawl with {len(plan.items) - len(rediscover)} clearly labelled SIMULATED source edits "
              f"(badge: SIMULATED) replayed through the real pipeline, plus {len(rediscover)} records re-discovered as NEW. "
              "The real database (atlas.db) contains no simulated values.")
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('demo_notice',?)", (notice,))
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('demo_plan',?)", (json.dumps(plan.items),))
    conn.commit()

    print(f"demo DB: {out}\nplanned source edits:")
    for p in plan.items:
        print(f"  - {p['kind']:55} {p['scholarship'][:55]}  {p['edit']}")

    router = LLMRouter(enabled=False) if args.llm == "off" else LLMRouter(chain=None if args.llm == "auto" else args.llm.split(","))
    last = rows(conn, "SELECT stats_json FROM crawl_runs WHERE stats_json IS NOT NULL ORDER BY id DESC LIMIT 1")
    max_pages = None
    if last:
        try:
            max_pages = json.loads(last[0]["stats_json"]).get("discovery_fetched")
        except (TypeError, ValueError):
            max_pages = None
    pipe = Pipeline(conn, Fetcher(mode="cache", overlays=plan.overlays), router, max_pages=max_pages, skip_search=True, mode="replay")
    pipe.run(label="demo run 2 (cache replay + labelled source edits)")
    return 0
