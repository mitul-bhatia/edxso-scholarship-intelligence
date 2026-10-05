# Scholarship Intelligence Crawler — technical note

## 1. Architecture and technology

The Python pipeline runs `Discoverer → Fetcher → extractors → grounding → scoring/lifecycle → SQLite`. A FastAPI application reads the resulting database and serves the dashboard and inspection API. The crawler is separate from the web process: `python -m scholarship_intel run` updates the database; `python -m scholarship_intel serve` shows its current contents. GitHub Actions can invoke a scheduled run and publish the database as a workflow artifact. The Vercel deployment serves a committed, read-only SQLite snapshot; updates require a new snapshot commit and deployment. This separation is necessary because Vercel functions do not provide durable filesystem writes.

The stack is Python, Requests, Beautiful Soup, pypdf, SQLite, FastAPI, and plain JavaScript. It needs no paid service. The rule extractor works without credentials. Optional Groq, Gemini, or locally installed Ollama models propose structured fields, each with a source quote. A language model is never permitted to assign confidence or status.

## 2. Discovery, extraction, and authenticity

`config/seeds.yaml` contains official hub URLs and topical search queries, not scholarship records. Search results and aggregator pages enter a lead queue. The discoverer scores candidate links, follows promising pages, and attempts to resolve aggregator leads to a provider or government page. The domain classifier uses government and academic suffixes, a small provider-domain registry, an aggregator deny-list, and a provider-to-domain ownership check. Unresolved and non-official leads are logged, but not stored as scholarships.

The fetcher observes `robots.txt`, limits request rate by domain, retries transient failures, and converts HTML or text-layer PDFs into stored text snapshots. The rule extractor identifies names, dates, amounts, requirements, and links. Optional LLM extraction returns `{value, quote}` claims. Grounding looks up the quote in the fetched official text, records character offsets, and checks that dates, amounts, categories, and URLs follow from the quote. Rejected claims are retained in `rejected_extractions` for audit. Missing fields are displayed as **Not specified**. Scanned PDFs and unreadable JavaScript pages stay unverified.

The trace path is `scholarships → field_evidence → pages → official_url`. Each evidence row has the extracted value, source quote, source URL, page ID, offsets, extractor name, and verification timestamp. A reviewer can open a scholarship in the dashboard and select a field's evidence to see its context.

## 3. Verification and confidence

The score is a deterministic sum of nine components, configured in `config/settings.yaml`: source authority (20), live presence (10), application URL (8), eligibility support (14), deadline support (12), benefit support (6), freshness (10), extractor agreement (10), and traceability (10). Each component's points and reason are saved in `confidence_breakdown`. Conflicting critical fields incur a penalty. Hard caps prevent a non-official source, unreadable page, missing name, absent critical fields, or single extractor from reaching VERIFIED. `VERIFIED` requires a final score of at least 95%; all other records are `REVIEW_REQUIRED`. The detailed calculation is in `docs/CONFIDENCE.md`.

The confidence label and lifecycle status answer different questions. A record can be supported by a primary source while its application window is expired. Lifecycle logic uses the grounded closing date, an official rolling-window statement, discontinuation text, HTTP 404/410, and consecutive fetch failures to decide `ACTIVE`, `EXPIRING_SOON`, `EXPIRED`, `REVIEW_REQUIRED`, or `NO_LONGER_VERIFIABLE`.

## 4. Repeated runs and change detection

Every run first revisits known official URLs, then discovers new candidates. Unchanged content reuses stored facts, while changed content is extracted and verified again. Field-level differences are written to `changes` with old and new values, detection time, official URL, old and new evidence, and run ID. If a code or extractor revision changes a value while the source text hash stays the same, the history labels it an **extraction correction**, not a source change. Status changes are also retained. The old value is not silently overwritten. `crawl_runs` records the mode and statistics for each run.

Real sites may not change during a short assessment. `python -m scholarship_intel demo-changes` copies the real database, replays cached official pages with explicitly **simulated** source edits, and runs the same extraction, scoring, diff, and lifecycle code. The demo database and dashboard label those changes. The production database never receives simulated facts. This proves the mechanism without presenting fabricated changes as real-world observations.

## 5. Limits and inspection

Page access, rate limits, unsupported PDF scans, and sparse official deadlines constrain the number of records and high scores. The system leaves unsupported fields empty rather than completing them from search snippets. Inspect the actual output with `python -m scholarship_intel stats`, the dashboard, `data/atlas.db`, or the CSV/JSON export in `data/sample/`. Run `python -m scholarship_intel run` twice to observe live re-verification; use the labelled replay to inspect deterministic change handling immediately.
