"""Money tracker: ledger, projected pipeline revenue, closed fees, expenses and the quarterly tax set-aside.

Wholesale assignment fees are self-employment income: federal income tax, self-employment tax (15.3% on
92.35% of net earnings) and NJ gross income tax. Nothing is withheld, so the operator pays quarterly estimates
(IRS 1040-ES, NJ-1040-ES) — due dates and thresholds come from config/nj_closing_partners.yaml (`tax:`), with
their sources. The engine sets aside `reserve_rate` (28%) of net profit as a habit; it is not a tax computation
— a CPA or the IRS / NJ worksheets decide what is actually owed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import BuyerBid, Lead, LeadStage, LedgerEntry

INCOME_CATEGORIES = ("assignment_fee", "double_close_profit", "jv_split", "other_income")
EXPENSE_CATEGORIES = ("marketing", "mail", "skip_trace", "dnc_data", "phone", "legal", "title", "earnest_money_lost",
                      "software", "education", "travel", "other")
# Chance a deal at this stage turns into a paid fee (planning heuristic; outcome learning can refine it).
STAGE_PROBABILITY = {LeadStage.OFFER_SENT_TO_AGENT: 0.05, LeadStage.OFFER_MADE: 0.10, LeadStage.NEGOTIATING: 0.20,
                     LeadStage.UNDER_CONTRACT: 0.50, LeadStage.ASSIGNED: 0.85}


def tax_cfg() -> dict[str, Any]:
    from engine.closing_coordinator import partners
    t = {"reserve_rate": 0.28, "self_employment_rate": 0.153, "se_net_earnings_factor": 0.9235,
         "federal_estimated_due_dates": [], "nj_estimated_due_dates": [], "notes": [], "sources": []}
    t.update(partners().get("tax") or {})
    return t


def add_entry(session: Session, *, kind: str, category: str, amount: float, entry_date: date | None = None,
              description: str | None = None, lead_id: int | None = None, source_ref: str | None = None
              ) -> LedgerEntry:
    """Record income or an expense. `source_ref` makes automatic entries idempotent (one fee per closing)."""
    if kind not in ("income", "expense"):
        raise ValueError("kind must be income or expense")
    allowed = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
    if category not in allowed:
        raise ValueError(f"{kind} category must be one of {', '.join(allowed)}")
    if amount <= 0:
        raise ValueError("amount must be positive")
    if source_ref:
        existing = session.scalar(select(LedgerEntry).where(LedgerEntry.source_ref == source_ref))
        if existing is not None:
            existing.amount, existing.entry_date = float(amount), entry_date or existing.entry_date
            session.flush()
            return existing
    row = LedgerEntry(kind=kind, category=category, amount=round(float(amount), 2), entry_date=entry_date or date.today(),
                      description=description, lead_id=lead_id, source_ref=source_ref)
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:  # pragma: no cover - concurrent duplicate source_ref
        session.rollback()
        raise ValueError(f"duplicate ledger entry {source_ref}") from exc
    return row


def expected_fee(session: Session, lead: Lead) -> float:
    """Accepted bid fee if a buyer is locked, else the underwriter's target fee."""
    bid = session.scalar(select(BuyerBid).where(BuyerBid.lead_id == lead.id, BuyerBid.status == "accepted"))
    if bid is not None and bid.bid_amount and lead.offer_amount:
        return max(bid.bid_amount - lead.offer_amount, 0)
    v = lead.property.current_valuation
    return float(v.target_assignment_fee or 0) if v else 0.0


@dataclass
class Quarter:
    due_date: str
    period_net: float
    set_aside: float
    paid_in_ledger: bool = False


@dataclass
class Summary:
    year: int
    income: float
    expenses: float
    net: float
    reserve_rate: float
    tax_reserve: float
    self_employment_tax_estimate: float
    pipeline_projected: float
    pipeline: list[dict[str, Any]] = field(default_factory=list)
    by_category: dict[str, float] = field(default_factory=dict)
    quarters: list[Quarter] = field(default_factory=list)
    next_due: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _period_bounds(dues: list[date], year: int) -> list[tuple[date, date, date]]:
    """(start, end, due) — IRS periods: Jan–Mar, Apr–May, Jun–Aug, Sep–Dec (due Apr 15, Jun 15, Sep 15, Jan 15)."""
    ends = [date(year, 3, 31), date(year, 5, 31), date(year, 8, 31), date(year, 12, 31)]
    starts = [date(year, 1, 1), date(year, 4, 1), date(year, 6, 1), date(year, 9, 1)]
    return [(s, e, d) for s, e, d in zip(starts, ends, dues)]


def summary(session: Session, year: int | None = None, today: date | None = None) -> Summary:
    today = today or date.today()
    year = year or today.year
    t = tax_cfg()
    rows = session.scalars(select(LedgerEntry).where(LedgerEntry.entry_date >= date(year, 1, 1),
                                                     LedgerEntry.entry_date <= date(year, 12, 31))).all()
    income = sum(r.amount for r in rows if r.kind == "income")
    expenses = sum(r.amount for r in rows if r.kind == "expense")
    net = income - expenses
    rate = float(t["reserve_rate"])
    by_cat: dict[str, float] = {}
    for r in rows:
        by_cat[r.category] = round(by_cat.get(r.category, 0) + (r.amount if r.kind == "income" else -r.amount), 2)
    pipe: list[dict[str, Any]] = []
    for lead in session.scalars(select(Lead).where(Lead.status_stage.in_(list(STAGE_PROBABILITY)))):
        fee = expected_fee(session, lead)
        p = STAGE_PROBABILITY[lead.status_stage]
        pipe.append({"lead_id": lead.id, "stage": lead.status_stage, "expected_fee": round(fee, -2), "probability": p,
                     "weighted": round(fee * p, -2), "address": lead.property.street_address})
    pipe.sort(key=lambda x: -x["weighted"])
    dues = [date.fromisoformat(d) for d in t.get("federal_estimated_due_dates") or []]
    quarters: list[Quarter] = []
    if len(dues) == 4 and dues[0].year == year:
        for start, end, due in _period_bounds(dues, year):
            p_net = sum(r.amount if r.kind == "income" else -r.amount for r in rows if start <= r.entry_date <= end)
            quarters.append(Quarter(due_date=due.isoformat(), period_net=round(p_net, 2),
                                    set_aside=round(max(p_net, 0) * rate, 2)))
    upcoming = next((q for q in quarters if date.fromisoformat(q.due_date) >= today), None)
    if not quarters:
        dues_note = [f"No {year} estimated-tax dates in config/nj_closing_partners.yaml — add them from the "
                     f"{year} Form 1040-ES / NJ-1040-ES."]
    else:
        dues_note = []
    notes = [f"Set aside {rate:.0%} of every fee the day it lands (separate savings account).",
             "Self-employment tax alone is about 14.1% of net profit (15.3% x 92.35%); federal and NJ income tax "
             "come on top — the 28% reserve is a habit, not a calculation. Check the IRS 1040-ES / NJ-1040-ES "
             "worksheets or a CPA before paying.",
             f"Estimated payments are due if you expect to owe ≥ ${t.get('federal_threshold_usd', 1000):,} federal or "
             f"> ${t.get('nj_threshold_usd', 400):,} NJ for the year."] + list(t.get("notes") or []) + dues_note
    return Summary(
        year=year, income=round(income, 2), expenses=round(expenses, 2), net=round(net, 2), reserve_rate=rate,
        tax_reserve=round(max(net, 0) * rate, 2),
        self_employment_tax_estimate=round(max(net, 0) * float(t["se_net_earnings_factor"])
                                           * float(t["self_employment_rate"]), 2),
        pipeline_projected=round(sum(x["weighted"] for x in pipe), -2), pipeline=pipe, by_category=by_cat,
        quarters=quarters,
        next_due={"date": upcoming.due_date, "set_aside": upcoming.set_aside} if upcoming else None, notes=notes)
