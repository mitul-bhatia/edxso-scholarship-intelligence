"use strict";
const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async p => { const r = await fetch("/api" + p); if (!r.ok) throw new Error(p + " → " + r.status); return r.json(); };
const NS = '<span class="ns">Not specified</span>';

const STATUS_CLS = { ACTIVE: "c-ok", EXPIRING_SOON: "c-warn", EXPIRED: "c-bad", REVIEW_REQUIRED: "c-warn", NO_LONGER_VERIFIABLE: "c-bad" };
const TYPE_LABEL = { GOVERNMENT: "Government", UNIVERSITY: "University", CORPORATE_CSR: "Corporate CSR", NGO_TRUST: "NGO / Trust", INTERNATIONAL: "International", UNKNOWN: "Unknown", AGGREGATOR: "Aggregator" };
const chip = (t, c) => `<span class="chip ${c}">${esc(t)}</span>`;
const statusChip = s => chip((s || "").replace(/_/g, " "), STATUS_CLS[s] || "c-gray");
const labelChip = l => chip(l === "VERIFIED" ? "VERIFIED" : "REVIEW REQUIRED", l === "VERIFIED" ? "c-ok" : "c-warn");
const confColor = c => c >= 95 ? "var(--ok)" : c >= 75 ? "var(--warn)" : "var(--bad)";
const confBar = c => `<div class="conf"><span>${Number(c).toFixed(1)}%</span><i><b style="width:${Math.min(100, c)}%;background:${confColor(c)}"></b></i></div>`;
const fmtDate = d => d ? new Date(d.replace(" ", "T")).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "—";
const fmtDT = d => d ? new Date(d.replace(" ", "T")).toLocaleString("en-IN", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const CUR = { INR: "₹", USD: "US$", GBP: "£", EUR: "€", AUD: "A$", CAD: "C$", CHF: "CHF " };
const PER = { per_year: "per year", per_month: "per month", one_time: "one-time", full_course: "full course" };
const money = (v, cur) => v == null ? "" : (CUR[cur] || (cur + " ")) + (cur === "INR" ? Number(v).toLocaleString("en-IN") : Number(v).toLocaleString("en-US"));
function amountText(s) {
  if (s.amount_max == null && s.amount_min == null) return null;
  const a = s.amount_min !== s.amount_max && s.amount_min != null ? `${money(s.amount_min, s.amount_currency)} – ${money(s.amount_max, s.amount_currency)}` : money(s.amount_max ?? s.amount_min, s.amount_currency);
  return a + (s.amount_period ? " " + (PER[s.amount_period] || s.amount_period) : "");
}

let THEME = localStorage.getItem("theme");
if (THEME) document.documentElement.dataset.theme = THEME;
$("#theme").onclick = () => { const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme:dark)").matches ? "dark" : "light"); const n = cur === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = n; localStorage.setItem("theme", n); };
$("#mclose").onclick = () => $("#modal").hidden = true;
$("#modal").onclick = e => { if (e.target.id === "modal") $("#modal").hidden = true; };
addEventListener("keydown", e => { if (e.key === "Escape") $("#modal").hidden = true; });

// ------------------------------------------------------------------ routing
async function route() {
  const h = location.hash || "#/";
  document.querySelectorAll("#nav a").forEach(a => a.classList.toggle("on", (h === "#/" && a.dataset.r === "home") || h.startsWith("#/" + a.dataset.r)));
  try {
    if (h.startsWith("#/s/")) await detail(+h.slice(4));
    else if (h.startsWith("#/changes")) await changesPage();
    else if (h.startsWith("#/runs")) await runsPage();
    else if (h.startsWith("#/leads")) await leadsPage();
    else await home();
  } catch (e) { app.innerHTML = `<p class="pad">Could not load: ${esc(e.message)}</p>`; }
  window.scrollTo(0, 0);
}
addEventListener("hashchange", route);
async function banner() {
  const st = await api("/stats");
  $("#dbnote").textContent = `data as of ${st.today} · ${st.runs} crawl run${st.runs === 1 ? "" : "s"}`;
}

// ------------------------------------------------------------------ dashboard
let F = { q: "", status: "", label: "", type: "", sort: "confidence", order: "desc" };
async function home() {
  const st = await api("/stats");
  const kp = (n, l, cls = "") => `<div class="kpi ${cls}"><div class="n">${n}</div><div class="l">${l}</div></div>`;
  const maxType = Math.max(1, ...st.by_source_type.map(x => x.n));
  const statuses = [["ACTIVE", st.active], ["EXPIRING_SOON", st.expiring_soon], ["EXPIRED", st.expired], ["REVIEW_REQUIRED", st.status_review], ["NO_LONGER_VERIFIABLE", st.no_longer_verifiable]];
  const maxS = Math.max(1, ...statuses.map(x => x[1]));
  const hist = st.confidence_histogram; const maxH = Math.max(1, ...hist.map(x => x.n));
  app.innerHTML = `
  <h1>Scholarship Intelligence</h1>
  <p class="muted">Every record below is traced to text fetched from its official source. Last crawl: ${st.last_run ? fmtDT(st.last_run.started_at) : "—"}.</p>
  <div class="kpis">
    ${kp(st.total, "Total discovered")}
    ${kp(st.verified, "Verified (confidence ≥ 95%)", "ok")}
    ${kp(st.review_required, "Review required", "warn")}
    ${kp(st.active, "Active")}
    ${kp(st.expired, "Expired", "bad")}
    ${kp(st.recently_updated, "Recently updated (7 days)")}
    ${kp(st.avg_confidence ?? "—", "Average confidence %")}
    ${kp(st.official_verified, "Verified against official source")}
  </div>
  <div class="kpis small">
    ${kp(st.expiring_soon, "Expiring soon")}${kp(st.no_longer_verifiable, "No longer verifiable")}${kp(st.field_changes, "Field changes detected")}
    ${kp(st.rejected_extractions, "Unsupported claims rejected")}${kp(st.unresolved_leads, "Aggregator leads w/o official page")}${kp(st.runs, "Crawl runs")}
    ${kp(st.extraction_corrections || 0, "Extraction corrections")}
  </div>
  <div class="grid2">
    <div class="card"><h2>By source type</h2>${st.by_source_type.map(x => `<div class="bar"><span class="t">${TYPE_LABEL[x.source_type] || x.source_type}</span><span class="track"><span class="fill" style="width:${x.n / maxType * 100}%;display:block"></span></span><span class="v">${x.n}</span></div>`).join("")}</div>
    <div class="card"><h2>Lifecycle status</h2>${statuses.map(([k, n]) => `<div class="bar"><span class="t">${k.replace(/_/g, " ").toLowerCase()}</span><span class="track"><span class="fill" style="width:${n / maxS * 100}%;display:block;background:${k === "ACTIVE" ? "var(--ok)" : k === "EXPIRED" || k === "NO_LONGER_VERIFIABLE" ? "var(--bad)" : "var(--warn)"}"></span></span><span class="v">${n}</span></div>`).join("")}</div>
    <div class="card"><h2>Confidence distribution</h2>${hist.map(x => `<div class="bar"><span class="t">${x.bucket}–${x.bucket + 10}%</span><span class="track"><span class="fill" style="width:${x.n / maxH * 100}%;display:block;background:${confColor(x.bucket + 5)}"></span></span><span class="v">${x.n}</span></div>`).join("")}</div>
  </div>
  <h2>Scholarships</h2>
  <div class="filters">
    <input type="search" id="q" placeholder="Search name, provider, eligibility, domain…" value="${esc(F.q)}">
    <select id="fs"><option value="">All statuses</option>${["ACTIVE", "EXPIRING_SOON", "EXPIRED", "REVIEW_REQUIRED", "NO_LONGER_VERIFIABLE"].map(s => `<option ${F.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
    <select id="fl"><option value="">All verification</option><option value="VERIFIED" ${F.label === "VERIFIED" ? "selected" : ""}>VERIFIED</option><option value="REVIEW_REQUIRED" ${F.label === "REVIEW_REQUIRED" ? "selected" : ""}>REVIEW_REQUIRED</option></select>
    <select id="ft"><option value="">All source types</option>${Object.entries(TYPE_LABEL).filter(([k]) => k !== "AGGREGATOR").map(([k, v]) => `<option value="${k}" ${F.type === k ? "selected" : ""}>${v}</option>`).join("")}</select>
  </div>
  <div id="list"></div>`;
  const reload = () => { F.q = $("#q").value; F.status = $("#fs").value; F.label = $("#fl").value; F.type = $("#ft").value; loadList(); };
  let t; $("#q").oninput = () => { clearTimeout(t); t = setTimeout(reload, 220); };
  ["#fs", "#fl", "#ft"].forEach(s => $(s).onchange = reload);
  loadList();
}
async function loadList() {
  const p = new URLSearchParams({ q: F.q, status: F.status, label: F.label, type: F.type, sort: F.sort, order: F.order, limit: 500 });
  const d = await api("/scholarships?" + p);
  const th = (k, l) => `<th data-k="${k}">${l}${F.sort === k ? (F.order === "desc" ? " ↓" : " ↑") : ""}</th>`;
  $("#list").innerHTML = `<p class="muted">${d.total} scholarship${d.total === 1 ? "" : "s"}</p><table><thead><tr>${th("name", "Scholarship")}<th>Source</th><th>Amount</th>${th("deadline", "Deadline")}<th>Status</th>${th("confidence", "Confidence")}${th("verified", "Last verified")}</tr></thead><tbody>${d.items.map(s => `
    <tr data-id="${s.id}"><td><b>${esc(s.name)}</b><div class="sub">${esc(s.provider || "Provider not specified")}</div></td>
    <td>${chip(TYPE_LABEL[s.source_type] || s.source_type, "c-info")}<div class="sub">${esc(s.official_domain)}</div></td>
    <td>${amountText(s) ? esc(amountText(s)) : NS}</td><td>${s.closing_date ? fmtDate(s.closing_date) : NS}</td>
    <td>${statusChip(s.status)}<div class="sub">${labelChip(s.verification_label)}</div></td><td>${confBar(s.confidence)}</td><td>${fmtDate(s.last_verified_at)}</td></tr>`).join("")}</tbody></table>`;
  document.querySelectorAll("#list tbody tr").forEach(tr => tr.onclick = () => location.hash = "#/s/" + tr.dataset.id);
  document.querySelectorAll("#list th[data-k]").forEach(h => h.onclick = () => { const k = h.dataset.k; if (F.sort === k) F.order = F.order === "desc" ? "asc" : "desc"; else { F.sort = k; F.order = k === "name" ? "asc" : "desc"; } loadList(); });
}

// ------------------------------------------------------------------ detail
const FIELD_ROWS = [
  ["name", "Scholarship", s => s.name], ["provider", "Provider", s => s.provider],
  ["amount", "Amount / benefit", s => amountText(s)], ["benefit_text", "Benefit (as stated)", s => s.benefit_text, true],
  ["eligibility_text", "Eligibility", s => s.eligibility_text, true], ["academic_requirements", "Academic requirements", s => s.academic_requirements, true],
  ["education_levels", "Course / education level", s => (s.education_levels || []).map(x => chip(x.replace(/_/g, " ").toLowerCase(), "c-info")).join(" ") || null, false, true],
  ["courses", "Courses", s => s.courses, true],
  ["income", "Income criteria", s => s.income_max_inr != null ? "Family income up to " + money(s.income_max_inr, "INR") : null],
  ["age", "Age criteria", s => (s.age_min != null || s.age_max != null) ? [s.age_min != null ? "min " + s.age_min : "", s.age_max != null ? "max " + s.age_max : ""].filter(Boolean).join(", ") + " years" : null],
  ["gender", "Gender criteria", s => s.gender], ["categories", "Category criteria", s => (s.categories || []).map(x => chip(x, "c-gray")).join(" ") || null, false, true],
  ["domicile", "Domicile / state", s => s.domicile, true], ["institution_requirements", "Institution requirements", s => s.institution_requirements, true],
  ["opening_date", "Opening date", s => s.opening_date ? fmtDate(s.opening_date) : null], ["closing_date", "Closing date", s => s.closing_date ? fmtDate(s.closing_date) : null],
  ["deadline_note", "Deadline note", s => s.deadline_note, true], ["documents_required", "Documents required", s => s.documents_required, true],
  ["selection_process", "Selection process", s => s.selection_process, true], ["renewal_requirements", "Renewal requirements", s => s.renewal_requirements, true],
  ["application_url", "Application URL", s => s.application_url ? `<a href="${esc(s.application_url)}" target="_blank" rel="noopener">${esc(s.application_url)}</a>` : null, false, true],
];
async function detail(id) {
  const d = await api("/scholarships/" + id); const s = d.scholarship;
  const evBy = {}; d.evidence.forEach(e => evBy[e.field] = e);
  const rowsHtml = FIELD_ROWS.map(([key, label, get, long, html]) => {
    const v = get(s); const e = evBy[key];
    let right = "";
    if (e && e.evidence_kind === "QUOTE") right = `<button class="tracebtn" data-e="${e.id}">trace ↗</button>`;
    else if (e && e.evidence_kind === "ABSENT") right = `<span class="chip c-gray" title="${esc(e.quote)}">absent ✓</span>`;
    else if (e && e.evidence_kind === "ABSENT_UNVERIFIED") right = `<span class="chip c-warn" title="${esc(e.quote)}">unverified</span>`;
    const shown = v == null || v === "" ? NS : (html ? v : esc(v));
    return `<dt>${label}</dt><dd><div class="val ${long && v ? "long" : ""}">${shown}</div>${right}</dd>`;
  }).join("");
  const bd = d.breakdown.filter(b => !["cap", "gate", "conflict_penalty"].includes(b.component));
  const extra = d.breakdown.filter(b => ["cap", "gate", "conflict_penalty"].includes(b.component));
  const compHtml = bd.map(b => `<div class="comp"><div class="row"><span>${b.component.replace(/_/g, " ")}</span><span>${b.points.toFixed(1)} / ${b.weight}</span></div><div class="track"><div class="fill" style="width:${b.score * 100}%;background:${confColor(b.score * 100)}"></div></div><p>${esc(b.detail)}</p></div>`).join("");
  const extraHtml = extra.map(b => `<div class="note ${b.component === "conflict_penalty" ? "c-bad" : "c-warn"}"><b>${b.component === "cap" ? "Cap" : b.component === "gate" ? "Gate" : "Penalty"}:</b> ${esc(b.detail)}${b.points ? ` (${b.points} pts)` : ""}</div>`).join("");
  const ch = d.changes.map(c => {
    if (c.change_type === "NEW") return `<li class="new"><b>Discovered</b> <span class="muted">${fmtDT(c.detected_at)}</span><div class="muted">${esc(c.note || "")}</div></li>`;
    if (c.change_type === "STATUS_CHANGED") return `<li class="chg"><b>Status changed</b> <span class="muted">${fmtDT(c.detected_at)}</span><div class="diff"><span class="old">${esc(c.old_value)}</span><span>→</span><span class="new">${esc(c.new_value)}</span></div><div class="muted">${esc(c.note || "")}</div>${c.new_evidence ? `<details><summary>evidence</summary><div class="ctx">${esc(c.new_evidence)}</div></details>` : ""}</li>`;
    const corrected = (c.note || "").startsWith("EXTRACTION_CORRECTION:");
    return `<li class="chg"><b>${corrected ? "EXTRACTION CORRECTION" : c.change_type === "FIELD_CHANGED" ? "CHANGE DETECTED" : c.change_type.replace(/_/g, " ")}</b> · ${esc(c.field)} <span class="muted">${fmtDT(c.detected_at)}</span>
      <div class="diff"><span class="old">${c.old_value == null ? "Not specified" : esc(c.old_value.slice(0, 160))}</span><span>→</span><span class="new">${c.new_value == null ? "Not specified" : esc(c.new_value.slice(0, 160))}</span></div>
      <div class="muted">Source: <a href="${esc(c.source_url)}" target="_blank" rel="noopener">${esc(c.source_url)}</a></div>
      ${c.note ? `<div class="muted">${esc(c.note)}</div>` : ""}
      ${c.new_evidence || c.old_evidence ? `<details><summary>evidence (old → new)</summary><div class="ctx"><b>Old:</b> ${esc(c.old_evidence || "—")}\n\n<b>New:</b> ${esc(c.new_evidence || "—")}</div></details>` : ""}</li>`;
  }).join("") || '<li class="muted">No history yet.</li>';
  const rej = d.rejected.map(r => `<tr><td>${esc(r.field)}</td><td>${esc((r.proposed_value || "").slice(0, 80))}</td><td>${esc(r.reason)}</td><td class="mono">${esc(r.extractor)}</td></tr>`).join("");
  const p = d.page, src = d.source;
  app.innerHTML = `
  <div class="crumbs"><a href="#/">← All scholarships</a></div>
  <div class="head card"><div>
    <h1>${esc(s.name)}</h1><div class="muted">${esc(s.provider || "Provider not specified")}</div>
    <p style="margin:10px 0 0">${statusChip(s.status)} ${labelChip(s.verification_label)} ${chip(TYPE_LABEL[s.source_type] || s.source_type, "c-info")} ${s.official_source_verified ? chip("official source verified", "c-ok") : chip("official source NOT verified", "c-bad")}</p>
    <p class="muted" style="margin:8px 0 0">${esc(s.status_reason || "")}</p>
    <p style="margin:8px 0 0"><a href="${esc(s.official_url)}" target="_blank" rel="noopener">Official source ↗</a>${s.application_url ? ` · <a href="${esc(s.application_url)}" target="_blank" rel="noopener">Application link ↗</a>` : ""}</p>
    <p class="muted" style="margin:6px 0 0">Last verified ${fmtDT(s.last_verified_at)} · first discovered ${fmtDT(s.first_discovered_at)} · discovered via <span class="mono">${esc(s.discovered_via)}</span></p>
  </div><div class="ring" style="--p:${s.confidence};--c:${confColor(s.confidence)}"><span>${s.confidence.toFixed(1)}%</span></div></div>
  <div class="detail"><div>
    <div class="card"><h2>Scholarship details</h2><dl class="fields">${rowsHtml}</dl></div>
    <div class="card" style="margin-top:16px"><h2>Change history</h2><ul class="timeline">${ch}</ul>
      ${d.status_history.length > 1 ? `<details><summary>status history (${d.status_history.length})</summary><ul class="timeline">${d.status_history.map(h => `<li>${h.old_status ? esc(h.old_status) + " → " : ""}<b>${esc(h.new_status)}</b> <span class="muted">${fmtDT(h.at)}</span><div class="muted">${esc(h.reason || "")}</div></li>`).join("")}</ul></details>` : ""}</div>
  </div><div>
    <div class="card"><h2>Why this score?</h2>
      <p class="muted" style="margin-top:0">Computed from checkable evidence (never an LLM-generated number). VERIFIED requires ≥ 95 and no cap or gate.</p>
      ${compHtml}${extraHtml}
      <div class="row" style="display:flex;justify-content:space-between;font-weight:700;border-top:1px solid var(--line);padding-top:8px;margin-top:8px"><span>Final confidence</span><span>${s.confidence.toFixed(1)}%</span></div></div>
    <div class="card" style="margin-top:16px"><h2>Official source</h2>
      <p style="margin:0 0 6px"><span class="mono">${esc(s.official_domain)}</span> ${src ? chip("tier " + src.tier, "c-gray") : ""}</p>
      <p class="muted" style="margin:0 0 8px">${esc(src ? src.reason : "")}</p>
      ${p ? `<p class="muted" style="margin:0">Snapshot: ${esc(p.title || "")}<br>HTTP ${p.status_code} · ${p.text_len.toLocaleString()} chars · fetched ${fmtDT(p.fetched_at)}${p.tls_verified ? "" : " · TLS certificate not verifiable"}</p>` : ""}</div>
    <div class="card" style="margin-top:16px"><h2>Unsupported claims rejected</h2>
      <p class="muted" style="margin-top:0">Anything an extractor proposed that could not be found verbatim in the source (or contradicted its quote) is discarded and logged.</p>
      ${rej ? `<table><thead><tr><th>Field</th><th>Proposed</th><th>Why rejected</th><th>By</th></tr></thead><tbody>${rej}</tbody></table>` : '<p class="muted">None for this record.</p>'}</div>
  </div></div>`;
  document.querySelectorAll(".tracebtn").forEach(b => b.onclick = () => trace(+b.dataset.e, s, d));
}

async function trace(eid, s, d) {
  const e = d.evidence.find(x => x.id === eid); const c = await api(`/evidence/${eid}/context`);
  const val = typeof e.value === "object" ? JSON.stringify(e.value) : e.value;
  $("#mbody").innerHTML = `<h2>Evidence trace — ${esc(e.label)}</h2>
    <div class="chain"><div><b>1 · Database</b>scholarships #${s.id}<br>${esc(s.name.slice(0, 60))}</div>
      <div><b>2 · Official source</b><a href="${esc(e.source_url)}" target="_blank" rel="noopener">${esc(s.official_domain)}</a><br>fetched ${fmtDT(c.fetched_at)}</div>
      <div><b>3 · Evidence</b>chars ${e.char_start ?? "—"}–${e.char_end ?? "—"} · match ${Number(e.match_score).toFixed(0)}%<br>by ${esc((e.extractor || "").split(",").length)} extractor(s)</div>
      <div><b>4 · Extracted value</b>${esc(String(val).slice(0, 120))}</div></div>
    ${c.found ? `<div class="ctx">…${esc(c.before)}<mark>${esc(c.quote)}</mark>${esc(c.after)}…</div>` : `<div class="ctx"><mark>${esc(e.quote)}</mark></div><p class="muted">(link evidence – the URL is present among the page's links)</p>`}
    <p class="muted" style="margin-top:10px">Extractors that produced/confirmed this value: <span class="mono">${esc(e.extractor)}</span></p>
    ${e.source_url ? `<p><a href="${esc(e.source_url)}" target="_blank" rel="noopener">Open official source ↗</a></p>` : ""}`;
  $("#modal").hidden = false;
}

// ------------------------------------------------------------------ other pages
async function changesPage() {
  const ch = await api("/changes?limit=300");
  app.innerHTML = `<h1>Change feed</h1><p class="muted">Source changes and extraction corrections are distinguished. Each difference retains its old value, new value, date, source and evidence.</p>
  <table><thead><tr><th>Detected</th><th>Scholarship</th><th>Type</th><th>Field</th><th>Old → New</th></tr></thead><tbody>${ch.map(c => `
    <tr data-id="${c.scholarship_id}"><td>${fmtDT(c.detected_at)}</td><td><b>${esc(c.scholarship_name)}</b><div class="sub">${esc(c.official_domain)}</div></td>
    <td>${chip((c.note || "").startsWith("EXTRACTION_CORRECTION:") ? "EXTRACTION CORRECTION" : c.change_type === "FIELD_CHANGED" ? "CHANGE DETECTED" : c.change_type.replace(/_/g, " "), c.change_type === "NEW" ? "c-ok" : "c-warn")}</td>
    <td>${esc(c.field)}</td><td>${c.change_type === "NEW" ? '<span class="muted">newly discovered</span>' : `<div class="diff"><span class="old">${c.old_value == null ? "Not specified" : esc(c.old_value.slice(0, 80))}</span><span>→</span><span class="new">${c.new_value == null ? "Not specified" : esc(c.new_value.slice(0, 80))}</span></div>`}</td></tr>`).join("")}</tbody></table>`;
  document.querySelectorAll("tbody tr").forEach(tr => tr.onclick = () => location.hash = "#/s/" + tr.dataset.id);
}
async function runsPage() {
  const r = await api("/runs");
  app.innerHTML = `<h1>Crawl runs</h1><table><thead><tr><th>#</th><th>Started</th><th>Mode</th><th>Label</th><th>Summary</th></tr></thead><tbody>${r.map(x => `<tr><td>${x.id}</td><td>${fmtDT(x.started_at)}</td><td>${esc(x.mode)}</td><td>${esc(x.label || "")}</td>
    <td class="mono">${x.stats ? esc(Object.entries(x.stats).filter(([k, v]) => typeof v === "number").map(([k, v]) => k + "=" + v).join("  ")) : "running…"}</td></tr>`).join("")}</tbody></table>`;
}
async function leadsPage() {
  const [l, src] = await Promise.all([api("/leads"), api("/sources")]);
  app.innerHTML = `<h1>Leads &amp; source classification</h1>
  <h2>Aggregator leads that could not be resolved to an official page</h2><p class="muted">These names were seen on aggregators/search results only. They are <b>not</b> in the repository because no official source could be established.</p>
  <table><thead><tr><th>Lead</th><th>Seen on</th><th>Reason</th></tr></thead><tbody>${l.map(x => `<tr><td>${esc(x.name)}</td><td><a href="${esc(x.lead_url)}" target="_blank" rel="noopener">${esc(x.lead_domain)}</a></td><td>${esc(x.reason)}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">None</td></tr>'}</tbody></table>
  <h2 style="margin-top:24px">Domain registry (source classification)</h2>
  <table><thead><tr><th>Domain</th><th>Type</th><th>Tier</th><th>Authority</th><th>Reason</th><th>Records</th></tr></thead><tbody>${src.map(x => `<tr><td class="mono">${esc(x.domain)}</td><td>${esc(TYPE_LABEL[x.source_type] || x.source_type)}</td><td>${esc(x.tier)}</td><td>${x.authority}</td><td>${esc(x.reason)}</td><td>${x.n}</td></tr>`).join("")}</tbody></table>`;
}
banner().then(route);
