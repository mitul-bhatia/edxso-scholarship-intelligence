# Atlas Scholarship Intelligence Crawler

An automatic crawler that **discovers → crawls → extracts → verifies → scores → stores → updates** scholarship
information for Indian students, built for the Atlas Funding repository.

> **Show us what your crawler found.** → `python -m scholarship_intel serve` and open <http://127.0.0.1:8000>,
> or inspect `data/atlas.db` with any SQLite browser.

**Where the data comes from:** `config/seeds.yaml` supplies official portal starting points and search queries. The
crawler follows links and search leads to provider, government, university, and foundation pages. It downloads those
pages, extracts only source-supported fields, and writes the result to `data/atlas.db`. The repository does not contain
a hand-written list of scholarship facts. Open a dashboard record and click a field's evidence button to see the
official URL and the exact page passage behind it. Search results and aggregators are discovery leads, never proof.

**Where the UI is:** `python -m scholarship_intel serve` starts the FastAPI API and dashboard at
<http://127.0.0.1:8000>. The `127.0.0.1` address works on your own computer only. The dashboard source is in
`scholarship_intel/api/static/`; its data comes from the crawler's SQLite database through `/api/*` routes.

Design principle: **accuracy over quantity.** Aggregators and search results are only *leads*. A record exists only
when its facts can be quoted from text fetched from an official source, and the confidence score is *computed from
evidence* — never produced by a language model.

```
 seeds (official hubs · web-search queries · aggregator leads)
   │ DISCOVER   best-first crawl, link scoring, domain classification, lead → official-page resolution
   ▼
 CRAWL          robots.txt · rate-limit · retries · TLS fallback · HTML + PDF → text snapshot (stored)
   ▼
 EXTRACT        rule extractor  +  LLM(s) (Groq / Gemini / Ollama) returning {value, verbatim quote}
   ▼
 GROUND         quote must be found in the page text (offsets stored) and the value must follow from the quote
   ▼
 VERIFY/SCORE   9 evidence components, conflict penalty, hard caps  →  VERIFIED (≥95) / REVIEW_REQUIRED
   ▼
 STORE + DIFF   SQLite; field-level change history (old, new, when, source, evidence); lifecycle status
   ▼
 DASHBOARD      FastAPI + static SPA: KPIs, search, "Why this score?", evidence trace, change history
```

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # paste free API keys (all optional – see "LLMs" below)

python -m scholarship_intel run                 # one crawl (discovery + re-verification of known records)
python -m scholarship_intel serve               # dashboard on http://127.0.0.1:8000
python -m scholarship_intel list                # plain-text table of what was found
python -m scholarship_intel audit               # actual assignment minimums, with honest pass/fail
python -m scholarship_intel run                 # run it AGAIN – Run 2 detects changes / expiry / removal
python -m scholarship_intel demo-changes        # build data/atlas_demo.db: labelled change/expiry demonstration
ATLAS_DB=data/atlas_demo.db python -m scholarship_intel serve --port 8001
python -m pytest -q                             # run the test suite
```

Useful flags: `--llm off|auto|groq,gemini,ollama` · `--max-pages N` · `--no-search` · `--offline` (replay from the
on-disk cache) · `--reverify-only` · `--as-of YYYY-MM-DD` (logical "today" for lifecycle logic).
Repeat automatically: `python -m scholarship_intel schedule --every-hours 24`, or cron / GitHub Actions
(`.github/workflows/crawl.yml` is included).

### Credentials and submission

No credentials are needed for official websites, search discovery, SQLite, or the dashboard. `GROQ_API_KEY` and
`GEMINI_API_KEY` are **optional free-tier extractor keys**: create them at the provider links below, paste them into
your local `.env`, and never commit `.env`. Ollama is another free option that runs on your computer and needs no key.
These models propose values with quotes; the grounding and scoring code decides what is accepted. The Vercel dashboard
does not need either LLM key because it only displays the already-crawled snapshot.

Submission package: the GitHub repository, the live dashboard URL (or these local commands), the real
`data/atlas.db` and `data/sample/` exports, and `docs/TECHNICAL_NOTE.md`. For a short demonstration: run the crawler,
open a record and its evidence/score breakdown, run it again, then use `demo-changes` to show labelled simulated
deadline and removal changes through the same pipeline. Keep the real and demonstration databases distinct.
The exact recording sequence is in `docs/DEMO.md`.

Vercel runs `app.py` as the FastAPI entry point and reads the committed SQLite snapshot. Its filesystem cannot keep
crawler writes between requests, so run the crawler locally or in GitHub Actions and deploy an updated snapshot. The
scheduled workflow's database artifact can be downloaded and inspected separately.

### LLMs (all free)

| Provider | Setup | Role |
|---|---|---|
| Groq (free tier) | `GROQ_API_KEY` in `.env` | primary extractor (Llama 3.3 70B) |
| Google Gemini (free tier) | `GEMINI_API_KEY` in `.env` | second independent extractor |
| Ollama (local) | install Ollama + any model | offline fallback (auto-detected) |

Models are discovered from each provider's `/models` endpoint, so a renamed model does not break the run.
With no LLM at all the system still runs on the rule extractor, but then nothing can reach VERIFIED
(a second independent extractor is required for cross-checking — see `docs/CONFIDENCE.md`).

## What is where

| Path | Purpose |
|---|---|
| `config/seeds.yaml` | discovery seeds: official hubs + search queries + aggregator-lead queries (**no scholarship records**) |
| `config/domains.yaml` | source-classification knowledge base: TLD tiers, provider registry, aggregator deny-list |
| `config/settings.yaml` | thresholds and the 9 confidence weights |
| `scholarship_intel/discovery/` | frontier crawl, link scoring, web search, domain classifier, lead resolution |
| `scholarship_intel/extraction/` | rule extractor, parsers (dates/₹/age), LLM clients + prompts, vocabularies |
| `scholarship_intel/verification/` | **grounding** (anti-hallucination), **scoring** (confidence), **lifecycle** (status) |
| `scholarship_intel/storage/repo.py` | persistence, evidence rows, change detection |
| `scholarship_intel/pipeline.py` | orchestrates a run |
| `scholarship_intel/api/` | FastAPI + dashboard (`static/`) |
| `scholarship_intel/demo.py` | labelled change/expiry replay on a *copy* of the data |
| `docs/` | `CONFIDENCE.md`, `TECHNICAL_NOTE.md`, `DEMO.md` |
| `data/atlas.db` | **the real database produced by the crawler** |
| `data/atlas_demo.db` | demo copy containing clearly labelled simulated source edits |
| `data/sample/` | CSV/JSON exports of the real records |

## Database schema (SQLite)

`scholarships` (canonical record: ~45 columns incl. status, confidence, label) ·
`field_evidence` (value + verbatim quote + char offsets + page id + extractors; `QUOTE` / `ABSENT` kinds) ·
`pages` (immutable text snapshots that offsets point into) · `confidence_breakdown` (every point of every score) ·
`changes` (old/new/detected_at/source/evidence, `simulated` flag) · `status_history` ·
`rejected_extractions` (everything a model claimed that failed grounding) · `sources` (domain registry) ·
`discovery_candidates` · `unresolved_leads` · `crawl_runs`. Full DDL: `scholarship_intel/db.py`.

Trace any value: `scholarships.id → field_evidence(field, quote, char_start, char_end, page_id) → pages.text[char_start:char_end] → official URL`.

## Honest limitations

* **JavaScript-only pages** (e.g. some state portals) return little text to a plain HTTP crawler; those pages fall to
  REVIEW_REQUIRED instead of being guessed. A Playwright fetch path would be the next step.
* **Scanned-image PDFs** have no text layer; they are skipped/flagged (`needs_ocr`) — no OCR is attempted.
* Some government hosts are unreachable or bot-blocked from some networks; they are retried next run and escalate to
  NO_LONGER_VERIFIABLE only after repeated failures.
* National Scholarship Portal scheme PDFs rarely contain per-cycle deadlines, so many central schemes stay
  REVIEW_REQUIRED (correctly: the deadline cannot be supported from the official page).
* Real sources seldom change between two crawls minutes apart; hence the **labelled replay demo** — the real
  database never receives simulated values.
* Free LLM tiers are rate limited; the pipeline paces calls, caches unchanged pages by content hash and falls
  back along the provider chain.
