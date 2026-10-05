# Scholarship Intelligence Crawler — Technical Note

## 1. Architecture and technology

`Discoverer → Fetcher → extractors → grounding → relevance gates → scoring/lifecycle → SQLite → FastAPI dashboard`.
`python -m scholarship_intel run` updates the database; `serve` displays it. Each run first **re-verifies every known
record against its official URL**, then discovers new candidates. A GitHub Actions schedule can repeat the crawl and
commit a snapshot; Vercel serves that committed SQLite file read-only (`snapshot` makes it one self-contained file).

Stack: Python 3.11, Requests, BeautifulSoup/lxml, pypdf, **Playwright** (free headless Chromium, used only when a plain
fetch returns almost no text — e.g. the Reliance Foundation portal is a JavaScript app), SQLite, FastAPI, plain JS.
Free LLM tiers: Groq (`openai/gpt-oss-120b`, picked from the provider's `/models` list), Gemini (`gemini-3.1-flash-lite`),
local Ollama as an offline fallback. **LLMs only propose `{value, verbatim quote}`; they never assign confidence or status.**
No paid service is used.

## 2. Discovery (not a URL → scraper list)

`config/seeds.yaml` holds official hubs (NSP, UGC, AICTE, DST, Chevening, Commonwealth, USIEF, foundations…), topical and
deadline-oriented search queries — no scholarship records. On top of that, discovery is **registry-driven**: for every
known provider domain it runs a `site:` query for scholarship/deadline pages, so programme pages are found without
per-page seeds. Aggregator pages and search snippets are only *leads*: each lead must resolve to an official page or it is
logged (`unresolved_leads`, 61 so far) and never stored. A best-first frontier scores links (keywords, row context, file
type), enforces per-origin budgets, and a per-domain circuit breaker stops dead hosts from stalling a run.

**Source classification** (every domain → type + tier + reason, stored in `sources`, 31 domains so far): government TLDs
(T1, 1.00); academic TLDs (T2, 0.95) — **kept only if the extracted provider actually owns the domain**, which rejects
coaching-site articles on `.ac.in`; a curated registry of verified provider domains (T3, 0.95); provider-name↔domain
match with self-identification (T3h, 0.80); unknown (T4) and aggregators (T5) can never be official.
**Relevance gates** reject listing pages (≥8 separate closing-date statements), news/press-release pages, foreign-only
programmes with no sign they are open to Indian students, and junk titles ("Q1. What are the eligibility criteria?").
The same programme found on two official domains is merged into one record.

## 3. Extraction

Text is taken from HTML or PDF (scanned PDFs are flagged, not guessed). A deterministic **rule extractor** (dates, ₹/lakh
amounts, income/age limits, categories, levels, sections such as *Eligibility* / *Documents*) and up to two **LLMs**
(strict JSON, `null` when absent, quote ≤ 400 chars) run independently. If the main page has no closing date, a
**second-hop step** reads same-domain FAQ/timeline/notice pages — a date is accepted only if that page names the
scholarship and falls in the current cycle, and the evidence then points at that page.

## 4. Verification and anti-hallucination

A claim becomes a fact only if (1) its quote is **found verbatim** in the stored page text (fuzzy ≥ 92 only for PDF
line-wrap noise), with character offsets saved, and (2) the value **follows from the quote**: dates parse to the same
date, amounts/income/age figures appear in it, enum members (SC, ST, OBC, PwD…) are justified by its wording, URLs are
among the page's links, and text fields must be *about* their field and not first-person narrative (this caught a scholar
testimonial filed as "selection process"). Everything else is discarded and logged in `rejected_extractions`
(498 audit rows). "Not specified" is itself checked: it is credited only if no wording in the document hints at the
field. Trace chain: `scholarships → field_evidence(quote, offsets) → pages.text → official URL`.

## 5. Confidence score (deterministic; `docs/CONFIDENCE.md`)

Nine components (weights): source authority 20 · live presence 10 · application URL 8 · eligibility support 14 ·
deadline support 12 · benefit support 6 · freshness 10 · extractor agreement 10 · traceability 10. Every point and its reason
is stored (`confidence_breakdown`) and shown under "Why this score?". Then: −3 per unresolved conflict on a critical field;
**caps** (non-official source ≤ 59, name not on page ≤ 40, page unreadable ≤ 50); **gates** (< 2 independent extractors ≤ 90;
a critical field without grounded evidence — name, provider, benefit, eligibility, closing date *or* open-window statement,
level — ≤ 94). `VERIFIED` ⇔ final score ≥ 95. Calibration choices, stated openly: a dated deadline earns 1.0 but "applications
are now open" without a date only 0.7; registry provider domains earn the same authority as academic domains (0.95).

## 6. Change detection and lifecycle

Page text is hashed; unchanged pages reuse stored facts (and a run never replaces a stored two-extractor result with a
weaker one when a provider is down). Field-level differences go to `changes` with old value, new value, detection time,
source URL and old/new evidence — **never overwritten**. If the source hash is unchanged but extractor output differs, it is
labelled `EXTRACTION_CORRECTION`, not a source change. Statuses: `ACTIVE`, `EXPIRING_SOON` (≤ 14 days), `EXPIRED`,
`REVIEW_REQUIRED`, `NO_LONGER_VERIFIABLE` (404/410, name gone, or repeated failures); every transition is in `status_history`.
Real official pages did not change during the hours between my crawls, so change handling is demonstrated on a **separate
copy** (`demo-changes` → `data/atlas_demo.db`): cached official pages are replayed with a few explicitly **SIMULATED** edits
(deadline extended or moved into the past, an amount revised, pages turned into 404) through the unchanged pipeline, plus
records re-found as NEW. The real database contains no simulated value; unit tests cover the same chain.

## 7. What the crawler produced — and where it falls short

Real database (`data/atlas.db`, 14 crawl runs): **54 records, all from official sources**: Government 19, International 16,
NGO/Trust 14, Corporate CSR 3, University 2. Lifecycle: 7 ACTIVE, 4 EXPIRING_SOON, 12 EXPIRED, 3 NO_LONGER_VERIFIABLE,
28 REVIEW_REQUIRED. Average confidence 84.5%.

**Only 3 records reach VERIFIED (≥ 95%): two Reliance Foundation scholarships and the FFE–Info Edge scholarship. The brief's
minimum of 10 is not met.** Most official scheme pages (NSP/AICTE/UGC guidelines) publish no dated deadline, so they correctly
stay REVIEW_REQUIRED; many corporate programmes apply through third-party portals or JavaScript pages; and free-tier
Groq/Gemini limits ("high demand", 20-minute rate limits) sometimes leave only one extractor, which caps a record at 90. I did not
lower the threshold to reach the count. No real source-field changes were observed in the crawl window; lifecycle changes
(ACTIVE → EXPIRING_SOON/EXPIRED, removals) are real, field-change examples come from the labelled demo. Other limits: scanned
PDFs are unreadable (no OCR), some NIC-hosted sites time out from some networks, and extractor choices on multi-line lists
can still be imperfect — which is why every field carries its quote for human review.
