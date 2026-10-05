"""Discovery engine: best-first crawl over a link-scored frontier, web-search discovery, and aggregator-lead
resolution. Seeds are only *starting points*; which pages become scholarship candidates is decided by scoring
links, classifying every domain met, and judging each fetched page – not by per-site scrapers.
"""
from __future__ import annotations

import heapq
import itertools
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterator

from rapidfuzz import fuzz

from .. import config
from ..fetcher import FetchResult, Fetcher
from ..storage.repo import Repo
from ..util import canonical_url, distinctive_tokens, host_of, is_generic_name, looks_like_list_title, registered_domain
from .classifier import SourceClass, classify_url, is_denied, provider_matches_domain
from .link_scoring import page_kind_score, score_link
from .search import SearchHit, lead_name_from_title, web_search

_BINARY = re.compile(r"\.(jpg|jpeg|png|gif|svg|ico|css|js|zip|rar|mp4|mp3|avi|doc|docx|xls|xlsx|ppt|pptx|apk|exe)(\?|$)", re.I)


@dataclass
class Cand:
    url: str
    via: str
    prio: float
    depth: int                 # number of further link hops allowed
    origin: str
    parent: str = ""
    anchor: str = ""
    context: str = ""
    lead_ref: str = ""         # aggregator URL that first mentioned this (provenance only)
    hub: bool = False


@dataclass
class DiscoveredDoc:
    cand: Cand
    res: FetchResult
    kind_score: float
    page_id: int


def current_cycle(today=None) -> str:
    t = today or config.today()
    y = t.year if t.month >= 6 else t.year - 1
    return f"{y}-{str(y + 1)[2:]}"


class Discoverer:
    def __init__(self, fetcher: Fetcher, repo: Repo, run_id: int, log: Callable[[str], None] = print,
                 max_pages: int | None = None, offline: bool = False, skip_search: bool = False):
        self.f, self.repo, self.run_id, self.log = fetcher, repo, run_id, log
        cfg = config.settings()["crawl"]
        self.cfg = cfg
        self.max_pages = max_pages or int(cfg["max_pages_per_run"])
        self.min_detail = float(cfg["min_detail_score"])
        self.seeds = config.seed_config()
        self.offline = offline
        self.skip_search = skip_search or offline
        self._heap: list = []
        self._counter = itertools.count()
        self._seen: set[str] = set()
        self._queued: dict[str, Cand] = {}
        self.fetched = 0
        self.origin_count: Counter = Counter()
        self.domain_count: Counter = Counter()
        self.stats = Counter()
        self.leads: list[tuple[str, SearchHit]] = []

    # ---------------------------------------------------------------------------- frontier
    def enqueue(self, c: Cand) -> bool:
        cu = canonical_url(c.url)
        if cu in self._seen or cu in self._queued or is_denied(c.url) or _BINARY.search(c.url):
            return False
        if not c.url.lower().startswith(("http://", "https://")):
            return False
        self._queued[cu] = c
        heapq.heappush(self._heap, (-c.prio, next(self._counter), cu))
        host = host_of(c.url)
        sc = classify_url(c.url)
        self.repo.conn.execute(
            """INSERT OR IGNORE INTO discovery_candidates(run_id,url,canonical_url,discovered_via,parent_url,anchor_text,context_text,
               relevance,depth,domain,source_type,status,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.run_id, c.url, cu, c.via, c.parent, c.anchor[:200], c.context[:300], round(c.prio, 3), c.depth, host, sc.source_type, "queued", ""))
        self.stats["queued"] += 1
        return True

    def _set_status(self, c: Cand, status: str, reason: str = "") -> None:
        self.repo.conn.execute("UPDATE discovery_candidates SET status=?, reason=? WHERE run_id=? AND canonical_url=?",
                               (status, reason, self.run_id, canonical_url(c.url)))

    # ---------------------------------------------------------------------------- seeding
    def seed(self) -> None:
        for h in self.seeds.get("hubs", []):
            self.enqueue(Cand(h["url"], "hub", 2.0, int(h.get("depth", 1)), h.get("label", h["url"]), hub=True))
        if self.skip_search:
            return
        cur = current_cycle()
        lim = self.seeds.get("crawl_limits", {})
        per_q = int(lim.get("results_per_query", 10))
        for cat, qs in (self.seeds.get("queries") or {}).items():
            for q in qs:
                query = q.replace("{cur}", cur)
                hits = web_search(query, per_q)
                self.log(f"  search[{cat}] {query[:70]!r} -> {len(hits)} results")
                taken = 0
                for h in hits:
                    sc = classify_url(h.url)
                    if sc.source_type == "AGGREGATOR" or "buddy4study" in h.url or "vidyasaarathi" in h.url:
                        if cat == "aggregator_leads" or sc.source_type == "AGGREGATOR":
                            name = lead_name_from_title(h.title)
                            if (len(name) >= 8 and not is_generic_name(name) and not looks_like_list_title(h.title)
                                    and not re.search(r"buddy4study|vidya?sa?ara?thi|vidhyasarthi|shiksha|scholarshipsin", name, re.I)
                                    and len(self.leads) < int(lim.get("max_leads_total", 30))):
                                self.leads.append((name, h))
                            else:
                                self.stats["leads_ignored_generic"] += 1
                        continue
                    if is_denied(h.url):
                        continue
                    prio = 1.2 if sc.official else 0.7
                    if self.enqueue(Cand(h.url, f"search:{cat}", prio, 1 if sc.official else 0, f"search:{cat}", anchor=h.title)):
                        taken += 1
        self._provider_site_queries(cur, per_q)
        self._resolve_leads()

    def _provider_site_queries(self, cur: str, per_q: int) -> None:
        """Registry-driven discovery: for every known non-government provider domain, ask the search engine for that
        domain's scholarship / deadline pages. New programme pages on trusted domains are found without any per-page seed."""
        for kp in config.domain_config().get("known_providers", []):
            if kp["type"] not in ("CORPORATE_CSR", "NGO_TRUST", "INTERNATIONAL"):
                continue
            query = f"site:{kp['domain']} scholarship application last date {cur}"
            hits = web_search(query, per_q)
            n = 0
            for h in hits:
                sc = classify_url(h.url)
                if sc.official and not is_denied(h.url) and self.enqueue(Cand(h.url, "search:provider-site", 1.25, 1, f"provider:{kp['domain']}", anchor=h.title)):
                    n += 1
            self.log(f"  provider-site[{kp['domain']}] -> {len(hits)} results, {n} queued")

    def _resolve_leads(self) -> None:
        """Aggregator leads -> official pages. Aggregator content itself is never used as evidence."""
        seen_names: set[str] = set()
        for name, hit in self.leads:
            key = re.sub(r"[^a-z0-9]", "", name.lower())
            if key in seen_names:
                continue
            seen_names.add(key)
            found = None
            for q in (f'"{name}" official website scholarship', f"{name} apply official"):
                for h in web_search(q, 8):
                    sc = classify_url(h.url)
                    if sc.source_type == "AGGREGATOR" or is_denied(h.url):
                        continue
                    title_match = fuzz.token_set_ratio(name.lower(), (h.title or "").lower())
                    blob = f"{h.title} {h.snippet} {h.url}".lower()
                    if not any(t in blob for t in distinctive_tokens(name)):
                        continue                  # the result never mentions what makes this scholarship distinctive
                    domain_match = any(provider_matches_domain(tok, host_of(h.url)) for tok in distinctive_tokens(name)[:3] if len(tok) >= 5)
                    if (sc.official and title_match >= 55) or (sc.tier == "T4" and domain_match and title_match >= 50):
                        found = (h, sc)
                        break
                if found:
                    break
            if found:
                h, sc = found
                self.enqueue(Cand(h.url, f"aggregator-lead:{registered_domain(host_of(hit.url))}", 1.4, 1, f"lead:{name[:40]}",
                                  anchor=name, lead_ref=hit.url))
                self.log(f"  lead {name[:55]!r} -> resolved to official {registered_domain(host_of(h.url))}")
                self.stats["leads_resolved"] += 1
            else:
                self.repo.log_lead(self.run_id, name, hit.url, registered_domain(host_of(hit.url)), "no official page could be resolved")
                self.log(f"  lead {name[:55]!r} -> UNRESOLVED (kept out of the repository)")
                self.stats["leads_unresolved"] += 1
        self.repo.conn.commit()

    # ---------------------------------------------------------------------------- crawl
    def _origin_budget(self, c: Cand) -> int:
        if c.hub:
            return 70
        if c.via.startswith("hub"):
            return 70
        return 6

    def _origin_key(self, c: Cand) -> str:
        return c.origin

    def crawl(self) -> Iterator[DiscoveredDoc]:
        skipped: list = []
        while self._heap and self.fetched < self.max_pages:
            _, _, cu = heapq.heappop(self._heap)
            c = self._queued.get(cu)
            if c is None or cu in self._seen:
                continue
            dom = registered_domain(host_of(c.url))
            if self.origin_count[self._origin_key(c)] >= self._origin_budget(c) or self.domain_count[dom] >= 90:
                self._set_status(c, "skipped", "origin/domain budget reached")
                self.stats["budget_skipped"] += 1
                continue
            self._seen.add(cu)
            self.origin_count[self._origin_key(c)] += 1
            self.domain_count[dom] += 1
            res = self.f.fetch(c.url)
            self.fetched += 1
            self.stats["fetched"] += 1
            if not res.ok:
                self._set_status(c, f"fetch-failed:{res.status}", res.error[:120])
                self.stats["fetch_failed"] += 1
                continue
            kscore, hits = page_kind_score(res.title, res.headings, res.text)
            is_detail = kscore >= self.min_detail and not res.needs_ocr
            kind = "detail" if is_detail else ("hub" if c.hub else "other")
            if is_detail or c.hub or res.needs_ocr:
                page_id = self.repo.save_page(self.run_id, res, kind, kscore)
            else:
                page_id = -1
            self._set_status(c, f"fetched:{kind}", f"kind_score={kscore}")
            if res.needs_ocr and kscore == 0:
                self.stats["scanned_docs"] += 1
            self._expand_links(c, res)
            if is_detail or (res.needs_ocr and re.search(r"scholar|fellow|scheme", res.title + c.anchor, re.I)):
                self.stats["detail_candidates"] += 1
                yield DiscoveredDoc(c, res, kscore, page_id)
            if self.fetched % 15 == 0:
                self.repo.conn.commit()
        self.repo.conn.commit()

    def _expand_links(self, c: Cand, res: FetchResult) -> None:
        if c.depth <= 0:
            return
        page_dom = registered_domain(host_of(res.final_url or c.url))
        page_class = classify_url(res.final_url or c.url)
        n_added = 0
        for ln in res.links:
            if not ln.url.lower().startswith(("http://", "https://")) or _BINARY.search(ln.url):
                continue
            ldom = registered_domain(host_of(ln.url))
            sc = classify_url(ln.url)
            if sc.source_type == "AGGREGATOR":
                continue
            score = score_link(ln.text, ln.url, ln.context)
            same = ldom == page_dom
            is_pdf = ln.url.lower().split("?")[0].endswith(".pdf")
            if same:
                if score < (0.25 if is_pdf else 0.35):
                    continue
            else:
                if c.depth < 1 or score < 0.45 or (not sc.official and score < 0.6):
                    continue
                if not (page_class.official or c.hub):
                    continue
            prio = score + (0.15 if sc.official else 0) - 0.05 * (3 - c.depth) + (0.2 if c.hub else 0)
            child = Cand(ln.url, "link", prio, c.depth - 1 if same else min(c.depth - 1, 1), c.origin,
                         parent=res.final_url or c.url, anchor=ln.text, context=ln.context, hub=False)
            if self.enqueue(child):
                n_added += 1
        self.stats["links_enqueued"] += n_added
