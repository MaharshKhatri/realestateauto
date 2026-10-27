"""Optional Claude coaching note for the post-call debrief (engine/debrief.py).

Off unless the operator ticks "AI coach" (cockpit) / passes --ai (CLI), the `anthropic` package is
installed (`pip install -e ".[ai]"`) and credentials resolve (ANTHROPIC_API_KEY or an `ant auth login`
profile). Any failure returns None and the rule-based coach stands alone.

Data minimisation (Daniel's Law, N.J.S.A. 56:8-166.1, restricts sharing home addresses/phones with vendors):
the request carries the town, the deal numbers, the call outcome and the operator's notes with phone
numbers, e-mails and street numbers stripped — never names, phone numbers or the street address.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.config import settings
from db.models import Lead
from engine.scripts import lead_facts

log = logging.getLogger(__name__)

SYSTEM = (
    "You coach a New Jersey real estate investor (a wholesaler acting as principal, not a licensed agent) right "
    "after a seller call. Be concise and practical. Respect these rules: never suggest advising a homeowner on "
    "postponing or stopping a foreclosure sale or negotiating with their lender; never suggest pressure, "
    "misrepresentation or claiming to be a cash buyer; owner-occupied homes in foreclosure need at least 82% of "
    "fair market value under the Foreclosure Rescue Fraud Prevention Act; honor any do-not-call request."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "read_on_seller": {"type": "string", "description": "One or two sentences on motivation and mindset."},
        "next_call_plan": {"type": "array", "items": {"type": "string"}, "description": "2-4 concrete steps."},
        "questions_to_ask": {"type": "array", "items": {"type": "string"}, "description": "Up to 3 questions."},
        "watch_out": {"type": "string", "description": "One risk or compliance point, or an empty string."},
    },
    "required": ["read_on_seller", "next_call_plan", "questions_to_ask", "watch_out"],
    "additionalProperties": False,
}
PHONE_RE = re.compile(r"\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
STREET_NO_RE = re.compile(r"\b\d{1,6}(?:-\d{1,6})?\s+(?=[A-Z][a-z]+)")


def _cfg() -> dict[str, Any]:
    return settings().get("ai", {})


def available() -> bool:
    if not _cfg().get("enabled", True):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def redact(text: str) -> str:
    text = PHONE_RE.sub("[phone]", text or "")
    text = EMAIL_RE.sub("[email]", text)
    return STREET_NO_RE.sub("", text)


def call_context(lead: Lead, outcome: str, notes: str, debrief: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    f = lead_facts(lead)
    keep = ("town", "county", "units", "year_built", "days_to_sale", "upset", "fmv", "arv", "repairs", "anchor",
            "walkaway", "verdict", "frfpa_track", "dom", "list_price")
    return {"situation": {k: f.get(k) for k in keep if f.get(k) is not None},
            "stage": lead.status_stage, "outcome": outcome, "notes": redact(notes)[:2000],
            "debrief": {k: redact(str(v))[:300] for k, v in (debrief or {}).items() if v not in (None, "")},
            "rule_based_next_action": rules.get("next_action"), "compliance_flags": rules.get("compliance_flags")}


def coach_note(lead: Lead, outcome: str, notes: str, debrief: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any] | None:
    if not available():
        return None
    import anthropic

    payload = call_context(lead, outcome, notes, debrief, rules)
    try:
        client = anthropic.Anthropic(timeout=float(_cfg().get("timeout_seconds", 60)), max_retries=2)
        response = client.beta.messages.create(
            model=_cfg().get("model", "claude-opus-5-5"),
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": _cfg().get("effort", "low"),
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": "Post-call debrief (JSON):\n" + json.dumps(payload, default=str)}],
        )
    except anthropic.AuthenticationError:
        log.info("AI coach skipped: no Anthropic credentials")
        return None
    except anthropic.APIConnectionError as exc:
        log.warning("AI coach unreachable: %s", exc)
        return None
    except anthropic.APIStatusError as exc:
        log.warning("AI coach error %s: %s", exc.status_code, exc.message)
        return None
    if response.stop_reason == "refusal":
        log.info("AI coach declined (%s)", getattr(response.stop_details, "category", None))
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    data["model"] = response.model
    return data
