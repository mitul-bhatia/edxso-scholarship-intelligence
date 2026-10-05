"""Small shared helpers (URL canonicalisation, hashing, text normalisation)."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urldefrag, urlparse, urlunparse, parse_qsl, urlencode

_SECOND_LEVEL = {"co", "ac", "gov", "nic", "edu", "org", "net", "res", "ernet", "com", "bank", "or"}
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref"}


def sha1(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8", "ignore")
    return hashlib.sha1(s).hexdigest()


def host_of(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def registered_domain(host: str) -> str:
    """Approximate eTLD+1 without the public-suffix list (good enough for .in/.gov.in/.ac.in/.org/.com)."""
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if labels[-1] in {"in", "uk", "au", "ca", "nz", "za"} and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def canonical_url(url: str) -> str:
    """Normalise a URL for dedupe: lower-case host, drop fragment/tracking params, trim trailing slash."""
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    if not p.scheme:
        return url
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in _TRACKING])
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((p.scheme.lower(), netloc, path, "", query, ""))


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"', ord(" "): " "}


def fold(s: str) -> str:
    """Aggressive folding used only for *matching* (never for stored values)."""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_DASHES).translate(_QUOTES)
    return re.sub(r"\s+", " ", s).strip().lower()


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def name_key(name: str) -> str:
    """Identity-normalised scholarship name: drops years, 'scheme/scholarship/programme' noise, punctuation."""
    s = fold(name)
    s = re.sub(r"\b(20\d{2}\s*[-–/]\s*(20)?\d{2}|20\d{2})\b", " ", s)
    s = re.sub(r"\b(scholarships?|schemes?|programme|program|fellowships?|the|for|of|and|in|to|a)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return norm_ws(s)


def truncate(s: str | None, n: int = 300) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


_GENERIC = {"scholarship", "scholarships", "fellowship", "fellowships", "scheme", "schemes", "programme", "program", "portal", "nsp", "national",
            "home", "welcome", "student", "students", "india", "indian", "about", "award", "awards", "apply", "online", "guidelines",
            "guideline", "specifications", "general", "overview", "information", "page", "new", "the", "for", "of", "and", "in", "to", "a",
            "financial", "aid", "assistance", "support", "funding", "grants", "grant", "study", "education", "details", "list", "all",
            "girls", "women", "woman", "girl", "boys", "meritorious", "college", "colleges", "university", "universities", "top", "best",
            "private", "fully", "funded", "abroad", "international", "latest", "free", "check", "status", "last", "date", "benefits",
            "eligibility", "development", "faq", "faqs", "frequently", "asked", "questions", "center", "centers", "centre", "centres", "integrated", "notice", "lakh", "lakhs", "year", "years", "amount", "ngo", "ngos", "corporate", "csr", "government", "central", "state", "minority", "post", "matric", "pre"}


def distinctive_tokens(name: str) -> list[str]:
    """Proper-noun-like tokens of a scholarship name (what makes it *this* scholarship)."""
    return [t for t in re.findall(r"[a-z0-9]+", fold(name)) if t not in _GENERIC and len(t) >= 4 and not re.fullmatch(r"20\d{2}", t)]


def looks_like_list_title(title: str) -> bool:
    t = title.strip()
    ascii_ratio = sum(c.isascii() for c in t) / max(1, len(t))
    return (ascii_ratio < 0.9 or bool(re.match(r"^(top|best|why|discover|how|what|list of|complete list|\d+\s|q\s?\d+[.)]|question)", t, re.I))
            or t.endswith("?")
            or bool(re.search(r"\b(faqs?|frequently asked|complete list|guide to)\b", t, re.I))
            or bool(re.search(r"\b(scholarships)\b", t, re.I) and len(distinctive_tokens(t)) < 2))


def is_generic_name(name: str) -> bool:
    """True for hub/listing titles such as 'Scholarships', 'NSP : National Scholarship Portal', 'About the Award'."""
    toks = [t for t in re.findall(r"[a-z0-9]+", fold(name)) if t not in _GENERIC and not re.fullmatch(r"20\d{2}|\d{1,2}", t)]
    if not any(len(t) >= 4 for t in toks):
        return True
    return bool(re.search(r"\bportal\b", fold(name))) and not re.search(r"scheme|yojana|scholarship for|fellowship for", fold(name))
