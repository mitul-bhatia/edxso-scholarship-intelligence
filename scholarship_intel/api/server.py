"""FastAPI backend + static dashboard. Read-only over the SQLite database the crawler produces."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..db import rows
from ..extraction.vocab import FIELD_LABELS

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Atlas Scholarship Intelligence", version="1.0")


def _conn():
    # The dashboard only reads the crawler's committed snapshot. In particular,
    # never create an empty database when a deployment forgot to package it.
    db = Path(os.getenv("ATLAS_DB") or config.db_path()).resolve()
    if not db.is_file():
        raise HTTPException(503, f"Crawler database is missing: {db.name}")
    c = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _j(v):
    if v is None:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return v


@app.get("/api/stats")
def stats():
    c = _conn()
    base = rows(c, """SELECT COUNT(*) total,
        COALESCE(SUM(verification_label='VERIFIED'),0) verified,
        COALESCE(SUM(verification_label='REVIEW_REQUIRED'),0) review_required,
        COALESCE(SUM(confidence>=95),0) conf_95,
        COALESCE(SUM(official_source_verified),0) official_verified,
        COALESCE(SUM(status='ACTIVE'),0) active,
        COALESCE(SUM(status='EXPIRING_SOON'),0) expiring_soon,
        COALESCE(SUM(status='EXPIRED'),0) expired,
        COALESCE(SUM(status='NO_LONGER_VERIFIABLE'),0) no_longer_verifiable,
        COALESCE(SUM(status='REVIEW_REQUIRED'),0) status_review,
        ROUND(AVG(confidence),1) avg_confidence FROM scholarships""")[0]
    last_run = rows(c, "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1")
    since = (config.today() - dt.timedelta(days=7)).isoformat()
    recent = rows(c, "SELECT COUNT(DISTINCT scholarship_id) n FROM changes WHERE change_type!='NEW' AND detected_at>=?", (since,))[0]["n"]
    n_changes = rows(c, """SELECT COUNT(*) n FROM changes
        WHERE change_type IN ('FIELD_CHANGED','FIELD_ADDED','FIELD_UNSUPPORTED')
        AND COALESCE(note,'') NOT LIKE 'EXTRACTION_CORRECTION:%'""")[0]["n"]
    corrections = rows(c, "SELECT COUNT(*) n FROM changes WHERE COALESCE(note,'') LIKE 'EXTRACTION_CORRECTION:%'")[0]["n"]
    by_type = rows(c, "SELECT source_type, COUNT(*) n, SUM(verification_label='VERIFIED') verified FROM scholarships GROUP BY source_type ORDER BY n DESC")
    hist = rows(c, """SELECT CAST(MIN(confidence,99.99)/10 AS INT)*10 bucket, COUNT(*) n FROM scholarships GROUP BY bucket ORDER BY bucket""")
    meta = {r["key"]: r["value"] for r in rows(c, "SELECT * FROM meta")}
    return {**base, "recently_updated": recent, "field_changes": n_changes, "extraction_corrections": corrections,
            "by_source_type": by_type, "confidence_histogram": hist,
            "last_run": last_run[0] if last_run else None, "runs": rows(c, "SELECT COUNT(*) n FROM crawl_runs")[0]["n"],
            "rejected_extractions": rows(c, "SELECT COUNT(*) n FROM rejected_extractions")[0]["n"],
            "unresolved_leads": rows(c, "SELECT COUNT(*) n FROM unresolved_leads")[0]["n"],
            "today": config.today().isoformat(), "meta": meta}


@app.get("/api/scholarships")
def list_scholarships(q: str = "", status: str = "", label: str = "", type: str = "", min_conf: float = 0.0,
                      sort: str = "confidence", order: str = "desc", limit: int = Query(200, le=500), offset: int = 0):
    c = _conn()
    where, params = ["1=1"], []
    if q:
        where.append("(name LIKE ? OR provider LIKE ? OR eligibility_text LIKE ? OR official_domain LIKE ?)")
        params += [f"%{q}%"] * 4
    if status:
        where.append("status=?"); params.append(status)
    if label:
        where.append("verification_label=?"); params.append(label)
    if type:
        where.append("source_type=?"); params.append(type)
    if min_conf:
        where.append("confidence>=?"); params.append(min_conf)
    col = {"confidence": "confidence", "name": "name", "deadline": "closing_date", "verified": "last_verified_at", "changed": "last_changed_at",
           "discovered": "first_discovered_at"}.get(sort, "confidence")
    od = "ASC" if order.lower() == "asc" else "DESC"
    sql = f"""SELECT id,name,provider,source_type,source_tier,official_url,official_domain,application_url,amount_text,amount_min,amount_max,
        amount_currency,amount_period,closing_date,opening_date,status,status_reason,verification_label,confidence,official_source_verified,
        last_verified_at,last_changed_at,first_discovered_at,discovered_via FROM scholarships WHERE {' AND '.join(where)}
        ORDER BY {col} {od} NULLS LAST, id LIMIT ? OFFSET ?"""
    total = rows(c, f"SELECT COUNT(*) n FROM scholarships WHERE {' AND '.join(where)}", tuple(params))[0]["n"]
    return {"total": total, "items": rows(c, sql, (*params, limit, offset))}


@app.get("/api/scholarships/{sid}")
def scholarship(sid: int):
    c = _conn()
    r = rows(c, "SELECT * FROM scholarships WHERE id=?", (sid,))
    if not r:
        raise HTTPException(404, "scholarship not found")
    s = r[0]
    s.pop("facts_json", None)
    for f in ("categories", "education_levels"):
        s[f] = _j(s.get(f))
    ev = rows(c, "SELECT * FROM field_evidence WHERE scholarship_id=? AND is_current=1 ORDER BY id", (sid,))
    for e in ev:
        e["label"] = FIELD_LABELS.get(e["field"], e["field"])
        e["value"] = _j(e["value"]) if e["evidence_kind"] == "QUOTE" and e["value"] and e["value"][:1] in "[{" else e["value"]
    last_run = rows(c, "SELECT MAX(run_id) r FROM confidence_breakdown WHERE scholarship_id=?", (sid,))[0]["r"]
    breakdown = rows(c, "SELECT component,weight,score,points,detail FROM confidence_breakdown WHERE scholarship_id=? AND run_id=? ORDER BY id", (sid, last_run))
    changes = rows(c, "SELECT * FROM changes WHERE scholarship_id=? ORDER BY id DESC", (sid,))
    hist = rows(c, "SELECT run_id, SUM(points) total FROM confidence_breakdown WHERE scholarship_id=? AND component NOT IN ('cap','gate') GROUP BY run_id ORDER BY run_id", (sid,))
    status_hist = rows(c, "SELECT * FROM status_history WHERE scholarship_id=? ORDER BY id DESC", (sid,))
    rejected = rows(c, "SELECT field,proposed_value,proposed_quote,reason,extractor,created_at FROM rejected_extractions WHERE scholarship_key=? ORDER BY id DESC LIMIT 40", (s["key"],))
    page = rows(c, "SELECT id,url,final_url,title,status_code,content_type,fetched_at,text_len,tls_verified FROM pages WHERE id=?", (s["primary_page_id"],))
    src = rows(c, "SELECT * FROM sources WHERE domain=?", (s["official_domain"],))
    return {"scholarship": s, "evidence": ev, "breakdown": breakdown, "changes": changes, "status_history": status_hist, "rejected": rejected,
            "page": page[0] if page else None, "source": src[0] if src else None, "confidence_history": hist,
            "field_labels": FIELD_LABELS}


@app.get("/api/evidence/{eid}/context")
def evidence_context(eid: int, pad: int = 260):
    c = _conn()
    e = rows(c, "SELECT * FROM field_evidence WHERE id=?", (eid,))
    if not e:
        raise HTTPException(404, "evidence not found")
    e = e[0]
    p = rows(c, "SELECT url,final_url,title,text,fetched_at FROM pages WHERE id=?", (e["page_id"],))
    if not p or e["char_start"] is None:
        return {"found": False, "quote": e["quote"], "page": p[0] if p else None}
    t = p[0]["text"]
    s, en = e["char_start"], e["char_end"]
    return {"found": True, "before": t[max(0, s - pad):s], "quote": t[s:en], "after": t[en:en + pad], "start": s, "end": en,
            "page_url": p[0]["final_url"] or p[0]["url"], "title": p[0]["title"], "fetched_at": p[0]["fetched_at"]}


@app.get("/api/changes")
def changes(limit: int = 100, kind: str = ""):
    c = _conn()
    w = "WHERE ch.change_type=?" if kind else ""
    return rows(c, f"""SELECT ch.*, s.name scholarship_name, s.official_domain FROM changes ch JOIN scholarships s ON s.id=ch.scholarship_id
        {w} ORDER BY ch.id DESC LIMIT ?""", ((kind, limit) if kind else (limit,)))


@app.get("/api/runs")
def runs():
    out = rows(_conn(), "SELECT * FROM crawl_runs ORDER BY id DESC")
    for r in out:
        r["stats"] = _j(r.pop("stats_json"))
    return out


@app.get("/api/leads")
def leads():
    return rows(_conn(), "SELECT * FROM unresolved_leads ORDER BY id DESC LIMIT 200")


@app.get("/api/sources")
def sources():
    return rows(_conn(), "SELECT s.*, (SELECT COUNT(*) FROM scholarships x WHERE x.official_domain=s.domain) n FROM sources s ORDER BY n DESC, domain")


DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
REPORT = Path(__file__).resolve().parents[2] / "output/pdf/Edxso_Scholarship_Intelligence_Technical_Report.pdf"
DOC_PAGES = {
    "technical-note": ("Technical note", "TECHNICAL_NOTE.md"),
    "confidence": ("Confidence methodology", "CONFIDENCE.md"),
}
_DOC_STYLE = """
 body{max-width:860px;margin:0 auto;padding:24px 20px 80px;line-height:1.6}
 h1,h2{letter-spacing:-.01em} h2{margin-top:1.6em;border-bottom:1px solid var(--line);padding-bottom:.2em}
 table{font-size:.9rem} code{background:var(--surface2);padding:1px 5px;border-radius:5px;font-size:.88em}
 pre{background:var(--surface2);padding:12px;border-radius:10px;overflow:auto} pre code{background:none;padding:0}
 .bar{display:flex;gap:14px;align-items:center;margin-bottom:18px;font-size:.9rem}
 @media print{.bar{display:none} body{max-width:none;padding:0;font-size:10.5pt} h2{margin-top:1em} a{color:inherit}}
"""


@app.get("/doc/{name}", response_class=HTMLResponse)
def doc(name: str):
    """Project documents rendered as web pages (printable to PDF), so the live site is a complete submission."""
    if name not in DOC_PAGES:
        raise HTTPException(404, "unknown document")
    title, fname = DOC_PAGES[name]
    path = DOCS_DIR / fname
    if not path.is_file():
        raise HTTPException(404, "document not packaged with this deployment")
    try:
        import markdown
    except ImportError:                                            # dashboard keeps working without the renderer
        raise HTTPException(503, "markdown renderer not installed")
    body = markdown.markdown(path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code", "sane_lists"])
    links = " · ".join(f'<a href="/doc/{k}">{v[0]}</a>' for k, v in DOC_PAGES.items())
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} – Atlas Scholarship Intelligence</title><link rel="stylesheet" href="/static/styles.css"><style>{_DOC_STYLE}</style></head>
<body><div class="bar"><a href="/">← Dashboard</a> {links} <a href="javascript:window.print()">Print / save as PDF</a></div>{body}</body></html>""")


@app.get("/report")
def report():
    if not REPORT.is_file():
        raise HTTPException(404, "technical report is not packaged with this deployment")
    return FileResponse(REPORT, media_type="application/pdf", filename=REPORT.name, content_disposition_type="inline")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
