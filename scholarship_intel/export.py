"""Export the database to CSV/JSON so reviewers can inspect records without SQLite tooling."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .db import connect, rows

CSV_FIELDS = ["id", "name", "provider", "source_type", "official_domain", "official_url", "application_url", "amount_text", "amount_min",
              "amount_max", "amount_currency", "amount_period", "income_limit_text", "income_max_inr", "age_min", "age_max", "gender",
              "categories", "education_levels", "domicile", "opening_date", "closing_date", "status", "status_reason",
              "verification_label", "confidence", "official_source_verified", "last_verified_at", "first_discovered_at", "discovered_via"]


def export(db: str | Path | None, out_dir: str | Path) -> dict:
    conn = connect(db, readonly=True)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    recs = rows(conn, "SELECT * FROM scholarships ORDER BY confidence DESC, id")
    with open(out / "scholarships.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)
    full = []
    for r in recs:
        r.pop("facts_json", None)
        ev = rows(conn, "SELECT field,value,quote,source_url,char_start,char_end,match_score,extractor,evidence_kind FROM field_evidence "
                        "WHERE scholarship_id=? AND is_current=1 ORDER BY id", (r["id"],))
        br = rows(conn, "SELECT component,weight,score,points,detail FROM confidence_breakdown WHERE scholarship_id=? AND run_id=(SELECT MAX(run_id) "
                        "FROM confidence_breakdown WHERE scholarship_id=?) ORDER BY id", (r["id"], r["id"]))
        ch = rows(conn, "SELECT field,change_type,old_value,new_value,detected_at,source_url,simulated FROM changes WHERE scholarship_id=? ORDER BY id", (r["id"],))
        full.append({**r, "evidence": ev, "confidence_breakdown": br, "change_history": ch})
    (out / "scholarships_full.json").write_text(json.dumps(full, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    changes = rows(conn, "SELECT ch.*, s.name scholarship FROM changes ch JOIN scholarships s ON s.id=ch.scholarship_id ORDER BY ch.id")
    (out / "changes.json").write_text(json.dumps(changes, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return {"scholarships": len(recs), "changes": len(changes), "dir": str(out)}
