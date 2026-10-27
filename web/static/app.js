"use strict";
// NJ Wholesale Cockpit — vanilla JS, hash routes: #/ (dashboard, ?tab=) and #/lead/<id>[?contact=<id>]

const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v === null || v === undefined || Number.isNaN(v) ? "—" : "$" + Math.round(v).toLocaleString());
const kmoney = (v) => (v === null || v === undefined ? "—" : v >= 1e6 ? "$" + (v / 1e6).toFixed(2) + "M" : "$" + Math.round(v / 1000) + "K");
const pct = (v) => (v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%");
const label = (s) => esc(String(s ?? "").replaceAll("_", " ").toLowerCase());
const fmtDate = (s) => (s ? new Date(s).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—");
const day = (s) => (s ? new Date(s + "T12:00:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) : "—");

let STATE = { tab: "all", dash: null, lead: null, outcome: null };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 409) throw new Error(data.detail ? JSON.stringify(data.detail) : res.statusText);
  return { status: res.status, data };
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 5000);
}

function verdictPill(v) {
  if (!v) return '<span class="pill">not valued</span>';
  const cls = { STRONG: "good", WORKABLE: "good", WORKABLE_82_PCT_FLOOR: "acc", TIGHT: "warn", SHORT_SALE_ONLY: "warn",
    EQUITY_RESCUE_REFERRAL: "vio", FRFPA_BLOCKED: "bad", PASS: "bad" }[v] || "";
  return `<span class="pill ${cls}">${label(v)}</span>`;
}
function contactPill(s) {
  const cls = s.startsWith("phone") ? (s.includes("unscrubbed") ? "warn" : "good") : s.startsWith("agent") ? "acc" : s.includes("DNC") ? "bad" : "";
  return `<span class="pill ${cls}">${esc(s)}</span>`;
}
function daysBadge(n) {
  if (n === null || n === undefined) return "";
  return `<span class="days ${n <= 7 ? "hot" : n <= 14 ? "warm" : ""}">${n}d</span>`;
}

// -- router ---------------------------------------------------------------------------------------
async function route() {
  const hash = location.hash || "#/";
  const [path, query] = hash.slice(1).split("?");
  const params = new URLSearchParams(query || "");
  try {
    const m = path.match(/^\/lead\/(\d+)/);
    if (m) return await renderLead(+m[1], params.get("contact"));
    STATE.tab = params.get("tab") || STATE.tab || "all";
    await renderDashboard(params.get("q"));
  } catch (e) {
    app.innerHTML = `<div class="card bad">Error: ${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);
$("#search").addEventListener("submit", (e) => {
  e.preventDefault();
  location.hash = `#/?tab=all&q=${encodeURIComponent($("#q").value)}`;
});

function statusBar(dnc) {
  const ftc = dnc.loaded ? (dnc.fresh ? `<span class="pill good">FTC DNC ${esc(dnc.list_date)}</span>`
    : `<span class="pill bad">FTC DNC stale (${esc(dnc.list_date)})</span>`) : '<span class="pill warn">FTC DNC not loaded</span>';
  const hours = dnc.calling_hours ? '<span class="pill good">calling hours</span>' : '<span class="pill bad">outside 8am–9pm</span>';
  $("#status").innerHTML = ftc + hours;
}

// -- dashboard ------------------------------------------------------------------------------------
async function renderDashboard(q) {
  const { data: d } = await api("/api/dashboard");
  STATE.dash = d;
  statusBar(d.dnc);
  const f = d.funnel;
  const stages = Object.keys(f).filter((s) => s !== "NEW");
  const max = Math.max(1, ...stages.map((s) => f[s]));
  app.innerHTML = `
    <div class="kpis">
      <div class="kpi"><b>${d.open_leads.toLocaleString()}</b><span>open leads (${f.NEW.toLocaleString()} new)</span></div>
      <div class="kpi"><b>${d.kpis.contact_ready_top100}%</b><span>top-100 contact-ready</span></div>
      <div class="kpi"><b>${d.kpis.deals}</b><span>deals with an offer verdict</span></div>
      <div class="kpi"><b>${d.kpis.followups_due}</b><span>follow-ups due now</span></div>
      <div class="kpi"><b>${d.kpis.sales_21d}</b><span>sheriff sales ≤ 21 days</span></div>
    </div>
    <div class="grid g-dash">
      <div>
        <div class="card">
          <div class="row spread"><h2>Call queue</h2><span class="muted">${d.call_queue.length} to dial · follow-ups first</span></div>
          <div class="tablewrap"><table>
            <thead><tr><th>#</th><th>Property</th><th>Why now</th><th>Contact</th><th>Verdict</th><th class="num">Priority</th></tr></thead>
            <tbody>${d.call_queue.map((r, i) => `
              <tr class="click" data-href="#/lead/${r.id}${r.contact ? "?contact=" + r.contact.id : ""}">
                <td>${i + 1}</td>
                <td><b>${esc(r.address)}</b><br><span class="muted">${esc(r.town)}, ${esc(r.county)}</span></td>
                <td>${esc(r.reason)}${r.due ? "<br><span class='muted'>due " + fmtDate(r.due) + "</span>" : ""}</td>
                <td>${r.contact ? `${esc(r.contact.name || "—")}<br><span class="mono">${esc(r.contact.phone_display || "")}</span> ${label(r.contact.type)}` : "—"}</td>
                <td>${verdictPill(r.verdict)}</td><td class="num">${r.priority}</td>
              </tr>`).join("") || '<tr><td colspan="6" class="muted">Nobody to call: skip-trace the top leads (manage.py skiptrace --export-top 50).</td></tr>'}
            </tbody></table></div>
        </div>
        <div class="card">
          <div class="tabs">${Object.entries(d.tabs).map(([k, v]) => `<button data-tab="${k}" class="${k === STATE.tab ? "on" : ""}">${esc(v)}</button>`).join("")}</div>
          <div id="leadtable" class="tablewrap"><p class="muted">Loading…</p></div>
        </div>
      </div>
      <div>
        <div class="card">
          <h2>Pipeline</h2>
          <div class="funnel">${stages.map((s) => `<div title="${label(s)}: ${f[s]}" style="height:${Math.max(3, (f[s] / max) * 100)}%"></div>`).join("")}</div>
          <div class="funnel-labels">${stages.map((s) => `<span title="${label(s)}">${f[s]}<br>${label(s)}</span>`).join("")}</div>
        </div>
        <div class="card countdown">
          <div class="row spread"><h2>Sheriff-sale countdown</h2><span class="muted">next 21 days</span></div>
          <ul>${d.countdown.map((r) => `
            <li class="click" data-href="#/lead/${r.id}">${daysBadge(r.days_to_sale)}
              <div style="flex:1"><a href="#/lead/${r.id}"><b>${esc(r.address)}</b></a> <span class="muted">${esc(r.town)}</span><br>
              <span class="muted">${day(r.sale_date)} · ${contactPill(r.contact_status)} ${verdictPill(r.verdict)}</span></div>
              <span class="mono muted">${r.priority}</span></li>`).join("") || '<li class="muted">No sales in the window.</li>'}
          </ul>
        </div>
      </div>
    </div>`;
  app.querySelectorAll("[data-href]").forEach((el) => el.addEventListener("click", (e) => {
    if (e.target.tagName !== "A") location.hash = el.dataset.href;
  }));
  app.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
    STATE.tab = b.dataset.tab;
    app.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
    loadTab(null);
  }));
  await loadTab(q);
}

async function loadTab(q) {
  const box = $("#leadtable");
  const { data } = await api(`/api/leads?tab=${encodeURIComponent(STATE.tab)}&limit=150${q ? "&q=" + encodeURIComponent(q) : ""}`);
  box.innerHTML = `<p class="muted">${data.total.toLocaleString()} leads${q ? ` matching “${esc(q)}”` : ""}</p>
    <table><thead><tr><th>Property</th><th>Signals / why</th><th>Stage</th><th>Contact</th><th>Verdict</th>
    <th class="num">ARV</th><th class="num">Anchor</th><th class="num">Pri</th></tr></thead>
    <tbody>${data.rows.map((r) => `
      <tr class="click" data-href="#/lead/${r.id}">
        <td><b>${esc(r.address)}</b><br><span class="muted">${esc(r.town)} · ${esc(r.county)}</span></td>
        <td>${r.days_to_sale !== null && r.days_to_sale >= 0 ? daysBadge(r.days_to_sale) + " " : ""}<span class="muted">${esc(r.reasons[0] || r.source || "")}</span></td>
        <td>${label(r.stage)}${r.next_followup_at ? "<br><span class='muted'>next " + fmtDate(r.next_followup_at) + "</span>" : ""}</td>
        <td>${contactPill(r.contact_status)}</td>
        <td>${verdictPill(r.verdict)}${r.frfpa_track ? `<br><span class="muted">${label(r.frfpa_track)}</span>` : ""}</td>
        <td class="num">${kmoney(r.arv)}</td><td class="num">${kmoney(r.anchor)}</td><td class="num">${r.priority}</td>
      </tr>`).join("")}</tbody></table>`;
  box.querySelectorAll("[data-href]").forEach((el) => el.addEventListener("click", () => (location.hash = el.dataset.href)));
}

// -- lead battle station --------------------------------------------------------------------------
async function renderLead(id, contactId) {
  const { data: d } = await api(`/api/leads/${id}${contactId ? "?contact_id=" + contactId : ""}`);
  STATE.lead = d;
  STATE.outcome = null;
  if (!STATE.dash) api("/api/dashboard").then(({ data }) => statusBar(data.dnc)).catch(() => {});
  const L = d.lead, P = d.property, V = d.valuation, C = d.card;
  const sel = contactId ? +contactId : null;
  app.innerHTML = `
    <div class="row spread" style="margin-bottom:12px">
      <div><a href="#/">← Dashboard</a>
        <h1>${esc(L.address)}, ${esc(L.town)} <span class="muted">${esc(L.county)} · ${esc(L.zip || "")}</span></h1>
        <div class="row">${verdictPill(L.verdict)} <span class="pill">${label(L.stage)}</span>
          <span class="pill acc">priority ${L.priority}</span> ${L.days_to_sale !== null ? daysBadge(L.days_to_sale) + " sale " + day(L.sale_date) : ""}
          <span class="pill">playbook ${esc(d.playbook)}</span> ${L.next_followup_at ? `<span class="pill warn">next touch ${fmtDate(L.next_followup_at)}</span>` : ""}</div>
      </div>
    </div>
    <div class="grid g-lead">
      <div>${leftColumn(d)}</div>
      <div>${contactsCard(d, sel)}${scriptCard(C)}</div>
      <div>${loggerCard(d)}${docsCard(d)}${historyCard(d)}</div>
    </div>`;
  wireLead(d);
}

function leftColumn(d) {
  const L = d.lead, P = d.property, V = d.valuation, F = d.frfpa;
  const signals = d.signals.map((s) => `<li><b>${label(s.type)}</b>${s.auction_date ? " · " + day(s.auction_date) : ""}${s.amount ? " · " + money(s.amount) : ""}
    ${s.plaintiff ? `<br><span class="muted">${esc(s.plaintiff)}</span>` : ""}${s.defendant ? `<br><span class="muted">v. ${esc(s.defendant)}</span>` : ""}
    ${s.adjournments ? `<br><span class="muted">${s.adjournments} adjournment(s)</span>` : ""}${s.dom ? `<br><span class="muted">${s.dom} DOM at ${money(s.list_price)}</span>` : ""}
    ${s.url ? ` <a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">source ↗</a>` : ""}</li>`).join("");
  const calc = V ? `
    <div class="card" id="calc">
      <h2>Deal calculator</h2>
      ${slider("arv", "ARV", V.arv_before_tax_drag || V.arv, Math.round((V.arv || 1e5) * 0.5), Math.round((V.arv || 1e5) * 1.5), 1000)}
      ${slider("repairs", "Repairs", V.repairs || 0, 0, Math.max(150000, Math.round((V.repairs || 0) * 2.5)), 500)}
      ${slider("mao_pct_conservative", "MAO % of ARV", d.deal_cfg.mao_pct_conservative, 0.55, 0.85, 0.01, true)}
      ${slider("anchor_discount_pct", "Anchor below MAO", d.deal_cfg.anchor_discount_pct, 0, 0.25, 0.01, true)}
      ${slider("debt", "Debt / upset", V.debt || 0, 0, Math.max(100000, Math.round((V.debt || 0) * 2)), 500)}
      <label><input type="checkbox" id="c-frfpa" ${F.applies ? "checked" : ""}> Price as FRFPA-covered (≥ 82% of as-is ${money(V.as_is)})</label>
      <div class="calc-out" id="calc-out"></div>
      <p class="muted" style="font-size:12px">Comps: ${V.comp_count} (${esc(V.comp_source || "")}) · confidence ${Math.round(V.confidence || 0)} · valued ${esc(V.valued_on || "")}<br>${(V.comp_notes || []).map(esc).join("<br>")}</p>
    </div>` : `<div class="card"><h2>Deal calculator</h2><p class="muted">Not underwritten yet — run <span class="mono">python manage.py underwrite --lead ${L.id}</span>.</p></div>`;
  return `
    <div class="card"><h2>Property</h2><dl class="kv">
      <dt>Parcel</dt><dd class="mono">${esc(P.parcel_id)}</dd><dt>Block/Lot</dt><dd>${esc(P.block)} / ${esc(P.lot)}${P.qual ? " / " + esc(P.qual) : ""}</dd>
      <dt>Type</dt><dd>${esc(P.building || "")} · ${P.units || "?"} unit(s) · built ${P.year_built || "?"}</dd>
      <dt>Size</dt><dd>${P.sqft ? P.sqft.toLocaleString() + " sq ft" : "—"}${P.beds ? ` · ${P.beds} bd` : ""}${P.baths ? ` / ${P.baths} ba` : ""}</dd>
      <dt>Taxes</dt><dd>${money(P.taxes)}/yr · assessed ${money(P.assessed)}</dd>
      <dt>Last sale</dt><dd>${P.last_sale_date ? esc(P.last_sale_date) + " · " + money(P.last_sale_price) : "—"}</dd>
      <dt>Owner</dt><dd>${esc(P.owner || "unknown (NJGIN redacts names)")}</dd>
      <dt>Mailing</dt><dd>${esc(P.owner_mailing || "—")} ${P.out_of_state ? '<span class="pill warn">out of state</span>' : P.absentee ? '<span class="pill">absentee</span>' : ""}</dd>
      ${P.lat ? `<dt>Map</dt><dd><a href="https://www.google.com/maps/search/?api=1&query=${P.lat},${P.lon}" target="_blank" rel="noopener noreferrer">open map ↗</a></dd>` : ""}
    </dl><h3 style="margin-top:10px">Signals</h3><ul style="margin:0;padding-left:18px">${signals || "<li class='muted'>none</li>"}</ul></div>
    <div class="card"><h2>FRFPA / occupancy</h2>
      <div class="row"><span class="pill ${F.applies ? "warn" : "good"}">${label(F.track)}</span><span class="pill">${esc(F.confidence)}</span>
        ${L.occupancy_confirmed_at ? `<span class="pill good">confirmed ${label(L.occupancy)}</span>` : ""}</div>
      <p>${esc(F.basis)}</p>${F.verify.length ? `<ul>${F.verify.map((v) => `<li class="flag">${esc(v)}</li>`).join("")}</ul>` : ""}
      ${V && V.exempt_scenario ? `<p class="muted">If nobody in the family lives there: ${label(V.exempt_scenario.verdict)}, anchor ${money(V.exempt_scenario.anchor_offer)}, MAO ${money(V.exempt_scenario.mao_conservative)}.</p>` : ""}
      <label>Record who lives there (from the call, agent, photos or Street View)</label>
      <div class="row"><select id="occ-status">${d.occupancy_values.filter((o) => o !== "UNKNOWN").map((o) => `<option value="${o}">${label(o)}</option>`).join("")}</select>
      <input type="text" id="occ-source" placeholder="source, e.g. 'seller on 10/27 call'" style="flex:1">
      <button class="btn" id="occ-save">Save &amp; re-price</button></div>
    </div>
    ${calc}
    <div class="card"><h2>Matched cash buyers</h2>${d.buyers.length ? `<table><tbody>${d.buyers.map((b) => `
      <tr><td>${b.rank}</td><td><b>${esc(b.name)}</b><br><span class="muted">${esc(b.reasons.join(" · "))}</span></td>
      <td class="num">${b.score}<br><a href="#" data-pitch="${b.id}">pitch</a></td></tr>`).join("")}</tbody></table>
      <div id="pitch"></div>` : '<p class="muted">No matches (only deals with an offer verdict are matched — run python -m engine.buyer_matcher).</p>'}</div>
    <div class="card"><h2>Why it ranks</h2><ul>${((L.score || {}).reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      ${L.brief ? `<p class="muted">${esc(L.brief)}</p>` : ""}</div>`;
}

function slider(key, name, value, min, max, step, isPct = false) {
  const v = Math.min(Math.max(value ?? min, min), max);
  return `<div class="slider"><div class="row"><span>${esc(name)}</span><b id="sv-${key}">${isPct ? pct(v) : money(v)}</b></div>
    <input type="range" id="s-${key}" data-key="${key}" data-pct="${isPct ? 1 : 0}" min="${min}" max="${max}" step="${step}" value="${v}"></div>`;
}

function contactsCard(d, sel) {
  const cards = d.contacts.map((c) => `
    <div class="contact ${sel === c.id ? "sel" : ""}">
      <div class="row spread"><b>${esc(c.name || "—")}</b><span class="pill">${label(c.type)}</span></div>
      ${c.company ? `<div class="muted">${esc(c.company)}</div>` : ""}
      ${c.phone ? `<div class="row"><a class="phone" href="${c.callable ? "tel:+1" + esc(c.phone) : "#"}">${esc(c.phone_display)}</a>
        <span class="pill">${esc(c.phone_type || "")}</span>
        <span class="pill ${c.dnc_status === "dnc" ? "bad" : c.callable ? (c.call_note === "ok" ? "good" : "warn") : "bad"}">${esc(c.dnc_status || "unchecked")}</span></div>
        ${c.call_note !== "ok" ? `<div class="${c.callable ? "warn" : "bad"}" style="font-size:12px">${esc(c.call_note)}</div>` : ""}` : ""}
      ${c.email ? `<div><a href="mailto:${esc(c.email)}">${esc(c.email)}</a></div>` : ""}
      ${c.mailing_address ? `<div class="muted">✉ ${esc(c.mailing_address)}</div>` : ""}
      ${c.notes ? `<div class="muted" style="font-size:12px">${esc(c.notes)}</div>` : ""}
      ${Object.keys(c.links).length ? `<div class="row" style="font-size:12px">${Object.entries(c.links).map(([k, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${label(k)} ↗</a>`).join(" ")}</div>` : ""}
      <div class="row" style="margin-top:6px"><a class="btn" href="#/lead/${d.lead.id}?contact=${c.id}">script for this contact</a></div>
    </div>`).join("");
  const st = d.skiptrace;
  return `<div class="card"><div class="row spread"><h2>Contacts</h2><span class="muted">${d.contacts.length}</span></div>
    ${cards || '<p class="muted">No contacts yet.</p>'}
    <h3 style="margin-top:12px">Skip-trace ${st.target ? "· " + esc(st.target.name) + " (" + label(st.target.role) + ")" : ""}</h3>
    <div class="row" style="font-size:13px">${Object.entries(st.links || {}).filter(([, u]) => u).map(([k, u]) => `<a class="btn" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${label(k)} ↗</a>`).join("") || '<span class="muted">No name to search yet.</span>'}</div>
    <label>Paste the people-search result (phones, emails, relatives are parsed and DNC-checked)</label>
    <textarea id="paste" placeholder="Paste the record text here…"></textarea>
    <div class="row" style="margin-top:6px"><button class="btn" id="paste-save">Import</button></div></div>`;
}

function scriptCard(C) {
  const offer = C.offer || {};
  return `<div class="card script"><div class="row spread"><h2>${esc(C.title)}</h2></div>
    <p><b>Calling:</b> ${esc(C.contact.name || "—")} ${C.contact.phone ? `<span class="mono">${esc(C.contact.phone)}</span>` : ""}</p>
    <h3>Open (first 30 seconds)</h3><p>${esc(C.opening)}</p><blockquote>${esc(C.disclosure)}</blockquote>
    <h3>Discovery</h3><ul>${C.discovery.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>
    <h3>Value</h3><ul>${C.value.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>
    <h3>Offer strategy</h3><p>${esc(offer.say || "")}</p>
    <h3>Close</h3><p>${esc(C.close)}</p>
    <h3>Objections</h3><div class="btns">${C.objections.map((o, i) => `<button class="btn" data-obj="${i}">${esc(o.label)}</button>`).join("")}</div>
    <div id="obj-resp"></div>
    ${C.dont_say.length ? `<h3 style="margin-top:12px">Never say</h3><ul>${C.dont_say.map((x) => `<li class="bad">${esc(x)}</li>`).join("")}</ul>` : ""}
    ${C.resources.length ? `<h3>Free resources to share</h3><ul>${C.resources.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    <h3>Compliance</h3><ul>${C.compliance.map((x) => `<li class="flag">${esc(x)}</li>`).join("")}</ul>
    <h3>Voicemail</h3><p class="muted">${esc(C.voicemail)}</p></div>`;
}

function loggerCard(d) {
  const contacts = d.contacts.filter((c) => c.phone || c.email);
  return `<div class="card" id="logger"><h2>Log this call</h2>
    <label>Who did you call?</label>
    <select id="lg-contact"><option value="">—</option>${contacts.map((c) => `<option value="${c.id}">${esc(c.name || c.phone_display)} (${label(c.type)})</option>`).join("")}</select>
    <label>Outcome</label>
    <div class="outcomes">${Object.entries(d.outcomes).map(([k, v]) => `<button class="btn" data-outcome="${k}">${esc(v)}</button>`).join("")}</div>
    <label>Notes</label><textarea id="lg-notes" placeholder="What did they say? Situation, condition, timeline…"></textarea>
    <details><summary class="muted" style="cursor:pointer;margin-top:8px">Debrief (fills the coach's checklist)</summary>
      <label>Who lives there?</label><select id="lg-occ"><option value="">not discussed</option>${d.occupancy_values.filter((o) => o !== "UNKNOWN").map((o) => `<option value="${o}">${label(o)}</option>`).join("")}</select>
      <label>Their number</label><input type="text" id="lg-ask" placeholder="e.g. 350k">
      <label>Timeline</label><input type="text" id="lg-timeline">
      <label>Condition</label><input type="text" id="lg-condition">
      <label>Everyone on title / decision makers</label><input type="text" id="lg-dm">
      <label>Motivation (their words)</label><input type="text" id="lg-motivation">
    </details>
    <label><input type="checkbox" id="lg-ai"> AI coach note (optional; sends redacted notes to Claude)</label>
    <div class="row" style="margin-top:8px"><button class="btn primary" id="lg-save" disabled>Save call</button></div>
    <div id="lg-result"></div></div>`;
}

const DOC_NAMES = { loi: "LOI", psa: "Purchase agreement (draft)", deal_sheet: "Deal sheet", assignment: "Assignment", jv: "JV agreement" };

function docsCard(d) {
  const names = DOC_NAMES;
  return `<div class="card"><h2>Documents</h2>
    <label>Price (blank = ${money(d.default_price)})</label><input type="number" id="doc-price" step="500" placeholder="${d.default_price || ""}">
    <div class="btns" style="margin-top:8px">${Object.entries(names).map(([k, v]) => {
      const g = d.gates[k];
      return `<button class="btn ${g.allowed ? "" : "bad"}" data-doc="${k}" title="${esc((g.reasons || []).join(" ") || (g.warnings || []).join(" "))}">${esc(v)}</button>`;
    }).join("")}</div>
    <div id="doc-result"></div>
    ${d.documents.length ? `<h3 style="margin-top:10px">Generated</h3><ul>${d.documents.map((x) => `<li>${esc(names[x.type] || x.type)} · ${money(x.price)} · ${fmtDate(x.at)}
      ${Object.entries(x.files).map(([e, u]) => `<a href="${esc(u)}" target="_blank">${e}</a>`).join(" ")}</li>`).join("")}</ul>` : ""}
    <h3 style="margin-top:12px">Record signed contract</h3>
    <div class="row"><input type="number" id="ct-price" placeholder="price" style="flex:1"><input type="date" id="ct-date" style="flex:1">
    <button class="btn" id="ct-save">Save</button></div>
    ${d.lead.contract_date ? `<p class="muted">Under contract ${esc(d.lead.contract_date)} at ${money(d.lead.offer_amount)} · review/cancellation ends ${esc(d.lead.attorney_review_ends)}</p>` : ""}</div>`;
}

function historyCard(d) {
  return `<div class="card"><h2>History</h2>${d.interactions.length ? `<ul class="timeline">${d.interactions.map((i) => `
    <li><b>${label(i.outcome)}</b> <span class="muted">${fmtDate(i.at)} · ${esc(i.channel)}</span>
    ${i.notes ? `<br>${esc(i.notes)}` : ""}${i.next ? `<br><span class="muted">next: ${fmtDate(i.next)}</span>` : ""}</li>`).join("")}</ul>` : '<p class="muted">No calls logged yet.</p>'}</div>`;
}

function coachHtml(c) {
  if (!c) return "";
  const list = (xs, cls = "") => (xs && xs.length ? `<ul>${xs.map((x) => `<li class="${cls}">${esc(x)}</li>`).join("")}</ul>` : "");
  const ai = c.ai ? `<h3 style="margin-top:8px">AI coach</h3><p>${esc(c.ai.read_on_seller)}</p>${list(c.ai.next_call_plan)}
    ${c.ai.questions_to_ask && c.ai.questions_to_ask.length ? "<b>Ask next time</b>" + list(c.ai.questions_to_ask) : ""}
    ${c.ai.watch_out ? `<p class="flag">${esc(c.ai.watch_out)}</p>` : ""}` : "";
  return `<div class="coach"><b>Next:</b> ${esc(c.next_action)}${list(c.compliance_flags, "flag")}
    ${c.missing_info && c.missing_info.length ? "<b>Still missing</b>" + list(c.missing_info) : ""}
    ${c.tips && c.tips.length ? "<b>Coaching</b>" + list(c.tips) : ""}${ai}</div>`;
}

function wireLead(d) {
  const id = d.lead.id;
  // objections
  app.querySelectorAll("[data-obj]").forEach((b) => b.addEventListener("click", () => {
    const o = d.card.objections[+b.dataset.obj];
    $("#obj-resp").innerHTML = `<div class="objection-resp"><b>${esc(o.label)}</b><p>${esc(o.response)}</p></div>`;
  }));
  // buyer pitch
  app.querySelectorAll("[data-pitch]").forEach((a) => a.addEventListener("click", async (e) => {
    e.preventDefault();
    const { data } = await api(`/api/buyers/${a.dataset.pitch}/pitch?lead_id=${id}`);
    $("#pitch").innerHTML = `<div class="objection-resp"><b>${esc(data.title)}</b><p>${esc(data.opening)}</p><blockquote>${esc(data.disclosure)}</blockquote>
      <ul>${data.discovery.map((q) => `<li>${esc(q)}</li>`).join("")}</ul><p class="flag">${esc(data.offer.say)}</p></div>`;
  }));
  // calculator
  const sliders = app.querySelectorAll("#calc input[type=range]");
  let timer = null;
  const recalc = async () => {
    const body = { frfpa: $("#c-frfpa").checked, fmv: d.valuation.as_is, annual_taxes: d.property.taxes,
      mao_pct_aggressive: d.deal_cfg.mao_pct_aggressive };
    sliders.forEach((s) => (body[s.dataset.key] = +s.value));
    if (!body.debt) body.debt = null;
    const { data: r } = await post("/api/calc", body);
    $("#calc-out").innerHTML = `
      <div><span class="muted">Anchor offer</span><b>${money(r.compliant_offer || r.anchor_offer)}</b></div>
      <div><span class="muted">Walk-away (MAO)</span><b>${money(r.mao_conservative)}</b></div>
      <div><span class="muted">Assignment fee</span><b>${money(r.track_fee || r.assignment_fee)}</b></div>
      <div><span class="muted">Buyer price / profit</span><b>${money(r.buyer_purchase_price)} / ${money(r.buyer_profit)}</b></div>
      <div><span class="muted">Spread over debt</span><b>${money(r.deal_spread)}</b></div>
      <div><span class="muted">Verdict</span><b>${label(r.verdict)}</b></div>
      ${r.frfpa_floor ? `<div style="grid-column:1/3"><span class="muted">FRFPA floor (82% FMV)</span><b>${money(r.frfpa_floor)}</b></div>` : ""}
      ${(r.flags || []).concat(r.track_notes || []).map((f) => `<div style="grid-column:1/3" class="flag">${esc(f)}</div>`).join("")}`;
  };
  sliders.forEach((s) => s.addEventListener("input", () => {
    $("#sv-" + s.dataset.key).textContent = s.dataset.pct === "1" ? pct(+s.value) : money(+s.value);
    clearTimeout(timer);
    timer = setTimeout(recalc, 150);
  }));
  if (sliders.length) { $("#c-frfpa").addEventListener("change", recalc); recalc(); }
  // occupancy
  $("#occ-save").addEventListener("click", async () => {
    const source = $("#occ-source").value.trim();
    if (source.length < 3) return toast("Say how you know (e.g. 'seller on 10/27 call').", true);
    try {
      const { data } = await post(`/api/leads/${id}/occupancy`, { status: $("#occ-status").value, source });
      toast(data.notes.join(" "));
      route();
    } catch (e) { toast(e.message, true); }
  });
  // skip-trace paste
  $("#paste-save").addEventListener("click", async () => {
    const text = $("#paste").value.trim();
    if (text.length < 5) return toast("Paste the record first.", true);
    try {
      const { data } = await post(`/api/leads/${id}/skiptrace`, { text });
      toast(`Imported ${data.parsed.contacts_added} contact(s): ${data.parsed.phones.length} phone(s), ${data.parsed.emails.length} email(s).`);
      route();
    } catch (e) { toast(e.message, true); }
  });
  // call logger
  app.querySelectorAll("[data-outcome]").forEach((b) => b.addEventListener("click", () => {
    STATE.outcome = b.dataset.outcome;
    app.querySelectorAll("[data-outcome]").forEach((x) => x.classList.toggle("on", x === b));
    $("#lg-save").disabled = false;
  }));
  const pre = new URLSearchParams((location.hash.split("?")[1]) || "").get("contact");
  if (pre) $("#lg-contact").value = pre;
  $("#lg-save").addEventListener("click", async () => {
    const body = { outcome: STATE.outcome, notes: $("#lg-notes").value, contact_id: +$("#lg-contact").value || null,
      occupancy: $("#lg-occ").value || null, use_ai: $("#lg-ai").checked,
      debrief: { occupancy: $("#lg-occ").value, asking_price: $("#lg-ask").value, timeline: $("#lg-timeline").value,
        condition: $("#lg-condition").value, decision_makers: $("#lg-dm").value, motivation: $("#lg-motivation").value } };
    $("#lg-save").disabled = true;
    try {
      const { data: r } = await post(`/api/leads/${id}/calls`, body);
      $("#lg-result").innerHTML = `<p><span class="pill">${label(r.stage)}</span> priority ${r.priority}
        ${r.next_followup_at ? ` · next touch <b>${fmtDate(r.next_followup_at)}</b>` : ""}${r.dnc_added ? ' · <span class="bad">added to DNC</span>' : ""}</p>
        ${r.notes.map((n) => `<p class="muted">${esc(n)}</p>`).join("")}
        ${r.suggested_docs.length ? `<div class="btns">${r.suggested_docs.map((t) => `<button class="btn good" data-doc="${t}">Generate ${esc(DOC_NAMES[t] || t)}</button>`).join("")}</div>` : ""}
        ${coachHtml(r.coach)}`;
      wireDocs(d, $("#lg-result"));
      toast("Call logged.");
    } catch (e) { toast(e.message, true); $("#lg-save").disabled = false; }
  });
  wireDocs(d, app);
  // contract
  $("#ct-save").addEventListener("click", async () => {
    const price = +$("#ct-price").value;
    if (!price) return toast("Enter the contract price.", true);
    try {
      const { data } = await post(`/api/leads/${id}/contract`, { price, signed: $("#ct-date").value || null });
      toast(data.notes.join(" "));
      route();
    } catch (e) { toast(e.message, true); }
  });
}

function wireDocs(d, root) {
  root.querySelectorAll("[data-doc]").forEach((b) => b.addEventListener("click", async () => {
    const price = +($("#doc-price") || {}).value || null;
    const top = d.buyers[0];
    const { status, data } = await post(`/api/leads/${d.lead.id}/docs/${b.dataset.doc}`,
      { price, buyer_id: b.dataset.doc === "assignment" && top ? top.id : null });
    const box = $("#doc-result");
    if (status === 409 || data.blocked) {
      box.innerHTML = `<div class="objection-resp bad"><b>Blocked</b><ul>${data.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>`;
      toast("Blocked by a compliance gate.", true);
      return;
    }
    box.innerHTML = `<div class="objection-resp"><b>${esc(b.dataset.doc.toUpperCase())}</b> at ${money(data.price)}:
      ${Object.entries(data.files).map(([e, u]) => `<a href="${esc(u)}" target="_blank">${e}</a>`).join(" · ")}
      ${data.warnings.length ? `<ul>${data.warnings.map((w) => `<li class="flag">${esc(w)}</li>`).join("")}</ul>` : ""}</div>`;
    toast("Document generated.");
  }));
}

route();
