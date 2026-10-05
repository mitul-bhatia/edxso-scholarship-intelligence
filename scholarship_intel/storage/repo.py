"""Persistence + change detection. History is append-only: values are never silently overwritten."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field

from rapidfuzz import fuzz

from .. import config
from ..discovery.classifier import SourceClass
from ..extraction import vocab as V
from ..extraction.models import Grounded
from ..fetcher import FetchResult
from ..util import canonical_url, fold, name_key, registered_domain, sha1
from ..verification.lifecycle import Status
from ..verification.scoring import ScoreResult

TRACKED_SCALARS = ["name", "provider", "application_url", "amount_min", "amount_max", "amount_currency", "amount_period",
                   "income_max_inr", "age_min", "age_max", "gender", "opening_date", "closing_date"]
TRACKED_TEXT = ["benefit_text", "eligibility_text", "academic_requirements", "courses", "institution_requirements",
                "documents_required", "selection_process", "renewal_requirements", "deadline_note", "domicile"]
TRACKED_JSON = ["categories", "education_levels"]
CRITICAL_CHANGE_FIELDS = {"closing_date", "opening_date", "amount_max", "amount_min", "income_max_inr", "age_max", "age_min",
                          "gender", "application_url", "categories", "education_levels"}


@dataclass
class Record:
    key: str
    name: str
    provider: str | None
    source: SourceClass
    official_url: str
    primary_page_id: int
    discovered_via: str
    facts: dict[str, Grounded]
    absent: dict[str, tuple[bool, str]]
    agreement: dict[str, float]
    conflicts: list[dict]
    n_extractors: int
    score: ScoreResult
    status: Status
    content_hash: str
    simulated: bool = False
    official_verified: bool = False
    extractors: list[str] = field(default_factory=list)


def make_key(name: str, official_url: str) -> str:
    return sha1(f"{name_key(name)}|{registered_domain(canonical_url(official_url).split('/')[2] if '//' in official_url else official_url)}")[:16]


def flatten(facts: dict[str, Grounded]) -> dict:
    """Grounded facts -> canonical column values. Absent => None ('Not specified')."""
    def v(f):
        return facts[f].value if f in facts else None
    out = {k: v(k) for k in ("name", "provider", "application_url", "gender", "opening_date", "closing_date")}
    for f in TRACKED_TEXT:
        out[f] = v(f)
    am = v("amount")
    out.update(amount_min=am.get("min") if am else None, amount_max=am.get("max") if am else None,
               amount_currency=am.get("currency") if am else None, amount_period=am.get("period") if am else None,
               amount_text=facts["amount"].quote if "amount" in facts else None)
    inc = v("income")
    out.update(income_max_inr=inc.get("max_inr") if inc else None, income_limit_text=facts["income"].quote if "income" in facts else None)
    ag = v("age")
    out.update(age_min=ag.get("min") if ag else None, age_max=ag.get("max") if ag else None,
               age_text=facts["age"].quote if "age" in facts else None)
    out["categories"] = json.dumps(v("categories")) if "categories" in facts else None
    out["education_levels"] = json.dumps(v("education_levels")) if "education_levels" in facts else None
    return out


def _differs(field_name: str, old, new) -> bool:
    if old is None and new is None:
        return False
    if (old is None) != (new is None):
        return True
    if field_name in ("name", "provider"):
        return fuzz.ratio(fold(str(old)), fold(str(new))) < 88
    if field_name in TRACKED_TEXT:
        return fuzz.ratio(fold(str(old)), fold(str(new))) < 96
    if field_name == "application_url":
        return canonical_url(str(old)) != canonical_url(str(new))
    if isinstance(old, float) or isinstance(new, float):
        try:
            return abs(float(old) - float(new)) > 1e-6
        except (TypeError, ValueError):
            return str(old) != str(new)
    if field_name in TRACKED_JSON:
        return sorted(json.loads(old)) != sorted(json.loads(new))
    return str(old) != str(new)


def serialise_facts(rec: Record) -> str:
    return json.dumps({
        "facts": {k: asdict(g) for k, g in rec.facts.items()},
        "absent": {k: list(v) for k, v in rec.absent.items()},
        "agreement": rec.agreement, "conflicts": rec.conflicts,
        "n_extractors": rec.n_extractors, "extractors": rec.extractors,
    }, default=str)


def load_facts(blob: str) -> tuple[dict[str, Grounded], dict, dict, list, int, list]:
    d = json.loads(blob)
    facts = {k: Grounded(**v) for k, v in d["facts"].items()}
    absent = {k: (bool(v[0]), v[1]) for k, v in d["absent"].items()}
    return facts, absent, d["agreement"], d["conflicts"], d["n_extractors"], d.get("extractors", [])


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------ runs / pages / sources
    def start_run(self, mode: str, label: str = "", notes: str = "") -> int:
        cur = self.conn.execute("INSERT INTO crawl_runs(started_at, mode, as_of, label, notes) VALUES (?,?,?,?,?)",
                                (config.now_iso(), mode, config.today().isoformat(), label, notes))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, stats: dict) -> None:
        self.conn.execute("UPDATE crawl_runs SET finished_at=?, stats_json=? WHERE id=?", (config.now_iso(), json.dumps(stats), run_id))
        self.conn.commit()

    def save_page(self, run_id: int, r: FetchResult, kind: str = "", kind_score: float = 0.0) -> int:
        from ..util import host_of
        cur = self.conn.execute(
            """INSERT INTO pages(run_id,url,final_url,domain,status_code,content_type,fetched_at,content_hash,title,text,text_len,
               links_json,error,tls_verified,last_modified,needs_ocr,simulated,page_kind,kind_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, r.url, r.final_url, host_of(r.final_url or r.url), r.status, r.content_type, r.fetched_at, r.content_hash, r.title,
             r.text, len(r.text), json.dumps([{"u": l.url, "t": l.text} for l in r.links[:300]]), r.error, int(r.tls_verified),
             r.last_modified, int(r.needs_ocr), int(r.simulated), kind, kind_score))
        self.conn.commit()
        return cur.lastrowid

    def upsert_source(self, sc: SourceClass, run_id: int) -> None:
        self.conn.execute(
            """INSERT INTO sources(domain,source_type,tier,authority,reason,provider_name,first_seen_run) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(domain) DO UPDATE SET source_type=excluded.source_type, tier=excluded.tier, authority=excluded.authority,
               reason=excluded.reason, provider_name=COALESCE(excluded.provider_name, sources.provider_name)""",
            (sc.domain, sc.source_type, sc.tier, sc.authority, sc.reason, sc.provider_name, run_id))

    def log_rejected(self, run_id: int, key: str, name: str, page_id: int, rejected) -> None:
        for r in rejected:
            self.conn.execute(
                "INSERT INTO rejected_extractions(run_id,scholarship_key,scholarship_name,page_id,field,proposed_value,proposed_quote,reason,extractor,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, key, name, page_id, r.field, json.dumps(r.proposed_value, default=str)[:600], (r.proposed_quote or "")[:600],
                 r.reason, r.extractor, config.now_iso()))

    def log_lead(self, run_id: int, name: str, url: str, domain: str, reason: str) -> None:
        self.conn.execute("INSERT INTO unresolved_leads(run_id,name,lead_url,lead_domain,reason,created_at) VALUES (?,?,?,?,?,?)",
                          (run_id, name, url, domain, reason, config.now_iso()))

    # ------------------------------------------------------------------ lookup
    def find_existing(self, official_url: str, key: str, name: str, domain: str) -> sqlite3.Row | None:
        cu = canonical_url(official_url)
        for row in self.conn.execute("SELECT * FROM scholarships WHERE official_url=?", (official_url,)):
            return row
        for row in self.conn.execute("SELECT * FROM scholarships"):
            if canonical_url(row["official_url"] or "") == cu:
                return row
        row = self.conn.execute("SELECT * FROM scholarships WHERE key=?", (key,)).fetchone()
        if row:
            return row
        nk = name_key(name)
        for row in self.conn.execute("SELECT * FROM scholarships WHERE official_domain=?", (domain,)):
            if fuzz.ratio(name_key(row["name"] or ""), nk) >= 92:
                return row
        return None

    def current_evidence(self, sid: int) -> dict[str, dict]:
        out = {}
        for r in self.conn.execute("SELECT * FROM field_evidence WHERE scholarship_id=? AND is_current=1 AND evidence_kind='QUOTE'", (sid,)):
            out[r["field"]] = dict(r)
        return out

    # ------------------------------------------------------------------ save + diff
    def save(self, run_id: int, rec: Record) -> tuple[int, list[dict]]:
        now = config.now_iso()
        cols = flatten(rec.facts)
        existing = self.find_existing(rec.official_url, rec.key, rec.name, rec.source.domain)
        changes: list[dict] = []
        simulated = int(rec.simulated)
        base = dict(
            name=rec.name, provider=rec.provider, source_type=rec.source.source_type, source_tier=rec.source.tier,
            official_url=rec.official_url, official_domain=rec.source.domain, status=rec.status.status, status_reason=rec.status.reason,
            verification_label=rec.score.label, confidence=rec.score.display, official_source_verified=int(rec.official_verified),
            miss_count=rec.status.miss_count, primary_page_id=rec.primary_page_id, last_run_id=run_id, last_seen_at=now,
            last_verified_at=now, content_hash=rec.content_hash, facts_json=serialise_facts(rec),
        )
        base.update(cols)
        base["name"] = rec.name

        if existing is None:
            base.update(key=rec.key, discovered_via=rec.discovered_via, first_run_id=run_id, first_discovered_at=now, last_changed_at=now)
            names = ",".join(base.keys())
            cur = self.conn.execute(f"INSERT INTO scholarships({names}) VALUES ({','.join('?' * len(base))})", tuple(base.values()))
            sid = cur.lastrowid
            self._change(sid, run_id, "*", "NEW", None, rec.name, rec.official_url, None, None, simulated, "newly discovered scholarship")
            changes.append({"field": "*", "type": "NEW"})
            self.conn.execute("INSERT INTO status_history(scholarship_id,run_id,old_status,new_status,reason,at) VALUES (?,?,?,?,?,?)",
                              (sid, run_id, None, rec.status.status, rec.status.reason, now))
        else:
            sid = existing["id"]
            old_ev = self.current_evidence(sid)
            old_page = self.conn.execute("SELECT content_hash FROM pages WHERE id=?", (existing["primary_page_id"],)).fetchone()
            same_source_text = bool(old_page and old_page["content_hash"] == rec.content_hash)
            changed_any = False
            for f in TRACKED_SCALARS + TRACKED_TEXT + TRACKED_JSON:
                old, new = existing[f], cols.get(f)
                if not _differs(f, old, new):
                    continue
                ev_field = {"amount_min": "amount", "amount_max": "amount", "amount_currency": "amount", "amount_period": "amount",
                            "income_max_inr": "income", "age_min": "age", "age_max": "age"}.get(f, f)
                new_q = rec.facts[ev_field].quote if ev_field in rec.facts else None
                old_q = old_ev.get(ev_field, {}).get("quote")
                src = rec.facts[ev_field].extractor if ev_field in rec.facts else None
                if old is None:
                    ctype, note = "FIELD_ADDED", "field now stated by the official source"
                elif new is None:
                    ctype, note = "FIELD_UNSUPPORTED", "previously extracted value is no longer supported by the official source on this crawl"
                else:
                    ctype, note = "FIELD_CHANGED", "CHANGE DETECTED"
                if same_source_text:
                    note = "EXTRACTION_CORRECTION: official page content is unchanged; extractor output or validation changed"
                self._change(sid, run_id, f, ctype, old, new, rec.official_url, old_q, new_q, simulated, note)
                changes.append({"field": f, "type": ctype, "old": old, "new": new})
                changed_any = True
            if existing["status"] != rec.status.status:
                self._change(sid, run_id, "status", "STATUS_CHANGED", existing["status"], rec.status.status, rec.official_url, None,
                             rec.status.evidence_quote, simulated, rec.status.reason)
                self.conn.execute("INSERT INTO status_history(scholarship_id,run_id,old_status,new_status,reason,at) VALUES (?,?,?,?,?,?)",
                                  (sid, run_id, existing["status"], rec.status.status, rec.status.reason, now))
                changes.append({"field": "status", "type": "STATUS_CHANGED", "old": existing["status"], "new": rec.status.status})
                changed_any = True
            if changed_any:
                base["last_changed_at"] = now
            sets = ",".join(f"{k}=?" for k in base)
            self.conn.execute(f"UPDATE scholarships SET {sets} WHERE id=?", (*base.values(), sid))

        self._write_evidence(sid, run_id, rec, now)
        self._write_breakdown(sid, run_id, rec, now)
        self.conn.commit()
        return sid, changes

    def _change(self, sid, run_id, field, ctype, old, new, url, old_ev, new_ev, simulated, note) -> None:
        self.conn.execute(
            """INSERT INTO changes(scholarship_id,run_id,field,change_type,old_value,new_value,detected_at,source_url,old_evidence,new_evidence,simulated,note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, run_id, field, ctype, None if old is None else str(old), None if new is None else str(new), config.now_iso(), url,
             old_ev, new_ev, simulated, note))

    def _write_evidence(self, sid: int, run_id: int, rec: Record, now: str) -> None:
        self.conn.execute("UPDATE field_evidence SET is_current=0 WHERE scholarship_id=?", (sid,))
        for f, g in rec.facts.items():
            self.conn.execute(
                """INSERT INTO field_evidence(scholarship_id,run_id,field,value,quote,page_id,source_url,char_start,char_end,match_score,
                   extractor,evidence_kind,verified_at,is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (sid, run_id, f, json.dumps(g.value, default=str) if not isinstance(g.value, str) else g.value, g.quote, rec.primary_page_id,
                 rec.official_url, g.start, g.end, g.match_score,
                 ",".join([g.extractor] + g.agreed_by), "QUOTE", now))
        for f, (verified, note) in rec.absent.items():
            if f in rec.facts:
                continue
            self.conn.execute(
                """INSERT INTO field_evidence(scholarship_id,run_id,field,value,quote,page_id,source_url,match_score,extractor,evidence_kind,verified_at,is_current)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
                (sid, run_id, f, None, note, rec.primary_page_id, rec.official_url, 100.0 if verified else 0.0,
                 ",".join(rec.extractors), "ABSENT" if verified else "ABSENT_UNVERIFIED", now))

    def _write_breakdown(self, sid: int, run_id: int, rec: Record, now: str) -> None:
        for c in rec.score.components:
            self.conn.execute(
                "INSERT INTO confidence_breakdown(scholarship_id,run_id,component,weight,score,points,detail,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (sid, run_id, c.name, c.weight, c.score, c.points, c.detail, now))
        extra = [("conflict_penalty", -rec.score.penalty, "unresolved extractor conflicts on critical fields")] if rec.score.penalty else []
        for lbl in rec.score.caps:
            extra.append(("cap", 0, lbl))
        for lbl in rec.score.gates:
            extra.append(("gate", 0, lbl))
        for name, pts, det in extra:
            self.conn.execute(
                "INSERT INTO confidence_breakdown(scholarship_id,run_id,component,weight,score,points,detail,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (sid, run_id, name, 0, 0, pts, det, now))

    # ------------------------------------------------------------------ not-seen bookkeeping
    def mark_missing(self, sid: int, run_id: int, status: Status, source_url: str, simulated: bool = False) -> None:
        """Official URL failed / scholarship vanished: update lifecycle without touching previously verified values."""
        row = self.conn.execute("SELECT status FROM scholarships WHERE id=?", (sid,)).fetchone()
        now = config.now_iso()
        label = "REVIEW_REQUIRED"
        self.conn.execute(
            "UPDATE scholarships SET status=?, status_reason=?, miss_count=?, verification_label=?, last_run_id=?, last_changed_at=CASE WHEN status!=? THEN ? ELSE last_changed_at END WHERE id=?",
            (status.status, status.reason, status.miss_count, label, run_id, status.status, now, sid))
        if row["status"] != status.status:
            self._change(sid, run_id, "status", "STATUS_CHANGED", row["status"], status.status, source_url, None, status.evidence_quote,
                         int(simulated), status.reason)
            self.conn.execute("INSERT INTO status_history(scholarship_id,run_id,old_status,new_status,reason,at) VALUES (?,?,?,?,?,?)",
                              (sid, run_id, row["status"], status.status, status.reason, now))
        self.conn.commit()
