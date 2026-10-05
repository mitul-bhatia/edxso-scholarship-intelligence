"""Build the three-page, source-grounded assignment report from the current SQLite snapshot."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/pdf/Edxso_Scholarship_Intelligence_Technical_Report.pdf"
DB = ROOT / "data/atlas.db"
DASH = ROOT / "docs/figures/dashboard_overview.png"
TRACE = ROOT / "docs/figures/evidence_trace.png"

FONT_ROOT = Path("/System/Library/Fonts/Supplemental")
pdfmetrics.registerFont(TTFont("ArialReport", str(FONT_ROOT / "Arial.ttf")))
pdfmetrics.registerFont(TTFont("ArialReportBold", str(FONT_ROOT / "Arial Bold.ttf")))

NAVY = colors.HexColor("#15253B")
BLUE = colors.HexColor("#2547D8")
PALE = colors.HexColor("#F2F5FA")
MUTED = colors.HexColor("#536174")
BORDER = colors.HexColor("#DCE4EE")
GREEN = colors.HexColor("#087D4E")
AMBER = colors.HexColor("#A76104")
RED = colors.HexColor("#B0352C")
WHITE = colors.white
W, H = A4
M = 40
CW = W - 2 * M
OUT.parent.mkdir(parents=True, exist_ok=True)

con = sqlite3.connect(DB)
q = lambda sql: con.execute(sql).fetchone()[0]
n = {
    "records": q("SELECT COUNT(*) FROM scholarships"),
    "official": q("SELECT COUNT(*) FROM scholarships WHERE official_source_verified=1"),
    "verified": q("SELECT COUNT(*) FROM scholarships WHERE confidence>=95"),
    "types": q("SELECT COUNT(DISTINCT source_type) FROM scholarships"),
    "expired": q("SELECT COUNT(*) FROM scholarships WHERE status IN ('EXPIRED','NO_LONGER_VERIFIABLE')"),
    "changes": q("""SELECT COUNT(*) FROM changes
                    WHERE change_type IN ('FIELD_CHANGED','FIELD_ADDED','FIELD_UNSUPPORTED')
                    AND COALESCE(note,'') NOT LIKE 'EXTRACTION_CORRECTION:%'"""),
    "corrections": q("SELECT COUNT(*) FROM changes WHERE COALESCE(note,'') LIKE 'EXTRACTION_CORRECTION:%'"),
    "evidence": q("SELECT COUNT(*) FROM field_evidence WHERE is_current=1 AND evidence_kind='QUOTE'"),
    "runs": q("SELECT COUNT(*) FROM crawl_runs"),
}
assert n["records"] == 47, "Review report wording when the database changes."
assert DASH.exists() and TRACE.exists(), "Capture current dashboard screenshots first."

c = canvas.Canvas(str(OUT), pagesize=A4, pageCompression=1)
c.setTitle("Edxso Scholarship Intelligence Crawler | Technical Report")
c.setAuthor("Scholarship Intelligence Crawler")
c.setSubject("Assignment 2 technical note and observed crawler results")

def text(x, y, value, size=9, bold=False, color=NAVY):
    c.setFont("ArialReportBold" if bold else "ArialReport", size)
    c.setFillColor(color)
    c.drawString(x, y, str(value))

def right(x, y, value, size=9, bold=False, color=NAVY):
    c.setFont("ArialReportBold" if bold else "ArialReport", size)
    c.setFillColor(color)
    c.drawRightString(x, y, str(value))

def para(value, x, top, width, size=8.8, leading=13, color=NAVY, bold=False):
    style = ParagraphStyle(
        "copy", fontName="ArialReportBold" if bold else "ArialReport",
        fontSize=size, leading=leading, textColor=color, spaceAfter=0)
    p = Paragraph(value, style)
    _, h = p.wrap(width, 500)
    p.drawOn(c, x, top-h)
    return top-h

def box(x, y, w, h, fill=WHITE, stroke=BORDER, radius=9):
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(.8)
    c.roundRect(x, y, w, h, radius, fill=1, stroke=1)

def header(page, kicker):
    c.setFillColor(BLUE)
    c.rect(0, H-9, W, 9, stroke=0, fill=1)
    text(M, H-37, "EDXSO  /  AI ENGINEER INTERN  /  ASSIGNMENT 2", 8.2, True, BLUE)
    right(W-M, H-37, kicker.upper(), 8.2, True, MUTED)
    c.setStrokeColor(BORDER)
    c.line(M, H-49, W-M, H-49)
    c.setStrokeColor(BORDER)
    c.line(M, 49, W-M, 49)
    text(M, 32, "Atlas Scholarship Intelligence  |  5 Oct 2026", 7.7, False, MUTED)
    right(W-M, 32, f"{page} / 3", 7.7, True, MUTED)

def link(label, url, x, y, size=9, color=BLUE):
    text(x, y, label, size, True, color)
    width = pdfmetrics.stringWidth(label, "ArialReportBold", size)
    c.linkURL(url, (x, y-2, x+width, y+size+2), relative=0, thickness=0)

def image_panel(path, x, top, width, height, caption):
    box(x, top-height-7, width, height+7, PALE, BORDER, 7)
    c.drawImage(ImageReader(str(path)), x+4, top-height+3, width-8, height-8,
                preserveAspectRatio=True, anchor="c", mask="auto")
    text(x+3, top-height-20, caption, 8, False, MUTED)

# PAGE 1 - summary, architecture, actual dashboard
header(1, "Working system")
text(M, 768, "Scholarship Intelligence Crawler", 23, True)
para("A working pipeline for Indian student funding: discover, crawl, extract, verify, score, store and update. Every retained field is tied to a fetched official-source page.", M, 753, CW, 10, 15, MUTED)
link("Live application", "https://edxso-scholarship-intelligence.vercel.app/", M, 705, 9)
link("GitHub repository", "https://github.com/mitul-bhatia/edxso-scholarship-intelligence", M+108, 705, 9)

metrics = [
    (str(n["records"]), "real records", BLUE),
    (str(n["official"]), "official-source links", GREEN),
    (str(n["verified"]), "at 95% or above", AMBER),
    (str(n["types"]), "source types", BLUE),
]
mw = (CW-27)/4
for i,(value,label,col) in enumerate(metrics):
    x=M+i*(mw+9)
    box(x, 627, mw, 62, PALE, BORDER)
    text(x+10, 660, value, 22, True, col)
    para(escape(label), x+10, 649, mw-16, 8, 10, MUTED)
text(M, 601, "HOW THE IMPLEMENTED PIPELINE WORKS", 9.3, True, NAVY)
steps = [("Discover", "hubs + search"),("Fetch", "HTML + PDF"),("Extract", "rules + LLM"),("Ground", "quote + offset"),("Score", "9 components"),("Publish", "SQLite + API")]
sw=(CW-50)/6
for i,(a,b) in enumerate(steps):
    x=M+i*(sw+10)
    box(x, 534, sw, 51, WHITE, BORDER, 6)
    text(x+8, 563, a, 9.1, True, BLUE)
    text(x+8, 548, b, 7.2, False, MUTED)
    if i<5:
        c.setStrokeColor(BLUE)
        c.setLineWidth(1.5)
        c.line(x+sw+2, 559, x+sw+8, 559)
para("Discovery uses official hubs, topical search and provider-domain expansion. Aggregators supply leads only. Requests, BeautifulSoup, pypdf and optional Playwright build page snapshots; Gemini, Groq or Ollama are optional free extraction aids. FastAPI exposes the stored result.", M, 522, CW, 8.8, 13)
image_panel(DASH, M, 470, CW, 349, "Figure 1. Dashboard captured from the submitted 47-record SQLite snapshot.")
text(M, 88, "Observed result:", 8.5, True, NAVY)
text(M+75, 88, f"{n['runs']} crawl runs  |  {n['evidence']} current quoted field-evidence rows  |  {n['expired']} expired/stale records", 8.4, False, MUTED)
c.showPage()

# PAGE 2 - provenance, extraction, screenshot
header(2, "Evidence and trust")
text(M, 768, "From source page to traceable fact", 21, True)
para("A model can suggest a value, but the verification gate accepts it only when the stored official page contains the proposed passage and that passage supports the value.", M, 751, CW, 9.6, 14, MUTED)

colw=(CW-12)/2
box(M, 607, colw, 106, PALE, BORDER)
text(M+13, 690, "Discovery and extraction", 10.5, True, BLUE)
para("Ranked links and search leads resolve to provider, government or academic domains. HTML/PDF text is stored, then rules and optional LLMs propose normalized fields with verbatim quotes. A same-provider supporting notice can supply a deadline.", M+13, 679, colw-26, 8.5, 12)
box(M+colw+12, 607, colw, 106, PALE, BORDER)
text(M+colw+25, 690, "Grounding and rejection", 10.5, True, GREEN)
para("The quote must match the page snapshot at recorded character offsets. Dates, amounts and categories must follow from that passage. Unsupported proposals are logged separately. Missing values stay Not specified.", M+colw+25, 679, colw-26, 8.5, 12)

text(M, 587, "A FIELD'S AUDIT PATH", 9.3, True)
labels=[("1  Database","value + record"),("2  Official page","URL + snapshot"),("3  Evidence","quote + offsets"),("4  Result","grounded field")]
fw=(CW-24)/4
for i,(a,b) in enumerate(labels):
    x=M+i*(fw+8)
    box(x, 530, fw, 45, WHITE, BORDER, 6)
    text(x+8, 556, a, 8.4, True, BLUE)
    text(x+8, 541, b, 7.5, False, MUTED)
    if i<3:
        c.setStrokeColor(BLUE); c.line(x+fw+1, 552, x+fw+7, 552)

# Screenshot includes the actual Reliance record and the evidence modal.
image_panel(TRACE, M, 512, CW, 341, "Figure 2. Real Reliance Foundation fact traced to an official-source passage.")
box(M, 86, CW, 62, PALE, BORDER)
text(M+13, 129, "Verification rule", 9.5, True, NAVY)
para("The score is computed from evidence, not generated by a model. VERIFIED requires 95% or more and no blocking gate. Conflicting or absent critical evidence leaves the record REVIEW REQUIRED.", M+13, 120, CW-26, 8.5, 12)
c.showPage()

# PAGE 3 - score, repeated runs, measured gaps, operations
header(3, "Scoring and updates")
text(M, 768, "Measured confidence and repeatability", 20, True)
para("The score is a weighted sum of nine configured checks (maximum 100), followed by conflict penalties and hard caps. It is stored with a per-component explanation.", M, 751, CW, 9.2, 13, MUTED)
text(M, 709, "CONFIDENCE MODEL", 9.4, True)
components=[
    ("Source authority",20),("Live presence",10),("Application URL",8),
    ("Eligibility support",14),("Deadline support",12),("Benefit support",6),
    ("Freshness",10),("Extractor agreement",10),("Traceability",10),
]
for i,(name,weight) in enumerate(components):
    y=688-i*18
    text(M, y, name, 8.8)
    barx=M+190
    barw=248
    c.setFillColor(PALE); c.roundRect(barx,y-3,barw,8,4,stroke=0,fill=1)
    c.setFillColor(BLUE if i<4 else GREEN)
    c.roundRect(barx,y-3,barw*weight/20,8,4,stroke=0,fill=1)
    right(W-M, y, f"{weight} pts", 8.7, True, NAVY)
para("Hard caps apply to non-official or unreadable pages, missing programme identity, insufficient independent extraction and unsupported critical fields. An expired deadline also reduces freshness.", M, 522, CW, 8.6, 12, MUTED)

text(M, 487, "REPEATED CRAWL AND CHANGE DECISION", 9.4, True)
box(M, 407, CW, 67, PALE, BORDER)
text(M+13, 454, "Re-fetch known URL", 8.8, True, BLUE)
text(M+159, 454, "Compare content hash", 8.8, True, BLUE)
text(M+332, 454, "Ground changed fields", 8.8, True, BLUE)
c.setStrokeColor(BLUE); c.line(M+122,456,M+147,456); c.line(M+287,456,M+320,456)
para("If source text is unchanged, an altered extraction is marked EXTRACTION CORRECTION. If the official page and grounded field change, retain old/new values, detection time, source and both evidence passages. Lifecycle tracks ACTIVE, EXPIRING SOON, EXPIRED, REVIEW REQUIRED and NO LONGER VERIFIABLE.", M+13, 446, CW-26, 8.2, 11)

text(M, 382, "SUBMISSION AUDIT  |  ACTUAL SNAPSHOT", 9.4, True)
audit=[
    ("Records / official-source records",f"{n['records']} / {n['official']}","meets 20 / 15"),
    ("Source types / expired or stale",f"{n['types']} / {n['expired']}","meets 3 / 2"),
    ("Records at 95%+",str(n['verified']),"below 10"),
    ("Observed official-source field changes",str(n['changes']),"below 2"),
]
for i,(label,value,verdict) in enumerate(audit):
    y=360-i*23
    if i%2==0:
        c.setFillColor(PALE); c.rect(M,y-7,CW,22,stroke=0,fill=1)
    text(M+8,y,label,8.7)
    right(M+397,y,value,8.7,True,NAVY)
    right(W-M-7,y,verdict,8.2,True,GREEN if i<2 else AMBER)
para(f"{n['corrections']} extractor/validator corrections were excluded from the observed source-change count. Real websites did not yield a documented field change across these runs; no source edits were manufactured.", M, 263, CW, 8.6, 12, MUTED)

text(M, 224, "DELIVERY AND REPRODUCTION", 9.4, True)
box(M, 106, CW, 105, WHITE, BORDER)
para("<b>Read-only live app:</b> Vercel serves the committed SQLite snapshot through FastAPI. GitHub Actions can crawl, export, checkpoint and commit future snapshots. The crawler uses free/open-source tools; model keys are optional and kept outside source control.", M+12, 198, CW-24, 8.6, 12)
para("<b>Inspect locally:</b> python -m scholarship_intel audit  |  python -m scholarship_intel list<br/><b>Run and serve:</b> python -m scholarship_intel run  |  python -m scholarship_intel serve --port 8010", M+12, 158, CW-24, 8.3, 12)
link("edxso-scholarship-intelligence.vercel.app", "https://edxso-scholarship-intelligence.vercel.app/", M+12, 121, 8.5)
text(M, 73, "Limitations: missing official deadlines, incomplete eligibility, scanned PDFs and free-tier rate limits reduce confidence.", 7.5, False, MUTED)
c.showPage()
c.save()
print(OUT)
print(n)
