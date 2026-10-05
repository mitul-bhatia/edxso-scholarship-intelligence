# Confidence methodology

The confidence score is **computed from checkable evidence**. No LLM is ever asked for a number; the LLM only
proposes `{value, verbatim quote}` pairs, which the grounding layer then accepts or rejects mechanically.

Implementation: [`scholarship_intel/verification/scoring.py`](../scholarship_intel/verification/scoring.py)
(weights in [`config/settings.yaml`](../config/settings.yaml); unit tests in `tests/test_scoring_lifecycle.py`).

## 1. Inputs to the score

For each scholarship the pipeline produces:

* the **source classification** of the page's domain (tier T1–T5, see below),
* the **live fetch** of the official URL in *this* run (HTTP status, readable text?),
* the set of **grounded facts** (field → value + quote + character offsets in the stored page snapshot),
* for fields that were *not* grounded: an **absence check** (does the document contain wording that suggests the
  field exists?),
* the **extractor opinions** (rules + 1–2 LLMs) and their per-field agreement,
* the result of probing the **application URL**.

## 2. Components (weights sum to 100)

| # | Component | Wt | Question it answers | How it is computed |
|---|---|---:|---|---|
| 1 | Source authority | 20 | Is the source official? | Tier authority: T1 gov = 1.00, T2 academic = 0.95, T3 curated-registry provider = 0.95, T3h provider-name↔domain + self-identification = 0.80, T4 unknown = 0.40, T5 aggregator = 0 |
| 2 | Live presence | 10 | Is the scholarship currently on the official source? | 0.5 fetched live this run & readable + 0.3 name in title/heading (0.2 if only in body) + 0.2 × page-kind score |
| 3 | Application URL | 8 | Is there an official application URL? | 0.6 link found on the official page + 0.3 reachable (0.15 if it merely blocks bots) + 0.1 trusted domain; 0.3 if the page itself invites applications |
| 4 | Eligibility support | 14 | Can the eligibility be directly supported? | Weighted mean over 9 eligibility fields: grounded = 1.0 (×agreement), verified-absent = 0.95 (2+ extractors) / 0.80 (1), absent-but-suspicious = 0.30 |
| 5 | Deadline support | 12 | Can the deadline be directly supported? | Grounded closing date = 1.0; grounded rolling/year-round statement = 0.9; verified absent = 0.45; suspicious absent = 0.10 |
| 6 | Benefit support | 6 | Is the benefit evidenced? | Grounded amount = 1.0; benefit stated in words and quoted = 0.9; absent = 0.3 |
| 7 | Freshness | 10 | Is the information current? | 0.4 fetched this run + 0.4 cycle currency (academic-year wording, or a future grounded deadline) + 0.2 deadline not passed; capped at 0.3 once the deadline has passed |
| 8 | Extractor agreement | 10 | Was extraction consistent? | 0.75 × mean per-field agreement on key fields + 0.25 × coverage. One extractor only = 0.4 |
| 9 | Traceability | 10 | Can important fields be traced to source evidence? | Share of important fields with a stored quote + offsets (exact = 1.0, fuzzy = 0.85, link = 0.8) × min(1, n/6) |

**Text fields are verbatim.** If two extractors quote different (but both valid) passages for e.g. academic requirements,
that is neutral, not a disagreement; overlapping quotes raise credit. Disagreement only matters for derived values (dates,
amounts, categories…).

**Absence is evidence too.** "Income limit: Not specified" earns credit only if the document contains no wording
that hints at an income limit *and* the extractors agree nothing was stated. If the text contains "income … lakh"
but nothing could be grounded, that is flagged as *unverified absence* and scores 0.30.

## 3. Penalties, caps and gates

* **Conflicts.** Each unresolved extractor conflict on a critical field (dates, amount, income, age, gender, categories,
  levels, application URL) costs 3 points (max 12) and blocks VERIFIED.
* **Hard caps** (the score can never exceed the cap):
  * source not official (T4/T5) → **59**
  * scholarship name not found on the live page → **40**
  * official page not fetched this run → **50**
  * scanned-image document that cannot be read → **50**
* **Gates** (prevent VERIFIED when evidence is structurally incomplete):
  * fewer than two independent extractors → **90**
  * any critical field without grounded evidence — name, provider, benefit (amount or description), eligibility text,
    closing date *or* an open-window statement, education level → **94**

## 4. Label

```
VERIFIED         ⇔ final score ≥ 95.0   (so: no cap, no gate, no conflict)
REVIEW_REQUIRED  ⇔ otherwise
```

The dashboard shows the floor of the score to one decimal (94.96 never displays as "95.0").

## 5. Why this is not arbitrary

* Every component maps 1:1 to a question in the brief (official? present? application URL? eligibility supported?
  deadline supported? current? conflicts? consistent? traceable?).
* Every point is stored (`confidence_breakdown` table) with a plain-English `detail`, so "Why this score?" is
  answerable per record, and per run (scores can be compared across crawls).
* The gates encode the rule *"missing evidence cannot be compensated by other evidence"*: a source with a perfect
  authority score still cannot be VERIFIED without a supported deadline.
* The unit tests assert the invariants: aggregator ≤ 59, single extractor ≤ 90, missing deadline < 95,
  conflicts block verification, fully-supported official record ≥ 95.

## 6. Source tiers

| Tier | Rule | Authority |
|---|---|---|
| T1 | `*.gov.in`, `*.nic.in`, `*.res.in`, foreign `*.gov` | 1.00 |
| T2 | `*.ac.in`, `*.edu.in`, `*.edu`, `*.ac.uk` | 0.95 |
| T3 | Curated registry of manually verified provider domains (a provider's own website is the primary source for its own scholarship) | 0.95 |
| T3h | Unknown domain whose registered name embeds the provider's name **and** whose page self-identifies as that provider | 0.80 |
| T4 | Anything else | 0.40 (can never be official) |
| T5 | Aggregators, blogs, news, social | 0 (discovery only) |

Aggregator pages are never evidence: a lead found on an aggregator must be resolved to an official page, and only
text fetched from that official page is quoted or scored.
