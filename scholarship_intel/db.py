"""SQLite schema + connection helper. The database is the product: every table is human-inspectable."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import db_path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL, finished_at TEXT,
  mode TEXT NOT NULL DEFAULT 'live',          -- live | replay (demo overlay)
  as_of TEXT, label TEXT, stats_json TEXT, notes TEXT
);

-- Domain registry produced by source classification
CREATE TABLE IF NOT EXISTS sources (
  domain TEXT PRIMARY KEY, source_type TEXT, tier TEXT, authority REAL,
  reason TEXT, provider_name TEXT, first_seen_run INTEGER
);

CREATE TABLE IF NOT EXISTS discovery_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, url TEXT, canonical_url TEXT, discovered_via TEXT, parent_url TEXT,
  anchor_text TEXT, context_text TEXT, relevance REAL, depth INTEGER, domain TEXT,
  source_type TEXT, status TEXT, reason TEXT,
  UNIQUE(run_id, canonical_url)
);

-- Every fetched document (immutable snapshot per fetch); evidence offsets point into pages.text
CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, url TEXT, final_url TEXT, domain TEXT, status_code INTEGER, content_type TEXT,
  fetched_at TEXT, content_hash TEXT, title TEXT, text TEXT, text_len INTEGER, links_json TEXT,
  error TEXT, tls_verified INTEGER, last_modified TEXT, needs_ocr INTEGER DEFAULT 0,
  simulated INTEGER DEFAULT 0, page_kind TEXT, kind_score REAL
);
CREATE INDEX IF NOT EXISTS idx_pages_url ON pages(url);

CREATE TABLE IF NOT EXISTS scholarships (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,
  name TEXT, provider TEXT,
  source_type TEXT, source_tier TEXT, official_url TEXT, official_domain TEXT, application_url TEXT,
  amount_text TEXT, amount_min REAL, amount_max REAL, amount_currency TEXT, amount_period TEXT,
  benefit_text TEXT, eligibility_text TEXT, academic_requirements TEXT,
  education_levels TEXT, courses TEXT,
  income_limit_text TEXT, income_max_inr REAL,
  age_text TEXT, age_min INTEGER, age_max INTEGER,
  gender TEXT, categories TEXT, domicile TEXT, institution_requirements TEXT,
  opening_date TEXT, closing_date TEXT, deadline_note TEXT,
  documents_required TEXT, selection_process TEXT, renewal_requirements TEXT,
  status TEXT, status_reason TEXT,
  verification_label TEXT, confidence REAL, official_source_verified INTEGER DEFAULT 0,
  miss_count INTEGER DEFAULT 0, primary_page_id INTEGER,
  discovered_via TEXT, first_run_id INTEGER, last_run_id INTEGER,
  first_discovered_at TEXT, last_seen_at TEXT, last_verified_at TEXT, last_changed_at TEXT,
  content_hash TEXT, facts_json TEXT
);

-- Database -> Official Source -> Evidence -> Extracted Value
CREATE TABLE IF NOT EXISTS field_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scholarship_id INTEGER NOT NULL REFERENCES scholarships(id) ON DELETE CASCADE,
  run_id INTEGER, field TEXT NOT NULL, value TEXT, quote TEXT,
  page_id INTEGER, source_url TEXT, char_start INTEGER, char_end INTEGER, match_score REAL,
  extractor TEXT, evidence_kind TEXT,         -- QUOTE | ABSENT
  verified_at TEXT, is_current INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_evidence_sch ON field_evidence(scholarship_id, is_current);

-- Anything an extractor claimed that failed grounding: kept for audit, never shown as fact
CREATE TABLE IF NOT EXISTS rejected_extractions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, scholarship_key TEXT, scholarship_name TEXT, page_id INTEGER, field TEXT,
  proposed_value TEXT, proposed_quote TEXT, reason TEXT, extractor TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS confidence_breakdown (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scholarship_id INTEGER NOT NULL REFERENCES scholarships(id) ON DELETE CASCADE,
  run_id INTEGER, component TEXT, weight REAL, score REAL, points REAL, detail TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conf_sch ON confidence_breakdown(scholarship_id, run_id);

CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scholarship_id INTEGER NOT NULL REFERENCES scholarships(id) ON DELETE CASCADE,
  run_id INTEGER, field TEXT, change_type TEXT,  -- NEW | FIELD_CHANGED | FIELD_ADDED | FIELD_UNSUPPORTED | STATUS_CHANGED
  old_value TEXT, new_value TEXT, detected_at TEXT, source_url TEXT,
  old_evidence TEXT, new_evidence TEXT, simulated INTEGER DEFAULT 0, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_sch ON changes(scholarship_id);

CREATE TABLE IF NOT EXISTS status_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scholarship_id INTEGER NOT NULL REFERENCES scholarships(id) ON DELETE CASCADE,
  run_id INTEGER, old_status TEXT, new_status TEXT, reason TEXT, at TEXT
);

-- Names found on aggregators/search results for which no official page could be resolved.
CREATE TABLE IF NOT EXISTS unresolved_leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, name TEXT, lead_url TEXT, lead_domain TEXT, reason TEXT, created_at TEXT
);
"""


def connect(path: Path | str | None = None, readonly: bool = False) -> sqlite3.Connection:
    """Open the database. `readonly=True` never writes (no schema/WAL changes) – used by stats/list/audit/export."""
    p = Path(path) if path else db_path()
    if readonly:
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
