"""The 2-hour daily money-making checklist (`python manage.py today`, the cockpit home page).

  Deadlines first  closing milestones due / overdue and follow-ups due now
  BLOCK 1 (30 min) the top 5 listed foreclosures with an agent — auction date, agent phone, script, LOI
  BLOCK 2 (45 min) the next 15 ready-to-call deals — ask vs MAO gap, agent phone / e-mail
  BLOCK 3 (25 min) 10 high-equity estates / vacant sheriff sales with no phone yet — manual people-search links
  BLOCK 4 (20 min) 5 named cash buyers who bought in Elizabeth / Newark / Jersey City — recent purchase + script

Everything comes from wholesaling.db at the moment it runs; nothing is invented. Empty blocks say why and what
fills them.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from agents.enrichment.public_owner_resolver import entity_lookup_links
from agents.enrichment.skiptrace_pipeline import callable_phone, lookup_links, skiptrace_target
from core.text import format_phone, is_entity, muni_base, postal_title
from db.models import CashBuyer, DistressSignal, Lead, LeadStage, Property, SignalType, Valuation
from engine import briefing, crm
from engine.scripts import build_buyer_pitch, buyer_display_name, lead_facts

BUYER_TOWNS = ("ELIZABETH", "NEWARK", "JERSEY CITY")
CLOSED = [LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED]
DEALS = ("STRONG", "WORKABLE", "WORKABLE_82_PCT_FLOOR", "TIGHT")


def block_listed(session: Session, n: int = 5, now: datetime | None = None) -> list[dict[str, Any]]:
    from engine.fast_track import agent_script, listed_foreclosures
    now = now or datetime.now()
    rows = []
    for lf in listed_foreclosures(session):
        if not lf.agent or lf.track in ("NO_OFFER", "EQUITY_MONITOR"):
            continue
        if lf.last_outreach and (now - datetime.fromisoformat(lf.last_outreach)).days < 2:
            continue                                    # already sent — it is in today's follow-ups
        sc = agent_script(lf)
        rows.append({**lf.to_dict(), "opening": sc["opening"], "offer_line": sc["offer"],
                     "cli": f"python manage.py fast-track --script {lf.lead_id}"})
        if len(rows) >= n:
            break
    return rows


def block_ready(session: Session, n: int = 15, skip: set[int] | None = None) -> list[dict[str, Any]]:
    skip = skip or set()
    rows = [r for r in briefing.ready_rows(session, n + len(skip) + 5) if r["lead_id"] not in skip][:n]
    for r in rows:
        r["address"], r["town"] = postal_title(r.get("address")), postal_title(muni_base(r.get("town")))
        ask = r.get("ask") or r.get("upset")
        r["gap_to_ask"] = round(ask - r["mao_70"], -2) if ask and r.get("mao_70") else None
        r["phone_display"] = format_phone(r["phone"]) if r.get("phone") else None
    return rows


def block_skiptrace(session: Session, n: int = 10) -> list[dict[str, Any]]:
    """Estates and vacant properties headed to sheriff sale, FRFPA-exempt or presumed exempt, no phone yet,
    biggest spread first — the leads where one skip trace is worth the most."""
    from engine.underwriter import frfpa_decision
    stmt = (select(Lead).join(Property, Lead.property_id == Property.id)
            .join(Valuation, (Valuation.property_id == Property.id) & Valuation.is_current.is_(True))
            .join(DistressSignal, (DistressSignal.property_id == Property.id)
                  & (DistressSignal.signal_type == SignalType.SHERIFF_SALE) & DistressSignal.active.is_(True))
            .where(Lead.status_stage.notin_(CLOSED), Valuation.verdict.in_(DEALS),
                   DistressSignal.scheduled_auction_date >= date.today())
            .order_by(Valuation.deal_spread.desc().nulls_last())
            .options(selectinload(Lead.contacts), selectinload(Lead.property)))
    rows = []
    for lead in session.scalars(stmt).unique():
        p = lead.property
        types = {s.signal_type for s in p.active_signals}
        estate = bool(p.is_estate_owned) or bool(types & {SignalType.ESTATE_OWNER, SignalType.PROBATE})
        if not (estate or SignalType.VACANT in types) or callable_phone(lead) is not None:
            continue
        if frfpa_decision(p).applies:
            continue
        target = skiptrace_target(lead)
        if target is None:
            continue
        v = p.current_valuation
        f = lead_facts(lead)
        rows.append({"lead_id": lead.id, "address": f["address"], "kind": "estate" if estate else "vacant",
                     "sale_date": f.get("sale_date"), "days_to_sale": f.get("days_to_sale"), "spread": v.deal_spread,
                     "mao_70": v.mao_conservative, "upset": f.get("upset"), "target": target.name,
                     "role": target.role, "links": {k: u for k, u in lookup_links(lead, target).items() if u}})
        if len(rows) >= n:
            break
    return rows


def block_buyers(session: Session, n: int = 5) -> list[dict[str, Any]]:
    """Named cash buyers whose recorded purchases include Elizabeth, Newark or Jersey City — flippers first."""
    out = []
    for b in session.scalars(select(CashBuyer)):
        name = buyer_display_name(b)
        if not name:
            continue
        buys = [x for x in (b.recent_purchases_json or []) if isinstance(x, dict)
                and muni_base(x.get("municipality")) in BUYER_TOWNS and x.get("address")]
        if not buys:
            continue
        buys.sort(key=lambda x: str(x.get("deed_date") or ""), reverse=True)
        recent = buys[0]
        flipper = (b.flips_count or 0) > 0 or (b.buyer_type or "") == "flipper"
        card = build_buyer_pitch(b)
        out.append({"buyer_id": b.id, "name": name, "entity": b.entity_name, "flipper": flipper,
                    "purchases_in_towns": len(buys), "purchases": b.cash_purchases_count or 0,
                    "recent": {"address": postal_title(recent.get("address")),
                               "town": postal_title(muni_base(recent.get("municipality"))),
                               "price": recent.get("price"), "date": recent.get("deed_date")},
                    "phone_display": format_phone(b.phone) if b.phone else None, "email": b.email,
                    "links": entity_lookup_links(b.entity_name) if is_entity((b.entity_name or "").upper()) else {},
                    "opening": card.opening, "discovery": card.discovery[:3],
                    "_rank": (not flipper, -len(buys), -(b.cash_purchases_count or 0), str(recent.get("deed_date")))})
    out.sort(key=lambda r: r["_rank"])
    for r in out:
        r.pop("_rank")
    return out[:n]


def deadlines(session: Session) -> dict[str, Any]:
    from engine import closing_coordinator as cc
    due = []
    for lead in crm.due_followups(session):
        last = lead.interactions[0] if lead.interactions else None
        due.append({"lead_id": lead.id, "address": lead_facts(lead)["address"], "stage": lead.status_stage,
                    "due": lead.next_followup_at.isoformat() if lead.next_followup_at else None,
                    "last": last.outcome if last else None})
    return {"closing": [{**a.__dict__, "due_date": a.due_date.isoformat()} for a in cc.alerts(session)],
            "followups": due[:15]}


def today(session: Session, now: datetime | None = None) -> dict[str, Any]:
    b1 = block_listed(session, 5, now)
    b2 = block_ready(session, 15, {r["lead_id"] for r in b1})
    b3 = block_skiptrace(session, 10)
    b4 = block_buyers(session, 5)
    return {
        "date": (now or datetime.now()).date().isoformat(),
        "deadlines": deadlines(session),
        "blocks": [
            {"key": "listed", "minutes": 30, "title": "Call / e-mail the top 5 listed foreclosures (auction + agent)",
             "rows": b1, "empty": "No listed foreclosures with an agent to contact — run `manage.py stack` after the "
                                  "morning scrape."},
            {"key": "ready", "minutes": 45, "title": "Call / blast the next 15 ready-to-call stale MLS fixers",
             "rows": b2, "empty": "Nothing ready to call — underwrite more listings and refresh listing agents."},
            {"key": "skiptrace", "minutes": 25, "title": "Skip-trace 10 high-equity exempt estates / vacant sheriff sales",
             "rows": b3, "empty": "No un-skip-traced exempt estates or vacant sheriff sales right now."},
            {"key": "buyers", "minutes": 20, "title": "Call 5 active cash buyers in Elizabeth / Newark / Jersey City",
             "rows": b4, "empty": "No named buyers with purchases there yet — import county deed exports."},
        ],
        "reminder": "Investor, not an agent · may assign · attorney review · 8am–9pm only · stop on any STOP request · "
                    "never offer to stop or postpone a sheriff's sale.",
    }


def summary_line(t: dict[str, Any]) -> str:
    counts = ", ".join(f"{b['key']} {len(b['rows'])}" for b in t["blocks"])
    return f"{t['date']}: {counts}; deadlines {len(t['deadlines']['closing'])}, follow-ups {len(t['deadlines']['followups'])}"

