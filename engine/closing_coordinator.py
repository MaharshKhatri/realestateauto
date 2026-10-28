"""NJ contract-to-close coordinator: milestones with NJ business-day deadlines, title order e-mail, partners.

When a seller contract is recorded (`crm.record_contract`) the deal gets its checklist:

  1 CONTRACT_EXECUTED                          day 0 — fully signed contract delivered to both parties
  2 ATTORNEY_REVIEW_WINDOW                     3 NJ business days (later of that and the FRFPA 10-business-day
                                               cancellation for an owner-occupant); nothing is marketed before it
                                               concludes with no disapproval
  3 TITLE_SEARCH_AND_MUNICIPAL_LIEN_ORDERED    1 business day after review — title, taxes, water/sewer, CO/smoke cert
  4 EMD_DEPOSITED                              3 business days after review (our PSA's deposit term)
  5 VIRTUAL_WALKTHROUGH_AND_OIL_TANK_SWEEP     buyer walkthrough + tank sweep, 1 business day before inspection ends
  6 ASSIGNMENT_OR_DOUBLE_CLOSE_EXECUTED        by the end of the inspection period (10 business days after review)
                                               — winning buyer signed, non-refundable buyer deposit in escrow
  7 CLEAR_TO_CLOSE                             3 business days before closing
  8 CLOSED_PAID                                closing (30 days after review) — the fee goes into the ledger
  + FRFPA_CONTRACT_RECORDED (covered deals only) contract recorded within 10 business days of signing

Deadlines skip weekends and NJ state holidays (core.dates). A disapproval during attorney review cancels the
file. Partners (title agencies / closing attorneys) come from config/nj_closing_partners.yaml.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.base import AgentContext, BaseAgent
from core.config import ROOT, settings
from core.dates import is_business_day, long_date, nj_business_days_after
from db.models import ClosingMilestone, Interaction, Lead, LeadStage
from engine.scripts import lead_facts, money

PARTNERS_FILE = ROOT / "config" / "nj_closing_partners.yaml"
MILESTONES = [
    ("CONTRACT_EXECUTED", "Contract executed (day 0)"),
    ("ATTORNEY_REVIEW_WINDOW", "Attorney review concluded — no disapproval"),
    ("TITLE_SEARCH_AND_MUNICIPAL_LIEN_ORDERED", "Title search + municipal lien search ordered"),
    ("EMD_DEPOSITED", "Earnest money deposited in title/attorney escrow"),
    ("VIRTUAL_WALKTHROUGH_AND_OIL_TANK_SWEEP", "Buyer walkthrough done + oil tank sweep ordered"),
    ("ASSIGNMENT_OR_DOUBLE_CLOSE_EXECUTED", "Assignment / double close signed + buyer deposit in escrow"),
    ("CLEAR_TO_CLOSE", "Clear to close (title, payoffs, CO/smoke cert)"),
    ("CLOSED_PAID", "Closed and paid"),
]
FRFPA_MILESTONE = ("FRFPA_CONTRACT_RECORDED", "FRFPA: contract recorded with the county clerk")
LABELS = dict(MILESTONES + [FRFPA_MILESTONE])


def _cfg() -> dict[str, Any]:
    return dict(settings().get("contracts", {}))


def add_business_days(start: date, days: int) -> date:
    return nj_business_days_after(start, days) if days > 0 else start


def business_days_before(d: date, days: int) -> date:
    out, n = d, 0
    while n < days:
        out -= timedelta(days=1)
        if is_business_day(out):
            n += 1
    return out


def schedule(contract_date: date, review_ends: date, frfpa: bool, cfg: dict[str, Any] | None = None
             ) -> dict[str, date]:
    """Deadline per milestone from the contract date and the (already FRFPA-adjusted) review end."""
    cfg = cfg or _cfg()
    inspection_end = add_business_days(review_ends, int(cfg.get("inspection_business_days", 10)))
    closing = review_ends + timedelta(days=int(cfg.get("closing_days", 30)))
    while not is_business_day(closing):
        closing += timedelta(days=1)
    due = {
        "CONTRACT_EXECUTED": contract_date,
        "ATTORNEY_REVIEW_WINDOW": review_ends,
        "TITLE_SEARCH_AND_MUNICIPAL_LIEN_ORDERED": add_business_days(review_ends, 1),
        "EMD_DEPOSITED": add_business_days(review_ends, int(cfg.get("deposit_business_days", 3))),
        "VIRTUAL_WALKTHROUGH_AND_OIL_TANK_SWEEP": business_days_before(inspection_end, 1),
        "ASSIGNMENT_OR_DOUBLE_CLOSE_EXECUTED": inspection_end,
        "CLEAR_TO_CLOSE": business_days_before(closing, 3),
        "CLOSED_PAID": closing,
    }
    if frfpa:
        due["FRFPA_CONTRACT_RECORDED"] = add_business_days(contract_date, 10)
    return due


def milestones(session: Session, lead: Lead) -> list[ClosingMilestone]:
    return list(session.scalars(select(ClosingMilestone).where(ClosingMilestone.lead_id == lead.id)
                                .order_by(ClosingMilestone.sequence)))


def open_file(session: Session, lead: Lead) -> list[ClosingMilestone]:
    """Create (or re-date) the checklist for a lead under contract. Idempotent; completed steps keep their status."""
    from engine.underwriter import frfpa_decision
    if not (lead.contract_date and lead.attorney_review_ends):
        raise ValueError("record the signed contract first (crm.record_contract)")
    frfpa = frfpa_decision(lead.property).applies
    due = schedule(lead.contract_date, lead.attorney_review_ends, frfpa)
    existing = {m.milestone: m for m in milestones(session, lead)}
    order = [k for k, _ in MILESTONES] + ([FRFPA_MILESTONE[0]] if frfpa else [])
    for seq, key in enumerate(order, 1):
        m = existing.get(key)
        if m is None:
            m = ClosingMilestone(lead_id=lead.id, milestone=key, sequence=seq, status="pending")
            session.add(m)
        m.due_date = due[key]
        if key == "CONTRACT_EXECUTED" and m.status == "pending":
            m.status, m.completed_at = "done", datetime.combine(lead.contract_date, datetime.min.time())
    session.flush()
    return milestones(session, lead)


def _get(session: Session, lead: Lead, key: str) -> ClosingMilestone:
    m = session.scalar(select(ClosingMilestone).where(ClosingMilestone.lead_id == lead.id,
                                                      ClosingMilestone.milestone == key))
    if m is None:
        raise ValueError(f"no {key} milestone — open the closing file first")
    return m


def complete(session: Session, lead: Lead, key: str, notes: str = "", when: datetime | None = None,
             status: str = "done") -> ClosingMilestone:
    if key not in LABELS:
        raise ValueError(f"unknown milestone {key!r}")
    m = _get(session, lead, key)
    if key != "ATTORNEY_REVIEW_WINDOW" and _review_open(session, lead, (when or datetime.now()).date()) and \
            key in ("VIRTUAL_WALKTHROUGH_AND_OIL_TANK_SWEEP", "ASSIGNMENT_OR_DOUBLE_CLOSE_EXECUTED"):
        raise ValueError("attorney review has not concluded — nothing is marketed or assigned before it does")
    m.status, m.completed_at = status, when or datetime.now()
    if notes:
        m.notes = "\n".join(x for x in (m.notes, notes) if x)
    session.add(Interaction(lead_id=lead.id, channel="closing", outcome="MILESTONE_DONE",
                            notes=f"{LABELS[key]}" + (f" — {notes}" if notes else ""), occurred_at=m.completed_at))
    session.flush()
    return m


def _review_open(session: Session, lead: Lead, today: date) -> bool:
    m = session.scalar(select(ClosingMilestone).where(ClosingMilestone.lead_id == lead.id,
                                                      ClosingMilestone.milestone == "ATTORNEY_REVIEW_WINDOW"))
    return m is not None and m.status != "done"


def sync(session: Session, lead: Lead, today: date | None = None) -> list[str]:
    """Advance what the calendar decides: attorney review concludes the business day after its deadline unless a
    disapproval was recorded. Returns notes."""
    today = today or date.today()
    notes: list[str] = []
    ar = session.scalar(select(ClosingMilestone).where(ClosingMilestone.lead_id == lead.id,
                                                       ClosingMilestone.milestone == "ATTORNEY_REVIEW_WINDOW"))
    if ar and ar.status == "pending" and ar.due_date and today > ar.due_date:
        ar.status, ar.completed_at = "done", datetime.combine(ar.due_date + timedelta(days=1), datetime.min.time())
        ar.notes = "\n".join(x for x in (ar.notes, "Concluded with no disapproval recorded.") if x)
        notes.append(f"Attorney review concluded {ar.due_date:%a %b %d} — the deal can be shown to buyers.")
    session.flush()
    return notes


def record_disapproval(session: Session, lead: Lead, notes: str = "") -> None:
    """Either attorney disapproved during review: the contract is void; close the file and go back to negotiating."""
    for m in milestones(session, lead):
        if m.status == "pending":
            m.status = "cancelled"
    lead.status_stage = LeadStage.NEGOTIATING
    lead.contract_date = None
    lead.attorney_review_ends = None
    session.add(Interaction(lead_id=lead.id, channel="closing", outcome="ATTORNEY_DISAPPROVED", notes=notes or None,
                            occurred_at=datetime.now()))
    session.flush()


def close_deal(session: Session, lead: Lead, fee: float, closed_on: date | None = None, notes: str = ""
               ) -> ClosingMilestone:
    """CLOSED_PAID: the lead moves to CLOSED and the fee is booked as income in the ledger."""
    from engine import finance
    closed_on = closed_on or date.today()
    for key in ("CLEAR_TO_CLOSE", "ASSIGNMENT_OR_DOUBLE_CLOSE_EXECUTED"):
        m = _get(session, lead, key)
        if m.status == "pending":
            m.status, m.completed_at = "done", datetime.combine(closed_on, datetime.min.time())
    m = complete(session, lead, "CLOSED_PAID", notes or f"assignment fee {money(fee)}",
                 when=datetime.combine(closed_on, datetime.min.time()))
    lead.status_stage = LeadStage.CLOSED
    finance.add_entry(session, kind="income", category="assignment_fee", amount=fee, entry_date=closed_on,
                      description=f"Assignment fee — {lead_facts(lead)['address']}", lead_id=lead.id,
                      source_ref=f"closed|{lead.id}")
    session.flush()
    return m


@dataclass
class Alert:
    lead_id: int
    address: str
    milestone: str
    label: str
    due_date: date
    days_left: int
    overdue: bool


def alerts(session: Session, today: date | None = None, horizon_business_days: int = 2) -> list[Alert]:
    """Pending milestones that are overdue or due within the next couple of business days."""
    today = today or date.today()
    limit = add_business_days(today, horizon_business_days)
    rows = session.scalars(select(ClosingMilestone).where(ClosingMilestone.status == "pending",
                                                          ClosingMilestone.due_date <= limit)
                           .order_by(ClosingMilestone.due_date)).all()
    out = []
    for m in rows:
        lead = session.get(Lead, m.lead_id)
        if lead is None:
            continue
        out.append(Alert(lead_id=lead.id, address=lead_facts(lead)["address"], milestone=m.milestone,
                         label=LABELS[m.milestone], due_date=m.due_date, days_left=(m.due_date - today).days,
                         overdue=m.due_date < today))
    return out


# -- partners + title order ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def partners() -> dict[str, Any]:
    return yaml.safe_load(PARTNERS_FILE.read_text()) if PARTNERS_FILE.exists() else {"regions": {}}


def region_for(county: str | None) -> str | None:
    return (partners().get("county_region") or {}).get((county or "").title())


def pick_partner(county: str | None, kind: str = "title_agencies") -> dict[str, Any] | None:
    """The operator's preferred partner if set, else the first one in the county's region whose own page says it
    handles investor deals (assignments / double closings first)."""
    data = partners()
    pref_key = "title_agency" if kind == "title_agencies" else "closing_attorney"
    pref = (data.get("preferred") or {}).get(pref_key)
    every = [p for r in (data.get("regions") or {}).values() for p in r.get(kind) or []]
    if pref:
        found = next((p for p in every if p.get("name") == pref), None)
        if found:
            return found
    region = (data.get("regions") or {}).get(region_for(county) or "", {})
    options = region.get(kind) or []

    def rank(p: dict[str, Any]) -> tuple:
        ev = (p.get("investor_evidence") or "").lower()
        return (not any(w in ev for w in ("assign", "double clos", "wholesal")), not ev)
    return sorted(options, key=rank)[0] if options else None


def title_order_email(lead: Lead, partner: dict[str, Any] | None = None, structure: str = "assignment"
                      ) -> dict[str, str]:
    """Opening e-mail to the title agency: the deal, the structure, and exactly what we need searched."""
    from engine.contracts import buyer_entity, property_legal, seller_parties
    op = settings().operator
    cfg = _cfg()
    f = lead_facts(lead)
    legal = property_legal(lead)
    partner = partner or pick_partner(lead.property.county)
    to = (partner or {}).get("name") or "Title Officer"
    review = lead.attorney_review_ends
    closing = (review + timedelta(days=int(cfg.get("closing_days", 30)))) if review else None
    struct = ("an assignment of contract (the assignee closes directly with the seller; our fee is paid at "
              "closing on the settlement statement)" if structure == "assignment" else
              "a double close (A-B then B-C on the same day, with separate funds for each closing)")
    lines = [
        f"Hello {to},", "",
        f"I'd like to open a title order for a residential purchase in {legal['municipality']}, {legal['county']}.", "",
        f"  • Property: {legal['address']}, Block {legal['block']}, Lot {legal['lot']}"
        + (f", Qual {legal['qual']}" if legal.get("qual") else ""),
        f"  • Seller(s): {seller_parties(lead)[0]}",
        f"  • Buyer: {buyer_entity()}",
        f"  • Contract price: {money(lead.offer_amount)}; contract dated {lead.contract_date or 'TBD'}; attorney review "
        f"{'ended' if review and review < date.today() else 'ends'} {long_date(review) if review else 'TBD'}",
        f"  • Earnest money: {money(cfg.get('earnest_money', 1000))} to be held in your escrow",
        f"  • Target closing: on or before {long_date(closing) if closing else 'TBD'}",
        f"  • Structure: we expect {struct}. Please confirm you will insure this structure.", "",
        "Please order:",
        "  1. Full title search and commitment (judgments, liens, open mortgages, lis pendens)",
        "  2. Municipal lien search: taxes, water/sewer, special assessments, any tax sale certificate",
        "  3. Certificate of occupancy / resale inspection and smoke-CO-fire extinguisher certificate requirements "
        "for the town",
        "  4. Flood zone determination",
    ]
    sheriff = next((s for s in lead.property.active_signals if s.signal_type == "SHERIFF_SALE"), None)
    if sheriff:
        lines.append(f"  5. Foreclosure: docket {sheriff.case_number or 'unknown'}, plaintiff "
                     f"{(sheriff.plaintiff_or_lienholder or 'unknown').title()}; sheriff's sale scheduled "
                     f"{long_date(sheriff.scheduled_auction_date) if sheriff.scheduled_auction_date else 'TBD'}. "
                     "Please request the payoff/judgment figure from plaintiff's counsel.")
    if f.get("year_built") and f["year_built"] < 1980:
        lines.append("  • Built before 1980: the buyer will order an underground oil tank sweep.")
    lines += ["", "The signed contract is attached. Let me know the fee schedule and anything else you need.", "",
              f"Thank you,\n{op.get('name')}\n{op.get('company_name')}"
              + (f"\n{op.get('phone')}" if op.get("phone") else "") + (f"\n{op.get('email')}" if op.get("email") else "")]
    subject = f"New title order: {legal['address']} (Block {legal['block']}, Lot {legal['lot']})"
    return {"to": to, "email_to": (partner or {}).get("email") or "", "subject": subject, "body": "\n".join(lines),
            "partner": partner or {}}


def pipeline(session: Session, today: date | None = None) -> list[dict[str, Any]]:
    """Every open closing file with its next pending milestone."""
    today = today or date.today()
    ids = sorted({m.lead_id for m in session.scalars(select(ClosingMilestone).where(
        ClosingMilestone.status == "pending"))})
    out = []
    for lid in ids:
        lead = session.get(Lead, lid)
        if lead is None:
            continue
        ms = milestones(session, lead)
        nxt = next((m for m in ms if m.status == "pending"), None)
        out.append({"lead_id": lid, "address": lead_facts(lead)["address"], "stage": lead.status_stage,
                    "contract_price": lead.offer_amount, "done": sum(1 for m in ms if m.status == "done"),
                    "total": len(ms), "next": LABELS[nxt.milestone] if nxt else None,
                    "next_key": nxt.milestone if nxt else None,
                    "next_due": nxt.due_date.isoformat() if nxt and nxt.due_date else None,
                    "overdue": bool(nxt and nxt.due_date and nxt.due_date < today)})
    return out


# -- swarm agent -------------------------------------------------------------------------------------
class ClosingAlertsAgent(BaseAgent):
    name = "closing_alerts"
    description = "Advances attorney-review milestones and surfaces closing deadlines in the call queue."

    def run(self, ctx: AgentContext) -> None:
        s = ctx.session
        today = date.today()
        files = pipeline(s, today)
        for f in files:
            lead = s.get(Lead, f["lead_id"])
            for note in sync(s, lead, today):
                ctx.note(f"#{lead.id}: {note}")
        due = alerts(s, today)
        for a in due:
            lead = s.get(Lead, a.lead_id)
            when = datetime.combine(a.due_date, datetime.min.time()).replace(hour=9)
            if lead.next_followup_at is None or lead.next_followup_at > when:
                lead.next_followup_at = when        # the deadline shows up in today's follow-ups
            ctx.note(f"{'OVERDUE' if a.overdue else 'Due'} {a.due_date:%a %b %d}: #{a.lead_id} {a.address} — {a.label}")
        ctx.records_processed = len(files)
        ctx.metrics.update({"open_files": len(files), "due_or_overdue": len(due),
                            "overdue": sum(1 for a in due if a.overdue)})
