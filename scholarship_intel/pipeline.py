"""End-to-end pipeline: Discover → Crawl → Extract → Ground → Verify → Score → Store → Update.

One `Pipeline.run()` is one crawl run. Run it repeatedly: known scholarships are re-verified first (change / expiry /
removal detection), then discovery looks for new ones.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
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
from .util import canonical_url, distinctive_tokens, host_of, is_generic_name, looks_like_list_title, registered_domain
from .verification import grounding, lifecycle, scoring
from .verification.relevance import relevance_gate


class Pipeline:
    def __init__(self, conn: sqlite3.Connection, fetcher: Fetcher, router: LLMRouter, log: Callable[[str], None] = print,
                 max_pages: int | None = None, skip_search: bool = False, mode: str = "live", reverify_only: bool = False,
                 discover: bool = True, force_reextract: bool = False, reextract_weak: bool = False):
        self.conn, self.f, self.router, self.log = conn, fetcher, router, log
        self.repo = Repo(conn)
        self.max_pages, self.skip_search, self.mode = max_pages, skip_search, mode
        self.reverify_only = reverify_only
        self.do_discover = discover
        self.force_reextract = force_reextract
        self.reextract_weak = reextract_weak
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
        for r in self.conn.execute("SELECT id,name FROM scholarships").fetchall():          # self-cleaning: junk titles from earlier runs
            if is_generic_name(r["name"]) or looks_like_list_title(r["name"]):
                self.repo.retire(r["id"], self.run_id, "title is a question / list heading / generic label, not a programme name")
                self.stats["retired"] += 1
                self.log(f"  ⌫ retired junk title {r['name'][:60]!r}")
        for lost, kept in self.repo.dedupe(self.run_id):
            self.stats["deduplicated"] += 1
            self.log(f"  ⌫ duplicate record #{lost} merged into #{kept}")
        self.f.close()
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
    def _needs_reextraction(self, existing, res: FetchResult) -> bool:
        if not existing["facts_json"] or existing["content_hash"] != res.content_hash:
            return True
        if self.force_reextract:
            return True
        if self.reextract_weak:
            try:
                n = json.loads(existing["facts_json"]).get("n_extractors", 0)
            except ValueError:
                return True
            return n < 2 or 80 <= (existing["confidence"] or 0) < 95
        return False

    def _extract_all(self, res: FetchResult) -> list[Extraction]:
        out = [rules.extract(res.text, res.title, res.headings, res.links, res.final_url or res.url)]
        llm_out: list[Extraction] = []
        done: set[str] = set()
        for attempt in range(2):
            for prov in self.router.providers_for_extraction(2):
                if prov.name in done:
                    continue
                ex = llm_extractor.llm_extract(self.router, prov, res.text, res.title, res.headings, res.links, res.final_url or res.url)
                if ex.error:
                    self.stats["llm_extract_errors"] += 1
                    self.log(f"    ! {ex.extractor}: {ex.error[:100]}")
                    continue
                llm_out.append(ex)
                done.add(prov.name)
                self.stats["llm_extractions"] += 1
            if len(llm_out) >= 2 or attempt == 1:
                break
            wait = self.router.seconds_until_ready()          # a provider is only briefly cooling down: wait once, then retry
            if wait is None:
                break
            self.log(f"    … waiting {wait:.0f}s for a rate-limited provider, then retrying")
            time.sleep(wait + 1)
        return llm_out + out          # LLMs first (priority), rules last

    _SUPPORT_LINK = re.compile(r"faq|frequently|dates?|timeline|deadline|schedule|how.?to.?apply|notice|announcement|guideline|brochure|instruction|important|apply", re.I)

    def _enrich_deadline(self, res: FetchResult, name: str, facts: dict) -> None:
        """Second-hop corroboration. Programmes often publish eligibility on one page and dates on another (FAQ, timeline,
        notice). If the primary page has no closing date, look at same-domain supporting pages; accept a date only if the
        page names the scholarship, the date is in the current cycle, and it is grounded in that page's own text.
        The evidence then points at the supporting page, so the trace stays exact."""
        if "closing_date" in facts:
            return
        today = config.today()
        ay = today.year if today.month >= 6 else today.year - 1
        lo, hi = dt.date(ay, 6, 1), dt.date(ay + 2, 6, 30)
        base = res.final_url or res.url
        dom = registered_domain(host_of(base))
        toks = distinctive_tokens(name)
        seen, tried = {canonical_url(base)}, 0
        for ln in res.links:
            cu = canonical_url(ln.url)
            if cu in seen or registered_domain(host_of(ln.url)) != dom or not self._SUPPORT_LINK.search(f"{ln.text} {ln.url}"):
                continue
            seen.add(cu)
            tried += 1
            if tried > 5:
                return
            sup = self.f.fetch(ln.url)
            if not sup.ok or sup.needs_ocr:
                continue
            folded = fold_text(sup.text)
            if not (sum(t in folded for t in toks) >= min(2, len(toks))):
                continue                                           # the supporting page must be about THIS scholarship
            ex = rules.extract(sup.text, sup.title, sup.headings, sup.links, sup.final_url)
            sub = Extraction("rules:supporting-page", {k: v for k, v in ex.claims.items() if k == "closing_date"})
            g, _ = grounding.ground_extraction(sub, Haystack(sup.text), sup.links)
            c = g.get("closing_date")
            if not c:
                continue
            try:
                d = dt.date.fromisoformat(c.value)
            except ValueError:
                continue
            if not (lo <= d <= hi):
                continue                                           # a date from an older / unrelated cycle is not evidence
            c.page_id = self.repo.save_page(self.run_id, sup, "supporting", 0.0)
            c.source_url = sup.final_url or sup.url
            facts["closing_date"] = c
            self.stats["deadlines_from_supporting_pages"] += 1
            return

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
            self.repo.mark_missing(existing["id"], self.run_id, st, url)
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

        # --------------------------------------------------------------- relevance gates (single programme? open to Indian students?)
        gate = relevance_gate(host_of(res.final_url or url), sc, text, res.final_url or url)
        if gate:
            if existing is not None:
                self.repo.retire(existing["id"], self.run_id, gate)
                self.stats["retired"] += 1
                self.log(f"  ⌫ retired {existing['name'][:55]!r}: {gate}")
                return "retired"
            self.stats["rejected_irrelevant"] += 1
            return "irrelevant"

        # --------------------------------------------------------------- extraction (or reuse when the page is unchanged)
        reused = False
        extractions: list[Extraction] = []
        rejected_all = []
        if existing is not None and not self._needs_reextraction(existing, res):
            facts, absent_prev, agreement, conflicts, n_extractors, extractors = load_facts(existing["facts_json"])
            facts, revalidated_out = grounding.revalidate(facts)
            rejected_all.extend(revalidated_out)
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
            if existing is not None and existing["facts_json"] and existing["content_hash"] == res.content_hash:
                prev = load_facts(existing["facts_json"])
                if n_extractors < prev[4]:
                    # Same source text, but a provider was unavailable this time: never replace a cross-checked result with a weaker one.
                    facts, absent_prev, agreement, conflicts, n_extractors, extractors = prev
                    reused = True
                    is_scholarship = True
                    self.stats["kept_prior_stronger_extraction"] += 1
                    self.log(f"    = kept previous {prev[4]}-extractor result for {existing['name'][:50]!r} (providers unavailable now)")

        # --------------------------------------------------------------- second-hop deadline corroboration
        if facts.get("name") and is_scholarship:
            facts = {k: v for k, v in facts.items() if not (k == "closing_date" and v.source_url)}   # re-derive supporting dates every run
            self._enrich_deadline(res, facts["name"].value, facts)

        # --------------------------------------------------------------- identity
        name_claim = facts.get("name")
        if not name_claim or not is_scholarship:
            if existing is not None:
                nf = bool(name_claim)
                st = lifecycle.determine(fetch_ok=True, fetch_status=res.status, fetch_error="", gone=False, transient=False, needs_ocr=False,
                                         name_found=nf, is_scholarship_page=is_scholarship, closing=None, has_rolling_note=False, text=text,
                                         prior_miss_count=existing["miss_count"] or 0, today=today, name=existing["name"])
                self.repo.mark_missing(existing["id"], self.run_id, st, url)
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
                     status=status, content_hash=res.content_hash, official_verified=official_verified,
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
