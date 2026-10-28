"""Operator setup from the dashboard (My Setup & Auto-Email): name, company, Google Voice number, e-mail, mailing
address and an optional Gmail app password.

  * Profile fields go to config/settings.local.yaml (git-ignored), merged over config/settings.yaml.
  * SMTP_USER / SMTP_APP_PASSWORD go to .env (git-ignored, chmod 600). The password is never returned to the
    browser, never logged, and an empty password field keeps the stored one.
  * Saving takes effect immediately in the running server: the settings cache is updated in place and the SMTP
    variables are put in the process environment.

"Live mode" means everything a compliant message needs is set: a real name and company (outreach identifies the
sender), a 10-digit phone (NJ telemarketing: give a callback number), an e-mail, and a postal address with a ZIP
(CAN-SPAM; letters stop printing the PROOF watermark once name, phone and address are set). Auto-e-mail needs
the Gmail app password on top.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from core.config import ENV_FILE, LOCAL_SETTINGS, ROOT, Section, settings
FIELDS = ("name", "company_name", "phone", "email", "mailing_address")
PLACEHOLDERS = {"name": {"", "operator"}, "company_name": {"", "your company llc"}}
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
POSTAL_RE = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
HEADER = ("# Personal settings written by the dashboard (My Setup). Git-ignored: keep your details out of the repo.\n"
          "# Same structure as config/settings.yaml; only the keys you change.\n")


class SetupError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _digits(phone: str | None) -> str:
    d = "".join(ch for ch in (phone or "") if ch.isdigit())
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def problems(op: dict[str, Any]) -> dict[str, str]:
    """Field -> what's wrong (empty dict = complete)."""
    out: dict[str, str] = {}
    for key in ("name", "company_name"):
        if str(op.get(key) or "").strip().lower() in PLACEHOLDERS[key]:
            out[key] = "required (outreach must say who you are)"
    if len(_digits(op.get("phone"))) != 10:
        out["phone"] = "10-digit US number (your Google Voice number is fine)"
    if not EMAIL_RE.fullmatch(str(op.get("email") or "").strip()):
        out["email"] = "a valid e-mail address"
    if not POSTAL_RE.search(str(op.get("mailing_address") or "").upper()):
        out["mailing_address"] = "full postal address with state and ZIP, e.g. PO Box 10, Edison, NJ 08817"
    return out


def status() -> dict[str, Any]:
    op = settings().operator
    values = {k: str(op.get(k) or "") for k in FIELDS}
    missing = problems(values)
    smtp_user = os.environ.get("SMTP_USER", "")
    has_password = bool(os.environ.get("SMTP_APP_PASSWORD"))
    return {**values, "smtp_user": smtp_user, "has_app_password": has_password, "missing": missing,
            "live": not missing, "auto_email": not missing and bool(smtp_user) and has_password,
            "local_file": str(LOCAL_SETTINGS.relative_to(ROOT)) if LOCAL_SETTINGS.is_relative_to(ROOT)
            else str(LOCAL_SETTINGS)}


def _write_env(updates: dict[str, str], path: Path) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    seen: set[str] = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    path.write_text("\n".join(out) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def save(data: dict[str, Any], *, local_path: Path | None = None, env_path: Path | None = None) -> dict[str, Any]:
    """Validate and persist the operator profile (+ optional SMTP credentials). Raises SetupError."""
    local_path = local_path or LOCAL_SETTINGS
    env_path = env_path or ENV_FILE
    op = {k: re.sub(r"\s+", " ", str(data.get(k) if data.get(k) is not None else "")).strip() for k in FIELDS}
    errors = problems(op)
    smtp_user = str(data.get("smtp_user") or "").strip()
    password = re.sub(r"\s+", "", str(data.get("app_password") or ""))
    if password and not smtp_user:
        smtp_user = op["email"]
    if smtp_user and not EMAIL_RE.fullmatch(smtp_user):
        errors["smtp_user"] = "the Gmail address you send from"
    if password and not re.fullmatch(r"[A-Za-z0-9]{16}", password):
        errors["app_password"] = "a Gmail app password is 16 letters (Google Account > Security > App passwords)"
    if errors:
        raise SetupError(errors)
    digits = _digits(op["phone"])
    op["phone"] = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    local = (yaml.safe_load(local_path.read_text()) or {}) if local_path.exists() else {}
    local.setdefault("operator", {}).update(op)
    scrapers = local.setdefault("scrapers", {})
    if not (scrapers.get("contact_email") or settings().get("scrapers", {}).get("contact_email")):
        scrapers["contact_email"] = op["email"]            # identifies us to record custodians (User-Agent)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(HEADER + yaml.safe_dump(local, sort_keys=False, allow_unicode=True))
    env: dict[str, str] = {}
    if smtp_user:
        env["SMTP_USER"] = smtp_user
    if password:
        env["SMTP_APP_PASSWORD"] = password
    if env:
        _write_env(env, env_path)
        os.environ.update(env)
    # Take effect now, in this process: update the cached settings in place.
    cached = settings()
    if not isinstance(cached.get("operator"), dict):
        cached["operator"] = Section()
    cached["operator"].update(op)
    if scrapers.get("contact_email"):
        cached.setdefault("scrapers", Section())["contact_email"] = scrapers["contact_email"]
    return status()
