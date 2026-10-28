"""Build the public seller / buyer funnel (site/) into a static folder for GitHub Pages / Netlify, and gate it.

    python manage.py build-site [--out site_dist] [--endpoint <form URL>] [--buyer-endpoint <URL>] [--check]

Operator details come from config/settings(.local).yaml, or from SITE_* environment variables (the GitHub Pages
workflow passes repository variables: SITE_COMPANY, SITE_OPERATOR, SITE_PHONE, SITE_EMAIL, SITE_POSTAL,
SITE_FORM_ENDPOINT, SITE_BUYER_ENDPOINT), so nothing personal has to live in the repository.

`check()` is the publish gate (no database needed, so it also works in CI):
  * every operator field the pages print is set (who we are, how to reach us, CAN-SPAM postal address);
  * the disclosures are present: investors not licensed agents, may assign the contract, attorney review,
    foreclosure-help numbers;
  * no e-mail address or phone number appears except the operator's own and the published help lines
    (HUD housing counseling, Legal Services of New Jersey) — a lead's contact details can never ride along;
  * only the expected files are in the folder.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from core.config import ROOT, settings

SRC = ROOT / "site"
FILES = ("index.html", "buyers.html", "site.css", "site.js")
HELP_LINES = {"8005694287", "8885765529"}            # HUD-approved counseling, Legal Services of NJ (1-800 / 1-888)
DISCLOSURES = {"index.html": ("not licensed real estate agents", "assign", "attorney review", "1-800-569-4287"),
               "buyers.html": ("assignment", "attorney review")}
ENV = {"company_name": "SITE_COMPANY", "name": "SITE_OPERATOR", "phone": "SITE_PHONE", "email": "SITE_EMAIL",
       "mailing_address": "SITE_POSTAL"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\d$.,])(?:1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")


def _digits(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def operator_overrides() -> dict[str, str]:
    return {k: os.environ[v].strip() for k, v in ENV.items() if os.environ.get(v, "").strip()}


def build(out: Path, endpoint: str | None = None, buyer_endpoint: str | None = None) -> dict[str, Any]:
    """Copy the pages and write config.js. Returns the config written."""
    from engine.inbound import site_config
    op = settings().operator
    saved = {k: op.get(k) for k in ENV}
    op.update(operator_overrides())
    try:
        cfg = site_config(endpoint or os.environ.get("SITE_FORM_ENDPOINT") or None,
                          buyer_endpoint or os.environ.get("SITE_BUYER_ENDPOINT") or None)
    finally:
        op.update(saved)
    out.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        shutil.copy2(SRC / f, out / f)
    (out / "config.js").write_text("window.SITE_CONFIG = " + json.dumps(cfg, indent=1) + ";\n", encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")            # GitHub Pages: serve files as they are
    return cfg


def check(out: Path, cfg: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in ("company", "phone", "email", "mailing_address"):
        if not cfg.get(key):
            problems.append(f"operator {key} is not set (settings.local.yaml or the SITE_* variables)")
    if cfg.get("company", "").strip().lower() in ("your company llc", ""):
        problems.append("company name is still the placeholder")
    if cfg.get("mode") != "endpoint":
        problems.append("no form endpoint: a static host can't receive the forms (set SITE_FORM_ENDPOINT / "
                        "--endpoint to your Formspree form)")
    allowed_emails = {cfg.get("email", "").lower()} - {""}
    allowed_phones = HELP_LINES | ({_digits(cfg.get("phone", ""))} - {""})
    expected = set(FILES) | {"config.js", ".nojekyll"}
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        rel = path.relative_to(out).as_posix()
        if rel not in expected:
            problems.append(f"unexpected file in the build: {rel}")
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for phrase in DISCLOSURES.get(rel, ()):
            if phrase.lower() not in text.lower():
                problems.append(f"{rel}: missing the disclosure “{phrase}”")
        for e in set(EMAIL_RE.findall(text)):
            if e.lower() not in allowed_emails and not e.lower().endswith((".test", "example.com")):
                problems.append(f"{rel}: e-mail address {e} is not the operator's")
        for ph in set(PHONE_RE.findall(text)):
            if _digits(ph) not in allowed_phones and len(_digits(ph)) == 10:
                problems.append(f"{rel}: phone number {ph} is not the operator's or a published help line")
    return problems
