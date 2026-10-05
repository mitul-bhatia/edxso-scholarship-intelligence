"""Deterministic rule-based extractor.

Every claim it makes is an exact slice of the page text, so it is grounded by construction; it exists
(1) as a no-LLM fallback and (2) as an *independent* second opinion for the cross-extractor agreement check.
"""
from __future__ import annotations

import re

from ..textproc import Link
from ..util import canonical_url, host_of, registered_domain
from . import parsers as P
from . import vocab as V
from .models import Claim, Extraction

NAME = "rules"

_SENT_SPLIT = re.compile(r"(?<=[.;!?])\s+(?=[A-Z0-9(\"'“])")
_HEADINGS = {
    "eligibility_text": r"(eligibility( criteria| conditions)?|who can apply|who is eligible|eligible (candidates|students)|conditions of eligibility|eligibility and selection)",
    "documents_required": r"(documents? (required|to be (uploaded|submitted|attached|enclosed))|required documents?|list of documents|supporting documents|documents needed|documentation)",
    "selection_process": r"(selection (process|criteria|procedure|of (candidates|students|beneficiaries)|methodology)|mode of selection|how (are|will) (candidates|students) (be )?selected|method of selection|evaluation (process|criteria))",
    "renewal_requirements": r"(renewal( of (the )?(scholarship|fellowship|award))?( criteria| conditions| process| requirements)?|continuation of (the )?(scholarship|award))",
    "benefit_text": r"(scholarship (amount|benefits?|value|details)|benefits?|financial (assistance|support|benefits?)|award details|what (do|will) you get|amount of (the )?(scholarship|award|assistance)|rate of (scholarship|assistance)|quantum of (scholarship|assistance))",
}
_ANY_HEADING = re.compile(
    r"^\W*(\d+(\.\d+)*[.)]?\s*)?(" + "|".join(v for v in _HEADINGS.values()) +
    r"|objectives?|about( the)? (scheme|scholarship|programme|program)|introduction|how to apply|application (process|procedure)|important dates?|"
    r"terms (and|&) conditions|general (conditions|instructions)|scope|overview|contact|faqs?|disbursement|duration|tenure|scheme details|"
    r"payment|selection|apply (online|now)|timeline|key dates|why apply|about us)\b", re.I)


def _lines(text: str):
    pos = 0
    for ln in text.split("\n"):
        yield pos, pos + len(ln), ln
        pos += len(ln) + 1


def _sentences(text: str, lo: int = 0, hi: int | None = None):
    """Yield (start, end, sentence) with offsets into text, optionally restricted to the window [lo, hi)."""
    hi = len(text) if hi is None else hi
    for ls, le, ln in _lines(text):
        if not ln.strip() or le <= lo or ls >= hi:
            continue
        if len(ln) < 260:
            yield ls, le, ln
            continue
        last = 0
        for m in _SENT_SPLIT.finditer(ln):
            yield ls + last, ls + m.start() + 1, ln[last:m.start() + 1]
            last = m.end()
        if last < len(ln):
            yield ls + last, le, ln[last:]


def _looks_like_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 85:
        return False
    if _ANY_HEADING.match(s) and len(s) < 70:
        return True
    if s.endswith(":") and len(s) < 70:
        return True
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 4 and s.isupper() and not s.endswith(".")


def _section(text: str, heading_rx: str, max_chars: int = 1100) -> tuple[str, int, int] | None:
    rx = re.compile(r"^\W*(\d+(\.\d+)*[.)]?\s*)?(" + heading_rx + r")\b[^\n]{0,60}?(?P<sep>[:\-–]\s*(?P<rest>.+))?$", re.I)
    lines = list(_lines(text))
    for i, (ls, le, ln) in enumerate(lines):
        s = ln.strip()
        if len(s) > 220 or not rx.match(s):
            continue
        m = rx.match(s)
        end = le
        taken = 0
        if m.group("rest") is None and len(s) > 75:      # a sentence that merely starts with the word, not a heading
            continue
        for ls2, le2, ln2 in lines[i + 1: i + 40]:
            if _looks_like_heading(ln2) and not re.match(rx, ln2.strip()) and taken > 0:
                break
            if le2 - ls > max_chars:
                break
            end = le2
            taken += 1
        body = text[ls:end].strip()
        if len(body) >= 40:
            return body, ls, ls + len(body)
    return None


def _claim(field: str, value, text: str, start: int, end: int, alt=None) -> Claim:
    return Claim(field, value, text[start:end].strip(), NAME, "QUOTE", alt or [])


# ------------------------------------------------------------------------------------------ fields
def _name(title: str, headings: list[str], text: str) -> Claim | None:
    kw = re.compile(r"(scholarship|fellowship|scheme|stipend|bursary|programme|program|award|grant|yojana|endowment|loan)", re.I)

    def clean(s: str) -> str:
        s = re.sub(r"\s+", " ", s).strip()
        parts = re.split(r"\s+[|•»«]\s+|\s+[-–—]\s+(?=[A-Z])|\s+::\s+", s)
        parts = [p for p in parts if p and len(p) > 3]
        good = [p for p in parts if kw.search(p)]
        s = (good or parts or [s])[0]
        s = re.sub(r"^(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?20\d{2}\s+", "", s, flags=re.I)
        return s.strip(" -–|:")

    cands: list[str] = []
    for h in headings[:6]:
        if kw.search(h) and 6 <= len(h) <= 140:
            cands.append(clean(h))
    if title and kw.search(title):
        cands.append(clean(title))
    # PDFs / pages with no usable headings: first prominent lines
    head_lines = [ln.strip() for ln in text.split("\n")[:30]]
    for i, ln in enumerate(head_lines):
        if kw.search(ln) and 8 <= len(ln) <= 140 and not ln.endswith(".") and (ln.isupper() or ln.istitle() or ln[:1].isupper()):
            joined = ln
            if i + 1 < len(head_lines) and head_lines[i + 1].isupper() and len(head_lines[i + 1]) < 60 and ln.isupper():
                joined = f"{ln}\n{head_lines[i + 1]}"
            cands.append(joined)
            break
    for c in cands:
        c = c.strip()
        if len(c) < 6:
            continue
        idx = text.find(c)
        if idx >= 0:
            return Claim("name", re.sub(r"\s+", " ", c), text[idx: idx + len(c)], NAME)
    return None


_ORG = re.compile(
    r"(Ministry of [A-Z][A-Za-z,&\- ]{3,60}?(?=[.,;\n(]|$)|Department of [A-Z][A-Za-z,&\- ]{3,60}?(?=[.,;\n(]|$)|"
    r"All India Council for Technical Education|University Grants Commission|National Testing Agency|"
    r"(?:[A-Z][A-Za-z.&']+ ){1,5}(?:Foundation|Trust|Commission|Council|Endowment|Society|Bank|Limited|Ltd\.?|Board|University|Institute of [A-Z][a-z]+(?: [A-Z][a-z]+)?)|"
    r"Indian Institute of [A-Z][a-z]+(?: [A-Z][a-z]+)?|Government of [A-Z][a-z]+(?: [A-Z][a-z]+)?)")


def _provider(text: str, title: str) -> Claim | None:
    head = text[:4000]
    for m in _ORG.finditer(head):
        val = re.sub(r"\s+", " ", m.group(0)).strip(" ,.;")
        if 6 <= len(val) <= 90 and not re.search(r"scholarship|scheme|fellowship", val, re.I):
            return Claim("provider", val, head[m.start():m.end()], NAME)
    return None


def _dates(text: str) -> dict[str, Claim]:
    out: dict[str, Claim] = {}
    hits = P.find_dates(text)
    closing, opening = [], []
    for h in hits:
        kind = P.classify_date_context(text, h)
        (closing if kind == "closing" else opening if kind == "opening" else []).append(h)
    # "from X to Y" ranges
    for i, h in enumerate(hits[:-1]):
        nxt = hits[i + 1]
        between = text[h.end:nxt.start]
        if len(between) < 25 and re.fullmatch(r"\s*(to|till|until|-|–|and)\s*", between, re.I):
            if P.classify_date_context(text, h) in ("opening", "other") and h not in opening:
                opening.append(h)
            if nxt not in closing:
                closing.append(nxt)
    if closing:
        best = max(closing, key=lambda h: h.date)           # extensions supersede earlier dates
        s, st, en = _quote_for(text, best.start, best.end)
        alts = sorted({h.date.isoformat() for h in closing if h is not best})
        out["closing_date"] = Claim("closing_date", best.date.isoformat(), s, NAME, alt_values=alts)
    if opening:
        best = max(opening, key=lambda h: h.date) if len(opening) == 1 else sorted(opening, key=lambda h: h.date)[-1]
        s, st, en = _quote_for(text, best.start, best.end)
        out["opening_date"] = Claim("opening_date", best.date.isoformat(), s, NAME)
    return out


def _quote_for(text: str, start: int, end: int, pad_before: int = 110, pad_after: int = 60) -> tuple[str, int, int]:
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    le = len(text) if le < 0 else le
    s = max(ls, start - pad_before)
    e = min(le, end + pad_after)
    # snap to word boundaries
    while s > ls and not text[s - 1].isspace():
        s -= 1
    while e < le and not text[e].isspace():
        e += 1
    return text[s:e].strip(), s, e


_BENEFIT_CTX = re.compile(r"(scholarship|stipend|fellowship|award|grant|bursary|per (annum|month|year)|p\.a\.|tuition|college fee|fee waiver|financial assistance|benefit|covers?|will be (paid|provided|given)|scholarship amount)", re.I)
_INCOME_CTX = re.compile(r"(income|earning|salary|annual family|per annum from all sources)", re.I)


def _amount(text: str) -> Claim | None:
    best, best_score = None, 0
    for st, en, sent in _sentences(text):
        if _INCOME_CTX.search(sent) and not re.search(r"(scholarship|stipend|fellowship)\s+(amount|of)", sent, re.I):
            continue
        hits = P.find_money(sent)
        if not hits or not _BENEFIT_CTX.search(sent):
            continue
        score = len(_BENEFIT_CTX.findall(sent)) + (2 if P.detect_period(sent) else 0) + (1 if re.search(r"scholarship|stipend", sent, re.I) else 0)
        if score > best_score:
            best, best_score = (st, en, sent, hits), score
    if not best:
        return None
    st, en, sent, hits = best
    vals = [h.value for h in hits if h.currency == hits[0].currency]
    return Claim("amount", {"min": min(vals), "max": max(vals), "currency": hits[0].currency, "period": P.detect_period(sent)},
                 sent.strip(), NAME)


def _income(text: str, lo: int = 0, hi: int | None = None) -> Claim | None:
    for st, en, sent in _sentences(text, lo, hi):
        if not re.search(r"\bincome\b", sent, re.I):
            continue
        hits = [h for h in P.find_money(sent, allow_bare_units=True) if h.currency == "INR"]
        if not hits:
            continue
        cond = re.search(r"(not (exceed|more than|above)|less than|below|up ?to|within|maximum|ceiling|limit|should not|does not exceed|<|upto|less)", sent, re.I)
        if cond:
            return Claim("income", {"max_inr": max(h.value for h in hits)}, sent.strip(), NAME)
    return None


def _age(text: str, lo: int = 0, hi: int | None = None) -> Claim | None:
    for st, en, sent in _sentences(text, lo, hi):
        if not re.search(r"\b(age|aged|years? old|years of age)\b", sent, re.I):
            continue
        lo, hi = P.parse_age(sent)
        if lo or hi:
            return Claim("age", {"min": lo, "max": hi}, sent.strip(), NAME)
    return None


def _gender(text: str, title: str, lo: int = 0, hi: int | None = None) -> Claim | None:
    for st, en, sent in _sentences(text, lo, hi):
        if V.GENDER_PATTERNS["ANY"].search(sent):
            return Claim("gender", "ANY", sent.strip(), NAME)
    for st, en, sent in _sentences(text, lo, hi):
        if re.search(r"(only|exclusively|solely|for|open to|reserved for|available (to|for)|meant for|intended for)\s+(the\s+)?(meritorious\s+|female\s+|eligible\s+|deserving\s+)?(girls?|female|women|woman|daughters?)", sent, re.I) \
                or re.search(r"\bsingle girl child\b", sent, re.I):
            return Claim("gender", "FEMALE", sent.strip(), NAME)
    return None


def _enum_claim(field: str, text: str, ctx_rx: re.Pattern | None = None, lo: int = 0, hi: int | None = None) -> Claim | None:
    pats = V.FIELD_ENUMS[field]
    best, best_vals = None, []
    for st, en, sent in _sentences(text, lo, hi):
        if len(sent) > 700:
            continue
        if ctx_rx and not ctx_rx.search(sent):
            continue
        vals = [k for k, rx in pats.items() if rx.search(sent)]
        if field == "categories":
            vals = [v for v in vals if v != "SPORTS" or re.search(r"sports", sent, re.I)]
            if re.search(r"(certificate|proof|attested|photocopy|self[- ]declaration|upload|enclose)", sent, re.I):
                continue                                  # document checklists are not eligibility statements
        if field == "education_levels":
            m = re.search(r"(passed|completed|cleared|qualified|after)\W+(?:\w+\W+){0,3}?(class|std|standard)?\s*(xii|12th|12)\b", sent, re.I)
            if m:                                         # "passed Class XII" => entering UG, not a school-level scheme
                vals = [v for v in vals if v not in ("SCHOOL_POST_MATRIC", "SCHOOL_PRE_MATRIC")] + ["UNDERGRADUATE"]
            elif re.search(r"(passed|completed|cleared|qualified|after)\W+(?:\w+\W+){0,3}?(class|std|standard)?\s*(x|10th|10)\b", sent, re.I):
                vals = [v for v in vals if v != "SCHOOL_PRE_MATRIC"] + ["SCHOOL_POST_MATRIC"]
            vals = list(dict.fromkeys(vals))
        if len(vals) > len(best_vals):
            best, best_vals = sent.strip(), vals
        if len(best_vals) >= 4:
            break
    return Claim(field, best_vals, best, NAME) if best_vals and best else None


_ELIG_CTX = re.compile(r"(eligib|candidates?|students?|applicants?|belong|should|must|open to|wards?|studying|pursuing|enrolled|admitted|category|categories)", re.I)


def _domicile(text: str, lo: int = 0, hi: int | None = None) -> Claim | None:
    rx = re.compile(r"(resident of|domicile|permanent resident|citizen of india|indian national|belong(s|ing)? to the state|native of|bonafide (resident|citizen)|"
                    r"indian citizen|nationals? of india|passport holders?)", re.I)
    for st, en, sent in _sentences(text, lo, hi):
        if rx.search(sent) and 30 < len(sent) < 600 and "___" not in sent and not re.search(r"resident of\s+(is|are)\b", sent, re.I):
            return Claim("domicile", sent.strip(), sent.strip(), NAME)
    return None


def _academic(text: str, lo: int = 0, hi: int | None = None) -> Claim | None:
    rx = re.compile(r"(\d{2}(\.\d+)?\s*%|per ?cent|cgpa|gpa|grade|marks|merit|rank|percentile|passed|qualif|first class|academic (record|performance|excellence))", re.I)
    for st, en, sent in _sentences(text, lo, hi):
        if rx.search(sent) and _ELIG_CTX.search(sent) and 25 < len(sent) < 600 and not re.search(r"income", sent, re.I):
            return Claim("academic_requirements", sent.strip(), sent.strip(), NAME)
    return None


def _institution(text: str, lo: int = 0, hi: int | None = None) -> Claim | None:
    rx = re.compile(r"(recognised|recognized|approved by|affiliated|accredited|NIRF|AICTE approved|UGC|IIT|IIM|NIT|central universit|government (college|institution)|institution(s)? (should|must|listed)|studying in (an? )?[a-z ]*(institution|college|university))", re.I)
    for st, en, sent in _sentences(text, lo, hi):
        if rx.search(sent) and _ELIG_CTX.search(sent) and 25 < len(sent) < 500:
            return Claim("institution_requirements", sent.strip(), sent.strip(), NAME)
    return None


def _deadline_note(text: str) -> Claim | None:
    best = None
    for st, en, sent in _sentences(text):
        kind = V.states_open_window(sent)
        if kind and len(sent) < 400:
            if kind == "rolling":
                return Claim("deadline_note", sent.strip(), sent.strip(), NAME)
            best = best or Claim("deadline_note", sent.strip(), sent.strip(), NAME)
    return best


def _application_url(links: list[Link], page_url: str) -> Claim | None:
    best, best_s = None, 0.0
    page_c = canonical_url(page_url)
    page_dom = registered_domain(host_of(page_url))
    deny = re.compile(r"(login|signin|facebook|twitter|instagram|youtube|linkedin|whatsapp|play\.google|apps\.apple|\.pdf$|mailto:|tel:)", re.I)
    for ln in links:
        if canonical_url(ln.url) == page_c or deny.search(ln.url):
            continue
        a = ln.text.lower()
        s = 0.0
        if re.search(r"\bapply (now|online|here|for)\b|\bapply\b", a):
            s += 0.6
        if re.search(r"application (form|portal|link)|online application|register( now| here| online)?|click here to apply|start (your )?application", a):
            s += 0.5
        if re.search(r"apply|application|register|otr", ln.url, re.I):
            s += 0.2
        if s <= 0:
            continue
        if registered_domain(host_of(ln.url)) == page_dom:
            s += 0.1
        if s > best_s:
            best, best_s = ln, s
    if best is None or best_s < 0.5:
        return None
    return Claim("application_url", best.url, best.text or best.context[:160], NAME, kind="LINK")


def extract(text: str, title: str, headings: list[str], links: list[Link], url: str) -> Extraction:
    ex = Extraction(NAME)
    # Eligibility-type fields are searched inside the eligibility section when the document has one.
    elig = _section(text, _HEADINGS["eligibility_text"], max_chars=3200)
    lo, hi = (elig[1], elig[2]) if elig else (0, None)
    claims: dict[str, Claim | None] = {
        "name": _name(title, headings, text),
        "provider": _provider(text, title),
        "application_url": _application_url(links, url),
        "amount": _amount(text),
        "income": _income(text, lo, hi) or (_income(text) if elig else None),
        "age": _age(text, lo, hi) or (_age(text) if elig else None),
        "gender": _gender(text, title, lo, hi),
        "categories": _enum_claim("categories", text, _ELIG_CTX, lo, hi),
        "education_levels": _enum_claim("education_levels", text, _ELIG_CTX, lo, hi),
        "domicile": _domicile(text, lo, hi),
        "academic_requirements": _academic(text, lo, hi),
        "institution_requirements": _institution(text, lo, hi),
        "deadline_note": _deadline_note(text),
    }
    claims.update(_dates(text))
    for fld, rx in _HEADINGS.items():
        sec = _section(text, rx, max_chars=1500 if fld == "eligibility_text" else 1100)
        if sec:
            body, s_, e_ = sec
            claims[fld] = Claim(fld, body, body, NAME)
    ex.claims = {k: v for k, v in claims.items() if v and v.quote}
    from .. import config  # local import to avoid cycle at import time
    from ..discovery.link_scoring import page_kind_score
    score, _ = page_kind_score(title, headings, text)
    substantive = sum(1 for k in ("amount", "eligibility_text", "closing_date", "selection_process", "documents_required",
                                  "benefit_text", "education_levels", "income") if k in ex.claims)
    ex.is_scholarship = score >= 0.25 and substantive >= 3 and "name" in ex.claims
    return ex
