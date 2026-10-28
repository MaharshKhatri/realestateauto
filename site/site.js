"use strict";
// Seller / buyer forms. window.SITE_CONFIG comes from config.js:
//   served live by the local cockpit at /site/config.js (mode "local": POST /api/inbound/<kind>),
//   or written by `python manage.py site --build` for static hosting (mode "endpoint": a Formspree / webhook URL,
//   or mode "mailto": opens an e-mail to the operator with the answers as "key: value" lines).
(function () {
  const cfg = window.SITE_CONFIG || { mode: "mailto", counties: ["Middlesex", "Union", "Essex", "Hudson", "Passaic", "Bergen", "Camden", "Mercer", "Monmouth"] };
  document.querySelectorAll("[data-cfg]").forEach((el) => {
    const v = cfg[el.dataset.cfg];
    if (v) el.textContent = v;
  });
  const sel = document.getElementById("county");
  if (sel) (cfg.counties || []).forEach((c) => sel.insertAdjacentHTML("beforeend", `<option>${c}</option>`));
  const box = document.getElementById("counties");
  if (box) (cfg.counties || []).forEach((c) => box.insertAdjacentHTML("beforeend",
    `<label><input type="checkbox" name="counties" value="${c}"> ${c}</label>`));

  function collect(form) {
    const data = {};
    new FormData(form).forEach((v, k) => {
      if (k in data) data[k] = [].concat(data[k], v);
      else data[k] = ["situation", "counties", "asset_classes"].includes(k) ? [v] : v;
    });
    data.submitted_at = new Date().toISOString();
    data.source = location.hostname || "file";
    return data;
  }
  function check(kind, d) {
    if (kind === "seller") {
      if (!d.address) return "Please enter the property address.";
      if (!d.county) return "Please choose the county.";
      if (!d.phone && !d.email) return "Please leave a phone number or an e-mail so we can reply.";
    } else {
      if (!d.name) return "Please enter your name.";
      if (!d.phone && !d.email) return "Please leave a phone number or an e-mail.";
      if (!d.consent) return "Please tick the box so we're allowed to send you deals.";
    }
    return null;
  }
  async function send(kind, data) {
    if (cfg.mode === "local") {
      const r = await fetch(`/api/inbound/${kind}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || "Something went wrong — please call or e-mail us instead.");
      return;
    }
    const url = kind === "seller" ? cfg.seller_endpoint : cfg.buyer_endpoint;
    if (cfg.mode === "endpoint" && url) {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(data) });
      if (!r.ok) throw new Error("Something went wrong — please call or e-mail us instead.");
      return;
    }
    const lines = Object.entries(data).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join("\n");
    location.href = `mailto:${cfg.email || ""}?subject=${encodeURIComponent((kind === "seller" ? "Offer request: " + (data.address || "") : "Buyers list: " + (data.name || "")))}&body=${encodeURIComponent(lines)}`;
  }
  document.querySelectorAll("form[data-kind]").forEach((form) => form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const kind = form.dataset.kind, msg = form.querySelector(".msg"), btn = form.querySelector("button");
    const data = collect(form);
    const problem = check(kind, data);
    msg.className = "msg";
    if (problem) { msg.textContent = problem; msg.className = "msg err"; return; }
    btn.disabled = true;
    try {
      await send(kind, data);
      msg.textContent = kind === "seller" ? "Thank you — we'll be in touch shortly." : "You're on the list. Thank you!";
      msg.className = "msg ok";
      if (cfg.mode !== "mailto") form.reset();
    } catch (err) {
      msg.textContent = err.message; msg.className = "msg err";
    } finally { btn.disabled = false; }
  }));
})();
