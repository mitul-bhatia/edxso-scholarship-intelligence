"""Relevance scoring for links (what to crawl next) and pages (is this a scholarship detail page?)."""
from __future__ import annotations

import re

from ..util import fold

_POS_ANCHOR = [
    (r"\bscholarships?\b", 0.40), (r"\bfellowships?\b", 0.35), (r"\bstipend\b", 0.25), (r"\bbursar(y|ies)\b", 0.3),
    (r"\bfinancial (aid|assistance|support)\b", 0.30), (r"\bmerit[- ]cum[- ]means\b", 0.4), (r"\bfree ?ship\b", 0.2),
    (r"\bguidelines?\b", 0.20), (r"\bscheme\b", 0.20), (r"\beligibility\b", 0.25), (r"\bhow to apply\b", 0.20),
    (r"\bapply( now| online)?\b", 0.12), (r"\bspecifications?\b", 0.18), (r"\bpublic notice\b", 0.10),
    (r"\bcall for (applications|nominations)\b", 0.30), (r"\bpost[- ]matric\b", 0.3), (r"\bpre[- ]matric\b", 0.3),
    (r"\bendowment\b", 0.2), (r"\bgrant\b", 0.1), (r"\bchevening|fulbright|commonwealth|inlaks|daad|rhodes\b", 0.35),
]
_POS_URL = [(r"scholar", 0.30), (r"fellow", 0.25), (r"scheme", 0.15), (r"guideline", 0.15), (r"financial-?aid", 0.25),
            (r"stipend", 0.2), (r"apply", 0.08), (r"eligib", 0.15), (r"bursar", 0.25)]
_NEG = [
    (r"\b(tender|recruitment|vacanc(y|ies)|career|job|walk[- ]in|rti|gallery|photo|video|annual report|press release|"
     r"login|sign ?in|register as|contact us|about us|sitemap|privacy|disclaimer|terms|copyright|hyperlink|feedback|"
     r"archive|result of|merit list|selected candidates|sanctioned list|notice board|circular|office order|"
     r"helpline|grievance|screen reader|accessibility)\b", -0.45),
    (r"^\s*faqs?\s*$", -0.3),
    (r"\.(jpg|jpeg|png|gif|svg|ico|css|js|zip|rar|mp4|mp3|doc|docx|xls|xlsx|ppt|pptx)(\?|$)", -1.0),
]


def score_link(anchor: str, url: str, context: str = "") -> float:
    a, u, c = fold(anchor), url.lower(), fold(context)[:300]
    s = 0.0
    for pat, w in _POS_ANCHOR:
        if re.search(pat, a):
            s += w
    for pat, w in _POS_URL:
        if re.search(pat, u):
            s += w
    # Row/list context helps for "View" / "Specifications" style anchors
    if s < 0.45 and c:
        for pat, w in _POS_ANCHOR[:5]:
            if re.search(pat, c):
                s += w * 0.6
                break
    for pat, w in _NEG:
        if re.search(pat, a) or re.search(pat, u):
            s += w
    return max(0.0, min(1.0, s))


_CUES = {
    "eligibility": r"\b(eligib\w*|who can apply|criteria|should (be|have)|must (be|have)|open to)\b",
    "benefit": r"(₹|\brs\.?\b|\binr\b|\bstipend\b|per (annum|month|year)|\bp\.a\.|tuition|fee waiver|financial assistance|award of|amount of|scholarship amount|\bgbp\b|£|\$|€|\bfully funded\b)",
    "process": r"\b(how to apply|apply (online|now|through|by)|application (form|process|portal|link)|last date|closing date|deadline|due date|submit(ted)? (the )?application)\b",
    "scholarship": r"\b(scholarships?|fellowships?|stipend|bursary|financial assistance|merit[- ]cum[- ]means)\b",
}


def page_kind_score(title: str, headings: list[str], text: str) -> tuple[float, dict]:
    """Heuristic probability that a document describes a specific scholarship programme (vs. a listing/news/other)."""
    head = fold(" ".join([title] + headings[:8]))
    body = fold(text[:60_000])
    hits = {k: len(re.findall(p, body)) for k, p in _CUES.items()}
    s = 0.0
    if re.search(_CUES["scholarship"], head):
        s += 0.30
    s += min(0.25, 0.05 * hits["eligibility"])
    s += min(0.20, 0.05 * hits["benefit"])
    s += min(0.20, 0.05 * hits["process"])
    s += min(0.10, 0.01 * hits["scholarship"])
    if len(text) < 300:
        s *= 0.4
    return round(min(1.0, s), 3), hits
