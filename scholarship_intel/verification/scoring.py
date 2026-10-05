"""Confidence engine – a *computed* score, never an LLM-generated number.

Nine auditable components (weights in config/settings.yaml, sum = 100) produce a base score; then a conflict
penalty and hard caps/gates are applied. VERIFIED <=> final score >= 95 (and therefore no cap/gate fired).
Every component stores a human-readable `detail` so the dashboard can answer "Why this score?".
See docs/CONFIDENCE.md for the full rationale.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field

from .. import config
from ..discovery.classifier import SourceClass
from ..extraction import vocab as V
from ..extraction.models import Grounded
from .grounding import CRITICAL_FIELDS

ABSENT_VERIFIED_MULTI = 0.95     # >=2 independent extractors found nothing and lexical scan found no hint
ABSENT_VERIFIED_SINGLE = 0.80
ABSENT_UNVERIFIED = 0.30         # document hints the field exists but nothing could be grounded

ELIG_WEIGHTS = {"eligibility_text": 3.0, "education_levels": 2.0, "income": 1.5, "categories": 1.5, "age": 1.0, "gender": 1.0,
                "domicile": 1.0, "academic_requirements": 1.0, "institution_requirements": 1.0}


@dataclass
class ScoreInput:
    source: SourceClass
    fetch_ok: bool
    fetch_status: int
    needs_ocr: bool
    name_location: str                         # title | body | missing
    page_kind: float
    facts: dict[str, Grounded]
    absent: dict[str, tuple[bool, str]]        # field -> (absence verified?, note)
    n_extractors: int
    agreement: dict[str, float]
    conflicts: list[dict]
    apply_check: dict | None
    page_text: str
    today: dt.date
    closing_date: dt.date | None = None
    tls_verified: bool = True


@dataclass
class Component:
    name: str
    weight: float
    score: float
    detail: str

    @property
    def points(self) -> float:
        return round(self.weight * self.score, 3)


@dataclass
class ScoreResult:
    total: float
    label: str
    components: list[Component]
    caps: list[str] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    penalty: float = 0.0
    base: float = 0.0

    @property
    def display(self) -> float:
        return math.floor(self.total * 10) / 10


def _academic_year_start(today: dt.date) -> int:
    return today.year if today.month >= 6 else today.year - 1


def cycle_status(text: str, today: dt.date) -> tuple[str, str]:
    ay = _academic_year_start(today)
    years = set()
    for m in re.finditer(r"\b(20\d{2})\s*[-–/]\s*(?:20)?(\d{2})\b", text):
        y1, y2 = int(m.group(1)), int(m.group(2))
        if (y1 + 1) % 100 == y2:
            years.add(y1)
    if not years:
        for m in re.finditer(r"(?:academic (?:year|session)|session|batch|for the year|intake|AY)\s*:?\s*(20\d{2})", text, re.I):
            years.add(int(m.group(1)))
    if not years:
        return "unknown", "no academic-year wording on the page"
    if max(years) >= ay:
        return "current", f"page refers to cycle {max(years)}-{str(max(years) + 1)[2:]} (current academic year starts {ay})"
    return "stale", f"newest cycle mentioned is {max(years)}-{str(max(years) + 1)[2:]}, older than current academic year {ay}-{str(ay + 1)[2:]}"


def _support(field_name: str, inp: ScoreInput) -> tuple[float, str]:
    """Support level 0..1 for one field: grounded (adjusted for agreement) or verified/unverified absence."""
    g = inp.facts.get(field_name)
    if g is not None:
        ag = inp.agreement.get(field_name)
        if field_name in V.TEXT_FIELDS and ag is not None and ag < 0.999:
            ag = None          # verbatim text: two extractors quoting different valid passages is not a disagreement
        if ag is None:
            return (1.0 if inp.n_extractors <= 1 else 0.92), "grounded in quote (single extractor addressed it)"
        if ag >= 0.999:
            return 1.0, f"grounded; confirmed by {1 + len(g.agreed_by)} extractors"
        if ag >= 0.6:
            return 0.8, f"grounded; majority agreement {ag:.2f}"
        return 0.35, f"grounded but extractors disagree (agreement {ag:.2f})"
    verified, note = inp.absent.get(field_name, (True, "not stated"))
    if verified:
        return (ABSENT_VERIFIED_MULTI if inp.n_extractors >= 2 else ABSENT_VERIFIED_SINGLE), f"'Not specified' – verified absent ({note})"
    return ABSENT_UNVERIFIED, f"'Not specified' but unverified – {note}"


def compute(inp: ScoreInput) -> ScoreResult:
    cfg = config.settings()
    W = cfg["weights"]
    vmin = float(cfg["verification"]["verified_min"])
    comps: list[Component] = []

    # 1. Source authority -------------------------------------------------------------------------
    auth = inp.source.authority
    comps.append(Component("source_authority", W["source_authority"], auth,
                           f"{inp.source.domain}: tier {inp.source.tier} ({inp.source.source_type}) – {inp.source.reason}"))

    # 2. Live presence on the official source -------------------------------------------------------
    pres = 0.0
    notes = []
    if inp.fetch_ok and not inp.needs_ocr:
        pres += 0.5
        notes.append(f"fetched live this run (HTTP {inp.fetch_status})")
    else:
        notes.append(f"official page NOT readable this run (HTTP {inp.fetch_status}{', scanned image' if inp.needs_ocr else ''})")
    if inp.name_location == "title":
        pres += 0.3
        notes.append("scholarship name appears in page title/heading")
    elif inp.name_location == "body":
        pres += 0.2
        notes.append("scholarship name appears in page body")
    else:
        notes.append("scholarship name NOT found on the page")
    pres += 0.2 * min(1.0, inp.page_kind / 0.7)
    notes.append(f"page-kind score {inp.page_kind:.2f}")
    comps.append(Component("live_presence", W["live_presence"], min(1.0, pres), "; ".join(notes)))

    # 3. Application URL ----------------------------------------------------------------------------
    a = inp.facts.get("application_url")
    if a is not None:
        s, n = 0.6, [f"application URL found on official page ({a.value})"]
        ck = inp.apply_check or {}
        st = ck.get("status")
        if st is None:
            s += 0.15
            n.append("reachability not checked")
        elif st < 400:
            s += 0.3
            n.append(f"reachable (HTTP {st})")
        elif st in (401, 403, 405, 429, 999):
            s += 0.15
            n.append(f"exists but blocks bots (HTTP {st})")
        else:
            n.append(f"NOT reachable (HTTP {st})")
        if ck.get("trusted"):
            s += 0.1
            n.append("on official / trusted domain")
        comps.append(Component("application_url", W["application_url"], min(1.0, s), "; ".join(n)))
    else:
        s = 0.3 if (inp.source.official and re.search(r"\b(apply|application)\b", inp.page_text, re.I)) else 0.0
        comps.append(Component("application_url", W["application_url"], s,
                               "no application link on the official page" + ("; page itself invites applications" if s else "")))

    # 4. Eligibility support --------------------------------------------------------------------------
    tot_w = sum(ELIG_WEIGHTS.values())
    acc, parts = 0.0, []
    for f, w in ELIG_WEIGHTS.items():
        sc, why = _support(f, inp)
        acc += w * sc
        parts.append(f"{f}={sc:.2f}")
    elig = acc / tot_w
    if "eligibility_text" not in inp.facts and not any(f in inp.facts for f in ELIG_WEIGHTS):
        elig = 0.0
    comps.append(Component("eligibility_support", W["eligibility_support"], elig, "weighted support: " + ", ".join(parts)))

    # 5. Deadline support ---------------------------------------------------------------------------
    if "closing_date" in inp.facts:
        sc, why = _support("closing_date", inp)
        det = f"closing date {inp.facts['closing_date'].value}: {why}"
    elif "deadline_note" in inp.facts:
        kind = V.states_open_window(inp.facts["deadline_note"].quote)
        if kind == "rolling":
            sc, det = 0.9, "no fixed date, but a rolling/year-round application statement is quoted"
        else:
            sc, det = 0.7, "page states applications are currently open, but publishes no closing date"
    else:
        verified, note = inp.absent.get("closing_date", (True, ""))
        sc = 0.45 if verified else 0.1
        det = "no closing date or open-window statement on the official page – cannot confirm the scholarship is open"
    comps.append(Component("deadline_support", W["deadline_support"], sc, det))

    # 6. Benefit support ------------------------------------------------------------------------------
    if "amount" in inp.facts:
        sc, why = _support("amount", inp)
        det = f"amount grounded: {why}"
    elif "benefit_text" in inp.facts:
        sc, det = 0.9, "benefit stated in words and quoted from the source (no numeric amount to ground)"
    else:
        verified, note = inp.absent.get("amount", (True, ""))
        sc = 0.3 if verified else 0.1
        det = "benefit/amount not stated on the official page"
    comps.append(Component("benefit_support", W["benefit_support"], sc, det))

    # 7. Freshness -----------------------------------------------------------------------------------
    cyc, cyc_note = cycle_status(inp.page_text, inp.today)
    passed = inp.closing_date is not None and inp.closing_date < inp.today
    cyc_score = {"current": 1.0, "unknown": 0.6, "stale": 0.3}[cyc]
    if inp.closing_date and not passed and "closing_date" in inp.facts:
        cyc_score, cyc_note = 1.0, f"closing date {inp.closing_date} is in the future, evidencing the current cycle"
    dl_ok = 0.0 if passed else 1.0
    fresh = 0.4 * (1.0 if inp.fetch_ok else 0.0) + 0.4 * cyc_score + 0.2 * dl_ok
    fnote = f"fetched this run={inp.fetch_ok}; {cyc_note}; deadline {'PASSED' if passed else 'not passed / none'}"
    if passed:
        fresh = min(fresh, 0.3)
    comps.append(Component("freshness", W["freshness"], fresh, fnote))

    # 8. Extractor agreement ---------------------------------------------------------------------------
    if inp.n_extractors <= 1:
        ag_score, ag_note = 0.4, "only one extractor ran – values cannot be cross-checked"
    else:
        compared = {f: v for f, v in inp.agreement.items() if f in CRITICAL_FIELDS | {"name", "provider"}}
        key_grounded = [f for f in inp.facts if f in CRITICAL_FIELDS | {"name", "provider"}]
        if not compared:
            ag_score, ag_note = 0.5, f"{inp.n_extractors} extractors ran but none overlapped on a key field"
        else:
            A = sum(compared.values()) / len(compared)
            cov = len(compared) / max(1, len(key_grounded))
            ag_score = 0.75 * A + 0.25 * min(1.0, cov)
            ag_note = (f"{inp.n_extractors} independent extractors; agreement on {len(compared)} key fields = {A:.2f}; "
                       f"coverage {cov:.2f} (" + ", ".join(f"{f}:{v:.2f}" for f, v in sorted(compared.items())) + ")")
    comps.append(Component("extractor_agreement", W["extractor_agreement"], ag_score, ag_note))

    # 9. Traceability --------------------------------------------------------------------------------
    important = ["name", "provider", "application_url", "amount", "benefit_text", "eligibility_text", "closing_date",
                 "deadline_note", "education_levels", "selection_process", "documents_required"]
    got = [inp.facts[f] for f in important if f in inp.facts]
    if got:
        q = []
        for g in got:
            if g.start is None:
                q.append(0.8)
            elif g.match_score >= 100:
                q.append(1.0)
            else:
                q.append(0.85)
        tr = (sum(q) / len(q)) * min(1.0, len(got) / 6)
        trn = f"{len(got)} important fields carry a stored quote with source offsets (mean quality {sum(q) / len(q):.2f}; need ≥6 for full credit)"
    else:
        tr, trn = 0.0, "no important field could be traced to evidence"
    comps.append(Component("traceability", W["traceability"], tr, trn))

    base = sum(c.points for c in comps)
    total = base

    # conflicts ------------------------------------------------------------------------------------------
    crit_conf = [c for c in inp.conflicts if c["field"] in CRITICAL_FIELDS]
    penalty = min(12.0, 3.0 * len(crit_conf))
    total -= penalty

    # hard caps & gates ------------------------------------------------------------------------------------
    caps: list[str] = []
    gates: list[str] = []

    def cap(limit: float, why: str, bucket: list[str]) -> None:
        nonlocal total
        if total > limit:
            total = limit
        bucket.append(f"{why} (cap {limit})")

    if not inp.source.official:
        cap(59.0, f"source is not official (tier {inp.source.tier}); aggregator/unknown domains can never verify", caps)
    if inp.name_location == "missing":
        cap(40.0, "scholarship name not found on the official page", caps)
    if not inp.fetch_ok:
        cap(50.0, "official page could not be fetched this run", caps)
    if inp.needs_ocr:
        cap(50.0, "document is a scanned image; text cannot be read or verified", caps)
    if inp.n_extractors < 2:
        cap(90.0, "fewer than two independent extractors – VERIFIED requires cross-checking", gates)
    critical_missing = []
    for label, ok in (("name", "name" in inp.facts), ("provider", "provider" in inp.facts),
                      ("benefit (amount or description)", "amount" in inp.facts or "benefit_text" in inp.facts),
                      ("eligibility text", "eligibility_text" in inp.facts),
                      ("closing date or open-window statement", "closing_date" in inp.facts or "deadline_note" in inp.facts),
                      ("education level", "education_levels" in inp.facts)):
        if not ok:
            critical_missing.append(label)
    if critical_missing:
        cap(94.0, "critical field(s) without grounded evidence: " + ", ".join(critical_missing), gates)
    if crit_conf:
        gates.append(f"{len(crit_conf)} unresolved extractor conflict(s) on: " + ", ".join(sorted({c['field'] for c in crit_conf})))
        if total >= vmin:
            total = vmin - 0.5

    total = max(0.0, min(100.0, total))
    label = "VERIFIED" if total >= vmin else "REVIEW_REQUIRED"
    return ScoreResult(total=round(total, 3), label=label, components=comps, caps=caps, gates=gates, penalty=penalty, base=round(base, 3))
