"""End-to-end pipeline: Discover → Crawl → Extract → Ground → Verify → Score → Store → Update.

One `Pipeline.run()` is one crawl run. Run it repeatedly: known scholarships are re-verified first (change / expiry /
removal detection), then discovery looks for new ones.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import Counter
from typing import Callable

from . import config
from .discovery.classifier import classify_url, upgrade_by_provider
from .discovery.discoverer import Cand, DiscoveredDoc, Discoverer
from .discovery.link_scoring import page_kind_score
from .extraction import llm_extractor, rules
from .extraction.llm import LLMRouter
from .extraction.models import Extraction, Grounded
from .extraction import vocab as V
from .fetcher import FetchResult, Fetcher
from .storage.repo import Record, Repo, load_facts, make_key
from .textproc import Haystack, fold_text
from .util import canonical_url, host_of, is_generic_name, looks_like_list_title, registered_domain
from .verification import grounding, lifecycle, scoring


class Pipeline:
    def __init__(self, conn: sqlite3.Connection, fetcher: Fetcher, router: LLMRouter, log: Callable[[str], None] = print,
                 max_pages: int | None = None, skip_search: bool = False, mode: str = "live", reverify_only: bool = False,
                 discover: bool = True):
        self.conn, self.f, self.router, self.log = conn, fetcher, router, log
        self.repo = Repo(conn)
        self.max_pages, self.skip_search, self.mode = max_pages, skip_search, mode
        self.reverify_only = reverify_only
        self.do_discover = discover
        self.stats: Counter = Counter()
        self.processed_urls: set[str] = set()
        self.saved_ids: set[int] = set()
        self.run_id = 0

    # ================================================================== public
    def run(self, label: str = "") -> dict:
        self.run_id = self.repo.start_run(self.mode, label, notes=f"llm: {self.router.describe()}")
        self.log(f"▶ run #{self.run_id} mode={self.mode} as_of={config.today()} llm=[{self.router.describe()}]")
        self._reverify_existing()
        if self.do_discover and not self.reverify_only:
            self._discover_new()
        stats = dict(self.stats)
        stats["fetch"] = self.f.stats
        stats["llm_calls"] = self.router.calls
        stats["llm_failures"] = self.router.failures[:10]
        self.repo.finish_run(self.run_id, stats)
        self.log(f"■ run #{self.run_id} done: {json.dumps({k: v for k, v in stats.items() if k not in ('fetch', 'llm_failures')})}")
        return stats

    # ================================================================== phase 0: re-verify known records
    def _reverify_existing(self) -> None:
        rows = self.conn.execute("SELECT * FROM scholarships ORDER BY id").fetchall()
        if not rows:
            return
        self.log(f"● re-verifying {len(rows)} known scholarship(s) against their official sources")
        for row in rows:
            res = self.f.fetch(row["official_url"])
            if self.f.mode == "cache" and res.error == "not in offline cache":
                self.stats["skipped_offline_cache_miss"] += 1       # replay limitation, not a source outage
                self.processed_urls.add(canonical_url(row["official_url"]))
                continue
            self.processed_urls.add(canonical_url(row["official_url"]))
            self.processed_urls.add(canonical_url(res.final_url or row["official_url"]))
            self.stats["reverified"] += 1
            self._process(res, existing=row, via=row["discovered_via"] or "re-verification", kscore=None)

    # ================================================================== phase 1: discovery of new scholarships
    def _discover_new(self) -> None:
        disc = Discoverer(self.f, self.repo, self.run_id, self.log, max_pages=self.max_pages, skip_search=self.skip_search)
        self.log("● discovery: seeding frontier (hubs + web search + aggregator leads)")
        disc.seed()
        for cu in self.processed_urls:
            disc._seen.add(cu)
        self.log(f"  frontier size {len(disc._heap)}; crawling up to {disc.max_pages} pages")
        for doc in disc.crawl():
            via = doc.cand.via + (f" ← {doc.cand.lead_ref}" if doc.cand.lead_ref else "")
            outcome = self._process(doc.res, existing=None, via=f"{via} [{doc.cand.origin}]", kscore=doc.kind_score, page_id=doc.page_id)
            disc._set_status(doc.cand, f"processed:{outcome}")
        for k, v in disc.stats.items():
            self.stats[f"discovery_{k}"] = v
        self.conn.commit()

    # ================================================================== core: one document -> one record
    def _extract_all(self, res: FetchResult) -> list[Extraction]:
        out = [rules.extract(res.text, res.title, res.headings, res.links, res.final_url or res.url)]
        llm_out = []
        for prov in self.router.providers_for_extraction(2):
            ex = llm_extractor.llm_extract(self.router, prov, res.text, res.title, res.headings, res.links, res.final_url or res.url)
            if ex.error:
                self.stats["llm_extract_errors"] += 1
                self.log(f"    ! {ex.extractor}: {ex.error[:120]}")
                continue
            llm_out.append(ex)
            self.stats["llm_extractions"] += 1
        return llm_out + out          # LLMs first (priority), rules last

    def _process(self, res: FetchResult, existing, via: str, kscore: float | None, page_id: int | None = None) -> str:
        today = config.today()
        url = existing["official_url"] if existing is not None else (res.final_url or res.url)
        sc = classify_url(res.final_url or url)

        # --------------------------------------------------------------- fetch failure paths
        if not res.ok:
            if existing is None:
                self.stats["skipped_fetch_failed"] += 1
                return "fetch-failed"
            if page_id is None:
                page_id = self.repo.save_page(self.run_id, res, "reverify", 0.0)
            st = lifecycle.determine(fetch_ok=False, fetch_status=res.status, fetch_error=res.error, gone=res.gone, transient=res.transient,
                                     needs_ocr=res.needs_ocr, name_found=True, is_scholarship_page=True, closing=None, has_rolling_note=False,
                                     text="", prior_miss_count=existing["miss_count"] or 0, today=today, name=existing["name"])
            self.repo.mark_missing(existing["id"], self.run_id, st, url, simulated=res.simulated)
            self.saved_ids.add(existing["id"])
            self.stats[f"status_{st.status}"] += 1
            self.log(f"  ✗ {existing['name'][:60]!r}: {st.status} – {st.reason}")
            return st.status.lower()

        if res.needs_ocr and existing is None:
            self.stats["skipped_scanned"] += 1
            self.log(f"  ~ skipped scanned-image document {res.final_url[-60:]}")
            return "scanned"

        text = res.text
        if page_id is None:
            kscore2, _ = page_kind_score(res.title, res.headings, text)
            page_id = self.repo.save_page(self.run_id, res, "reverify" if existing is not None else "detail", kscore2)
            kscore = kscore2 if kscore is None else kscore
        kscore = kscore or 0.0
        hay = Haystack(text)

        # --------------------------------------------------------------- extraction (or reuse when the page is unchanged)
        reused = False
        extractions: list[Extraction] = []
        rejected_all = []
        if existing is not None and existing["content_hash"] == res.content_hash and existing["facts_json"]:
            facts, absent_prev, agreement, conflicts, n_extractors, extractors = load_facts(existing["facts_json"])
            reused = True
            is_scholarship = True
            self.stats["unchanged_pages"] += 1
        else:
            extractions = self._extract_all(res)
            # If a model actually inspected the page, its specific-programme
            # classification takes precedence over the broad keyword rules.
            # A rules-only run still works when no model is available.
            model_flags = [e.is_scholarship for e in extractions
                           if e.extractor.startswith("llm:") and e.is_scholarship is not None]
            rule_flags = [e.is_scholarship for e in extractions if e.is_scholarship is not None]
            is_scholarship = any(model_flags) if model_flags else any(rule_flags)
            per = []
            for ex in extractions:
                g, rej = grounding.ground_extraction(ex, hay, res.links)
                per.append((ex.extractor, g))
                rejected_all.extend(rej)
            facts, agreement, conflicts = grounding.merge_extractions(per)
            n_extractors = len(extractions)
            extractors = [e.extractor for e in extractions]
            absent_prev = {}

        # --------------------------------------------------------------- identity
        name_claim = facts.get("name")
        if not name_claim or not is_scholarship:
            if existing is not None:
                nf = bool(name_claim)
                st = lifecycle.determine(fetch_ok=True, fetch_status=res.status, fetch_error="", gone=False, transient=False, needs_ocr=False,
                                         name_found=nf, is_scholarship_page=is_scholarship, closing=None, has_rolling_note=False, text=text,
                                         prior_miss_count=existing["miss_count"] or 0, today=today, name=existing["name"])
                self.repo.mark_missing(existing["id"], self.run_id, st, url, simulated=res.simulated)
                self.saved_ids.add(existing["id"])
                self.stats[f"status_{st.status}"] += 1
                self.log(f"  ✗ {existing['name'][:60]!r}: {st.status} – {st.reason}")
                return st.status.lower()
            self.stats["rejected_not_scholarship"] += 1
            return "not-a-scholarship"
        name = name_claim.value
        if existing is None and (is_generic_name(name) or looks_like_list_title(name)):
            self.stats["rejected_generic_title"] += 1
            return "generic-title"
        provider = facts["provider"].value if "provider" in facts else None

        # --------------------------------------------------------------- source authority
        sc = upgrade_by_provider(sc, provider, res.title, text)
        self.repo.upsert_source(sc, self.run_id)
        if sc.source_type == "AGGREGATOR":
            self.stats["rejected_aggregator"] += 1
            return "aggregator"
        if existing is None and not sc.official:
            # Not an official source => never a record. Kept only as a lead for audit.
            self.repo.log_lead(self.run_id, name, res.final_url or url, sc.domain, f"not an official source: {sc.reason}")
            self.stats["rejected_non_official"] += 1
            return "non-official"
        key = make_key(name, res.final_url or url)
        official_url = existing["official_url"] if existing is not None else (res.final_url or res.url)
        if existing is None:
            dup = self.repo.find_existing(official_url, key, name, sc.domain)
            if dup is not None:
                self.stats["duplicates_skipped"] += 1
                return "duplicate"
        elif existing["id"] in self.saved_ids:
            return "duplicate"

        # --------------------------------------------------------------- absence verification for un-grounded fields
        absent: dict[str, tuple[bool, str]] = {}
        for f in V.ALL_FIELDS:
            if f in facts:
                continue
            if f in ("name",):
                continue
            key_f = {"amount": "amount", "income": "income", "age": "age"}.get(f, f)
            absent[f] = grounding.absence_check(key_f, text)
        if n_extractors < 2:
            pass   # single extractor: scoring discounts absence credit accordingly

        # --------------------------------------------------------------- application URL probe
        apply_check = None
        if "application_url" in facts:
            au = facts["application_url"].value
            pr = self.f.probe(au)
            asc = classify_url(au)
            apply_check = {"url": au, "status": pr.get("status"),
                           "trusted": asc.official or registered_domain(host_of(au)) == registered_domain(host_of(official_url))}

        # --------------------------------------------------------------- name location, closing date
        fname = fold_text(name)
        head = fold_text(" ".join([res.title] + res.headings[:8]) + " " + text[:500])
        name_loc = "title" if fname and fname in head else ("body" if fname and fname in hay.folded else "missing")
        closing = None
        if "closing_date" in facts:
            try:
                closing = dt.date.fromisoformat(facts["closing_date"].value)
            except ValueError:
                closing = None

        inp = scoring.ScoreInput(
            source=sc, fetch_ok=True, fetch_status=res.status, needs_ocr=res.needs_ocr, name_location=name_loc, page_kind=kscore,
            facts=facts, absent=absent, n_extractors=n_extractors, agreement=agreement, conflicts=conflicts, apply_check=apply_check,
            page_text=text, today=today, closing_date=closing, tls_verified=res.tls_verified)
        score = scoring.compute(inp)

        status = lifecycle.determine(
            fetch_ok=True, fetch_status=res.status, fetch_error="", gone=False, transient=False, needs_ocr=res.needs_ocr,
            name_found=name_loc != "missing", is_scholarship_page=True, closing=closing, has_rolling_note="deadline_note" in facts and "closing_date" not in facts,
            text=text, prior_miss_count=0, today=today, name=name)

        official_verified = bool(sc.official and name_loc != "missing" and not res.needs_ocr)
        rec = Record(key=key, name=name, provider=provider, source=sc, official_url=official_url, primary_page_id=page_id, discovered_via=via,
                     facts=facts, absent=absent, agreement=agreement, conflicts=conflicts, n_extractors=n_extractors, score=score,
                     status=status, content_hash=res.content_hash, simulated=res.simulated, official_verified=official_verified,
                     extractors=extractors)
        sid, changes = self.repo.save(self.run_id, rec)
        self.saved_ids.add(sid)
        if rejected_all:
            self.repo.log_rejected(self.run_id, key, name, page_id, rejected_all)
            self.stats["rejected_extractions"] += len(rejected_all)
        self.conn.commit()

        kinds = {c["type"] for c in changes}
        if "NEW" in kinds:
            self.stats["new"] += 1
            tag = "NEW"
        elif any(c["type"] in ("FIELD_CHANGED", "FIELD_ADDED", "FIELD_UNSUPPORTED") for c in changes):
            self.stats["changed"] += 1
            tag = "CHANGED"
        elif any(c["type"] == "STATUS_CHANGED" for c in changes):
            self.stats["status_changed"] += 1
            tag = "STATUS"
        else:
            self.stats["unchanged"] += 1
            tag = "same"
        self.stats[f"label_{score.label}"] += 1
        self.stats[f"status_{status.status}"] += 1
        self.log(f"  {'↻' if reused else '+'} [{tag:7}] {name[:58]!r:62} {sc.source_type:13} {score.display:5.1f}% {score.label:15} {status.status}")
        for c in changes:
            if c["type"] in ("FIELD_CHANGED", "FIELD_UNSUPPORTED", "FIELD_ADDED"):
                self.log(f"        ⚑ {c['type']}: {c['field']}: {c.get('old')!r} → {c.get('new')!r}")
        return tag.lower()
