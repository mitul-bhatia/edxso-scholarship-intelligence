"""Command line: run a crawl, serve the dashboard, show stats, schedule repeats, run the change-detection demo."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from . import config
from .db import connect, rows
from .extraction.llm import LLMRouter
from .fetcher import Fetcher
from .pipeline import Pipeline


def _router(args) -> LLMRouter:
    if getattr(args, "llm", "auto") == "off":
        return LLMRouter(enabled=False)
    chain = None if args.llm == "auto" else [p.strip() for p in args.llm.split(",")]
    return LLMRouter(chain=chain)


def cmd_run(args) -> int:
    if args.as_of:
        config.set_as_of(dt.date.fromisoformat(args.as_of))
    conn = connect(args.db)
    fetcher = Fetcher(mode="cache" if args.offline else "live")
    pipe = Pipeline(conn, fetcher, _router(args), max_pages=args.max_pages, skip_search=args.no_search or args.offline,
                    mode="replay" if args.offline else "live", reverify_only=args.reverify_only,
                    force_reextract=getattr(args, "force_reextract", False),
                    reextract_weak=getattr(args, "reextract_weak", False))
    pipe.run(label=args.label or "")
    return 0


def cmd_stats(args) -> int:
    conn = connect(args.db, readonly=True)
    r = rows(conn, "SELECT COUNT(*) n, SUM(verification_label='VERIFIED') v, SUM(verification_label='REVIEW_REQUIRED') rr, "
                   "SUM(official_source_verified) off, ROUND(AVG(confidence),1) avg FROM scholarships")[0]
    print(json.dumps(r, indent=1))
    for q in ("SELECT status, COUNT(*) n FROM scholarships GROUP BY status", "SELECT source_type, COUNT(*) n FROM scholarships GROUP BY source_type"):
        for row in rows(conn, q):
            print("  ", row)
    print("runs:", rows(conn, "SELECT id, started_at, mode, label FROM crawl_runs ORDER BY id DESC LIMIT 5"))
    return 0


def cmd_audit(args) -> int:
    """Report the assignment's minimum output counts without inflating demo data."""
    conn = connect(args.db, readonly=True)
    counts = rows(conn, """SELECT COUNT(*) records,
        COALESCE(SUM(official_source_verified),0) official_source_verified,
        COALESCE(SUM(confidence>=95),0) confidence_95,
        COALESCE(SUM(status IN ('EXPIRED','NO_LONGER_VERIFIABLE')),0) expired_or_stale,
        COUNT(DISTINCT source_type) source_types FROM scholarships""")[0]
    counts["real_field_changes"] = rows(conn, """SELECT COUNT(*) n FROM changes
        WHERE change_type IN ('FIELD_CHANGED','FIELD_ADDED','FIELD_UNSUPPORTED') AND simulated=0
        AND COALESCE(note,'') NOT LIKE 'EXTRACTION_CORRECTION:%'""")[0]["n"]
    counts["extraction_corrections"] = rows(conn, """SELECT COUNT(*) n FROM changes
        WHERE COALESCE(note,'') LIKE 'EXTRACTION_CORRECTION:%'""")[0]["n"]
    counts["demo_field_changes"] = rows(conn, """SELECT COUNT(*) n FROM changes
        WHERE change_type IN ('FIELD_CHANGED','FIELD_ADDED','FIELD_UNSUPPORTED') AND simulated=1""")[0]["n"]
    checks = {"20+ real records": counts["records"] >= 20,
              "15+ official-source records": counts["official_source_verified"] >= 15,
              "10+ confidence >=95": counts["confidence_95"] >= 10,
              "3+ source types": counts["source_types"] >= 3,
              "2+ real field changes": counts["real_field_changes"] >= 2,
              "2+ expired/stale records": counts["expired_or_stale"] >= 2}
    print(json.dumps({"counts": counts, "minimum_checks": checks}, indent=2))
    return 0 if all(checks.values()) else 1


def cmd_list(args) -> int:
    conn = connect(args.db, readonly=True)
    for r in rows(conn, "SELECT id,name,provider,source_type,confidence,verification_label,status,closing_date,official_domain FROM scholarships ORDER BY confidence DESC"):
        print(f"{r['id']:>3} {r['confidence']:>5.1f}% {r['verification_label']:<15} {r['status']:<20} {r['source_type']:<13} {r['closing_date'] or '-':<11} {r['name'][:60]}  [{r['official_domain']}]")
    return 0


def cmd_export(args) -> int:
    from .export import export
    print(json.dumps(export(args.db, args.out)))
    return 0


def cmd_snapshot(args) -> int:
    """Fold the WAL into the main file and switch to rollback-journal mode, so data/atlas.db is ONE self-contained file
    that can be committed and opened read-only by a serverless host (Vercel)."""
    import sqlite3
    path = args.db or config.db_path()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    conn.execute("VACUUM")
    n = conn.execute("SELECT COUNT(*) FROM scholarships").fetchone()[0]
    conn.close()
    print(json.dumps({"db": str(path), "journal_mode": mode, "scholarships": n}))
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    import os
    os.environ["ATLAS_DB"] = str(args.db or config.db_path())
    uvicorn.run("scholarship_intel.api.server:app", host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_schedule(args) -> int:
    """Simple in-process scheduler (use cron / GitHub Actions in production – see README)."""
    every = float(args.every_hours) * 3600
    while True:
        print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] scheduled crawl starting")
        cmd_run(args)
        print(f"sleeping {args.every_hours}h")
        time.sleep(every)


def cmd_demo(args) -> int:
    from .demo import run_demo
    return run_demo(args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scholarship_intel", description="Atlas Scholarship Intelligence Crawler")
    ap.add_argument("--db", default=None, help="SQLite path (default data/atlas.db or $ATLAS_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def run_args(p):
        p.add_argument("--llm", default="auto", help="auto | off | comma list of groq,gemini,ollama")
        p.add_argument("--max-pages", type=int, default=None)
        p.add_argument("--no-search", action="store_true", help="skip web-search discovery (hubs only)")
        p.add_argument("--offline", action="store_true", help="replay from on-disk cache; no network")
        p.add_argument("--reverify-only", action="store_true", help="only re-verify known scholarships, no discovery")
        p.add_argument("--reextract-weak", action="store_true", help="re-run extraction only for records below 95 or single-extractor")
        p.add_argument("--force-reextract", action="store_true", help="re-run extraction even when a page is unchanged (after changing extraction logic)")
        p.add_argument("--as-of", default=None, help="logical 'today' YYYY-MM-DD (lifecycle logic)")
        p.add_argument("--label", default="")

    p = sub.add_parser("run", help="run one crawl"); run_args(p); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("list"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("audit", help="check actual output against the assignment minimums"); p.set_defaults(fn=cmd_audit)
    p = sub.add_parser("export", help="write CSV/JSON of all records + evidence"); p.add_argument("--out", default="data/sample"); p.set_defaults(fn=cmd_export)
    p = sub.add_parser("snapshot", help="make the DB a single read-only-safe file for deployment"); p.set_defaults(fn=cmd_snapshot)
    p = sub.add_parser("serve"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8000); p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("schedule", help="repeat crawls forever"); run_args(p); p.add_argument("--every-hours", default="24"); p.set_defaults(fn=cmd_schedule)
    p = sub.add_parser("demo-changes", help="build the labelled change/expiry demo database from a real run")
    p.add_argument("--source-db", default=None); p.add_argument("--out", default="data/atlas_demo.db"); p.add_argument("--llm", default="off")
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
