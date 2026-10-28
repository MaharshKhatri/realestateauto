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
function stackPill(b) {
  return b ? `<span class="pill stack">${esc(b)}</span>` : "";
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
    const section = path === "/" || path === "" ? "autopilot"
      : (path.match(/^\/(today|ready|agents|inspect|dispo|lead|fast|closing|records)/) || [])[1] || "dash";
    document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("on", a.dataset.nav === section));
    const m = path.match(/^\/lead\/(\d+)/);
    if (m) return await renderLead(+m[1], params.get("contact"));
    if (path === "/" || path === "") return await renderAutopilot();
    if (path === "/today") return await renderToday();
    if (path === "/records") return await renderRecords();
    if (path === "/ready") return await renderReady();
    if (path === "/fast") return await renderFast();
    const cl = path.match(/^\/closing(?:\/(\d+))?/);
    if (cl) return await renderClosing(cl[1] ? +cl[1] : null);
    if (path === "/agents") return await renderAgents();
    const ins = path.match(/^\/inspect(?:\/(\d+))?/);
    if (ins) return await renderInspect(ins[1] ? +ins[1] : null);
    const dsp = path.match(/^\/dispo(?:\/(\d+))?/);
    if (dsp) return await renderDispo(dsp[1] ? +dsp[1] : null);
    STATE.tab = params.get("tab") || STATE.tab || "all";
    await renderDashboard(params.get("q"));
  } catch (e) {
    app.innerHTML = `<div class="card bad">Error: ${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);
$("#search").addEventListener("submit", (e) => {
  e.preventDefault();
  location.hash = `#/dashboard?tab=all&q=${encodeURIComponent($("#q").value)}`;
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
      <div class="kpi click" data-href="#/ready"><b>${d.kpis.ready_to_call}</b><span>ready to call now</span></div>
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
        <td><b>${esc(r.address)}</b> ${stackPill(r.badge)}${r.ready ? ' <span class="pill good">ready</span>' : ""}<br><span class="muted">${esc(r.town)} · ${esc(r.county)}</span></td>
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
      <div><a href="#/dashboard">← Dashboard</a>
        <h1>${esc(L.address)}, ${esc(L.town)} <span class="muted">${esc(L.county)} · ${esc(L.zip || "")}</span></h1>
        <div class="row">${verdictPill(L.verdict)} <span class="pill">${label(L.stage)}</span>
          <span class="pill acc">priority ${L.priority}</span> ${L.days_to_sale !== null ? daysBadge(L.days_to_sale) + " sale " + day(L.sale_date) : ""}
          <span class="pill">playbook ${esc(d.playbook)}</span> ${L.next_followup_at ? `<span class="pill warn">next touch ${fmtDate(L.next_followup_at)}</span>` : ""}
          ${stackPill(L.badge)} <a class="btn" href="#/inspect/${L.id}">Virtual inspection</a> <a class="btn" href="#/dispo/${L.id}">Walkthroughs &amp; bids</a> <a class="btn" href="#/closing/${L.id}">Closing file</a></div>
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


// -- Phase 3: ready to call ------------------------------------------------------------------------
const reach = (r) => r.who ? `${esc(r.who)} <span class="muted">${label(r.role)}</span><br><span class="mono">${esc(r.phone_display || r.email || "")}</span>`
  : '<span class="muted">no contact</span>';
const place = (r) => `<b>${esc(r.address)}</b> ${stackPill(r.badge)}<br><span class="muted">${esc(r.town)} · ${esc(r.county)}</span>`;
function wireHrefs(root = app) {
  root.querySelectorAll("[data-href]").forEach((el) => el.addEventListener("click", (e) => {
    if (!["A", "BUTTON"].includes(e.target.tagName)) location.hash = el.dataset.href;
  }));
}

async function renderReady() {
  const { data: d } = await api("/api/briefing?top=50");
  const c = d.counts;
  app.innerHTML = `
    <div class="kpis">
      <div class="kpi"><b>${c.ready_to_call}</b><span>ready to call now</span></div>
      <div class="kpi"><b>${c.stacked_2x}</b><span>2x stacked distress</span></div>
      <div class="kpi"><b>${c.stacked_3x_plus}</b><span>3x+ stacked</span></div>
      <div class="kpi"><b>${c.portfolio_owners}</b><span>portfolio-owner signals</span></div>
      <div class="kpi click" data-href="#/agents"><b>LOI →</b><span>send agent offers</span></div>
    </div>
    <div class="card"><div class="row spread"><h2>(A) Ready to call now</h2><span class="muted">underwritten · live phone or agent e-mail · ask within reach of our 75% ceiling</span></div>
      <div class="tablewrap"><table><thead><tr><th>Lead</th><th>Property</th><th>Call / e-mail</th><th class="num">Ask</th>
        <th class="num">MAO 70 / 75</th><th class="num">75% ÷ ask</th><th class="num">Gap to ask</th><th>Verdict</th><th class="num">Pri</th></tr></thead>
      <tbody>${d.ready.map((r) => {
        const ask = r.ask || r.upset;
        return `<tr class="click" data-href="#/lead/${r.lead_id}${r.contact_id ? "?contact=" + r.contact_id : ""}">
          <td>#${r.lead_id}</td><td>${place(r)}</td><td>${reach(r)}</td>
          <td class="num">${r.ask ? kmoney(r.ask) : r.upset ? "upset " + kmoney(r.upset) : "—"}</td>
          <td class="num">${kmoney(r.mao_70)} / ${kmoney(r.mao_75)}</td><td class="num">${pct(r.list_to_mao_75)}</td>
          <td class="num">${ask && r.mao_70 ? kmoney(ask - r.mao_70) : "—"}</td>
          <td>${verdictPill(r.verdict)}${r.striking_distance ? '<br><span class="pill acc">striking distance</span>' : ""}</td>
          <td class="num">${Math.round(r.priority)}</td></tr>`;
      }).join("") || '<tr><td colspan="9" class="muted">Nothing ready: underwrite + score (swarm run-all) or add phones (skip-trace).</td></tr>'}</tbody></table></div></div>
    <div class="grid g-dash" style="margin-top:14px">
      <div class="card"><h2>(B) High-spread sheriff-sale / estate deals</h2><div class="tablewrap"><table>
        <thead><tr><th>Property</th><th>Sale</th><th class="num">Upset</th><th class="num">ARV</th><th class="num">MAO 70</th><th class="num">Spread</th><th>Verdict</th><th>Contact</th></tr></thead>
        <tbody>${d.spread.map((r) => `<tr class="click" data-href="#/lead/${r.lead_id}"><td>${place(r)}</td><td>${r.sale_date ? day(r.sale_date) : label(r.kind)}</td>
          <td class="num">${kmoney(r.upset)}</td><td class="num">${kmoney(r.arv)}</td><td class="num">${kmoney(r.mao_70)}</td><td class="num">${kmoney(r.spread)}</td>
          <td>${verdictPill(r.verdict)}${r.frfpa_track ? `<br><span class="muted">${label(r.frfpa_track)}</span>` : ""}</td><td>${reach(r)}</td></tr>`).join("")}</tbody></table></div></div>
      <div class="card"><h2>(C) Stacked distress &amp; portfolio owners</h2><div class="tablewrap"><table>
        <thead><tr><th>Property</th><th>Signals</th><th>Verdict</th><th>Contact</th><th class="num">Pri</th></tr></thead>
        <tbody>${d.stacked.map((r) => `<tr class="click" data-href="#/lead/${r.lead_id}"><td>${place(r)}</td>
          <td>${r.families.map(label).join(" + ")}${r.portfolio_size ? `<br><span class="muted">owner holds ${r.portfolio_size} properties</span>` : ""}</td>
          <td>${verdictPill(r.verdict)}</td><td>${reach(r)}</td><td class="num">${Math.round(r.priority)}</td></tr>`).join("")}</tbody></table></div></div>
    </div>`;
  wireHrefs();
}

// -- Phase 3: B2B listing-agent LOI blast ----------------------------------------------------------
async function renderAgents() {
  const { data: d } = await api("/api/agent-outreach?top=50");
  const smtp = d.smtp.ready ? `<span class="pill good">SMTP ready · ${d.sent_24h}/${d.daily_limit} today</span>`
    : `<span class="pill warn">SMTP off: ${esc(d.smtp.why)}</span>`;
  app.innerHTML = `
    <div class="card"><div class="row spread"><h2>B2B listing-agent offers</h2><div class="row">${smtp}</div></div>
      <p class="muted">Two cash options per listing (anchor and best), unrepresented-buyer language, investor/assignment disclosure,
      opt-out line. Review each one, send it (mailto, copy, or SMTP), then it is logged as <b>offer sent to agent</b> with a
      48-hour follow-up call. SMS is always sent by you from your phone — copy it, never automated.</p>
      <div class="tablewrap" style="max-height:none"><table><thead><tr><th>Listing</th><th>Agent</th><th class="num">Ask</th>
        <th class="num">Anchor / best</th><th class="num">75% ÷ ask</th><th>Send</th></tr></thead>
      <tbody>${d.packets.map((p, i) => `<tr>
        <td><a href="#/lead/${p.lead_id}"><b>${esc(p.address)}</b></a><br><span class="muted">${p.dom || "?"} DOM · ${esc(p.reasons.join(" · "))}</span>
          ${p.warnings.length ? `<details><summary class="flag">${p.warnings.length} note(s)</summary><ul>${p.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></details>` : ""}</td>
        <td>${esc(p.agent_name)}<br><span class="muted">${esc(p.brokerage || "")}</span><br><span class="mono">${esc(p.agent_phone || "")}</span><br><span class="muted">${esc(p.agent_email || "")}</span></td>
        <td class="num">${kmoney(p.list_price)}</td>
        <td class="num">${p.kind === "offer" ? `${kmoney(p.anchor)} / ${kmoney(p.best)}` : '<span class="pill warn">inquiry</span>'}</td>
        <td class="num">${pct(p.list_to_mao_75)}</td>
        <td><div class="btns">
          ${p.mailto ? `<a class="btn" href="${esc(p.mailto)}" data-sent="${i}" data-channel="mailto">Open e-mail</a>` : ""}
          <button class="btn" data-copy="${i}" data-what="email">Copy e-mail</button>
          <button class="btn" data-copy="${i}" data-what="sms">Copy SMS</button>
          ${p.kind === "offer" ? `<button class="btn" data-loi="${i}">LOI PDF</button>` : ""}
          ${d.smtp.ready && p.agent_email ? `<button class="btn primary" data-smtp="${i}">Send (SMTP)</button>` : ""}
          <button class="btn good" data-mark="${i}">Mark sent</button>
          <button class="btn bad" data-optout="${i}">Opted out</button></div>
          <details><summary class="muted">preview</summary><pre class="pre">${esc(p.email_body)}</pre><p><b>SMS:</b> ${esc(p.sms_text)}</p></details></td>
      </tr>`).join("") || '<tr><td colspan="6" class="muted">No eligible listings: underwrite stale listings and run listing_agents.</td></tr>'}</tbody></table></div></div>`;
  const P = d.packets;
  app.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    const p = P[+b.dataset.copy];
    const text = b.dataset.what === "sms" ? p.sms_text : `Subject: ${p.subject}\n\n${p.email_body}`;
    try { await navigator.clipboard.writeText(text); toast("Copied — send it, then press Mark sent."); }
    catch { toast("Clipboard blocked: open preview and copy by hand.", true); }
  }));
  app.querySelectorAll("[data-loi]").forEach((b) => b.addEventListener("click", async () => {
    const p = P[+b.dataset.loi];
    const { data } = await post(`/api/agent-outreach/${p.lead_id}/loi`, {});
    if (data.files && data.files.pdf) window.open(data.files.pdf, "_blank"); else toast("LOI blocked: " + (data.packet?.warnings || []).join("; "), true);
  }));
  const send = async (p, channel) => {
    try {
      const { data } = await post(`/api/agent-outreach/${p.lead_id}/send`, { channel });
      toast(`Logged (${channel}): stage ${label(data.stage)}, follow-up ${fmtDate(data.followup_at)}`);
    } catch (e) { toast(e.message, true); }
  };
  app.querySelectorAll("[data-mark]").forEach((b) => b.addEventListener("click", () => send(P[+b.dataset.mark], "email")));
  app.querySelectorAll("[data-smtp]").forEach((b) => b.addEventListener("click", () => {
    if (confirm("Send this e-mail now from your Gmail account?")) send(P[+b.dataset.smtp], "smtp");
  }));
  app.querySelectorAll("[data-sent]").forEach((a) => a.addEventListener("click", () => {
    setTimeout(() => { if (confirm("Did you send the e-mail? Log it as sent?")) send(P[+a.dataset.sent], "mailto"); }, 400);
  }));
  app.querySelectorAll("[data-optout]").forEach((b) => b.addEventListener("click", async () => {
    const p = P[+b.dataset.optout];
    if (!confirm(`${p.agent_name} asked to stop? Their number goes on the internal do-not-call list.`)) return;
    await post("/api/agent-outreach/opt-out", { contact_id: p.agent_contact_id });
    toast("Opted out.");
    renderAgents();
  }));
}

// -- Phase 3: virtual inspector + mail merge -------------------------------------------------------
async function renderInspect(id) {
  if (id) {
    const { data: x } = await api(`/api/leads/${id}/inspect`);
    app.innerHTML = `<a href="#/lead/${id}">← Lead #${id}</a> · <a href="#/inspect">Inspector home</a>
      <h1>${esc(x.address)}</h1>
      <div class="grid g-lead">
        <div class="card"><h2>Look at it from here</h2><div class="btns">${Object.entries(x.links).map(([k, u]) =>
          `<a class="btn" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${label(k)} ↗</a>`).join("")}</div>
          ${x.notes.map((n) => `<p class="muted">${esc(n)}</p>`).join("")}
          ${x.listing_excerpt ? `<h3 style="margin-top:12px">Listing text</h3><p class="muted">${esc(x.listing_excerpt)}</p>` : ""}</div>
        <div class="card"><h2>Condition flags</h2>${x.flags.length ? `<ul>${x.flags.map((f) => `<li><b class="flag">${label(f.flag)}</b> <span class="muted">(${esc(f.evidence)})</span><br>${esc(f.advice)}</li>`).join("")}</ul>` : '<p class="muted">No flags in the listing text — ask for photos.</p>'}
          ${x.occupancy_hint ? `<p><span class="pill warn">${esc(x.occupancy_hint)}</span></p>` : ""}
          <h3>Remote due-diligence checklist</h3><ul>${x.checklist.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>
        <div class="card"><h2>Photo / video requests</h2>${Object.entries(x.requests).map(([k, v]) => `<h3>${label(k)}</h3>
          <pre class="pre">${esc(v)}</pre><button class="btn" data-copytext="${esc(v)}">Copy</button>`).join("")}</div>
      </div>`;
    app.querySelectorAll("[data-copytext]").forEach((b) => b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(b.dataset.copytext); toast("Copied."); } catch { toast("Clipboard blocked.", true); }
    }));
    return;
  }
  const { data: m } = await api("/api/mail-merge?top=50");
  const counts = m.letters.reduce((a, l) => ((a[l.kind] = (a[l.kind] || 0) + 1), a), {});
  app.innerHTML = `
    <div class="grid g-dash">
      <div class="card"><div class="row spread"><h2>Mail merge — owners we can't call</h2>
        <div class="row">${m.operator_ready ? '<span class="pill good">operator details set</span>' : '<span class="pill warn">PROOF mode: set operator name, phone, mailing address</span>'}</div></div>
        <p class="muted">${m.letters.length} letters ready (${Object.entries(counts).map(([k, v]) => `${label(k)} ${v}`).join(", ")}). Pre-auction, compassionate-estate and
          tired-landlord templates with investor disclosure, HUD / Legal Services resources and a STOP line. Suppressed (Daniel's Law / stop-mail) addresses are skipped.</p>
        <div class="btns"><button class="btn primary" id="mm-go">Generate PDFs + Click2Mail CSV</button></div><div id="mm-out"></div>
        <div class="tablewrap"><table><thead><tr><th>Lead</th><th>Letter</th><th>To</th><th>Property</th></tr></thead><tbody>
          ${m.letters.map((l) => `<tr class="click" data-href="#/inspect/${l.lead_id}"><td>#${l.lead_id}</td><td>${label(l.kind)}</td>
            <td>${esc(l.recipient)}<br><span class="muted">${esc(l.address_lines.join(", "))}</span></td><td>${esc(l.property_address)}</td></tr>`).join("")}</tbody></table></div></div>
      <div>
        <div class="card"><h2>Inspect a lead</h2><form id="ins-form" class="row"><input type="number" id="ins-id" placeholder="lead #" style="max-width:140px">
          <button class="btn">Open dossier</button></form>
          <p class="muted">Street View, satellite, FEMA flood map, the listing photos, condition flags and a photo/video request — no driving.</p></div>
        <div class="card"><h2>Batches</h2><ul>${m.batches.map((b) => `<li>${esc(b)} <button class="btn" data-mailed="${esc(b)}">Mark mailed</button></li>`).join("") || '<li class="muted">none yet</li>'}</ul></div>
        <div class="card"><h2>Stop mailing an address</h2><form id="sup-form"><input type="text" id="sup-addr" placeholder="12 Main St, Newark NJ 07102">
          <label><input type="checkbox" id="sup-dl"> Daniel's Law request (honor within 10 business days)</label><button class="btn bad">Suppress</button></form></div>
      </div>
    </div>`;
  wireHrefs();
  $("#ins-form").addEventListener("submit", (e) => { e.preventDefault(); if ($("#ins-id").value) location.hash = `#/inspect/${$("#ins-id").value}`; });
  $("#mm-go").addEventListener("click", async () => {
    const { data } = await post("/api/mail-merge", { top: 50 });
    $("#mm-out").innerHTML = `<div class="objection-resp">${data.letters} letters in <b>${esc(data.batch)}</b>${data.proof ? ' <span class="pill warn">proof</span>' : ""}:
      ${Object.entries(data.files).map(([n, u]) => `<a href="${esc(u)}" target="_blank">${esc(n)}</a>`).join(" · ")}</div>`;
  });
  app.querySelectorAll("[data-mailed]").forEach((b) => b.addEventListener("click", async () => {
    try { const { data } = await post(`/api/mail-merge/${encodeURIComponent(b.dataset.mailed)}/mailed`, {}); toast(`${data.logged} letters logged as mailed.`); }
    catch (e) { toast(e.message, true); }
  }));
  $("#sup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await post("/api/mail-suppress", { address: $("#sup-addr").value, source: $("#sup-dl").checked ? "daniels_law" : "internal" }); toast("Address suppressed."); }
    catch (err) { toast(err.message, true); }
  });
}

// -- Phase 3: reverse wholesaling — hotzones, remote walkthroughs, bids ---------------------------
async function renderDispo(id) {
  if (!id) {
    const { data } = await api("/api/hotzones?top=10");
    app.innerHTML = `<div class="card"><h2>Buyer hotzones — where cash buyers pay the most</h2>
      <p class="muted">Town + product + price band from recorded cash-buyer purchases (recent buys weigh more; one buyer's bulk buy counts at most 3 times).
      Our underwritten deals that fit each pocket are listed with the headroom between the median price paid and our buyer price.</p>
      <form id="dsp-form" class="row"><input type="number" id="dsp-id" placeholder="lead # under contract" style="max-width:200px"><button class="btn">Walkthroughs &amp; bids</button></form></div>
      ${data.zones.map((z, i) => `<div class="card"><div class="row spread"><h2>${i + 1}. ${esc(z.label)}</h2><span class="muted">score ${Math.round(z.score).toLocaleString()}</span></div>
        <ul>${z.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
        <p class="muted">Buyers: ${z.top_buyers.map((b) => `#${b.buyer_id} ${esc(b.name)} (${b.purchases})`).join(" · ")}</p>
        ${z.leads.length ? `<table><thead><tr><th>Our deal</th><th>Verdict</th><th class="num">Buyer price</th><th class="num">Headroom</th><th></th></tr></thead><tbody>
          ${z.leads.map((h) => `<tr class="click" data-href="#/lead/${h.lead_id}"><td>#${h.lead_id} ${esc(h.address)}</td><td>${verdictPill(h.verdict)}</td>
            <td class="num">${kmoney(h.buyer_price)}</td><td class="num ${h.headroom >= 0 ? "good" : "bad"}">${kmoney(h.headroom)}</td>
            <td>${h.under_contract ? `<a class="btn" href="#/dispo/${h.lead_id}">bids</a>` : ""}</td></tr>`).join("")}</tbody></table>` : ""}</div>`).join("")}`;
    wireHrefs();
    $("#dsp-form").addEventListener("submit", (e) => { e.preventDefault(); if ($("#dsp-id").value) location.hash = `#/dispo/${$("#dsp-id").value}`; });
    return;
  }
  const { data: w } = await api(`/api/leads/${id}/walkthrough`);
  const P = w.plan;
  app.innerHTML = `<a href="#/lead/${id}">← Lead #${id}</a> · <a href="#/dispo">Hotzones</a>
    <h1>Remote walkthrough &amp; bids — ${esc(P.address)}</h1>
    <div class="grid g-lead">
      <div class="card"><h2>Inspection window</h2>
        <p><span class="pill ${P.status === "open" ? "good" : "warn"}">${label(P.status)}</span> ${P.opens ? `${day(P.opens)} → ${day(P.closes)}` : ""}</p>
        <p><b>Access:</b> ${esc(P.access)}</p>
        ${P.blocked.length ? `<div class="objection-resp bad"><ul>${P.blocked.map((b) => `<li>${esc(b)}</li>`).join("")}</ul></div>` : ""}
        ${P.slots.length ? `<p><b>Proposed slots:</b> ${P.slots.map(fmtDate).join(" · ")}</p>` : ""}
        ${P.tip ? `<p class="muted">${esc(P.tip)}</p>` : ""}
        ${P.access_request ? `<h3>Access request</h3><pre class="pre">${esc(P.access_request)}</pre>` : ""}
        ${(P.steps || []).map((s) => `<p class="muted">${esc(s)}</p>`).join("")}</div>
      <div class="card"><h2>Matched buyers</h2>${(P.invites || []).map((v) => `<div class="contact"><b>#${v.buyer_id} ${esc(v.buyer)}</b> <span class="mono">${esc(v.phone || "")}</span>
          <details><summary class="muted">invite text / e-mail</summary><pre class="pre">${esc(v.sms)}</pre><pre class="pre">${esc(v.email)}</pre></details>
          <div class="btns"><button class="btn" data-act="invite" data-buyer="${v.buyer_id}">Invited</button><button class="btn" data-act="walked" data-buyer="${v.buyer_id}">Walked it</button></div></div>`).join("")
        || '<p class="muted">Invites unlock once attorney review has ended (and buyers are matched).</p>'}</div>
      <div class="card"><h2>Bids</h2><p class="muted">Contract ${money(w.contract_price)} · asking buyers ${money(w.asking_price)}</p>
        <table><thead><tr><th>Buyer</th><th>Status</th><th class="num">Bid</th><th class="num">Fee</th><th class="num">EMD</th><th></th></tr></thead><tbody>
        ${w.bids.map((b) => `<tr><td>${esc(b.buyer)}${b.id === w.best_bid_id ? ' <span class="pill good">best</span>' : ""}</td><td>${label(b.status)}</td>
          <td class="num">${money(b.amount)}</td><td class="num">${money(b.fee)}</td><td class="num">${money(b.emd)}</td>
          <td>${b.amount && b.status !== "accepted" ? `<button class="btn good" data-accept="${b.id}">Accept</button>` : ""}</td></tr>`).join("") || '<tr><td colspan="6" class="muted">No bids yet.</td></tr>'}</tbody></table>
        <form id="bid-form"><label>Buyer #</label><input type="number" id="bid-buyer"><label>Total price (contract + fee)</label><input type="number" id="bid-amt">
          <label>Deposit offered</label><input type="number" id="bid-emd"><label>Notes</label><input type="text" id="bid-notes">
          <button class="btn primary" style="margin-top:8px">Record bid</button></form><div id="bid-out"></div></div>
    </div>`;
  const out = (data) => {
    $("#bid-out").innerHTML = data.blocked ? `<div class="objection-resp bad"><ul>${data.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>`
      : `<div class="objection-resp">${(data.warnings || data.notes || []).map((x) => `<p>${esc(x)}</p>`).join("")}</div>`;
  };
  app.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
    const { data } = await post(`/api/leads/${id}/bids`, { buyer_id: +b.dataset.buyer, action: b.dataset.act });
    if (data.blocked) return out(data);
    toast(`Buyer #${b.dataset.buyer}: ${label(data.bid.status)}`);
    renderDispo(id);
  }));
  $("#bid-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const { data } = await post(`/api/leads/${id}/bids`, { buyer_id: +$("#bid-buyer").value, action: "bid", amount: +$("#bid-amt").value,
      emd: $("#bid-emd").value ? +$("#bid-emd").value : null, notes: $("#bid-notes").value });
    out(data);
    if (!data.blocked) setTimeout(() => renderDispo(id), 1500);
  });
  app.querySelectorAll("[data-accept]").forEach((b) => b.addEventListener("click", async () => {
    const structure = confirm("OK = Assignment of Contract · Cancel = single-deal JV agreement") ? "assignment" : "jv";
    const assignee = prompt("Assignee legal name / entity (leave blank if the buyer's name on file is correct):") || null;
    const { data } = await post(`/api/bids/${b.dataset.accept}/accept`, { structure, assignee_name: assignee });
    if (data.blocked) return out(data);
    $("#bid-out").innerHTML = `<div class="objection-resp">${data.notes.map((n) => `<p>${esc(n)}</p>`).join("")}
      ${Object.entries(data.files).map(([e, u]) => `<a href="${esc(u)}" target="_blank">${e}</a>`).join(" · ")}</div>`;
    toast("Bid accepted — document generated.");
  }));
}


// -- Phase 4: fast track ---------------------------------------------------------------------------
const TRACK_CLS = { CASH_OFFER: "good", FRFPA_FLOOR_OFFER: "acc", CONFIRM_OCCUPANCY: "warn", SHORT_SALE: "warn",
  EQUITY_MONITOR: "vio", NO_OFFER: "bad" };
function countdown(r) {
  if (!r.sale_date) return "—";
  return `${daysBadge(r.days_to_sale)} <span class="muted">${day(r.sale_date)}</span>`;
}
async function renderFast() {
  const { data: d } = await api("/api/fast-track?portfolios=20");
  const withAgent = d.listed.filter((r) => r.agent);
  app.innerHTML = `
    <div class="card"><div class="row spread"><h2>Listed foreclosures — sheriff sale + MLS listing</h2>
      <span class="muted">${d.listed.length} listed · ${withAgent.length} with a listing agent · call these first</span></div>
      <p class="muted">The seller already wants to sell, the auction date is fixed, and the agent earns nothing if it goes to the sale.
        Each track is what the numbers honestly support. Never offer to stop or postpone the sale or to negotiate with the lender.</p>
      <div class="tablewrap" style="max-height:none"><table><thead><tr><th>Property</th><th>Sale</th><th class="num">Upset</th>
        <th class="num">Ask</th><th class="num">DOM</th><th>Track</th><th class="num">Offer</th><th>Listing agent</th><th></th></tr></thead>
      <tbody>${d.listed.map((r, i) => `<tr>
        <td><a href="#/lead/${r.lead_id}"><b>${esc(r.address)}</b></a><br><span class="muted">${esc(r.county)} · ${label(r.verdict || "not valued")}</span></td>
        <td>${countdown(r)}</td><td class="num">${kmoney(r.upset)}</td><td class="num">${kmoney(r.list_price)}</td>
        <td class="num">${r.dom ?? "?"}</td>
        <td><span class="pill ${TRACK_CLS[r.track] || ""}">${esc(r.track_label)}</span><br><span class="muted" style="font-size:12px">${esc(r.reasons[0] || "")}</span></td>
        <td class="num">${r.anchor ? kmoney(r.anchor) + " / " + kmoney(r.best) : "—"}</td>
        <td>${r.agent ? `${esc(r.agent.name || "")}<br><span class="mono">${esc(r.agent.phone_display || "")}</span><br><span class="muted">${esc(r.agent.email || "")}</span>` : '<span class="muted">no agent contact</span>'}
          ${r.last_outreach ? `<br><span class="pill good">sent ${fmtDate(r.last_outreach)}</span>` : ""}</td>
        <td><div class="btns">
          <button class="btn" data-script="${i}">Script</button>
          ${r.agent && r.track !== "NO_OFFER" && r.track !== "EQUITY_MONITOR" ? `<button class="btn primary" data-pkt="${i}">Packet + LOI</button>` : ""}
        </div></td></tr>
        <tr id="sc-${i}" hidden><td colspan="9"><div class="script objection-resp">
          <p><b>Opening:</b> ${esc(r.script.opening)}</p><ul>${r.script.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>
          <p><b>Offer:</b> ${esc(r.script.offer)}</p><p><b>Close:</b> ${esc(r.script.close)}</p>
          <p class="muted">${esc(r.script.disclosure)}</p>
          <p class="flag">Never: ${r.script.never_say.map(esc).join(" · ")}</p>
          <div id="pk-${i}"></div></div></td></tr>`).join("") || '<tr><td colspan="9" class="muted">No listed foreclosures — run the signal stacker (manage.py stack).</td></tr>'}</tbody></table></div></div>
    <div class="card"><h2>Portfolio owners — one call or letter, every address</h2>
      ${d.portfolios.map((p, i) => `<div class="contact"><div class="row spread"><b>${esc(p.owner || "Owner (name not on record)")}</b>
        <span class="muted">${p.size} properties · ${p.deal_count} with an offer verdict · ${p.phone ? esc(p.phone.phone_display) : "mail only"}</span></div>
        <ul>${p.properties.slice(0, 6).map((x) => `<li><a href="#/lead/${x.lead_id}">${esc(x.address)}</a>, ${esc(x.town)} <span class="muted">${x.signals.map(label).join(", ")} · ${label(x.verdict || "not valued")}</span></li>`).join("")}</ul>
        <details><summary>Call script</summary><p>${esc(p.script.opening)}</p><ul>${p.script.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>
        <p class="muted">${esc(p.script.disclosure)}</p></details></div>`).join("") || '<p class="muted">No portfolio owners yet.</p>'}
      <p class="muted">Letters: <code>python manage.py fast-track --portfolio-letters 20</code></p></div>`;
  const L = d.listed;
  app.querySelectorAll("[data-script]").forEach((b) => b.addEventListener("click", () => {
    const row = $(`#sc-${b.dataset.script}`); row.hidden = !row.hidden;
  }));
  app.querySelectorAll("[data-pkt]").forEach((b) => b.addEventListener("click", async () => {
    const i = +b.dataset.pkt, r = L[i];
    $(`#sc-${i}`).hidden = false;
    try {
      const { data } = await post(`/api/fast-track/${r.lead_id}/packet`, {});
      const p = data.packet;
      $(`#pk-${i}`).innerHTML = `<h3>${label(p.kind)} packet</h3><pre class="pre">${esc(p.subject)}\n\n${esc(p.email_body)}</pre>
        <p><b>SMS:</b> ${esc(p.sms_text)}</p>${p.warnings.length ? `<ul>${p.warnings.map((w) => `<li class="flag">${esc(w)}</li>`).join("")}</ul>` : ""}
        <div class="btns">${p.mailto ? `<a class="btn" href="${esc(p.mailto)}">Open e-mail</a>` : ""}
        ${data.files.pdf ? `<a class="btn" href="${esc(data.files.pdf)}" target="_blank">LOI PDF</a>` : ""}
        <button class="btn good" data-mark="${r.lead_id}">Mark sent</button></div>`;
      $(`#pk-${i} [data-mark]`).addEventListener("click", async () => {
        try { const { data: s } = await post(`/api/agent-outreach/${r.lead_id}/send`, { channel: "email" });
          toast(`Logged: ${label(s.stage)}, follow-up ${fmtDate(s.followup_at)}`); } catch (e) { toast(e.message, true); }
      });
    } catch (e) { toast(e.message, true); }
  }));
}


// -- Phase 4: closing coordinator + money ----------------------------------------------------------
async function renderClosing(id) {
  if (id) {
    const { data: c } = await api(`/api/closing/${id}`);
    if (!c.open) { app.innerHTML = `<a href="#/lead/${id}">← Lead #${id}</a><div class="card">${esc(c.reason)}</div>`; return; }
    const e = c.title_email;
    app.innerHTML = `<a href="#/lead/${id}">← Lead #${id}</a> · <a href="#/closing">All closing files</a>
      <h1>Closing file — lead #${id}</h1>
      <div class="grid g-dash">
        <div class="card"><h2>NJ contract-to-close milestones</h2>${(c.notes || []).map((n) => `<p class="good">${esc(n)}</p>`).join("")}
          <table><thead><tr><th>#</th><th>Milestone</th><th>Due</th><th>Status</th><th></th></tr></thead><tbody>
          ${c.milestones.map((m) => `<tr><td>${m.sequence}</td><td>${esc(m.label)}${m.notes ? `<br><span class="muted">${esc(m.notes)}</span>` : ""}</td>
            <td>${day(m.due_date)}</td><td><span class="pill ${m.status === "done" ? "good" : m.overdue ? "bad" : m.status === "cancelled" ? "" : "warn"}">${m.overdue ? "overdue" : label(m.status)}</span></td>
            <td>${m.status === "pending" && m.key !== "CLOSED_PAID" ? `<button class="btn" data-done="${m.key}">Done</button>` : ""}</td></tr>`).join("")}</tbody></table>
          <form id="close-form" class="row" style="margin-top:10px"><input type="number" id="close-fee" placeholder="fee received $" style="max-width:180px">
            <button class="btn good">Closed &amp; paid</button></form>
          <button class="btn bad" id="disapproved" style="margin-top:8px">Attorney disapproved</button></div>
        <div class="card"><h2>Title order e-mail</h2>
          <p class="muted">${e.partner && e.partner.name ? `${esc(e.partner.name)} · ${esc(e.partner.phone || "")} · <a href="${esc(e.partner.website || "#")}" target="_blank" rel="noopener noreferrer">site ↗</a>
            ${e.partner.investor_evidence ? `<br>“${esc(e.partner.investor_evidence)}”` : ""}<br>Confirm they insure assignments / double closes before ordering.` : "Pick a title agency in config/nj_closing_partners.yaml."}</p>
          <pre class="pre">Subject: ${esc(e.subject)}\n\n${esc(e.body)}</pre><button class="btn" id="copy-title">Copy</button></div>
      </div>`;
    app.querySelectorAll("[data-done]").forEach((b) => b.addEventListener("click", async () => {
      const notes = prompt(`Notes for "${b.dataset.done}" (optional):`) || "";
      try { await post(`/api/closing/${id}/milestones/${b.dataset.done}`, { notes }); renderClosing(id); } catch (err) { toast(err.message, true); }
    }));
    $("#copy-title").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(`Subject: ${e.subject}\n\n${e.body}`); toast("Copied."); } catch { toast("Clipboard blocked.", true); }
    });
    $("#close-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!$("#close-fee").value) return;
      try { await post(`/api/closing/${id}/close`, { fee: +$("#close-fee").value }); toast("Closed — fee booked."); renderClosing(id); } catch (err) { toast(err.message, true); }
    });
    $("#disapproved").addEventListener("click", async () => {
      if (!confirm("An attorney disapproved during review? The contract is void and the file closes.")) return;
      await post(`/api/closing/${id}/disapproved`, { notes: prompt("Notes:") || "" }); renderClosing(id);
    });
    return;
  }
  const [{ data: o }, { data: f }] = await Promise.all([api("/api/closing"), api("/api/finance")]);
  app.innerHTML = `
    <div class="kpis">
      <div class="kpi"><b>${money(f.income)}</b><span>fees ${f.year}</span></div>
      <div class="kpi"><b>${money(f.net)}</b><span>net after ${money(f.expenses)} expenses</span></div>
      <div class="kpi"><b>${money(f.tax_reserve)}</b><span>tax reserve (${Math.round(f.reserve_rate * 100)}%)</span></div>
      <div class="kpi"><b>${money(f.pipeline_projected)}</b><span>pipeline (probability-weighted)</span></div>
      <div class="kpi"><b>${f.next_due ? day(f.next_due.date) : "—"}</b><span>next estimated tax ${f.next_due ? "· set aside " + money(f.next_due.set_aside) : ""}</span></div>
    </div>
    <div class="grid g-dash">
      <div>
        <div class="card"><h2>Deadlines</h2><ul>${o.alerts.map((a) => `<li><a href="#/closing/${a.lead_id}">${esc(a.address)}</a> — ${esc(a.label)}
          <span class="pill ${a.overdue ? "bad" : "warn"}">${a.overdue ? "overdue" : "due"} ${day(a.due_date)}</span></li>`).join("") || '<li class="muted">Nothing due in the next 2 business days.</li>'}</ul></div>
        <div class="card"><h2>Open closing files</h2><table><thead><tr><th>Property</th><th>Stage</th><th class="num">Contract</th><th>Progress</th><th>Next</th></tr></thead><tbody>
          ${o.files.map((r) => `<tr class="click" data-href="#/closing/${r.lead_id}"><td>${esc(r.address)}</td><td>${label(r.stage)}</td><td class="num">${money(r.contract_price)}</td>
            <td>${r.done}/${r.total}</td><td>${esc(r.next || "—")} <span class="${r.overdue ? "bad" : "muted"}">${day(r.next_due)}</span></td></tr>`).join("") || '<tr><td colspan="5" class="muted">No deals under contract yet — record one with manage.py contract &lt;id&gt; --price … --signed ….</td></tr>'}</tbody></table></div>
        <div class="card"><h2>Ledger</h2><table><thead><tr><th>Date</th><th>Kind</th><th>Category</th><th class="num">Amount</th><th>Note</th></tr></thead><tbody>
          ${f.entries.map((x) => `<tr><td>${day(x.date)}</td><td>${label(x.kind)}</td><td>${label(x.category)}</td><td class="num ${x.kind === "income" ? "good" : ""}">${money(x.amount)}</td><td>${esc(x.description || "")}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">No entries yet.</td></tr>'}</tbody></table></div>
      </div>
      <div>
        <div class="card"><h2>Add income / expense</h2><form id="led-form">
          <label>Kind</label><select id="led-kind"><option value="expense">expense</option><option value="income">income</option></select>
          <label>Category</label><select id="led-cat"></select>
          <label>Amount</label><input type="number" id="led-amt" step="0.01"><label>Note</label><input type="text" id="led-desc">
          <button class="btn primary" style="margin-top:8px">Add</button></form></div>
        <div class="card"><h2>Taxes</h2><ul>${f.quarters.map((q) => `<li>due ${day(q.due_date)}: period net ${money(q.period_net)} → set aside <b>${money(q.set_aside)}</b></li>`).join("")}</ul>
          ${f.notes.map((n) => `<p class="muted">${esc(n)}</p>`).join("")}</div>
      </div>
    </div>`;
  wireHrefs();
  const cats = () => { $("#led-cat").innerHTML = f.categories[$("#led-kind").value].map((c) => `<option>${esc(c)}</option>`).join(""); };
  $("#led-kind").addEventListener("change", cats); cats();
  $("#led-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try { await post("/api/finance", { kind: $("#led-kind").value, category: $("#led-cat").value, amount: +$("#led-amt").value, description: $("#led-desc").value });
      toast("Added."); renderClosing(null); } catch (err) { toast(err.message, true); }
  });
}


// -- Phase 4: Today (home) -------------------------------------------------------------------------
async function renderToday() {
  const { data: t } = await api("/api/today");
  if (!STATE.dash) api("/api/dashboard").then(({ data }) => statusBar(data.dnc)).catch(() => {});
  const [b1, b2, b3, b4] = t.blocks;
  const head = (b, cls) => `<div class="row spread"><h2 class="${cls}">Block ${t.blocks.indexOf(b) + 1} · ${b.minutes} min — ${esc(b.title)}</h2>
    <span class="muted">${b.rows.length} today</span></div>`;
  const empty = (b) => (b.rows.length ? "" : `<p class="muted">${esc(b.empty)}</p>`);
  const dl = t.deadlines;
  app.innerHTML = `
    <div class="row spread" style="margin-bottom:10px"><h1>Today — ${day(t.date)}</h1><span class="muted">${esc(t.reminder)}</span></div>
    ${dl.closing.length || dl.followups.length ? `<div class="card"><h2>First: deadlines &amp; follow-ups</h2><ul>
      ${dl.closing.map((a) => `<li><a href="#/closing/${a.lead_id}">${esc(a.address)}</a> — ${esc(a.label)} <span class="pill ${a.overdue ? "bad" : "warn"}">${a.overdue ? "overdue" : "due"} ${day(a.due_date)}</span></li>`).join("")}
      ${dl.followups.map((f) => `<li><a href="#/lead/${f.lead_id}">${esc(f.address)}</a> — follow up (${label(f.stage)}${f.last ? ", last: " + label(f.last) : ""})</li>`).join("")}</ul></div>` : ""}
    <div class="card">${head(b1, "bad")}
      <table><thead><tr><th>Property</th><th>Sale</th><th class="num">Upset</th><th class="num">Ask</th><th class="num">DOM</th><th>Track</th><th>Agent</th><th></th></tr></thead><tbody>
      ${b1.rows.map((r) => `<tr><td><a href="#/lead/${r.lead_id}"><b>${esc(r.address)}</b></a></td><td>${countdown(r)}</td><td class="num">${kmoney(r.upset)}</td>
        <td class="num">${kmoney(r.list_price)}</td><td class="num">${r.dom ?? "?"}</td><td><span class="pill ${TRACK_CLS[r.track] || ""}">${esc(r.track_label)}</span>
        ${r.anchor ? `<br><span class="muted">${kmoney(r.anchor)} / ${kmoney(r.best)}</span>` : ""}</td>
        <td>${esc(r.agent.name || "")}<br><span class="mono">${esc(r.agent.phone_display || r.agent.email || "")}</span></td>
        <td><a class="btn" href="#/fast">Script + LOI</a></td></tr>`).join("")}</tbody></table>${empty(b1)}</div>
    <div class="card">${head(b2, "good")}
      <table><thead><tr><th>Property</th><th>Call / e-mail</th><th class="num">Ask</th><th class="num">MAO 70</th><th class="num">Gap</th><th></th></tr></thead><tbody>
      ${b2.rows.map((r) => `<tr><td><a href="#/lead/${r.lead_id}${r.contact_id ? "?contact=" + r.contact_id : ""}"><b>${esc(r.address)}</b></a> ${stackPill(r.badge)}<br><span class="muted">${esc(r.town)}</span></td>
        <td>${esc(r.who || "")}<br><span class="mono">${esc(r.phone_display || r.email || "")}</span></td>
        <td class="num">${kmoney(r.ask)}</td><td class="num">${kmoney(r.mao_70)}</td><td class="num">${kmoney(r.gap_to_ask)}</td>
        <td><a class="btn" href="#/agents">LOI</a></td></tr>`).join("")}</tbody></table>${empty(b2)}</div>
    <div class="grid g-dash">
      <div class="card">${head(b3, "warn")}<ul>
        ${b3.rows.map((r) => `<li style="margin-bottom:8px"><a href="#/lead/${r.lead_id}"><b>${esc(r.address)}</b></a> · ${label(r.kind)} · sale ${esc(r.sale_date || "—")} · spread ${kmoney(r.spread)}<br>
          look up <b>${esc(r.target)}</b> (${label(r.role)}): ${Object.entries(r.links).map(([k, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${label(k)} ↗</a>`).join(" · ")}</li>`).join("")}</ul>
        ${b3.rows.length ? '<p class="muted">Paste what you find into the lead\'s Skip-trace box — numbers are DNC-checked on import.</p>' : empty(b3)}</div>
      <div class="card">${head(b4, "acc")}<ul>
        ${b4.rows.map((r) => `<li style="margin-bottom:8px"><b>${esc(r.name)}</b>${r.flipper ? ' <span class="pill good">flipper</span>' : ""} · ${r.purchases_in_towns} buys there<br>
          <span class="muted">recent: ${esc(r.recent.address)}, ${esc(r.recent.town)} ${money(r.recent.price)} (${esc(r.recent.date || "")})</span><br>
          ${r.phone_display || r.email ? `<span class="mono">${esc(r.phone_display || r.email)}</span>` : Object.entries(r.links).map(([k, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${label(k)} ↗</a>`).join(" · ")}
          <details><summary class="muted">script</summary><p>${esc(r.opening)}</p><ul>${r.discovery.map((q) => `<li>${esc(q)}</li>`).join("")}</ul></details></li>`).join("")}</ul>${empty(b4)}</div>
    </div>`;
}

// -- Phase 5: command center (KPI pills, My Setup, swarm refresh, step-by-step autopilot, kanban) -----------
const AP = { deck: null, i: 0, setup: null, poll: null, wasRunning: false };

function modal(html) {
  const d = $("#dlg");
  d.innerHTML = html;
  if (!d.open) d.showModal();
  return d;
}
function closeModal() {
  const d = $("#dlg");
  if (d.open) d.close();
}
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

async function loadCmdbar() {
  const bar = $("#cmdbar");
  try {
    const [{ data: k }, { data: st }] = await Promise.all([api("/api/kpis"), api("/api/settings")]);
    AP.setup = st;
    const pill = (icon, n, text, href, cls = "") =>
      `<a class="kpill ${cls}" href="${href}"><b>${icon} ${esc(n)}</b><span>${esc(text)}</span></a>`;
    bar.innerHTML = `<div class="kpills">
        ${pill("📋", k.open_leads.toLocaleString(), "Total open leads", "#/dashboard")}
        ${pill("🚨", k.listed_foreclosures, `Listed foreclosures · ${k.listed_actionable} actionable`, "#/fast", "bad")}
        ${pill("📞", k.ready_to_call, "Ready-to-call now", "#/ready", "good")}
        ${pill("📨", k.offers_sent_30d, "Offers sent (30 days)", "#/agents", "acc")}
        ${pill("💰", k.active_cash_buyers, "Active cash buyers", "#/dispo", "vio")}
        ${pill("🏦", kmoney(k.pipeline_value), "Pipeline value", "#/closing", "warn")}
      </div>
      <div class="cmd-actions">
        ${st.live ? '<span class="pill good live" id="live-badge">✅ Setup Complete — Live Mode Active</span>'
                  : '<span class="pill warn" id="live-badge">Setup incomplete: e-mails stay drafts</span>'}
        <button class="btn" id="btn-setup">⚙️ My Setup &amp; Auto-Email</button>
        <button class="btn" id="btn-swarm">🤖 Run Autopilot Data Refresh</button>
        <button class="btn" id="btn-opra">🏛️ Request Free Town Tax-Lien Spreadsheets (OPRA)</button>
        <span id="swarm-live" class="muted"></span>
      </div>`;
    $("#btn-setup").onclick = openSetup;
    $("#btn-swarm").onclick = openSwarm;
    $("#btn-opra").onclick = openOpra;
    pollSwarm(false);
  } catch (e) {
    bar.innerHTML = `<span class="bad">Command bar: ${esc(e.message)}</span>`;
  }
}

// -- My Setup & Auto-Email ------------------------------------------------------------------------------
function setupProblems(b) {
  const out = {};
  if (!b.name || b.name.trim().toLowerCase() === "operator") out.name = "required";
  if (!b.company_name || b.company_name.trim().toLowerCase() === "your company llc") out.company_name = "required";
  const digits = (b.phone || "").replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
  if (digits.length !== 10) out.phone = "10-digit US number";
  if (!/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test((b.email || "").trim())) out.email = "valid e-mail";
  if (!/\b[A-Za-z]{2}\s+\d{5}(-\d{4})?\b/.test(b.mailing_address || "")) out.mailing_address = "street or PO box, town, state and ZIP";
  if (b.app_password && b.app_password.replace(/\s/g, "").length !== 16) out.app_password = "a Gmail app password is 16 letters";
  return out;
}
async function openSetup() {
  const { data: s } = await api("/api/settings");
  const f = (k, lbl, type, ph, val) => `<label>${lbl}<input name="${k}" type="${type}" value="${esc(val ?? s[k] ?? "")}"
      placeholder="${esc(ph)}" autocomplete="off"><small class="bad" data-err="${k}"></small></label>`;
  modal(`<form id="setup-form" class="setup" novalidate>
      <div class="row spread"><h2>⚙️ My Setup &amp; Auto-Email</h2>${s.live ? '<span class="pill good">✅ Live mode</span>' : ""}</div>
      <p class="muted">Saved on this laptop only (${esc(s.local_file)} and .env, both git-ignored). Every e-mail, text and
        letter signs with these details, and NJ / CAN-SPAM rules need all five.</p>
      ${f("name", "Your full name", "text", "Jordan Smith")}
      ${f("company_name", "Company name", "text", "Garden State Home Buyers LLC")}
      ${f("phone", "Google Voice phone number", "tel", "(908) 555-0100")}
      ${f("email", "E-mail address", "email", "you@gmail.com")}
      ${f("mailing_address", "Mailing address (printed under every e-mail and on letters)", "text", "PO Box 10, Edison, NJ 08817")}
      <details ${s.has_app_password ? "" : "open"}><summary>Optional: send e-mails straight from the dashboard (Gmail app password)</summary>
        ${f("smtp_user", "Gmail address to send from", "email", "you@gmail.com", s.smtp_user || "")}
        ${f("app_password", s.has_app_password ? "Gmail app password (saved, leave blank to keep it)" : "Gmail app password (16 letters)",
            "password", s.has_app_password ? "saved" : "abcd efgh ijkl mnop", "")}
        <p class="muted">Google Account → Security → 2-Step Verification → App passwords. The password stays in .env on this
          laptop and is never shown again. Sending stays personal: one e-mail per click, at most 25 a day.</p>
      </details>
      <div class="row spread" style="margin-top:12px"><span id="setup-msg" class="muted"></span>
        <span class="row"><button type="button" class="btn" id="setup-cancel">Cancel</button>
        <button type="submit" class="btn good" id="setup-save">Save</button></span></div>
    </form>`);
  $("#setup-cancel").onclick = closeModal;
  $("#setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    document.querySelectorAll("[data-err]").forEach((el) => (el.textContent = ""));
    const local = setupProblems(body);
    const show = (errs) => Object.entries(errs).forEach(([k, v]) => { const el = $(`[data-err="${k}"]`); if (el) el.textContent = v; });
    if (Object.keys(local).length) return show(local);
    const res = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return show((data.detail && data.detail.errors) || { name: data.detail || "could not save" });
    closeModal();
    toast(data.live ? "✅ Setup Complete — Live Mode Active" : "Saved. A few details are still missing.");
    await loadCmdbar();
    if (!location.hash || location.hash === "#/") renderAutopilot();
  });
}

// -- swarm refresh --------------------------------------------------------------------------------------
function stepsHtml(st) {
  const icon = { pending: "·", running: "⏳", success: "✅", partial: "⚠️", failed: "❌", skipped: "–" };
  return `<ol class="steps">${(st.steps || []).map((x) => `<li class="${esc(x.status)}">${icon[x.status] || "·"} ${label(x.agent)}
    ${x.seconds != null ? `<span class="muted">${x.seconds}s</span>` : ""}${x.new_leads ? ` <span class="good">+${x.new_leads} leads</span>` : ""}
    ${x.errors ? ` <span class="warn">${x.errors} errors</span>` : ""}</li>`).join("")}</ol>
    ${st.error ? `<p class="bad">${esc(st.error)}</p>` : ""}`;
}
async function openSwarm() {
  const { data: st } = await api("/api/swarm/status");
  modal(`<h2>🤖 Run Autopilot Data Refresh</h2>
    <p class="muted"><b>Quick</b> re-stacks, re-scores and re-checks every valuation already in the database (a few minutes, no scraping).
      <b>Full</b> also pulls today's sheriff sales and listings, lis pendens imports and contacts, then underwrites
      (10-30 minutes; every source keeps its polite rate limit).</p>
    <div class="row"><button class="btn good" data-mode="quick" ${st.running ? "disabled" : ""}>Quick refresh</button>
      <button class="btn" data-mode="full" ${st.running ? "disabled" : ""}>Full refresh (new sheriff sales + listings)</button>
      <button class="btn" id="sw-close">Close</button></div>
    <div id="sw-steps">${st.steps && st.steps.length ? stepsHtml(st) : ""}</div>`);
  $("#sw-close").onclick = closeModal;
  document.querySelectorAll("#dlg [data-mode]").forEach((b) => (b.onclick = async () => {
    document.querySelectorAll("#dlg [data-mode]").forEach((x) => (x.disabled = true));
    try {
      await post("/api/swarm/trigger", { mode: b.dataset.mode });
      toast(`${b.dataset.mode === "quick" ? "Quick" : "Full"} refresh started`);
      pollSwarm(true);
    } catch (e) {
      toast(e.message, true);
    }
  }));
}
async function pollSwarm(active) {
  clearTimeout(AP.poll);
  let st;
  try {
    ({ data: st } = await api("/api/swarm/status"));
  } catch (e) {
    return;
  }
  const done = (st.steps || []).filter((x) => !["pending", "running"].includes(x.status)).length;
  const live = $("#swarm-live");
  if (live) live.textContent = st.running ? `🤖 ${st.mode} refresh: ${String(st.current || "starting").replaceAll("_", " ")} (${done}/${st.steps.length})`
    : st.finished_at ? `Last refresh ${fmtDate(st.finished_at)}` : "";
  const box = $("#sw-steps");
  if (box && st.steps && st.steps.length) box.innerHTML = stepsHtml(st);
  if (st.running) {
    AP.wasRunning = true;
    AP.poll = setTimeout(() => pollSwarm(true), 2000);
  } else if (AP.wasRunning || active) {
    AP.wasRunning = false;
    if (st.finished_at) {
      toast(st.error ? "Refresh finished with an error" : "Refresh finished: deck and numbers updated", !!st.error);
      document.querySelectorAll("#dlg [data-mode]").forEach((x) => (x.disabled = false));
      const k = await api("/api/kpis").catch(() => null);
      if (k) loadCmdbar();
      if (!location.hash || location.hash === "#/") renderAutopilot();
    }
  }
}

// -- step-by-step autopilot -----------------------------------------------------------------------------
async function renderAutopilot() {
  const { data } = await api("/api/autopilot");
  AP.deck = data;
  AP.i = Math.min(AP.i, Math.max(0, data.cards.length - 1));
  app.innerHTML = `<div class="card autopilot" id="ap"></div><div id="kanban" class="card"><p class="muted">Loading queues…</p></div>`;
  drawCard();
  renderKanban();
}
function progressHtml(d) {
  const w = Math.min(100, Math.round((100 * d.done) / Math.max(1, d.goal)));
  return `<div class="row spread"><h1>🎯 Step-by-Step Mode</h1><b id="ap-goal">Today's Goal: ${d.done} / ${d.goal} Deals Contacted</b></div>
    <div class="progress" role="progressbar" aria-label="Today's goal" aria-valuemin="0" aria-valuemax="${d.goal}"
      aria-valuenow="${d.done}"><div style="width:${w}%"></div></div>`;
}
function drawCard() {
  const d = AP.deck;
  const box = $("#ap");
  const banner = d.setup_complete ? "" : `<div class="banner">Finish <button class="btn" id="ap-setup">⚙️ My Setup</button> first:
    every e-mail signs with your name, phone and mailing address (CAN-SPAM), and letters print as PROOF until it's done.</div>`;
  if (!d.cards.length) {
    box.innerHTML = progressHtml(d) + banner + `<p class="muted">Nothing left in today's deck. Run the data refresh for new
      sheriff sales and listings, or work the queues below.</p>`;
    if ($("#ap-setup")) $("#ap-setup").onclick = openSetup;
    return;
  }
  const c = d.cards[AP.i];
  const a = c.agent || {};
  const smtp = d.auto_email && c.can_smtp;
  const cls = { listed_foreclosure: "bad", followup: "warn", mls_fixer: "good" }[c.kind] || "";
  box.innerHTML = `${progressHtml(d)}${banner}
    <div class="row spread ap-nav"><b>Action ${AP.i + 1} of ${d.cards.length}</b>
      <span class="row"><button class="btn" id="ap-prev" ${AP.i === 0 ? "disabled" : ""}>◀ Previous</button>
      <button class="btn" id="ap-next" ${AP.i >= d.cards.length - 1 ? "disabled" : ""}>Next ▶</button></span></div>
    <div class="row ap-title"><span class="pill ${cls}">${esc(c.label)}</span>
      <h2><a href="${esc(c.lead_url)}">${esc(c.address)}</a></h2>${c.days_to_sale != null ? daysBadge(c.days_to_sale) : ""}
      <a class="muted" href="${esc(c.inspect_url)}">photos, maps &amp; flood →</a></div>
    <div class="grid ap-grid">
      <div>
        <h3>What this property is</h3><p>${esc(c.what)}</p>
        <div class="whybox"><h3>Why this deal makes money</h3><ul>${c.why.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>
        <h3>Exact action to take right now</h3><p class="action">${esc(c.action)}</p>
        <h3>Phone script (3 sentences)</h3><ol class="ap-script">${c.phone_script.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>
        ${a.phone_display ? `<p>📞 <a class="phone" href="tel:${esc(a.phone)}">${esc(a.phone_display)}</a> · ${esc(a.name || "")}
          ${a.company ? `<span class="muted">(${esc(a.company)})</span>` : ""}</p>` : '<p class="muted">No phone on file for this agent.</p>'}
        <h3>📞 I called: log the outcome &amp; go to the next deal</h3>
        <div class="ap-outcomes">
          <button class="btn good" data-out="interested">✅ Interested - Send Offer</button>
          <button class="btn" data-out="voicemail">📞 Left Voicemail</button>
          <button class="btn" data-out="not_interested">🚫 Not Interested</button>
          <button class="btn bad" data-out="sold">🏷️ Sold / Dead - Skip</button>
        </div>
        <div id="ap-result"></div>
      </div>
      <div>
        <h3>E-mail to ${esc(a.name || "the listing agent")} ${a.email ? `<span class="mono">&lt;${esc(a.email)}&gt;</span>` : "(no e-mail on file)"}</h3>
        <label>Subject<input type="text" id="ap-subject" value="${esc(c.subject)}"></label>
        <label>Plain-text message (edit anything; keep the summary block under your name)
          <textarea id="ap-body" rows="16" spellcheck="true">${esc(c.body)}</textarea></label>
        ${c.style.length ? `<p class="flag">Style check: ${esc(c.style.join("; "))}</p>` : ""}
        ${c.warnings.length ? `<ul>${c.warnings.map((w) => `<li class="flag">${esc(w)}</li>`).join("")}</ul>` : ""}
        <button class="bigbtn" id="ap-send" ${a.email ? "" : "disabled"}>✉️ Send Email &amp; Log (Auto-Advance to Next Deal)</button>
        <p class="muted">${smtp ? `Sends from your Gmail (${d.sent_24h}/${d.daily_email_limit} in the last 24 hours) with the 1-page offer attached.`
          : "Opens your e-mail app with this message and logs it as sent. Add a Gmail app password in My Setup to send from here."}</p>
        <div class="smsbox"><h3>Short text message</h3><p>${esc(c.sms)}</p>
          <button class="btn" id="ap-sms" ${a.phone ? "" : "disabled"}>📋 Copy Short Text Message to Agent</button>
          <span class="muted">Paste it into Google Voice; texts are always sent by you, never automated.</span></div>
      </div>
    </div>`;
  if ($("#ap-setup")) $("#ap-setup").onclick = openSetup;
  $("#ap-prev").onclick = () => { AP.i -= 1; drawCard(); };
  $("#ap-next").onclick = () => { AP.i += 1; drawCard(); };
  $("#ap-send").onclick = () => apSend(c, smtp);
  $("#ap-sms").onclick = () => apSms(c);
  document.querySelectorAll("#ap [data-out]").forEach((b) => (b.onclick = () => apOutcome(c, b.dataset.out)));
}
function apAdvance(r) {
  if (r && r.done != null) AP.deck.done = r.done;
  AP.deck.cards.splice(AP.i, 1);
  AP.i = Math.min(AP.i, Math.max(0, AP.deck.cards.length - 1));
  drawCard();
  loadCmdbar();
}
async function apSend(c, smtp) {
  const subject = $("#ap-subject").value;
  const body = $("#ap-body").value;
  $("#ap-send").disabled = true;
  try {
    if (!smtp) {
      const link = document.createElement("a");
      link.href = `mailto:${encodeURIComponent(c.agent.email)}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
      link.click();
    }
    const { status, data } = await post(`/api/autopilot/${c.lead_id}/send`, { channel: smtp ? "smtp" : "mailto", subject, body });
    if (status === 409) throw new Error(data.detail || "not sent");
    toast(smtp ? `Sent to ${c.agent.name || c.agent.email}. Follow-up scheduled.` : "Logged as sent. Finish sending in your e-mail app.");
    apAdvance(data);
  } catch (e) {
    $("#ap-send").disabled = false;
    toast(e.message, true);
  }
}
async function apSms(c) {
  const ok = await copyText(c.sms);
  try {
    const { status, data } = await post(`/api/autopilot/${c.lead_id}/send`, { channel: "sms" });
    if (status === 409) throw new Error(data.detail || "not logged");
    AP.deck.done = data.done;
    $("#ap-goal").textContent = `Today's Goal: ${data.done} / ${data.goal} Deals Contacted`;
    toast(ok ? "Copied. Paste it into Google Voice; logged as texted." : "Logged as texted (copy the text above by hand).");
  } catch (e) {
    toast(e.message, true);
  }
}
async function apOutcome(c, out) {
  try {
    const { status, data } = await post(`/api/autopilot/${c.lead_id}/outcome`, { outcome: out });
    if (status === 409) throw new Error(data.detail || "not logged");
    if (out === "interested") {
      AP.deck.done = data.done;
      $("#ap-goal").textContent = `Today's Goal: ${data.done} / ${data.goal} Deals Contacted`;
      $("#ap-result").innerHTML = `<div class="whybox"><b>${esc(data.message)}</b>
        ${Object.keys(data.files).length ? `<p>Written offer: ${Object.entries(data.files).map(([k, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(k.toUpperCase())}</a>`).join(" · ")}
          (attached automatically when you send from here)</p>` : ""}
        ${data.warnings.map((w) => `<p class="flag">${esc(w)}</p>`).join("")}</div>`;
      toast("Logged. Send the written offer now.");
    } else {
      toast(data.message);
      apAdvance(data);
    }
  } catch (e) {
    toast(e.message, true);
  }
}

// -- all-in-one kanban ----------------------------------------------------------------------------------
async function renderKanban() {
  const { data: t } = await api("/api/today");
  const [b1, b2, , b4] = t.blocks;
  const item = (href, title, sub) => `<a class="kcard" href="${href}"><b>${esc(title)}</b><span>${sub}</span></a>`;
  const col = (title, cls, items, empty) => `<div class="kcol"><h2 class="${cls}">${title} <span class="muted">${items.length}</span></h2>
    ${items.length ? items.join("") : `<p class="muted">${esc(empty)}</p>`}</div>`;
  $("#kanban").innerHTML = `<h2>All queues</h2><div class="kanban">
      ${col("🚨 Urgent Listed Foreclosures", "bad", b1.rows.map((r) => item(`#/lead/${r.lead_id}`, r.address,
        `${countdown(r)} · ${esc(r.track_label)}`)), b1.empty)}
      ${col("📞 Ready-to-Call MLS Fixers", "good", b2.rows.map((r) => item(`#/lead/${r.lead_id}`, r.address,
        `${esc(r.town)} · ask ${kmoney(r.ask)} · MAO ${kmoney(r.mao_70)}`)), b2.empty)}
      ${col("💰 Cash Buyers to Call", "acc", b4.rows.map((r) => item("#/today", r.name,
        `${r.purchases_in_towns} buys · ${esc(r.recent.town)} ${kmoney(r.recent.price)}`)), b4.empty)}
      ${col("⏰ Follow-Ups Due Today", "warn", t.deadlines.followups.map((f) => item(`#/lead/${f.lead_id}`, f.address,
        label(f.stage))), "No follow-ups due right now.")}
    </div>
    <div class="row" style="margin-top:12px"><a class="btn" href="#/inspect">🔎 Virtual Inspector &amp; Mail</a>
      <a class="btn" href="#/closing">📑 Closing Coordinator &amp; Money</a><a class="btn" href="#/today">🗒️ Full 2-hour checklist</a></div>`;
}

// -- public records: OPRA tax lists, clerk filings, uploads ---------------------------------------------------
function openOpra() {
  location.hash = "#/records";
}
const COUNTIES = ["Middlesex", "Union", "Essex", "Hudson", "Passaic", "Bergen", "Camden", "Mercer", "Monmouth"];
async function renderRecords() {
  const [{ data: o }, { data: cf }] = await Promise.all([api("/api/opra"), api("/api/clerk-filings")]);
  const chan = { email: "✉️ e-mail", portal: "🌐 portal", verify: "📞 call first" };
  const statusPill = (r) => r.status === "received" ? '<span class="pill good">list received</span>'
    : r.status === "sent" ? `<span class="pill warn">sent · due ${day(r.due_date)}</span>` : '<span class="pill">not sent</span>';
  const opts = COUNTIES.map((c) => `<option>${c}</option>`).join("");
  app.innerHTML = `
    <div class="card"><div class="row spread"><h1>🏛️ Request Free Town Tax-Lien Spreadsheets (OPRA)</h1>
      <span class="muted">${o.requests.length} towns · commercial requests: up to ${o.rules.commercial_business_days} business days</span></div>
      <p>Each town's tax collector keeps the tax sale list, the town-held lien certificates and the delinquency report.
        Under the Open Public Records Act you can ask for them for free. Review each request, send it from your own e-mail or the
        town's portal, then click <b>Mark sent</b> so the due date is tracked.</p>
      <p class="flag">The request certifies that you WILL use the records for a commercial purpose (true for house buying; a
        false certification carries a ${esc(o.rules.penalty || "civil penalty")}). Read the three certifications before you send, and only
        send if each is true. Never use OPRAmachine for these: it publishes the replies.</p>
      ${o.operator_ready ? "" : '<p class="banner">Finish ⚙️ My Setup first: the request must include your name, address, phone and e-mail.</p>'}
      <div class="tablewrap"><table><thead><tr><th>Town</th><th class="num">Leads</th><th>How</th><th>Status</th><th></th></tr></thead><tbody>
      ${o.requests.map((r, i) => `<tr><td><b>${esc(r.town)}</b> <span class="muted">${esc(r.county)}</span>
          ${r.clerk ? `<br><span class="muted">${esc(r.clerk)}</span>` : ""}${r.notes.map((n) => `<br><span class="flag">${esc(n)}</span>`).join("")}</td>
        <td class="num">${r.leads}</td><td>${chan[r.channel] || esc(r.channel)}${r.to ? `<br><span class="mono">${esc(r.to)}</span>` : ""}</td>
        <td>${statusPill(r)}</td>
        <td class="btns">${r.mailto ? `<a class="btn" href="${esc(r.mailto)}">✉️ Open e-mail</a>` : ""}
          ${r.portal ? `<a class="btn" href="${esc(r.portal)}" target="_blank" rel="noopener noreferrer">🌐 Portal ↗</a>` : ""}
          <button class="btn" data-copy="${i}">📋 Copy text</button>
          <button class="btn good" data-sent="${i}">Mark sent</button>
          <details><summary class="muted">read the request</summary><pre class="pre">${esc(r.subject)}\n\n${esc(r.body)}</pre>
          ${r.source ? `<a class="muted" href="${esc(r.source)}" target="_blank" rel="noopener noreferrer">source ↗</a>` : ""}</details></td></tr>`).join("")}
      </tbody></table></div></div>
    <div class="grid g-dash" style="margin-top:14px">
      <div class="card"><h2>📥 1-click import: the town's spreadsheet</h2>
        <p class="muted">When the tax collector replies, upload their file (Excel or CSV with Block and Lot columns). Each parcel gets a
          tax-delinquent signal that stacks on the lead.</p>
        <form id="tax-form" class="row"><select name="county">${opts}</select><input type="text" name="town" placeholder="Town (e.g. Newark)" required>
          <input type="file" name="file" accept=".csv,.xlsx,.xlsm,.tsv,.txt" required><button class="btn good" type="submit">Import list</button></form>
        <div id="tax-result"></div>
        <h2 style="margin-top:16px">📥 Lis pendens export (browser-only clerks)</h2>
        <p class="muted">Passaic, Hudson, Bergen, Middlesex, Mercer: run the clerk's search in your browser, save the results page (.html) or
          export (.csv), and upload it here.</p>
        <form id="lp-form" class="row"><select name="county">${opts}</select>
          <input type="file" name="file" accept=".csv,.html,.htm,.txt,.tsv" required><button class="btn" type="submit">Import filings</button></form>
        <div id="lp-result"></div></div>
      <div class="card"><h2>⚖️ New foreclosure filings (last 45 days)</h2>
        <p class="muted">${cf.counts.total} filings from the county clerks · ${cf.counts.matched} tied to a parcel. For the rest, open the
          clerk record, read the property address off the document, and attach it.</p>
        ${cf.matched.length ? `<h3>Matched</h3><ul>${cf.matched.map((x) => `<li><a href="#/lead/${x.lead_id}">${esc(x.county)} · ${esc(x.town || "")} · ${esc(x.instrument)}</a>
          ${x.estate ? '<span class="pill vio">estate</span>' : ""}${x.tax_lien ? '<span class="pill warn">tax lien</span>' : ""}</li>`).join("")}</ul>` : ""}
        <h3>Need an address</h3><div class="tablewrap" style="max-height:420px"><table><tbody>
        ${cf.unmatched.map((x) => `<tr><td>${esc(x.recorded || "")}<br><span class="muted">${esc(x.county)}${x.town ? " · " + esc(x.town) : ""}</span></td>
          <td>${esc(x.defendants.join("; "))}<br><span class="muted">v. ${esc(x.plaintiffs.join("; "))}</span>
          ${x.estate ? '<span class="pill vio">estate</span>' : ""}${x.tax_lien ? '<span class="pill warn">tax lien</span>' : ""}</td>
          <td>${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">clerk record ↗</a>` : esc(x.instrument)}
          <form class="row locate" data-id="${x.id}"><input type="text" name="address" placeholder="street address">
          ${x.town ? "" : '<input type="text" name="town" placeholder="town">'}<button class="btn" type="submit">Attach</button></form></td></tr>`).join("")}
        </tbody></table></div></div>
    </div>`;
  document.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = async () => {
    const r = o.requests[+b.dataset.copy];
    toast((await copyText(`${r.subject}\n\n${r.body}`)) ? "Copied: paste it into the e-mail or portal" : "Select the text to copy it");
  }));
  document.querySelectorAll("[data-sent]").forEach((b) => (b.onclick = async () => {
    const r = o.requests[+b.dataset.sent];
    const { data } = await post("/api/opra/sent", { county: r.county, town: r.town, channel: r.channel === "portal" ? "portal" : "email" });
    toast(`Logged. ${r.town} should answer by ${day(data.due_date)}.`);
    renderRecords();
  }));
  const upload = (formId, url, out, fmt) => $(formId).addEventListener("submit", async (e) => {
    e.preventDefault();
    $(out).innerHTML = '<p class="muted">Importing… (looking parcels up in MOD-IV can take a minute)</p>';
    const res = await fetch(url, { method: "POST", body: new FormData(e.target) });
    const data = await res.json().catch(() => ({}));
    $(out).innerHTML = res.ok ? fmt(data) : `<p class="bad">${esc(data.detail || res.statusText)}</p>`;
    if (res.ok) loadCmdbar();
  });
  upload("#tax-form", "/api/imports/tax-list", "#tax-result", (d) => `<p class="good">${d.parcels} parcels: ${d.matched_local} already tracked,
    ${d.matched_njgin} new from MOD-IV, ${d.new_leads} new leads, ${d.unmatched} unmatched, ${d.non_residential} non-residential skipped.</p>
    ${d.notes.map((n) => `<p class="muted">${esc(n)}</p>`).join("")}`);
  upload("#lp-form", "/api/imports/lis-pendens", "#lp-result", (d) => `<p class="good">${d.filings} filings read, ${d.new_leads} new leads
    (${esc(d.status)}).</p>`);
  document.querySelectorAll("form.locate").forEach((f) => f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(f).entries());
    const res = await fetch(`/api/clerk-filings/${f.dataset.id}/locate`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return toast(data.detail || "not found", true);
    toast("Attached: the filing is now a lis pendens lead");
    location.hash = `#/lead/${data.lead_id}`;
  }));
}

loadCmdbar();
route();
