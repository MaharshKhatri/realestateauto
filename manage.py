#!/usr/bin/env python3
"""Operator CLI.

    python manage.py init                     create/upgrade the database
    python manage.py briefing [--top 15]      Daily Action Briefing
    python manage.py lead <id>                everything about one lead
    python manage.py underwrite [--top 15]    comps / ARV / MAO for the top leads
    python manage.py buyers [--top 20]        active cash buyers
    python manage.py stats                    database and pipeline stats
    python manage.py swarm <args...>          passthrough to `python -m swarm.orchestrator`
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, timedelta

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import func, select

from db.database import init_db, session_scope
from db.models import AgentRun, CashBuyer, DistressSignal, Lead, Property, SignalType, Valuation

console = Console(width=200)
TERMINAL = ["DEAD", "DO_NOT_CONTACT", "CLOSED"]


def money(v: float | None) -> str:
    return f"${v:,.0f}" if v is not None else "—"


def short_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v / 1000:,.0f}K" if abs(v) < 1_000_000 else f"${v / 1_000_000:,.2f}M"


def headline_reason(lead: Lead) -> str:
    reasons = (lead.score_breakdown_json or {}).get("reasons") or []
    return reasons[0] if reasons else (lead.primary_source or "")


def owner_label(lead: Lead) -> str:
    p = lead.property
    if p.owner_name:
        src = {"sheriff_defendant": "defendant"}.get(p.owner_name_source or "", "")
        return f"{p.owner_name}" + (f" ({src})" if src else "")
    if lead.contact_name:
        return lead.contact_name
    if p.owner_mailing_address:
        return f"[name pending] mail: {p.owner_mailing_address}, {p.owner_city_state_zip or ''}"
    return "[name pending]"


def cmd_briefing(args: argparse.Namespace) -> None:
    init_db()
    today = date.today()
    with session_scope() as s:
        q = select(Lead).where(Lead.status_stage.notin_(TERMINAL))
        if args.source:
            q = q.where(Lead.primary_source == args.source)
        if args.underwritten:
            q = q.join(Valuation, Valuation.property_id == Lead.property_id).where(Valuation.is_current.is_(True))
        leads = s.scalars(q.order_by(Lead.priority_rank_score.desc()).limit(args.top)).all()
        open_total = s.scalar(select(func.count(Lead.id)).where(Lead.status_stage.notin_(TERMINAL)))
        by_source = Counter(r[0] for r in s.execute(select(Lead.primary_source).where(Lead.status_stage.notin_(TERMINAL))))
        auctions_14 = s.scalar(select(func.count(DistressSignal.id)).where(
            DistressSignal.signal_type == SignalType.SHERIFF_SALE, DistressSignal.active.is_(True),
            DistressSignal.scheduled_auction_date.between(today, today + timedelta(days=14))))
        vals = s.scalars(select(Valuation).where(Valuation.is_current.is_(True))).all()
        verdicts = Counter(v.verdict for v in vals)
        buyers = s.scalar(select(func.count(CashBuyer.id)))
        last_runs = {}
        for r in s.scalars(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(60)):
            last_runs.setdefault(r.agent_name, r)

        console.print(Panel.fit(
            f"[bold]Daily Action Briefing — {today:%A, %B %d, %Y}[/bold]\n"
            f"Open leads: [bold]{open_total:,}[/bold]  ({', '.join(f'{k} {v:,}' for k, v in by_source.most_common())})\n"
            f"Sheriff sales in next 14 days: [bold]{auctions_14}[/bold]   ·   Underwritten: {len(vals)} "
            f"({', '.join(f'{k} {v}' for k, v in verdicts.most_common())})   ·   Cash buyers on file: {buyers}\n"
            f"Data freshness: " + ", ".join(
                f"{n} {r.started_at:%m/%d %H:%M} ({r.status})" for n, r in sorted(last_runs.items())),
            title="NJ Wholesale Engine", border_style="cyan"))

        table = Table(box=box.SIMPLE_HEAVY, show_lines=False, header_style="bold magenta",
                      title=f"Top {len(leads)} leads to work today", title_justify="left")
        for col, kw in [("#", {"justify": "right"}), ("Pri", {"justify": "right"}), ("M/V", {}),
                        ("Property", {"max_width": 34}), ("Owner / contact", {"max_width": 30}),
                        ("Why now", {"max_width": 44}), ("ARV", {"justify": "right"}), ("As-is", {"justify": "right"}),
                        ("Repairs", {"justify": "right"}), ("MAO 70/75", {"justify": "right"}),
                        ("Anchor", {"justify": "right"}), ("Fee", {"justify": "right"}), ("Debt", {"justify": "right"}),
                        ("Verdict", {})]:
            table.add_column(col, **kw)
        for i, lead in enumerate(leads, 1):
            p = lead.property
            v = p.current_valuation
            verdict_style = {"STRONG": "bold green", "WORKABLE": "green", "TIGHT": "yellow",
                             "SHORT_SALE_ONLY": "red", "FRFPA_BLOCKED": "red", "PASS": "dim red"}.get(v.verdict if v else "", "dim")
            table.add_row(
                str(i), f"{lead.priority_rank_score:.0f}", f"{lead.motivation_score:.0f}/{lead.deal_viability_score:.0f}",
                f"{p.street_address or '?'}\n[dim]{p.municipality or p.city or ''} {p.zip_code or ''} · {p.county}[/dim]",
                owner_label(lead), headline_reason(lead),
                short_money(v.arv_estimate) if v else "—", short_money(v.as_is_value) if v else short_money(p.estimated_value),
                short_money(v.est_repair_cost) + (f"\n[dim]{v.rehab_tier}[/dim]" if v else "") if v else "—",
                f"{short_money(v.mao_conservative)}/{short_money(v.mao_aggressive)}" if v else "—",
                short_money(v.anchor_offer) if v else "—", short_money(v.target_assignment_fee) if v else "—",
                short_money(v.debt_estimate) if v and v.debt_estimate else "—",
                f"[{verdict_style}]{v.verdict if v else 'not underwritten'}[/{verdict_style}]"
                + (f"\n[dim]conf {v.confidence_score:.0f}[/dim]" if v else ""))
        console.print(table)

        console.print("[bold]Why the engine ranked them here[/bold]")
        for i, lead in enumerate(leads, 1):
            p = lead.property
            reasons = (lead.score_breakdown_json or {}).get("reasons") or []
            factors = (lead.score_breakdown_json or {}).get("factors") or {}
            top_factors = ", ".join(f"{k} +{v:.0f}" for k, v in sorted(factors.items(), key=lambda kv: -kv[1])[:4])
            console.print(f"[bold cyan]{i:>2}. {p.street_address}, {p.municipality or ''}[/bold cyan] "
                          f"[dim](lead #{lead.id}; motivation {lead.motivation_score:.0f}: {top_factors})[/dim]")
            for r in reasons[1:5]:
                console.print(f"     • {r}")
            if lead.ai_strategy_brief:
                console.print(f"     [green]Plan:[/green] {lead.ai_strategy_brief}")
        console.print(Panel(
            "Every call: identify yourself as a real estate investor (not an agent), say you may assign the contract to "
            "another investor, and encourage attorney review. Call 8am–9pm only; scrub numbers against the National DNC "
            "list before calling; stop immediately on any do-not-call request.",
            title="Compliance reminder", border_style="yellow"))


def cmd_lead(args: argparse.Namespace) -> None:
    init_db()
    with session_scope() as s:
        lead = s.get(Lead, args.id)
        if not lead:
            console.print(f"[red]lead {args.id} not found[/red]")
            return
        p = lead.property
        console.print(Panel.fit(
            f"[bold]{p.street_address}, {p.municipality} {p.zip_code or ''} ({p.county})[/bold]\n"
            f"Parcel {p.parcel_id} · class {p.property_class} · {p.building_desc or ''} · {p.dwellings or '?'} unit(s) · "
            f"built {p.year_built or '?'} · {p.sqft or '?'} sqft · {p.lot_size or '?'} ac\n"
            f"Owner: {owner_label(lead)}\nMailing: {p.owner_mailing_address or '—'}, {p.owner_city_state_zip or ''}"
            f" · absentee={p.is_absentee} out-of-state={p.is_out_of_state}\n"
            f"Assessed {money(p.assessed_total)} · taxes {money(p.annual_taxes)}/yr · last sale {p.last_sale_date} for "
            f"{money(p.last_sale_price)} · est. value {money(p.estimated_value)} · est. equity {money(p.estimated_equity)}\n"
            f"Scores: priority {lead.priority_rank_score:.0f} · motivation {lead.motivation_score:.0f} · "
            f"viability {lead.deal_viability_score:.0f} · stage {lead.status_stage} · source {lead.primary_source}",
            title=f"Lead #{lead.id}"))
        for sig in p.signals:
            console.print(f"  [{'green' if sig.active else 'dim'}]{sig.signal_type}[/] {sig.status or ''} "
                          f"auction={sig.scheduled_auction_date} owed={money(sig.amount_owed)} "
                          f"plaintiff={(sig.plaintiff_or_lienholder or '')[:60]} case={sig.case_number or ''} {sig.source_url or ''}")
        v = p.current_valuation
        if v:
            a = v.assumptions_json or {}
            console.print(Panel.fit(
                f"ARV {money(v.arv_estimate)} (before tax drag {money(a.get('arv_before_tax_drag'))}) · as-is {money(v.as_is_value)}\n"
                f"Repairs {money(v.est_repair_cost)} [{v.rehab_tier}] {a.get('rehab_by_tier')} — {'; '.join(a.get('rehab_reasons') or [])}\n"
                f"MAO conservative {money(v.mao_conservative)} · aggressive {money(v.mao_aggressive)} · anchor {money(v.anchor_offer)} · "
                f"fee {money(v.target_assignment_fee)}\nDebt {money(v.debt_estimate)} · spread {money(v.deal_spread)} · "
                f"verdict {v.verdict} · confidence {v.confidence_score} · {v.comp_count} comps ({v.comp_source})\n"
                f"End buyer pays {money(a.get('buyer_purchase_price'))}, projected profit {money(a.get('buyer_profit'))} "
                f"({(a.get('buyer_roi') or 0):.0%} ROI)\nNotes: {' | '.join(a.get('comp_notes') or [])}\nFlags: {'; '.join(a.get('flags') or []) or 'none'}",
                title="Underwriting"))
            t = Table(box=box.MINIMAL, title="Comps")
            for col in ("bucket", "address", "sold", "price", "sqft", "$/sqft", "dist mi", "source"):
                t.add_column(col)
            for c in (v.comps_json or [])[:15]:
                t.add_row(c.get("bucket", ""), c.get("address", "")[:40], c.get("sold_date", ""), money(c.get("sold_price")),
                          str(c.get("sqft") or ""), f"{c['price_per_sqft']:.0f}" if c.get("price_per_sqft") else "",
                          str(c.get("distance_mi") or ""), c.get("source", ""))
            console.print(t)
        if lead.ai_strategy_brief:
            console.print(Panel(lead.ai_strategy_brief, title="Strategy"))


def cmd_buyers(args: argparse.Namespace) -> None:
    init_db()
    with session_scope() as s:
        t = Table(box=box.SIMPLE_HEAVY, title="Active cash buyers (recorded purchases, last 24 months of deed data)")
        for col in ("score", "buyer (mailing address)", "type", "buys", "volume", "price range", "last buy", "towns"):
            t.add_column(col)
        for b in s.scalars(select(CashBuyer).order_by(CashBuyer.reliability_score.desc()).limit(args.top)):
            t.add_row(f"{b.reliability_score:.0f}", b.entity_name[:60], b.buyer_type or "", str(b.cash_purchases_count),
                      short_money(b.total_volume), f"{short_money(b.min_arv)}–{short_money(b.max_arv)}",
                      str(b.last_purchase_date or ""), ", ".join((b.target_municipalities or [])[:4]))
        console.print(t)


def cmd_stats(_: argparse.Namespace) -> None:
    init_db()
    with session_scope() as s:
        t = Table(box=box.SIMPLE, title="Database")
        t.add_column("table")
        t.add_column("rows", justify="right")
        for model in (Property, Lead, DistressSignal, Valuation, CashBuyer, AgentRun):
            t.add_row(model.__tablename__, f"{s.scalar(select(func.count()).select_from(model)):,}")
        console.print(t)
        sig = Table(box=box.SIMPLE, title="Active distress signals")
        sig.add_column("type")
        sig.add_column("count", justify="right")
        for typ, n in s.execute(select(DistressSignal.signal_type, func.count()).where(DistressSignal.active.is_(True))
                                .group_by(DistressSignal.signal_type).order_by(func.count().desc())):
            sig.add_row(typ, f"{n:,}")
        console.print(sig)
        c = Table(box=box.SIMPLE, title="Leads by county")
        c.add_column("county")
        c.add_column("leads", justify="right")
        for county, n in s.execute(select(Property.county, func.count(Lead.id)).join(Lead).group_by(Property.county)
                                   .order_by(func.count(Lead.id).desc())):
            c.add_row(county, f"{n:,}")
        console.print(c)


def cmd_underwrite(args: argparse.Namespace) -> None:
    from engine.underwriter import UnderwriterAgent
    from swarm.orchestrator import setup_logging
    setup_logging()
    run = UnderwriterAgent(top=args.top, force=args.force).execute()
    console.print(f"underwriter: {run.status} processed={run.records_processed} errors={run.error_count} "
                  f"verdicts={run.metrics_json.get('verdicts')}")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "swarm":
        from swarm.orchestrator import main as swarm_main
        swarm_main(argv[1:])
        return
    parser = argparse.ArgumentParser(description="NJ wholesale engine — operator CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    b = sub.add_parser("briefing")
    b.add_argument("--top", type=int, default=15)
    b.add_argument("--source", help="only leads from this primary source (e.g. sheriff_sale)")
    b.add_argument("--underwritten", action="store_true", help="only leads that have a valuation")
    l = sub.add_parser("lead")
    l.add_argument("id", type=int)
    u = sub.add_parser("underwrite")
    u.add_argument("--top", type=int, default=15)
    u.add_argument("--force", action="store_true")
    by = sub.add_parser("buyers")
    by.add_argument("--top", type=int, default=20)
    sub.add_parser("stats")
    args = parser.parse_args(argv)
    {"init": lambda a: (init_db(), console.print("[green]database ready[/green]")),
     "briefing": cmd_briefing, "lead": cmd_lead, "underwrite": cmd_underwrite, "buyers": cmd_buyers,
     "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
