# Technical note — Scholarship Intelligence Crawler

**Edxso AI Engineer Intern, Assignment 2 · 5 October 2026**
[Live application](https://edxso-scholarship-intelligence.vercel.app/) · [GitHub](https://github.com/mitul-bhatia/edxso-scholarship-intelligence) · [Three-page PDF](../output/pdf/Edxso_Scholarship_Intelligence_Technical_Report.pdf)

## 1. Architecture and technology

The implemented pipeline is **discover → fetch → extract → ground → score → store → re-verify**. Python coordinates a ranked crawl frontier and official-domain classification. Requests fetches HTML and PDFs with robots checks, pacing and retries; BeautifulSoup and pypdf create text snapshots; Playwright is a fallback for JavaScript-only pages. Rules and optional free-tier Gemini, Groq or local Ollama propose facts. SQLite stores programme records, page snapshots, evidence, scores, rejected claims and change history. FastAPI serves a plain-JavaScript dashboard. GitHub Actions can refresh the committed SQLite snapshot; Vercel serves it read-only.

~~~mermaid
flowchart LR
  A[Official hubs + search leads] --> B[Ranked frontier]
  B --> C[Official-domain gate]
  C --> D[HTML / PDF fetch]
  D --> E[Rules + optional LLM extraction]
  E --> F[Quote and value grounding]
  F --> G[Deterministic score + lifecycle]
  G --> H[(SQLite + change log)]
  H --> I[FastAPI dashboard + exports]
  H -->|Next crawl| B
~~~

## 2. Discovery methodology

[config/seeds.yaml](../config/seeds.yaml) contains official hub URLs and topical queries, not scholarship records. The crawler follows promising links and searches provider domains. Search results and aggregator pages can supply names or links, but they cannot verify a scholarship. The [domain classifier](../scholarship_intel/discovery/classifier.py) accepts government, academic and curated provider domains only when the page and programme align. Unresolved leads are kept separately for inspection. Relevance gates reject list pages, news, generic headings and programmes with no indication that Indian applicants are eligible.

## 3. Extraction and normalization

The [rule extractor](../scholarship_intel/extraction/rules.py) identifies names, benefits, eligibility clauses, dates and other structured fields. Optional model extractors return a value paired with a proposed verbatim quote. The pipeline combines compatible claims and detects disagreements. It can follow same-provider supporting pages when, for example, the programme page links to a deadline notice. Fields absent from the official material stay empty and appear as **Not specified**. The normalized record covers application URL, benefit, academic level, income, age, category, domicile, documents, renewal, status and verification time in addition to programme identity.

## 4. Verification and anti-hallucination

The [grounding gate](../scholarship_intel/verification/grounding.py) locates a proposed quote in the saved page text and records its page ID and character offsets. It also checks that the claimed date, amount, enum or text follows from the quote. Unsupported claims go to **rejected_extractions**, never to the scholarship fact table. The dashboard's trace view follows **database value → official URL → stored passage → extracted value**. A model never supplies a confidence number. Official-source classification alone is insufficient for a **VERIFIED** label: evidence coverage, currentness, agreement and critical-field gates all matter.

![A stored fact traced to its official-source passage](figures/evidence_trace.png)

## 5. Confidence methodology

The score is a deterministic weighted sum of nine evidence components, then conflict penalties and hard caps are applied. Weights are configured in [config/settings.yaml](../config/settings.yaml).

| Component | Maximum points | Evidence considered |
|---|---:|---|
| Source authority | 20 | Official domain tier and provider match |
| Live presence | 10 | Successful fetch, name on page, page relevance |
| Application URL | 8 | Official application link and reachability |
| Eligibility support | 14 | Grounded criteria and audited absences |
| Deadline support | 12 | Grounded closing date or open-window statement |
| Benefit support | 6 | Grounded amount or described benefit |
| Freshness | 10 | Recent fetch, cycle and deadline currency |
| Extractor agreement | 10 | Independent extraction consistency and coverage |
| Traceability | 10 | Important fields with stored source evidence |

**VERIFIED requires at least 95/100** and no blocking cap or critical conflict; otherwise the label is **REVIEW REQUIRED**. Non-official sources, missing programme names, unreadable pages, a single extractor and unsupported critical fields limit the maximum score. A lifecycle status such as **EXPIRED** is separate from this verification label.

## 6. Repeated crawling, changes and lifecycle

Each run re-verifies known official URLs first, then discovers new opportunities. Saved content hashes distinguish source changes from extractor drift. On a changed source, a changed grounded field is appended to **changes** with old and new values, detection time, source URL and evidence. If the source text is unchanged but extraction changes, it is labelled **EXTRACTION CORRECTION** and excluded from the real-source-change count. A closed deadline can produce **EXPIRED**; a missing or unreachable official source is handled through the lifecycle rules and repeated verification, rather than assuming every transient error means removal. The dashboard exposes runs, changes and status history.

## 7. Submitted snapshot and limits

The committed snapshot on **5 October 2026**, after removing five page-heading or ineligible entries, contains **47 records**, **47 classified official-source records**, **five source types**, **two records at 95% or above**, and **nine expired/stale records**. There are **zero observed official-source field changes**. These figures come from the CLI audit and SQLite, not from a staged scenario. The assignment minimums for 10 high-confidence records and two source changes are therefore outstanding.

The two 95% records are Reliance Foundation undergraduate and postgraduate scholarships. Other records remain review-required where a current deadline, complete critical fields or independent corroboration cannot be supported. Scanned PDFs need OCR; network blocks and free-tier model rate limits can limit coverage. The exact current results can be reproduced with:

~~~bash
python -m scholarship_intel audit
python -m scholarship_intel list
python -m pytest -q
~~~

The dashboard and exports contain the actual crawler output; source code, schema, configuration and the SQLite file are all in the repository.
