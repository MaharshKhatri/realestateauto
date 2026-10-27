"""Post-call coach: what was learned, what's still missing, what to do next, and compliance flags.

Rule-based and deterministic (works offline, on every call). When the operator opts in and the
`anthropic` package plus credentials are available, engine/ai.py adds a short Claude-written coaching
note on top — the rule-based output is always present and always wins on compliance.
"""
from __future__ import annotations

import re
from typing import Any

from db.models import CallOutcome, Lead
from engine.scripts import lead_facts, money

DEBRIEF_FIELDS = {
    "occupancy": "Who lives there (owner / heir / tenant / vacant)",
    "asking_price": "Their number (what they'd need to walk away with)",
    "timeline": "Timeline / urgency",
    "condition": "Condition (roof, HVAC, kitchen/baths, water)",
    "decision_makers": "Everyone on title / who decides",
    "motivation": "Why they'd sell (in their words)",
}
KEYWORD_TIPS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bbankrupt|chapter\s*(7|13)\b", re.I), "compliance",
     "Bankruptcy: the automatic stay (11 U.S.C. 362) applies — no contract without the debtor's bankruptcy attorney; "
     "a sale needs trustee/court approval."),
    (re.compile(r"\b(postpone|adjourn|stop the sale|delay the sale|push the sale)", re.I), "compliance",
     "They asked about postponing the sale: don't advise on it (regulated distressed-property service). Point them to "
     "the sheriff's office, an attorney or a HUD counselor (1-800-569-4287)."),
    (re.compile(r"\b(don'?t call|stop calling|take me off|remove my number|do not call)", re.I), "compliance",
     "They asked not to be called: log the outcome as DNC so the number is suppressed today."),
    (re.compile(r"\b(divorc|separat)", re.I), "tip",
     "Divorce/separation: both spouses (and any court order) control the sale — get both on the next call."),
    (re.compile(r"\b(passed away|passed(?! on)|deceased|died|estate|probate|executor|administrat|heir)", re.I), "tip",
     "Estate situation: switch to Playbook B — only the court-appointed executor/administrator can sign."),
    (re.compile(r"\btenant|renter|lease\b", re.I), "tip",
     "Tenants: NJ's Anti-Eviction Act means leases stay with the property — price it as a tenant-occupied sale and ask "
     "for the rent roll."),
    (re.compile(r"\b(mediation|modif|loss mitigation|forbearance)", re.I), "tip",
     "They're working with the lender: stay out of it, keep the door open, and follow up before the sale date."),
    (re.compile(r"\b(lawyer|attorney)\b", re.I), "tip",
     "They have/want an attorney: good — send the LOI to both and mention the 3-business-day attorney review."),
    (re.compile(r"\b(realtor|list it|listing|agent)\b", re.I), "tip",
     "They're weighing a listing: compare net proceeds side by side (commission, repairs, carrying costs, time)."),
    (re.compile(r"\b(roof|foundation|mold|water damage|fire|oil tank|knob)", re.I), "tip",
     "Condition issue mentioned: note it for the rehab estimate and the deal sheet — it supports the as-is price."),
]
OUTCOME_NEXT = {
    CallOutcome.NO_ANSWER: "Try a different time of day next attempt; after 3 tries leave a voicemail and send a letter.",
    CallOutcome.LEFT_VM: "Follow up in a few days at a different hour; mail a short letter with your number.",
    CallOutcome.WRONG_NUMBER: "Skip-trace the next-best number (relatives, heirs) and log it on the lead.",
    CallOutcome.SPOKE_WARM_FOLLOWUP: "Call back on the agreed day; send a one-line text/e-mail recap if they allowed it.",
    CallOutcome.SPOKE_NEEDS_OFFER: "Send a written LOI today while they're warm; book a follow-up call to walk through it.",
    CallOutcome.SPOKE_NOT_INTERESTED: "Respect it. One check-in in ~6 weeks, or sooner if the sale date moves.",
    CallOutcome.AGENT_WILL_PRESENT_LOI: "Send the LOI to the agent now with proof of funds; confirm the seller's response date.",
    CallOutcome.OFFER_ACCEPTED: "Generate the purchase agreement for attorney review; record the contract when fully signed.",
    CallOutcome.DEAD: "Close it out with the reason so the learner can use it.",
    CallOutcome.DNC: "Done — the number is suppressed. Don't contact this person by phone again.",
}
SPOKE = {CallOutcome.SPOKE_WARM_FOLLOWUP, CallOutcome.SPOKE_NEEDS_OFFER, CallOutcome.SPOKE_NOT_INTERESTED,
         CallOutcome.AGENT_WILL_PRESENT_LOI, CallOutcome.OFFER_ACCEPTED}


def rule_coach(lead: Lead, outcome: str, notes: str, debrief: dict[str, Any]) -> dict[str, Any]:
    f = lead_facts(lead)
    tips: list[str] = []
    flags: list[str] = []
    for pattern, kind, text in KEYWORD_TIPS:
        if notes and pattern.search(notes):
            (flags if kind == "compliance" else tips).append(text)
    if outcome == CallOutcome.DNC:
        flags = [x for x in flags if "log the outcome as DNC" not in x]
    missing: list[str] = []
    if outcome in SPOKE:
        known_occupancy = debrief.get("occupancy") or (lead.occupancy_status not in (None, "UNKNOWN"))
        for key, label in DEBRIEF_FIELDS.items():
            if key == "occupancy" and known_occupancy:
                continue
            if not debrief.get(key):
                missing.append(label)
        if f.get("frfpa_applies") and not known_occupancy:
            flags.append("Occupancy still unknown on a foreclosure lead: FRFPA pricing (>= 82% of FMV) applies until "
                         "you confirm nobody in the family lives there.")
        if lead.status_stage in ("NEW", "SKIP_TRACE", "CONTACT_ATTEMPTED", "CONTACTED"):
            tips.append("First conversation: the goal is trust and facts, not a price — end with a scheduled next step.")
    offer: dict[str, Any] = {}
    if outcome in (CallOutcome.SPOKE_NEEDS_OFFER, CallOutcome.AGENT_WILL_PRESENT_LOI, CallOutcome.OFFER_ACCEPTED):
        offer = {"anchor": f.get("anchor"), "walkaway": f.get("walkaway"), "floor": f.get("frfpa_floor"),
                 "verdict": f.get("verdict")}
        ask = _num(debrief.get("asking_price"))
        if ask and f.get("walkaway"):
            gap = ask - f["walkaway"]
            offer["gap_to_ask"] = gap
            tips.append(f"Their number {money(ask)} is {money(abs(gap))} "
                        f"{'above' if gap > 0 else 'below'} your walk-away {money(f['walkaway'])}" +
                        (" — work terms (close date, possession, no repairs) before moving on price." if gap > 0 else
                         " — don't raise your offer; confirm and write it up."))
        if f.get("verdict") in ("EQUITY_RESCUE_REFERRAL", "FRFPA_BLOCKED", "PASS"):
            flags.append(f"Verdict {f.get('verdict')}: no investor offer on this one — refer honestly (no referral fee).")
    days = f.get("days_to_sale")
    if days is not None and 0 <= days <= 14 and outcome not in (CallOutcome.DNC, CallOutcome.DEAD):
        tips.append(f"Sale in {days} days: keep follow-ups daily, but never pressure — urgency is theirs to feel.")
    summary = f"{outcome.replace('_', ' ').title()} — {f.get('address')}"
    if debrief.get("motivation"):
        summary += f"; motivation: {debrief['motivation']}"
    return {"source": "rules", "summary": summary, "next_action": OUTCOME_NEXT.get(outcome, ""),
            "missing_info": missing, "tips": tips, "compliance_flags": flags, "offer": offer}


def _num(v: Any) -> float | None:
    """'$350,000' / '350k' / 350000 → 350000.0; anything else → None."""
    if isinstance(v, (int, float)):
        return float(v)
    text = str(v or "").strip().lower()
    digits = re.sub(r"[^\d.]", "", text)
    try:
        n = float(digits) if digits else None
    except ValueError:
        return None
    if n is not None and re.search(r"\d\s*k\b", text):
        n *= 1000
    return n


def coach_call(lead: Lead, outcome: str, notes: str, debrief: dict[str, Any], use_ai: bool = False) -> dict[str, Any]:
    coach = rule_coach(lead, outcome, notes, debrief)
    if use_ai and outcome in SPOKE:
        from engine import ai
        extra = ai.coach_note(lead, outcome, notes, debrief, coach)
        if extra:
            coach["ai"] = extra
    return coach
