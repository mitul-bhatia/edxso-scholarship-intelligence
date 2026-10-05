"""Evidence grounding: the anti-hallucination gate.

A claim becomes a stored fact only if
  (1) its quote is located in the fetched page text (exactly, or fuzzily >= threshold for PDF line-wrap noise), AND
  (2) its normalised value is derivable from that quote (dates parse to the same date, numbers appear in the quote,
      enum members are justified by wording in the quote, URLs are present among the page's links).
Everything else is moved to `rejected_extractions` and the field is reported as 'Not specified'.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from .. import config
from ..extraction import parsers as P
from ..extraction import vocab as V
from ..extraction.models import Claim, Extraction, Grounded, Rejected
from ..textproc import Haystack, Link, fold_text
from ..util import canonical_url, host_of, name_key


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-6, 0.001 * max(abs(a), abs(b)))


def _validate_value(field: str, value, quote: str) -> tuple[object | None, str]:
    """Return (possibly-narrowed value, '') if derivable from quote else (None, reason)."""
    q = quote
    if field in ("name", "provider"):
        fv, fq = fold_text(str(value)), fold_text(q)
        if fv and (fv in fq or fuzz.partial_ratio(fv, fq) >= 90):
            return str(value).strip(), ""
        return None, "value not contained in quote"
    if field == "deadline_note":
        return (q.strip(), "") if V.states_open_window(q) else (None, "quote does not state a rolling or currently-open application window")
    if field in V.TEXT_FIELDS:
        why = V.topic_ok(field, q)
        return (None, why) if why else (q.strip(), "")
    if field in ("opening_date", "closing_date"):
        dates = {h.date.isoformat() for h in P.find_dates(q)}
        if value not in dates:
            return None, f"date {value} not written in quote"
        if P.NEG_DATE.search(q):
            return None, "quote is about a different date (referees, notification, results, programme dates, bank details…), not the application window"
        if field == "closing_date" and not P.closing_strength(q) and not re.search(r"\b(until|till|by|before|due|deadline|close[sd]?|last date|ends?)\b", q, re.I):
            return None, "quote does not state that this is a closing / deadline date"
        return value, ""
    if field == "amount":
        # A number on a scholarship site is not necessarily a scholarship
        # benefit (for example, an alumnus describing their VC fund).
        if not re.search(r"scholarship|stipend|fellowship|award|grant|bursary|tuition|college fee|fee waiver|financial assistance|benefit|"
                         r"will be (paid|provided|given)|per (annum|month|year)|p\.a\.", q, re.I):
            return None, "quote does not connect the amount to a student benefit"
        nums = P.numbers_in(q)
        want = [v for v in (value.get("min"), value.get("max")) if v is not None]
        if not want or not all(any(_close(w, n) for n in nums) for w in want):
            return None, "amount figures not present in quote"
        cur = value.get("currency", "INR")
        sym = {"INR": r"₹|rs\b|rs\.|inr|rupee|lakh|lac|crore|/-", "USD": r"\$|usd|dollar", "GBP": r"£|gbp|pound", "EUR": r"€|eur\b|euro",
               "AUD": r"a\$|aud", "CAD": r"c\$|cad", "CHF": r"chf"}.get(cur)
        if sym and not re.search(sym, q, re.I):
            return None, f"currency {cur} not indicated in quote"
        return value, ""
    if field == "income":
        v = value.get("max_inr")
        if v is None or not re.search(r"income|earning|salary", q, re.I):
            return None, "quote does not discuss income"
        return (value, "") if any(_close(v, n) for n in P.numbers_in(q)) else (None, "income figure not present in quote")
    if field == "age":
        if not re.search(r"\bage|aged|years", q, re.I):
            return None, "quote does not discuss age"
        nums = P.numbers_in(q)
        want = [v for v in (value.get("min"), value.get("max")) if v is not None]
        return (value, "") if want and all(float(w) in nums for w in want) else (None, "age figures not present in quote")
    if field == "gender":
        pat = V.GENDER_PATTERNS.get(str(value))
        return (value, "") if pat and pat.search(q) else (None, "gender wording not present in quote")
    if field in ("categories", "education_levels"):
        ok = V.enum_values_supported_by(q, field, list(value))
        if not ok:
            return None, f"none of {value} justified by quote wording"
        return ok, ""
    return value, ""


def ground_extraction(ex: Extraction, hay: Haystack, links: list[Link]) -> tuple[dict[str, Grounded], list[Rejected]]:
    min_ratio = float(config.settings()["verification"]["fuzzy_quote_min"])
    grounded: dict[str, Grounded] = {}
    rejected: list[Rejected] = []
    link_set = {canonical_url(l.url) for l in links}
    link_by_url = {canonical_url(l.url): l for l in links}

    for fld, c in ex.claims.items():
        if c.kind == "LINK":
            url = str(c.value).strip()
            cu = canonical_url(url)
            if cu in link_set or url in hay.text:
                ln = link_by_url.get(cu)
                quote = (ln.text or ln.context[:200]) if ln else url
                m = hay.find(quote) if quote else None
                grounded[fld] = Grounded(fld, url, quote or url, m.start if m else None, m.end if m else None,
                                         100.0 if ln else 90.0, c.extractor, "LINK")
            else:
                rejected.append(Rejected(fld, c.value, c.quote, "URL not present among the page's links or text", c.extractor))
            continue
        quote = (c.quote or "").strip()
        if len(quote) < 4 or len(quote) > 2500:
            rejected.append(Rejected(fld, c.value, c.quote, "quote missing or implausible length", c.extractor))
            continue
        m = hay.find(quote, min_ratio)
        if not m:
            rejected.append(Rejected(fld, c.value, c.quote, "quote not found in source text (possible hallucination)", c.extractor))
            continue
        val, why = _validate_value(fld, c.value, hay.window(m.start, m.end))
        if val is None:
            # retry against the model's own quote text (handles fuzzy match widening/narrowing)
            val, why = _validate_value(fld, c.value, quote)
        if val is None:
            rejected.append(Rejected(fld, c.value, c.quote, f"value inconsistent with quote: {why}", c.extractor))
            continue
        exact_quote = hay.window(m.start, m.end).strip() if not m.exact else quote
        grounded[fld] = Grounded(fld, val, hay.window(m.start, m.end).strip() if not m.exact else exact_quote,
                                 m.start, m.end, m.score, c.extractor, "QUOTE")

    # cross-field sanity
    o, cl = grounded.get("opening_date"), grounded.get("closing_date")
    if o and cl and o.value > cl.value:
        rejected.append(Rejected("opening_date", o.value, o.quote, f"opening date {o.value} is after closing date {cl.value}", o.extractor))
        del grounded["opening_date"]
    return grounded, rejected


def revalidate(facts: dict[str, Grounded]) -> tuple[dict[str, Grounded], list[Rejected]]:
    """Re-apply the CURRENT value-vs-quote validators to facts stored by an earlier run (no LLM call needed), so a fix to a
    validator immediately removes facts the old validator let through."""
    kept: dict[str, Grounded] = {}
    rejected: list[Rejected] = []
    for f, g in facts.items():
        if g.kind == "LINK":
            kept[f] = g
            continue
        val, why = _validate_value(f, g.value, g.quote)
        if val is None:
            rejected.append(Rejected(f, g.value, g.quote, f"re-validation: {why}", g.extractor))
        else:
            kept[f] = g
    return kept, rejected


# ------------------------------------------------------------------------------- cross-extractor merge
def _norm_equal(field: str, a, b) -> float:
    """1.0 agree, 0.5 partial, 0.0 conflict."""
    if field in ("opening_date", "closing_date", "gender"):
        return 1.0 if a == b else 0.0
    if field == "amount":
        am, bm = a.get("max") or a.get("min"), b.get("max") or b.get("min")
        if am and bm and _close(am, bm) and a.get("currency") == b.get("currency"):
            return 1.0
        return 0.0
    if field == "income":
        return 1.0 if _close(a["max_inr"], b["max_inr"]) else 0.0
    if field == "age":
        return 1.0 if (a.get("min"), a.get("max")) == (b.get("min"), b.get("max")) else 0.0
    if field in ("categories", "education_levels"):
        sa, sb = set(a), set(b)
        if sa == sb:
            return 1.0
        j = len(sa & sb) / max(1, len(sa | sb))
        return 0.5 if j >= 0.4 else 0.0
    if field == "application_url":
        ca, cb = canonical_url(str(a)), canonical_url(str(b))
        return 1.0 if ca == cb else (0.5 if host_of(ca) == host_of(cb) else 0.0)
    if field == "name":
        ka, kb = name_key(a), name_key(b)
        return 1.0 if (fuzz.ratio(ka, kb) >= 80 or fuzz.token_set_ratio(ka, kb) >= 88) else 0.0
    if field == "provider":
        fa, fb = fold_text(a), fold_text(b)
        if fuzz.token_set_ratio(fa, fb) >= 80:
            return 1.0
        initials = lambda t: "".join(w[0] for w in re.findall(r"[a-z]+", t) if w not in ("of", "the", "for", "and"))   # noqa: E731
        ca, cb = re.sub(r"[^a-z]", "", fa), re.sub(r"[^a-z]", "", fb)
        return 1.0 if (len(ca) >= 3 and initials(fb) == ca) or (len(cb) >= 3 and initials(fa) == cb) else 0.0
    return 1.0   # text fields handled by span overlap below


def _overlap(a: Grounded, b: Grounded) -> float:
    if a.start is None or b.start is None:
        return 1.0 if fold_text(a.quote)[:80] == fold_text(b.quote)[:80] else 0.0
    inter = max(0, min(a.end, b.end) - max(a.start, b.start))
    return inter / max(1, min(a.end - a.start, b.end - b.start))


def _union_if_justified(fld: str, opinions: list[tuple[str, Grounded]], per_extractor) -> Grounded | None:
    """Set-valued fields (categories, education levels) are often spread over several lines of a list. If extractors
    returned different subsets, accept their union only when ONE quote from the page justifies every member."""
    sets = [set(o.value) for _, o in opinions]
    union = set().union(*sets)
    if all(s == sets[0] for s in sets):
        return None
    if any(a < b for a in sets for b in sets):
        return None        # one answer is a subset of the other: the extra value rests on a single extractor – do not union it in
    pool = [g[ "eligibility_text"] for _, g in per_extractor if "eligibility_text" in g] + [o for _, o in opinions]
    for cand in pool:
        if len(V.enum_values_supported_by(cand.quote, fld, sorted(union))) == len(union):
            return Grounded(fld, sorted(union), cand.quote, cand.start, cand.end, cand.match_score, cand.extractor, "QUOTE",
                            agreed_by=[n for n, _ in opinions if n != cand.extractor])
    return None


CRITICAL_FIELDS = {"closing_date", "opening_date", "amount", "income", "age", "gender", "categories", "education_levels", "application_url"}


def merge_extractions(per_extractor: list[tuple[str, dict[str, Grounded]]]) -> tuple[dict[str, Grounded], dict[str, float], list[dict]]:
    """Combine grounded claims from several independent extractors by majority clustering.

    Returns (final facts, per-field agreement in [0,1] for fields ≥2 extractors addressed, conflicts).
    For each field the largest cluster of mutually-consistent values wins; ties go to exact quotes, then to the
    order of `per_extractor` (LLMs first, rules last). agreement = |winning cluster| / |opinions|.
    """
    final: dict[str, Grounded] = {}
    agreement: dict[str, float] = {}
    conflicts: list[dict] = []
    fields = {f for _, g in per_extractor for f in g}
    order = {n: i for i, (n, _) in enumerate(per_extractor)}
    for fld in fields:
        opinions = [(n, g[fld]) for n, g in per_extractor if fld in g]
        if fld in V.TEXT_FIELDS:
            # verbatim text: agreement = share of other extractors whose passage overlaps the primary's
            opinions.sort(key=lambda t: (-(t[1].match_score >= 100), order[t[0]]))
            pn, primary = opinions[0]
            hits = 0
            for n, o in opinions[1:]:
                if _overlap(primary, o) >= 0.3:
                    primary.agreed_by.append(n)
                    hits += 1
            if len(opinions) > 1:
                agreement[fld] = (1 + hits) / len(opinions)
            final[fld] = primary
            continue
        if fld in V.FIELD_ENUMS and len(opinions) > 1:
            union = _union_if_justified(fld, opinions, per_extractor)
            if union is not None:
                final[fld] = union
                agreement[fld] = 0.9            # complementary, individually justified – slightly below exact agreement
                continue
        clusters: list[list[tuple[str, Grounded]]] = []
        for n, o in opinions:
            for cl in clusters:
                if _norm_equal(fld, cl[0][1].value, o.value) >= 0.5:
                    cl.append((n, o))
                    break
            else:
                clusters.append([(n, o)])
        clusters.sort(key=lambda cl: (-len(cl), min(order[n] for n, _ in cl)))
        win = clusters[0]
        win.sort(key=lambda t: (-(t[1].match_score >= 100), order[t[0]]))
        pn, primary = win[0]
        primary.agreed_by = [n for n, _ in win[1:]]
        for cl in clusters[1:]:
            for n, o in cl:
                primary.conflicting[n] = o.value
                conflicts.append({"field": fld, "chosen": primary.value, "chosen_by": pn, "other": o.value, "other_by": n})
        if len(opinions) > 1:
            agreement[fld] = len(win) / len(opinions)
        final[fld] = primary
    return final, agreement, conflicts


# ----------------------------------------------------------------------------------- absence checks
_ABSENCE_HINTS = {
    "income": r"\bincome\b[^.\n]{0,120}(\d|lakh|bpl|poverty)",
    "age": r"\bage\b[^.\n]{0,80}\d{1,2}\s*years?",
    "closing_date": r"(last date|closing date|deadline|due date|apply (by|before|till|until))",
    "opening_date": r"(opening date|start date|applications? (open|start|commence)|open from)",
    "gender": r"\b(girls?|female|women|woman|boys?)\b",
    "categories": r"\b(SC|ST|OBC|EWS|minorit\w+|PwD|disabilit\w+|orphan\w*)\b",
    "amount": r"(₹|\brs\.?\s?\d|\binr\b|lakh|per (annum|month)|stipend|£|\$\s?\d|€)",
    "education_levels": r"\b(class|undergraduate|postgraduate|graduate|ph\.?d|diploma|degree|b\.?tech|master|bachelor|post[- ]matric|pre[- ]matric|matric)\b",
}


def absence_check(field: str, text: str) -> tuple[bool, str]:
    """For a field with no grounded value: is 'Not specified' trustworthy? (False => extractors may have missed it)."""
    rx = _ABSENCE_HINTS.get(field)
    if not rx:
        return True, "no lexical hint of this field in the document"
    m = re.search(rx, text, re.I | re.S)
    if m:
        return False, f"document contains wording that may state this field ('{text[max(0, m.start() - 20): m.end() + 20].strip()[:90]}') but no value could be grounded"
    return True, "no lexical hint of this field anywhere in the document"
