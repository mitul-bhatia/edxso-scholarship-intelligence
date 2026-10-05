"""Free web search for discovery (DuckDuckGo via the `ddgs` package; no API key) with a small on-disk cache."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from .. import config
from ..util import sha1


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


_CACHE_TTL = 12 * 3600


def web_search(query: str, max_results: int = 10, use_cache: bool = True) -> list[SearchHit]:
    cdir = config.CACHE_DIR / "search"
    cdir.mkdir(parents=True, exist_ok=True)
    cpath = cdir / f"{sha1(query)}.json"
    if use_cache and cpath.exists() and time.time() - cpath.stat().st_mtime < _CACHE_TTL:
        try:
            return [SearchHit(**h) for h in json.loads(cpath.read_text())]
        except (ValueError, TypeError):
            pass
    try:
        from ddgs import DDGS
    except ImportError:
        return []
    hits: list[SearchHit] = []
    for attempt in range(3):
        try:
            res = DDGS().text(query, region="in-en", max_results=max_results)
            hits = [SearchHit(r.get("title", ""), r.get("href", ""), r.get("body", "")) for r in res if r.get("href")]
            if hits:
                break
        except Exception:
            time.sleep(2.5 * (attempt + 1))
    if hits:
        cpath.write_text(json.dumps([h.__dict__ for h in hits]))
    time.sleep(1.2)
    return hits


_SITE_SUFFIX = re.compile(r"\s*[|\-–—:»]\s*(buddy4study|vidyasaarathi|scholarships? ?portal|apply now|online application|official (website|site)|home|"
                          r"[a-z0-9 .]*(foundation|university|ministry|government|india|portal)[a-z0-9 .]*)\s*$", re.I)
_NOISE = re.compile(r"\b(20\d{2}\s*[-–/]\s*(20)?\d{2}|20\d{2}|apply (now|online)|last date( to apply)?|eligibility|how to apply|benefits?|"
                    r"application (form|process|status)|check (details|here)|registration|deadline|dates?)\b", re.I)


def lead_name_from_title(title: str) -> str:
    t = title.strip()
    for _ in range(2):
        t = _SITE_SUFFIX.sub("", t)
    t = _NOISE.sub(" ", t)
    t = re.sub(r"[\s|:–—-]+$", "", re.sub(r"^[\s|:–—-]+", "", re.sub(r"\s{2,}", " ", t)))
    return t.strip()
