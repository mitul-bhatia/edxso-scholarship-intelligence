"""Deterministic value parsers shared by the rule extractor and the grounding validators.

They answer "which dates / amounts / ages are literally written in this string?" – the grounding layer uses
them to check that an extracted value is derivable from its quote.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

_DMY = re.compile(rf"\b(?P<d>\d{{1,2}})\s*(?:st|nd|rd|th)?[\s,.\-]*(?:of\s+)?(?P<m>{_MON})\b\.?[\s,.\-]*(?P<y>\d{{4}})\b", re.I)
_MDY = re.compile(rf"\b(?P<m>{_MON})\b\.?\s+(?P<d>\d{{1,2}})\s*(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})\b", re.I)
_NUM = re.compile(r"(?<![\d/.\-])(?P<d>\d{1,2})\s*[/.\-]\s*(?P<m>\d{1,2})\s*[/.\-]\s*(?P<y>\d{4})(?![\d/])")
_ISO = re.compile(r"\b(?P<y>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})\b")


@dataclass
class DateHit:
    date: dt.date
    start: int
    end: int


def _mk(y: str, m: str | int, d: str) -> dt.date | None:
    try:
        mi = int(m) if not isinstance(m, int) and str(m).isdigit() else (m if isinstance(m, int) else _MONTHS[str(m)[:3].lower()])
        date = dt.date(int(y), mi, int(d))
    except (ValueError, KeyError):
        return None
    return date if 2000 <= date.year <= 2100 else None


def find_dates(text: str) -> list[DateHit]:
    hits: list[DateHit] = []
    taken: list[tuple[int, int]] = []

    def add(m: re.Match) -> None:
        if any(m.start() < e and s < m.end() for s, e in taken):
            return
        d = _mk(m.group("y"), m.group("m"), m.group("d"))
        if d:
            hits.append(DateHit(d, m.start(), m.end()))
            taken.append((m.start(), m.end()))

    for rx in (_ISO, _DMY, _MDY, _NUM):
        for m in rx.finditer(text):
            add(m)
    hits.sort(key=lambda h: h.start)
    return hits


_CLOSE_CUES = re.compile(r"(last\s*(date|day)|closing\s*date|close[sd]?\b|closes?\s*on|deadline|due\s*date|end\s*date|apply\s*(by|before|till|until|on or before)|"
                         r"(till|until|upto|up to|before|by|on or before|not later than)\s*$|submitted\s*(by|before|on or before)|extended\s*(up)?\s*to|extended\s*till|"
                         r"last\s*date\s*of\s*(online\s*)?(application|registration|submission)|window\s*closes)", re.I)
_OPEN_CUES = re.compile(r"(opening\s*date|open(s|ed)?\s*(on|from)|start(s|ing)?\s*(date|from|on)|commenc\w+|begin(s|ning)?|launch(ed)?|"
                        r"invited\s*(from|w\.e\.f)|w\.e\.f\.?|from\s*$|applications?\s*(start|open)|registration\s*(starts|opens))", re.I)


# A date in a sentence about any of these is NOT the application deadline (referees, results, programme dates, bank details…).
NEG_DATE = re.compile(r"(referee|reference letters?|recommendation|notified|notification|will be informed|announce|results?\b|shortlist|interview|"
                      r"aptitude|test (date|will)|programme dates?|program dates?|programme (start|begin)|commenc|start(s|ing)? (of )?the (programme|course|session|fellowship)|"
                      r"\bbank\b|\bPAN\b|account details|orientation|joining|departure|travel|disburs|communication from|must assume|hard cop|courier|"
                      r"host applications?|visa|proposed programme)", re.I)
# Phrases that explicitly say "this is the deadline for APPLICATIONS".
STRONG_CLOSE = re.compile(r"(deadline for (all |the |your )?(online )?applications?|application (deadline|due date|closing date)|"
                          r"last date (to|for|of) (online )?(apply|applying|application|registration|submi\w+)|apply (by|before|until|till)|"
                          r"applications? (will )?(close|closes|closing)|applications? (are |is )?(open|accepted|invited) (until|till|up to)|"
                          r"(open|opens) (from|on)[^.]{0,45}(till|until|to) |closing date (for|of) (the )?(online )?applications?|"
                          r"submit(ted)? (your |the )?(online )?applications? (by|before|until|on or before))", re.I)
_APP_WORD = re.compile(r"appl|regist|submi|nominat|enrol", re.I)


def closing_strength(sentence: str) -> int:
    """0 = not an application deadline (or negative context), 1 = generic deadline wording, 2 = deadline + application context, 3 = explicit."""
    if NEG_DATE.search(sentence):
        return 0
    if STRONG_CLOSE.search(sentence):
        return 3
    if _CLOSE_CUES.search(sentence):
        return 2 if _APP_WORD.search(sentence) else 1
    if _APP_WORD.search(sentence) and re.search(r"\b(until|till|up ?to|before|by|on or before)\b", sentence, re.I):
        return 2                              # "Open for applications until 6 October 2026"
    return 0


def classify_date_context(text: str, hit: DateHit) -> str:
    """Return 'closing', 'opening' or 'other' from the words just before the date."""
    ls = text.rfind("\n", 0, hit.start) + 1
    le = text.find("\n", hit.end)
    line = text[ls: len(text) if le < 0 else le]
    if NEG_DATE.search(line[:600]):
        return "other"                      # e.g. referee deadline, results date, programme dates, bank-details deadline
    before = text[max(ls, hit.start - 90): hit.start]
    after = text[hit.end: min(len(text), hit.end + 40)]
    best, kind = -1, "other"
    for rx, label in ((_CLOSE_CUES, "closing"), (_OPEN_CUES, "opening")):
        for m in rx.finditer(before):
            if m.end() > best:
                best, kind = m.end(), label
    if kind == "other" and re.match(r"\s*(is|was)?\s*(the )?(last|closing|deadline)", after, re.I):
        kind = "closing"
    return kind


# ----------------------------------------------------------------------------------- money
_NUMBER = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT = r"(?:lakhs?|lacs?|lakh|crores?|cr\b|thousand|k\b|million|mn\b)"
_CUR = r"(?:₹|rs\.?|inr|rupees?|us\$|usd|\$|£|gbp|€|eur|a\$|aud|c\$|cad|chf)"
_MONEY = re.compile(rf"(?P<cur>{_CUR})\s*(?P<n>{_NUMBER})(?:\s*/-)?\s*(?P<u>{_UNIT})?", re.I)
_MONEY_POST = re.compile(rf"(?P<n>{_NUMBER})\s*(?P<u>{_UNIT})\s*(?P<cur>(?:rupees?|inr)?)", re.I)

_CUR_MAP = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupee": "INR", "rupees": "INR", "$": "USD", "us$": "USD", "usd": "USD",
            "£": "GBP", "gbp": "GBP", "€": "EUR", "eur": "EUR", "a$": "AUD", "aud": "AUD", "c$": "CAD", "cad": "CAD", "chf": "CHF"}


@dataclass
class MoneyHit:
    value: float
    currency: str
    start: int
    end: int
    text: str


def _scale(n: str, unit: str | None) -> float:
    v = float(n.replace(",", ""))
    u = (unit or "").lower()
    if u.startswith(("lakh", "lac")):
        v *= 1e5
    elif u.startswith(("crore", "cr")):
        v *= 1e7
    elif u in ("thousand", "k"):
        v *= 1e3
    elif u in ("million", "mn"):
        v *= 1e6
    return v


def find_money(text: str, allow_bare_units: bool = False) -> list[MoneyHit]:
    hits: list[MoneyHit] = []
    taken: list[tuple[int, int]] = []
    for m in _MONEY.finditer(text):
        cur = _CUR_MAP.get(m.group("cur").lower().rstrip("."), None) or _CUR_MAP.get(m.group("cur").lower(), "INR")
        v = _scale(m.group("n"), m.group("u"))
        hits.append(MoneyHit(v, cur, m.start(), m.end(), m.group(0)))
        taken.append((m.start(), m.end()))
    if allow_bare_units:   # "8 lakh", "2.5 lakhs" – without a currency symbol
        for m in _MONEY_POST.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            hits.append(MoneyHit(_scale(m.group("n"), m.group("u")), "INR", m.start(), m.end(), m.group(0)))
    hits.sort(key=lambda h: h.start)
    return hits


_PERIODS = [
    (re.compile(r"per\s*(annum|year)|p\.?\s?a\.?\b|annual(ly)?|yearly|each year|every year", re.I), "per_year"),
    (re.compile(r"per\s*month|monthly|p\.?\s?m\.?\b|every month", re.I), "per_month"),
    (re.compile(r"one[- ]time|lump\s*sum|once only|one off", re.I), "one_time"),
    (re.compile(r"full (tuition|course|fee)|entire (course|duration)|duration of the (course|programme|program)|fully funded|complete fee", re.I), "full_course"),
]


def detect_period(s: str) -> str | None:
    for rx, label in _PERIODS:
        if rx.search(s):
            return label
    return None


# -------------------------------------------------------------------------------------- age
_AGE_RANGE = re.compile(r"(?:between|from|aged?)\s*(?:the\s*age\s*of\s*)?(\d{1,2})\s*(?:and|to|-|–)\s*(\d{1,2})\s*years?", re.I)
_AGE_RANGE2 = re.compile(r"\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*years?\b", re.I)
_AGE_MAX = re.compile(r"(?:not\s*(?:exceed(?:ing)?|more\s*than|above|older\s*than)|below|under|less\s*than|up\s*to|upto|maximum(?:\s*age)?(?:\s*(?:limit|of))?|upper\s*age\s*limit(?:\s*of)?|age\s*limit(?:\s*of)?|within|not\s*be\s*(?:more|above|older)\s*than)\s*(\d{1,2})\s*years?", re.I)
_AGE_MIN = re.compile(r"(?:at\s*least|minimum(?:\s*age)?(?:\s*of)?|not\s*(?:less|below|younger)\s*than|above|over|more\s*than|attained(?:\s*the\s*age\s*of)?)\s*(\d{1,2})\s*years?", re.I)


def parse_age(s: str) -> tuple[int | None, int | None]:
    m = _AGE_RANGE.search(s) or _AGE_RANGE2.search(s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 3 <= a <= b <= 80:
            return a, b
    lo = hi = None
    m = _AGE_MAX.search(s)
    if m and 3 <= int(m.group(1)) <= 80:
        hi = int(m.group(1))
    m = _AGE_MIN.search(s)
    if m and 3 <= int(m.group(1)) <= 80:
        lo = int(m.group(1))
    return lo, hi


def numbers_in(s: str) -> set[float]:
    """Every numeric value literally present in a string, with lakh/crore/thousand scaling applied *and* raw."""
    out: set[float] = set()
    for m in re.finditer(rf"({_NUMBER})\s*({_UNIT})?", s, re.I):
        raw = float(m.group(1).replace(",", ""))
        out.add(raw)
        if m.group(2):
            out.add(_scale(m.group(1), m.group(2)))
    return out
