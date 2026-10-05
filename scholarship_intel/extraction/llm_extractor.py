"""LLM-driven structured extraction with a strict 'verbatim quote or null' contract.

The model proposes; grounding.py disposes. Nothing returned here is trusted until its quote is located in the page.
"""
from __future__ import annotations

import re

from ..textproc import Link
from . import vocab as V
from .llm import LLMError, LLMRouter, Provider
from .models import Claim, Extraction

SYSTEM = """You are a precise information-extraction engine for Indian scholarship data. You output ONE JSON object and nothing else.

HARD RULES
1. Use ONLY the DOCUMENT provided. Never use outside knowledge, never guess, never infer, never calculate eligibility.
2. For every field return {"value": ..., "quote": ...}.
   - "quote" MUST be copied character-for-character from the DOCUMENT: one contiguous passage, at most 400 characters, no ellipses, no paraphrase, no added words.
   - If the DOCUMENT does not explicitly state the field, return {"value": null, "quote": null}. A missing income limit is null – never an assumed number.
3. For text fields ("benefit_text", "eligibility_text", ...) the value must be the same string as the quote (you may use up to 700 characters of quote for list-like sections).
4. Dates: "YYYY-MM-DD" only when day, month and year are all written. If the DOCUMENT gives several deadlines (e.g. extensions), return the latest one stated for the current/upcoming cycle and quote that sentence.
5. Money: numbers only (rupees); convert lakh = 100000, crore = 10000000. Keep the original currency code.
6. "is_scholarship_page" = true only if the DOCUMENT is about ONE specific scholarship / fellowship / financial-aid programme that students can apply to. False for lists of many schemes, news, results/merit lists, generic portal pages, circulars unrelated to a programme.
7. If the DOCUMENT covers several programmes, extract the single programme named in the title and ignore the others.
8. Enumerations: only use the allowed values below; only include a value if the quote you give contains words that justify it.
"""

SCHEMA_TEXT = """Return exactly this JSON structure (every key must be present):
{
 "is_scholarship_page": true|false,
 "name": {"value": "official scholarship name", "quote": "..."},
 "provider": {"value": "organisation that funds/runs it", "quote": "..."},
 "application_url": {"value": "URL taken from LINKS or DOCUMENT", "quote": "the anchor text or sentence that mentions it"},
 "benefit_text": {"value": "...", "quote": "..."},
 "amount": {"value": {"min": number|null, "max": number|null, "currency": "INR|USD|GBP|EUR|...", "period": "per_year|per_month|one_time|full_course|null"}, "quote": "..."},
 "eligibility_text": {"value": "...", "quote": "..."},
 "academic_requirements": {"value": "...", "quote": "..."},
 "education_levels": {"value": ["SCHOOL_PRE_MATRIC","SCHOOL_POST_MATRIC","UNDERGRADUATE","POSTGRADUATE","DOCTORAL","DIPLOMA","VOCATIONAL","PROFESSIONAL"], "quote": "..."},
 "courses": {"value": "...", "quote": "..."},
 "income": {"value": {"max_inr": number}, "quote": "..."},
 "age": {"value": {"min": integer|null, "max": integer|null}, "quote": "..."},
 "gender": {"value": "FEMALE|MALE|TRANSGENDER|ANY", "quote": "..."},
 "categories": {"value": ["SC","ST","OBC","EWS","MINORITY","PWD","ORPHAN","DEFENCE_WARD","SPORTS","GENERAL"], "quote": "..."},
 "domicile": {"value": "...", "quote": "..."},
 "institution_requirements": {"value": "...", "quote": "..."},
 "opening_date": {"value": "YYYY-MM-DD", "quote": "..."},
 "closing_date": {"value": "YYYY-MM-DD", "quote": "..."},
 "deadline_note": {"value": "...", "quote": "ONLY a sentence stating applications are rolling / year-round OR are currently open now. Never a future timeline, never a closed notice."},
 "documents_required": {"value": "...", "quote": "..."},
 "selection_process": {"value": "...", "quote": "..."},
 "renewal_requirements": {"value": "...", "quote": "..."}
}
In the enumerations above list only the values that apply (the lists show ALL allowed values, not defaults)."""

_CUES = re.compile(r"(eligib|income|age\b|years|last date|closing|deadline|apply|application|amount|rs\.|₹|stipend|per annum|per month|"
                   r"document|selection|renew|merit|marks|%|category|girl|women|sc\b|st\b|obc|minority|resident|domicile|"
                   r"scholarship|fellowship|benefit|award|open to|students|course|institution|gpa|cgpa)", re.I)


def select_text(text: str, budget: int) -> str:
    """Keep the head of the document plus the most information-dense lines, in original order."""
    if len(text) <= budget:
        return text
    lines = text.split("\n")
    keep = set(range(min(len(lines), 25)))
    used = sum(len(lines[i]) + 1 for i in keep)
    scored = sorted(range(len(lines)), key=lambda i: -len(_CUES.findall(lines[i])))
    for i in scored:
        if used >= budget:
            break
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(lines) and j not in keep:
                keep.add(j)
                used += len(lines[j]) + 1
    out, prev = [], -1
    for i in sorted(keep):
        if prev >= 0 and i != prev + 1:
            out.append("[...]")
        out.append(lines[i])
        prev = i
    return "\n".join(out)[: budget + 2000]


def _links_block(links: list[Link], limit: int = 40) -> str:
    seen, rows = set(), []
    for ln in links:
        if ln.url in seen or not ln.text or len(ln.text) > 120:
            continue
        if re.search(r"apply|application|register|portal|form|login|guideline|notice|official|website|brochure", ln.text + ln.url, re.I):
            seen.add(ln.url)
            rows.append(f"- {ln.text} -> {ln.url}")
        if len(rows) >= limit:
            break
    return "\n".join(rows) or "(none)"


def _enum_list(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [str(x).strip().upper().replace(" ", "_") for x in (v or []) if x]


def _num(x):
    try:
        if x is None or x == "":
            return None
        return float(str(x).replace(",", ""))
    except ValueError:
        return None


def parse_response(data: dict, extractor: str) -> Extraction:
    ex = Extraction(extractor, raw=data)
    ex.is_scholarship = bool(data.get("is_scholarship_page")) if "is_scholarship_page" in data else None
    for fld in V.ALL_FIELDS:
        item = data.get(fld)
        if not isinstance(item, dict):
            continue
        quote, value = item.get("quote"), item.get("value")
        if value in (None, "", [], {}) or not isinstance(quote, str) or not quote.strip():
            continue
        quote = quote.strip()
        try:
            if fld == "amount" and isinstance(value, dict):
                value = {"min": _num(value.get("min")), "max": _num(value.get("max")),
                         "currency": (value.get("currency") or "INR").upper(), "period": value.get("period") or None}
                if value["min"] is None and value["max"] is None:
                    continue
            elif fld == "income" and isinstance(value, dict):
                value = {"max_inr": _num(value.get("max_inr") or value.get("max"))}
                if value["max_inr"] is None:
                    continue
            elif fld == "age" and isinstance(value, dict):
                lo, hi = _num(value.get("min")), _num(value.get("max"))
                value = {"min": int(lo) if lo is not None else None, "max": int(hi) if hi is not None else None}
                if value["min"] is None and value["max"] is None:
                    continue
            elif fld == "gender":
                value = str(value).strip().upper()
                if value not in ("FEMALE", "MALE", "TRANSGENDER", "ANY"):
                    continue
            elif fld in ("categories", "education_levels"):
                value = [v for v in _enum_list(value) if v in V.FIELD_ENUMS[fld]]
                if not value:
                    continue
            elif fld in ("opening_date", "closing_date"):
                value = str(value).strip()
            elif fld in V.TEXT_FIELDS:
                value = quote                       # text fields are verbatim by definition
            else:
                value = str(value).strip()
        except (TypeError, ValueError):
            continue
        ex.claims[fld] = Claim(fld, value, quote, extractor, "LINK" if fld == "application_url" else "QUOTE")
    return ex


def llm_extract(router: LLMRouter, provider: Provider, text: str, title: str, headings: list[str],
                links: list[Link], url: str) -> Extraction:
    budget = router.max_chars.get(provider.name, 12000)
    doc = select_text(text, budget)
    user = (f"{SCHEMA_TEXT}\n\nPAGE URL: {url}\nPAGE TITLE: {title}\n\nLINKS FOUND ON THE PAGE (for application_url):\n"
            f"{_links_block(links)}\n\n=== DOCUMENT START ===\n{doc}\n=== DOCUMENT END ===")
    label = f"llm:{provider.label}"
    try:
        data = router.call(provider, SYSTEM, user)
    except LLMError as exc:
        return Extraction(label, error=str(exc), prompt_chars=len(user))
    ex = parse_response(data, label)
    ex.prompt_chars = len(user)
    return ex
