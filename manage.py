#!/usr/bin/env python3
"""Operator CLI.

    python manage.py init                     create/upgrade the database
    python manage.py briefing [--top 15]      Daily Action Briefing: (A) ready to call (B) sheriff/estate spreads (C) stacked
    python manage.py lead <id>                everything about one lead
    python manage.py underwrite [--top 15]    comps / ARV / MAO for the top leads
    python manage.py buyers [--top 20]        active cash buyers
    python manage.py stats                    database and pipeline stats
    python manage.py server [--port 8000]     local CRM cockpit at http://127.0.0.1:8000
    python manage.py queue [--top 25]         today's call queue (follow-ups first)
    python manage.py script <lead> [--contact ID] [--buyer ID]   pre-call battle card (playbooks A–E)
    python manage.py call <lead> --outcome SPOKE_NEEDS_OFFER [--notes ..] [--occupancy VACANT] [--ask 350k] [--ai]
    python manage.py occupancy <lead> VACANT --source "seller on 10/27 call"     confirm + re-price
    python manage.py contract <lead> --price 250000 [--signed 2026-10-27]       record a signed contract
    python manage.py docs <lead> [--type loi|psa|assignment|jv|deal_sheet|all] [--price N] [--buyer ID]
    python manage.py skiptrace --export-top 50 | --import found.csv | --lead ID --paste file.txt
    python manage.py dnc --load ftc_201.txt [--date YYYY-MM-DD] | --add PHONE [--source daniels_law] | --status
    python manage.py match [--top 50]         top-5 cash buyers for each top deal
    python manage.py dnc --suppress-address "12 Main St, Newark NJ 07102" [--source daniels_law]   stop mail to it
    python manage.py pii-scan                 check git-tracked files for names from the database (run before pushing)
    python manage.py agent-blast [--top 50] [--send 10] | --mark-sent LEAD [--channel sms] | --opt-out CONTACT
    python manage.py mail-merge [--top 50] [--kinds pre_auction,estate,landlord] | --mark-mailed deals/mail_batches/<dir>
    python manage.py inspect <lead>           virtual inspection: Street View/satellite/FEMA links, condition flags
    python manage.py stack                    stack distress signals (listed foreclosures, portfolio owners)
    python manage.py buyer-hotzones [--top 10]   where cash buyers pay the most + our deals in those pockets
    python manage.py walkthrough <lead> [--invite BUYER] [--walked BUYER] [--when 2026-11-02T11:00]
    python manage.py bid <lead> [--buyer B --amount 470k --emd 5k] [--accept BID --assignee "ABC LLC"]
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

from core.text import format_phone
from db.database import init_db, session_scope
from db.models import AgentRun, CashBuyer, Contact, DistressSignal, Lead, Property, SignalType, Valuation

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


def buyer_label(b: CashBuyer) -> str:
    name = b.entity_name or ""
    return (name[8:] if name.upper().startswith("OWNER @ ") else name)[:28]


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
        from engine import compliance, crm
        top100 = s.scalars(select(Lead).where(Lead.status_stage.notin_(TERMINAL))
                           .order_by(Lead.priority_rank_score.desc()).limit(100)).all()
        ready = sum(1 for lead in top100 if crm.best_phone(s, lead))
        due = crm.due_followups(s)
        ftc = compliance.ftc_data_status(s)

        console.print(Panel.fit(
            f"[bold]Daily Action Briefing — {today:%A, %B %d, %Y}[/bold]\n"
            f"Open leads: [bold]{open_total:,}[/bold]  ({', '.join(f'{k} {v:,}' for k, v in by_source.most_common())})\n"
            f"Sheriff sales in next 14 days: [bold]{auctions_14}[/bold]   ·   Underwritten: {len(vals)} "
            f"({', '.join(f'{k} {v}' for k, v in verdicts.most_common())})   ·   Cash buyers on file: {buyers}\n"
            f"Contact-ready (top 100): [bold]{ready}%[/bold]   ·   Follow-ups due: [bold]{len(due)}[/bold]   ·   "
            f"National DNC data: " + (f"{ftc['list_date']} ({'fresh' if ftc['fresh'] else '[red]STALE — reload[/red]'})"
                                      if ftc["loaded"] else "[yellow]not loaded (manage.py dnc --load)[/yellow]") + "\n"
            "Data freshness: " + ", ".join(
                f"{n} {r.started_at:%m/%d %H:%M} ({r.status})" for n, r in sorted(last_runs.items())),
            title="NJ Wholesale Engine", border_style="cyan"))

        from engine import briefing as brief
        tally = brief.counts(s)
        console.print(f"[bold]Ready to call now:[/bold] {tally['ready_to_call']}   ·   Stacked distress: "
                      f"2x {tally['stacked_2x']}, 3x+ {tally['stacked_3x_plus']}   ·   Portfolio-owner signals: "
                      f"{tally['portfolio_owners']}")
        if not args.classic:
            print_briefing_sections(s, brief, args.top)
            if due:
                console.print(f"[bold]Follow-ups due now ({len(due)}):[/bold] " + ", ".join(
                    f"#{lead.id} {lead.property.street_address}" for lead in due[:12]))
            console.print(Panel(COMPLIANCE_REMINDER, title="Compliance reminder", border_style="yellow"))
            return

        table = Table(box=box.SIMPLE_HEAVY, show_lines=False, header_style="bold magenta",
                      title=f"Top {len(leads)} leads to work today", title_justify="left")
        for col, kw in [("#", {"justify": "right"}), ("Pri", {"justify": "right"}), ("M/V", {}),
                        ("Property", {"max_width": 34}), ("Owner / contact", {"max_width": 30}),
                        ("Why now", {"max_width": 44}), ("ARV", {"justify": "right"}), ("As-is", {"justify": "right"}),
                        ("Repairs", {"justify": "right"}), ("MAO 70/75", {"justify": "right"}),
                        ("Anchor", {"justify": "right"}), ("Fee", {"justify": "right"}), ("Debt", {"justify": "right"}),
                        ("Verdict / FRFPA", {"max_width": 24}), ("Contact", {"max_width": 18}),
                        ("Top buyers", {"max_width": 30})]:
            table.add_column(col, **kw)
        for i, lead in enumerate(leads, 1):
            p = lead.property
            v = p.current_valuation
            verdict_style = {"STRONG": "bold green", "WORKABLE": "green", "WORKABLE_82_PCT_FLOOR": "cyan", "TIGHT": "yellow",
                             "SHORT_SALE_ONLY": "red", "FRFPA_BLOCKED": "red", "EQUITY_RESCUE_REFERRAL": "magenta",
                             "PASS": "dim red"}.get(v.verdict if v else "", "dim")
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
                + (f"\n[dim]conf {v.confidence_score:.0f}[/dim]" if v else "")
                + (f"\n[dim]{lead.frfpa_track}[/dim]" if lead.frfpa_track else ""),
                crm.contact_status(s, lead) + (f"\n[dim]{lead.status_stage.lower()}[/dim]" if lead.status_stage != "NEW" else ""),
                "\n".join(f"{m.rank}. {buyer_label(m.buyer)}" for m in lead.buyer_matches[:3]) or "[dim]—[/dim]")
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
        if due:
            console.print(f"[bold]Follow-ups due now ({len(due)}):[/bold] " + ", ".join(
                f"#{lead.id} {lead.property.street_address}" for lead in due[:12]))
        console.print(Panel(COMPLIANCE_REMINDER, title="Compliance reminder", border_style="yellow"))


COMPLIANCE_REMINDER = (
    "Every call: identify yourself as a real estate investor (not an agent), say you may assign the contract to "
    "another investor, and encourage attorney review. Call 8am–9pm only; scrub numbers against the National DNC "
    "list before calling; stop immediately on any do-not-call request. Listing agents: you are an unrepresented "
    "buyer — never imply you will pay them.")


def pct(v: float | None) -> str:
    return f"{v:.0%}" if v is not None else "—"


def place(row: dict) -> str:
    badge = f" [bold yellow]{row['badge']}[/bold yellow]" if row.get("badge") else ""
    return f"{row['address'] or '?'}{badge}\n[dim]{row['town'] or ''} {row['zip'] or ''} · {row['county'] or ''}[/dim]"


def reach_text(row: dict) -> str:
    who = f"{row['who'] or ''} [dim]({(row['role'] or '').lower().replace('_', ' ')})[/dim]" if row.get("who") else ""
    line = format_phone(row["phone"]) if row.get("phone") else (row.get("email") or "[dim]no contact[/dim]")
    return f"{who}\n{line}".strip()


def print_briefing_sections(s, brief, top: int) -> None:
    a = brief.ready_rows(s, top)
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold green", title_justify="left",
              title=f"(A) Ready to call now — top {len(a)} underwritten deals with a live phone / agent e-mail")
    for col, kw in [("#", {"justify": "right"}), ("Lead", {"justify": "right"}), ("Pri", {"justify": "right"}),
                    ("Property", {"max_width": 40}), ("Call / e-mail", {"max_width": 34}), ("Ask", {"justify": "right"}),
                    ("MAO 70/75", {"justify": "right"}), ("75% ÷ ask", {"justify": "right"}),
                    ("Gap to ask", {"justify": "right"}), ("Verdict", {}), ("Why", {"max_width": 40})]:
        t.add_column(col, **kw)
    for i, r in enumerate(a, 1):
        ask = r.get("ask") or r.get("upset")
        gap = (ask - r["mao_70"]) if ask and r.get("mao_70") else None
        t.add_row(str(i), str(r["lead_id"]), f"{r['priority']:.0f}", place(r), reach_text(r),
                  short_money(r.get("ask")) if r.get("ask") else (f"[dim]upset[/dim] {short_money(r['upset'])}"
                                                                  if r.get("upset") else "—"),
                  f"{short_money(r['mao_70'])}/{short_money(r['mao_75'])}", pct(r.get("list_to_mao_75")),
                  short_money(gap) if gap is not None else "—",
                  (r["verdict"] or "") + ("\n[cyan]striking distance[/cyan]" if r.get("striking_distance") else ""),
                  (r.get("via") or "") + (f" · sale {r['sale_date']:%b %d}" if r.get("sale_date") else ""))
    console.print(t)
    if not a:
        console.print("[yellow]No ready-to-call deals: run the swarm (underwrite → lead_scoring) or add phones via "
                      "manage.py skiptrace.[/yellow]")
    b = brief.high_spread_rows(s, 10)
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold magenta", title_justify="left",
              title=f"(B) Top {len(b)} high-spread sheriff-sale / estate deals")
    for col, kw in [("Lead", {"justify": "right"}), ("Property", {"max_width": 40}), ("Kind", {}), ("Sale", {}),
                    ("Upset", {"justify": "right"}), ("ARV", {"justify": "right"}), ("MAO 70", {"justify": "right"}),
                    ("Spread", {"justify": "right"}), ("Verdict / FRFPA", {"max_width": 28}),
                    ("Contact", {"max_width": 30})]:
        t.add_column(col, **kw)
    for r in b:
        t.add_row(str(r["lead_id"]), place(r), r["kind"], f"{r['sale_date']:%b %d}" if r.get("sale_date") else "—",
                  short_money(r.get("upset")), short_money(r["arv"]), short_money(r["mao_70"]), short_money(r["spread"]),
                  f"{r['verdict']}" + (f"\n[dim]{r['frfpa_track']}[/dim]" if r.get("frfpa_track") else ""), reach_text(r))
    console.print(t)
    c = brief.stacked_rows(s, 10)
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold yellow", title_justify="left",
              title=f"(C) Top {len(c)} stacked-distress / portfolio-owner leads")
    for col, kw in [("Lead", {"justify": "right"}), ("Pri", {"justify": "right"}), ("Property", {"max_width": 40}),
                    ("Signals", {"max_width": 34}), ("Portfolio", {"justify": "right"}), ("Verdict", {}),
                    ("MAO 70", {"justify": "right"}), ("Contact", {"max_width": 30})]:
        t.add_column(col, **kw)
    for r in c:
        t.add_row(str(r["lead_id"]), f"{r['priority']:.0f}", place(r), " + ".join(r["families"]),
                  f"{r['portfolio_size']} props" if r.get("portfolio_size") else "—", r["verdict"] or "[dim]—[/dim]",
                  short_money(r["mao_70"]), reach_text(r))
    console.print(t)


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
    run = UnderwriterAgent(top=args.top, force=args.force, lead_ids=args.lead or None).execute()
    console.print(f"underwriter: {run.status} processed={run.records_processed} errors={run.error_count} "
                  f"verdicts={run.metrics_json.get('verdicts')}")


def _lead(s, lead_id: int) -> Lead:
    lead = s.get(Lead, lead_id)
    if lead is None:
        console.print(f"[red]lead {lead_id} not found[/red]")
        raise SystemExit(2)
    return lead


def cmd_server(args: argparse.Namespace) -> None:
    import uvicorn
    init_db()
    console.print(f"[green]Cockpit:[/green] http://127.0.0.1:{args.port}  (local only — Ctrl+C to stop)")
    uvicorn.run("web.app:app", host="127.0.0.1", port=args.port, log_level="warning")


def cmd_queue(args: argparse.Namespace) -> None:
    from engine import compliance, crm
    init_db()
    with session_scope() as s:
        if not compliance.within_calling_hours():
            console.print("[yellow]Outside 8am–9pm Eastern: prepare, don't dial.[/yellow]")
        t = Table(box=box.SIMPLE_HEAVY, title="Call queue")
        for col in ("#", "lead", "property", "why", "call", "phone", "verdict"):
            t.add_column(col)
        for i, item in enumerate(crm.call_queue(s, args.top), 1):
            c, p, v = item.contact, item.lead.property, item.lead.property.current_valuation
            t.add_row(str(i), str(item.lead.id), f"{p.street_address}, {p.city or p.municipality}", item.reason,
                      f"{c.name} ({c.contact_type.lower()})" if c else "—",
                      f"{format_phone(c.phone) if c and c.phone else ''} [dim]{c.dnc_status if c else ''}[/dim]",
                      v.verdict if v else "—")
        console.print(t)


def cmd_script(args: argparse.Namespace) -> None:
    from engine.scripts import build_battle_card, build_buyer_pitch, render_markdown
    init_db()
    with session_scope() as s:
        lead = _lead(s, args.id)
        if args.buyer:
            buyer = s.get(CashBuyer, args.buyer)
            card = build_buyer_pitch(buyer, lead)
        else:
            contact = s.get(Contact, args.contact) if args.contact else None
            card = build_battle_card(lead, contact)
        console.print(render_markdown(card), markup=False, highlight=False)


def cmd_call(args: argparse.Namespace) -> None:
    from engine import crm
    init_db()
    with session_scope() as s:
        lead = _lead(s, args.id)
        contact = s.get(Contact, args.contact) if args.contact else None
        debrief = {"asking_price": args.ask, "occupancy": args.occupancy, "motivation": args.motivation,
                   "timeline": args.timeline}
        r = crm.log_call(s, lead, args.outcome.upper(), notes=args.notes or "", contact=contact,
                         occupancy=args.occupancy, debrief={k: v for k, v in debrief.items() if v}, use_ai=args.ai)
        console.print(f"[green]logged[/green] stage={r.stage} priority={r.priority} next={r.next_followup_at or '—'}"
                      + (" [red]added to DNC[/red]" if r.dnc_added else ""))
        for n in r.notes:
            console.print(f"  {n}")
        c = r.coach
        console.print(f"[bold]Next:[/bold] {c['next_action']}")
        for label, items in (("Compliance", c["compliance_flags"]), ("Still missing", c["missing_info"]),
                             ("Coaching", c["tips"])):
            for x in items:
                console.print(f"  [{'yellow' if label == 'Compliance' else 'dim'}]{label}:[/] {x}")
        if c.get("ai"):
            console.print(Panel(f"{c['ai']['read_on_seller']}\n- " + "\n- ".join(c["ai"]["next_call_plan"]),
                                title="AI coach"))
        if r.suggested_docs:
            console.print(f"Suggested: python manage.py docs {lead.id} --type {r.suggested_docs[0]}")


def cmd_occupancy(args: argparse.Namespace) -> None:
    from engine import crm
    init_db()
    with session_scope() as s:
        for n in crm.confirm_occupancy(s, _lead(s, args.id), args.status.upper(), args.source):
            console.print(n)


def cmd_contract(args: argparse.Namespace) -> None:
    from engine import crm
    init_db()
    with session_scope() as s:
        signed = date.fromisoformat(args.signed) if args.signed else None
        for n in crm.record_contract(s, _lead(s, args.id), args.price, signed):
            console.print(n)


def cmd_docs(args: argparse.Namespace) -> None:
    from engine.contracts import DOC_TYPES, ContractBlocked, generate
    init_db()
    with session_scope() as s:
        lead = _lead(s, args.id)
        buyer = s.get(CashBuyer, args.buyer) if args.buyer else (lead.buyer_matches[0].buyer if lead.buyer_matches else None)
        for t in (DOC_TYPES if args.type == "all" else [args.type]):
            try:
                g = generate(s, lead, t, price=args.price, buyer=buyer, partner=args.partner)
            except ContractBlocked as exc:
                console.print(f"[red]{t} blocked:[/red] " + " ".join(exc.reasons))
                continue
            console.print(f"[green]{t}[/green] {money(g.price)} → " + ", ".join(str(x) for x in g.paths))
            for w in g.warnings:
                console.print(f"  [yellow]{w}[/yellow]")


def cmd_skiptrace(args: argparse.Namespace) -> None:
    from pathlib import Path
    from agents.enrichment import skiptrace_pipeline as st
    init_db()
    with session_scope() as s:
        if args.export_top:
            csv_path, html_path, n = st.export_queue(s, args.export_top)
            console.print(f"[green]{n} leads[/green] → {csv_path}\n  open {html_path} and click the search links; "
                          "fill phone/email columns, then: python manage.py skiptrace --import <csv>")
        elif args.import_csv:
            leads, contacts = st.import_csv(s, Path(args.import_csv))
            console.print(f"imported {contacts} contact(s) on {leads} lead(s)")
        elif args.lead:
            lead = _lead(s, args.lead)
            text = sys.stdin.read() if args.paste in (None, "-") else Path(args.paste).read_text()
            parsed = st.import_text(s, lead, text, name=args.name, role=args.role)
            console.print(f"phones {parsed['phones']} emails {parsed['emails']} relatives {parsed['relatives'][:5]} "
                          f"→ {parsed['contacts_added']} contact(s) added")
        else:
            console.print("use --export-top N, --import CSV, or --lead ID [--paste FILE]")


def cmd_dnc(args: argparse.Namespace) -> None:
    from pathlib import Path
    from engine import compliance
    init_db()
    with session_scope() as s:
        if args.load:
            n = compliance.load_ftc_file(s, Path(args.load), date.fromisoformat(args.date) if args.date else None)
            console.print(f"loaded {n:,} registry numbers from {args.load}")
        if args.add:
            row = compliance.add_internal(s, args.add, source=args.source, notes=args.notes)
            console.print(f"added {row.phone} ({row.source})" if row else "[red]not a valid phone number[/red]")
        if args.suppress_address:
            row = compliance.suppress_address(s, args.suppress_address, source=args.source, notes=args.notes)
            console.print(f"suppressed mailing address ({row.source})" if row else "[red]not a usable address[/red]")
        if args.status or not (args.load or args.add or args.suppress_address):
            st = compliance.ftc_data_status(s)
            console.print(f"FTC data: {st}  sources: " + ", ".join(
                f"{src} {n:,}" for src, n in s.execute(select(compliance.DncEntry.source, func.count())
                                                     .group_by(compliance.DncEntry.source))))
            console.print("Free area codes that cover the most of our contacts: " + ", ".join(
                f"{a} ({n})" for a, n in compliance.recommended_free_area_codes(s)))
            if st["loaded"]:
                console.print(f"Re-download before {compliance.stale_after(date.fromisoformat(st['list_date']))} (31-day rule).")


def cmd_pii_scan(_: argparse.Namespace) -> None:
    from rich.markup import escape

    from core.pii_scan import scan
    init_db()
    with session_scope() as s:
        hits = scan(s)
    for h in hits:
        console.print(f"[red]{escape(h.path)}:{h.line}[/red] {escape(h.name)}: {escape(h.text)}")
    console.print(f"{len(hits)} personal name(s) from the database found in tracked files.")
    raise SystemExit(1 if hits else 0)


def cmd_match(args: argparse.Namespace) -> None:
    from engine.buyer_matcher import BuyerMatcherAgent
    run = BuyerMatcherAgent(top=args.top).execute()
    console.print(f"buyer_matcher: {run.status} {run.metrics_json}")


def cmd_agent_blast(args: argparse.Namespace) -> None:
    from pathlib import Path

    from engine import agent_outreach as ao
    init_db()
    with session_scope() as s:
        if args.opt_out:
            contact = s.get(Contact, args.opt_out)
            if contact is None:
                console.print(f"[red]contact {args.opt_out} not found[/red]")
                return
            ao.opt_out(s, contact)
            console.print(f"{contact.name}: opted out — phone on the internal DNC list, no more offers.")
            return
        if args.mark_sent:
            lead = s.get(Lead, args.mark_sent)
            if lead is None:
                console.print(f"[red]lead {args.mark_sent} not found[/red]")
                return
            packet = ao.build_packet(lead, session=s)
            stage = ao.record_sent(s, lead, packet, args.channel)
            console.print(f"Lead #{lead.id}: {packet.kind} logged via {args.channel}; stage {stage}; follow-up call "
                          f"{ao.followup_time():%a %b %d %H:%M}.")
            return
        index, packets = ao.write_batch(s, args.top, Path(args.out) if args.out else None)
        t = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", title_justify="left",
                  title=f"{len(packets)} listing-agent packets (review before sending)")
        for col in ("Lead", "Listing", "DOM", "Ask", "Anchor / best", "75% ÷ ask", "Agent", "Reach", "Kind"):
            t.add_column(col)
        for p in packets:
            t.add_row(str(p.lead_id), p.address[:38], str(p.dom or "?"), short_money(p.list_price),
                      f"{short_money(p.anchor)} / {short_money(p.best)}", pct(p.list_to_mao_75),
                      f"{p.agent_name[:22]}\n[dim]{(p.brokerage or '')[:26]}[/dim]",
                      (p.agent_email or "") + ("\n" + format_phone(p.agent_phone) if p.agent_phone else ""), p.kind)
        console.print(t)
        console.print(f"Review page: {index}")
        if args.send:
            ok, why = ao.smtp_ready()
            if not ok:
                console.print(f"[yellow]SMTP not configured ({why}); use the mailto links / copy buttons, then "
                              "--mark-sent LEAD.[/yellow]")
                return
            sent = 0
            for p in [p for p in packets if p.agent_email][: args.send]:
                lead = s.get(Lead, p.lead_id)
                try:
                    ao.send_smtp(s, lead, p)
                    sent += 1
                    console.print(f"  sent → {p.agent_email} (lead #{p.lead_id})")
                except RuntimeError as exc:
                    console.print(f"[yellow]  stopped: {exc}[/yellow]")
                    break
            console.print(f"{sent} e-mail(s) sent; {ao.sent_last_24h(s)} in the last 24 hours.")


def cmd_mail_merge(args: argparse.Namespace) -> None:
    from pathlib import Path

    from engine import direct_mail as dm
    init_db()
    with session_scope() as s:
        if args.mark_mailed:
            n = dm.mark_mailed(s, Path(args.mark_mailed))
            console.print(f"{n} letters logged as mailed.")
            return
        kinds = set(args.kinds.split(",")) if args.kinds else None
        out, letters = dm.write_batch(s, args.top, Path(args.out) if args.out else None, kinds=kinds,
                                      all_verdicts=args.all_verdicts, include_callable=args.include_callable)
        counts = Counter(lt.kind for lt in letters)
        console.print(f"{len(letters)} letters → {out}  ({', '.join(f'{k} {v}' for k, v in counts.items())})")
        if not dm.operator_ready():
            console.print("[yellow]PROOF batch: set operator.name, phone and mailing_address in config/settings.yaml "
                          "before mailing (the PDFs are watermarked).[/yellow]")
        console.print("Upload <kind>.pdf + mailing_list.csv to Click2Mail (or print), then: "
                      f"python manage.py mail-merge --mark-mailed {out}")


def cmd_inspect(args: argparse.Namespace) -> None:
    from engine.virtual_inspector import build_dossier
    init_db()
    with session_scope() as s:
        lead = s.get(Lead, args.id)
        if lead is None:
            console.print(f"[red]lead {args.id} not found[/red]")
            return
        d = build_dossier(s, lead)
        console.print(Panel.fit("\n".join(f"[bold]{k}[/bold]: {v}" for k, v in d.links.items()),
                                title=f"Virtual inspection — {d.address}"))
        for f in d.flags:
            console.print(f"  [yellow]{f['flag']}[/yellow] ({f['evidence']}): {f['advice']}")
        if d.occupancy_hint:
            console.print(f"  Occupancy hint: {d.occupancy_hint}")
        console.print("[bold]Remote due-diligence checklist[/bold]")
        for item in d.checklist:
            console.print(f"  ☐ {item}")
        for k, v in d.requests.items():
            console.print(Panel(v, title=f"Photo / video request — {k}"))
        for n in d.notes:
            console.print(f"[dim]{n}[/dim]")


def cmd_stack(args: argparse.Namespace) -> None:
    from agents.enrichment.signal_stacker import SignalStackerAgent
    run = SignalStackerAgent(no_agents=args.no_agents).execute()
    console.print(f"signal_stacker: {run.status} {run.metrics_json}")
    for n in (run.self_improvement_notes or "").splitlines():
        console.print(f"  {n}")


def cmd_buyer_hotzones(args: argparse.Namespace) -> None:
    from engine.disposition import buyer_hotzones
    init_db()
    with session_scope() as s:
        zones = buyer_hotzones(s, args.top, include_commercial=args.include_commercial)
        for i, z in enumerate(zones, 1):
            console.print(f"[bold cyan]{i:>2}. {z.label}[/bold cyan]  [dim]score {z.score:,.0f}[/dim]")
            for r in z.reasons:
                console.print(f"     • {r}")
            console.print("     Buyers: " + "; ".join(f"#{b['buyer_id']} {b['name']} ({b['purchases']})"
                                                     for b in z.top_buyers))
            for h in z.leads:
                console.print(f"     → lead #{h['lead_id']} {h['address']} · buyer price {short_money(h['buyer_price'])} "
                              f"· {h['verdict']} · headroom {short_money(h['headroom'])}"
                              + (" · [green]under contract[/green]" if h["under_contract"] else ""))
        if not zones:
            console.print("[yellow]No buyer purchases on file — import county deed exports (owner_records).[/yellow]")


def _parse_when(text: str | None):
    from datetime import datetime
    return datetime.fromisoformat(text) if text else None


def cmd_walkthrough(args: argparse.Namespace) -> None:
    from engine import disposition as dp
    init_db()
    with session_scope() as s:
        lead = s.get(Lead, args.id)
        if lead is None:
            console.print(f"[red]lead {args.id} not found[/red]")
            return
        try:
            for ref in args.invite or []:
                buyer = dp.find_buyer(s, ref)
                if buyer is None:
                    console.print(f"[red]buyer {ref!r} not found[/red]")
                    continue
                bid = dp.invite(s, lead, buyer, _parse_when(args.when))
                console.print(f"invited {dp.buyer_label(buyer)} — status {bid.status}"
                              + (f" at {bid.walkthrough_date:%a %b %d %H:%M}" if bid.walkthrough_date else ""))
            for ref in args.walked or []:
                buyer = dp.find_buyer(s, ref)
                bid = next((b for b in dp.bids_for(s, lead) if buyer and b.buyer_id == buyer.id), None)
                if bid is None:
                    console.print(f"[red]no invitation for {ref!r} on this lead[/red]")
                    continue
                dp.mark_walked(s, bid, _parse_when(args.when))
                console.print(f"{dp.buyer_label(buyer)} walked it — send: {dp.post_walk_followup(lead, buyer)}")
        except dp.DispositionBlocked as exc:
            console.print(f"[red]Blocked:[/red] {'; '.join(exc.reasons)}")
            return
        plan = dp.walkthrough_plan(lead, dp.matched_buyers(s, lead))
        console.print(Panel.fit(
            f"Status: [bold]{plan['status']}[/bold]" + (f" · window {plan['opens']} → {plan['closes']}" if plan.get("opens") else "")
            + f"\nAccess: {plan['access']}\n" + (f"[yellow]{'; '.join(plan['blocked'])}[/yellow]\n" if plan["blocked"] else "")
            + (f"Proposed slots: {', '.join(f'{x:%a %b %d %H:%M}' for x in plan['slots'])}\n" if plan["slots"] else "")
            + (plan.get("tip") or ""), title=f"Remote walkthrough — {plan['address']}"))
        if plan["blocked"]:
            return
        console.print(Panel(plan["access_request"], title="Access request (agent / seller)"))
        for inv in plan["invites"]:
            console.print(Panel(f"{inv['sms']}\n\n[dim]{inv['email']}[/dim]",
                                title=f"Invite → #{inv['buyer_id']} {inv['buyer']} {format_phone(inv['phone']) if inv['phone'] else ''}"))
        for step in plan["steps"]:
            console.print(step)


def cmd_bid(args: argparse.Namespace) -> None:
    from engine import disposition as dp
    init_db()
    with session_scope() as s:
        lead = s.get(Lead, args.id)
        if lead is None:
            console.print(f"[red]lead {args.id} not found[/red]")
            return
        try:
            if args.buyer and args.amount:
                buyer = dp.find_buyer(s, args.buyer)
                if buyer is None:
                    console.print(f"[red]buyer {args.buyer!r} not found[/red]")
                    return
                bid, warnings = dp.record_bid(s, lead, buyer, money_arg(args.amount),
                                              money_arg(args.emd) if args.emd else None, notes=args.notes or "")
                console.print(f"bid #{bid.id}: {dp.buyer_label(buyer)} {money(bid.bid_amount)}")
                for w in warnings:
                    console.print(f"  [yellow]{w}[/yellow]")
            if args.accept:
                from db.models import BuyerBid
                bid = s.get(BuyerBid, args.accept)
                if bid is None or bid.lead_id != lead.id:
                    console.print(f"[red]bid {args.accept} is not on lead {lead.id}[/red]")
                    return
                doc, notes = dp.accept_bid(s, bid, args.structure, args.split, assignee_name=args.assignee)
                for n in notes:
                    console.print(n)
                console.print("Generated: " + ", ".join(str(p) for p in doc.paths))
        except dp.DispositionBlocked as exc:
            console.print(f"[red]Blocked:[/red] {'; '.join(exc.reasons)}")
            return
        t = Table(box=box.SIMPLE, title=f"Bids on lead #{lead.id} (contract {money(lead.offer_amount)})")
        for col in ("Bid", "Buyer", "Status", "Walkthrough", "Amount", "Fee", "Deposit", "Notes"):
            t.add_column(col)
        best = dp.best_bid(s, lead)
        for b in dp.bids_for(s, lead):
            fee = (b.bid_amount - lead.offer_amount) if b.bid_amount and lead.offer_amount else None
            t.add_row(f"{b.id}{' ★' if best and b.id == best.id else ''}", dp.buyer_label(b.buyer), b.status,
                      f"{b.walkthrough_date:%b %d %H:%M}" if b.walkthrough_date else "—", money(b.bid_amount),
                      money(fee), money(b.emd_amount), (b.notes or "")[:40])
        console.print(t)


def money_arg(text: str) -> float:
    t = text.strip().lower().replace("$", "").replace(",", "")
    mult = 1_000 if t.endswith("k") else 1_000_000 if t.endswith("m") else 1
    return float(t.rstrip("km")) * mult


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
    b.add_argument("--classic", action="store_true", help="the single top-N table instead of sections A/B/C")
    l = sub.add_parser("lead")
    l.add_argument("id", type=int)
    u = sub.add_parser("underwrite")
    u.add_argument("--top", type=int, default=15)
    u.add_argument("--force", action="store_true")
    u.add_argument("--lead", type=int, action="append", help="underwrite this lead id (repeatable)")
    by = sub.add_parser("buyers")
    by.add_argument("--top", type=int, default=20)
    sub.add_parser("stats")
    sv = sub.add_parser("server")
    sv.add_argument("--port", type=int, default=8000)
    qu = sub.add_parser("queue")
    qu.add_argument("--top", type=int, default=25)
    sc = sub.add_parser("script")
    sc.add_argument("id", type=int)
    sc.add_argument("--contact", type=int)
    sc.add_argument("--buyer", type=int, help="playbook E: pitch this cash buyer")
    ca = sub.add_parser("call")
    ca.add_argument("id", type=int)
    ca.add_argument("--outcome", required=True, help="NO_ANSWER LEFT_VM WRONG_NUMBER SPOKE_WARM_FOLLOWUP "
                    "SPOKE_NEEDS_OFFER SPOKE_NOT_INTERESTED AGENT_WILL_PRESENT_LOI OFFER_ACCEPTED DEAD DNC")
    ca.add_argument("--notes")
    ca.add_argument("--contact", type=int)
    ca.add_argument("--occupancy", help="OWNER_OCCUPIED HEIR_OCCUPIED TENANT_OCCUPIED VACANT")
    ca.add_argument("--ask", help="their number, e.g. 350k")
    ca.add_argument("--motivation")
    ca.add_argument("--timeline")
    ca.add_argument("--ai", action="store_true", help="add a Claude coaching note (needs the [ai] extra + key)")
    oc = sub.add_parser("occupancy")
    oc.add_argument("id", type=int)
    oc.add_argument("status")
    oc.add_argument("--source", required=True)
    ct = sub.add_parser("contract")
    ct.add_argument("id", type=int)
    ct.add_argument("--price", type=float, required=True)
    ct.add_argument("--signed", help="YYYY-MM-DD the fully signed contract reached both parties")
    dc = sub.add_parser("docs")
    dc.add_argument("id", type=int)
    dc.add_argument("--type", default="all", choices=["all", "loi", "psa", "assignment", "jv", "deal_sheet"])
    dc.add_argument("--price", type=float)
    dc.add_argument("--buyer", type=int)
    dc.add_argument("--partner")
    sk = sub.add_parser("skiptrace")
    sk.add_argument("--export-top", type=int)
    sk.add_argument("--import", dest="import_csv")
    sk.add_argument("--lead", type=int)
    sk.add_argument("--paste", help="file with the pasted people-search text ('-' or omitted = stdin)")
    sk.add_argument("--name")
    sk.add_argument("--role")
    dn = sub.add_parser("dnc")
    dn.add_argument("--load", help="FTC registry download (one number per line)")
    dn.add_argument("--date", help="list date of the download (default: file date)")
    dn.add_argument("--add", help="phone to suppress")
    dn.add_argument("--source", default="internal", choices=["internal", "daniels_law", "litigator"])
    dn.add_argument("--notes")
    dn.add_argument("--status", action="store_true")
    dn.add_argument("--suppress-address", help="stop mailing/sharing this address (Daniel's Law or a stop request)")
    sub.add_parser("pii-scan")
    mt = sub.add_parser("match")
    mt.add_argument("--top", type=int, default=50)
    ab = sub.add_parser("agent-blast", help="B2B listing-agent LOI packets (e-mail + SMS + 1-page LOI)")
    ab.add_argument("--top", type=int, default=50)
    ab.add_argument("--out", help="output folder (default deals/agent_blast/<date>)")
    ab.add_argument("--send", type=int, default=0, help="send up to N e-mails over SMTP (daily limit applies)")
    ab.add_argument("--mark-sent", type=int, metavar="LEAD", help="log a packet you sent by hand")
    ab.add_argument("--channel", default="email", choices=["email", "sms", "mailto", "phone"])
    ab.add_argument("--opt-out", type=int, metavar="CONTACT", help="agent asked to stop: suppress them")
    mm = sub.add_parser("mail-merge", help="pre-auction / estate / landlord letters (PDF + Click2Mail CSV)")
    mm.add_argument("--top", type=int, default=50)
    mm.add_argument("--out")
    mm.add_argument("--kinds", help="comma list: pre_auction,estate,landlord")
    mm.add_argument("--all-verdicts", action="store_true", help="include leads without an offer verdict")
    mm.add_argument("--include-callable", action="store_true", help="also mail owners we can already call")
    mm.add_argument("--mark-mailed", metavar="DIR", help="log a batch as mailed")
    ins = sub.add_parser("inspect", help="virtual inspection dossier (Street View, flags, photo request)")
    ins.add_argument("id", type=int)
    st = sub.add_parser("stack", help="run the signal stacker (listed distress, portfolio owners)")
    st.add_argument("--no-agents", action="store_true")
    hz = sub.add_parser("buyer-hotzones", help="micro-markets where cash buyers pay the most + our deals there")
    hz.add_argument("--top", type=int, default=10)
    hz.add_argument("--include-commercial", action="store_true")
    wt = sub.add_parser("walkthrough", help="remote buyer walkthrough plan, invites and scripts")
    wt.add_argument("id", type=int)
    wt.add_argument("--invite", action="append", metavar="BUYER", help="buyer id or name (repeatable)")
    wt.add_argument("--walked", action="append", metavar="BUYER")
    wt.add_argument("--when", help="YYYY-MM-DDTHH:MM walkthrough time")
    bd = sub.add_parser("bid", help="record / list / accept buyer bids")
    bd.add_argument("id", type=int)
    bd.add_argument("--buyer", help="buyer id or name")
    bd.add_argument("--amount", help="buyer's total price, e.g. 470k")
    bd.add_argument("--emd", help="deposit offered, e.g. 5k")
    bd.add_argument("--notes")
    bd.add_argument("--accept", type=int, metavar="BID_ID")
    bd.add_argument("--structure", default="assignment", choices=["assignment", "jv"])
    bd.add_argument("--split", type=float, help="JV partner share in percent")
    bd.add_argument("--assignee", help="legal name/entity of the assignee (required for unnamed buyers)")
    args = parser.parse_args(argv)
    {"init": lambda a: (init_db(), console.print("[green]database ready[/green]")),
     "briefing": cmd_briefing, "lead": cmd_lead, "underwrite": cmd_underwrite, "buyers": cmd_buyers,
     "stats": cmd_stats, "server": cmd_server, "queue": cmd_queue, "script": cmd_script, "call": cmd_call,
     "occupancy": cmd_occupancy, "contract": cmd_contract, "docs": cmd_docs, "skiptrace": cmd_skiptrace,
     "dnc": cmd_dnc, "match": cmd_match, "pii-scan": cmd_pii_scan, "agent-blast": cmd_agent_blast,
     "mail-merge": cmd_mail_merge, "inspect": cmd_inspect, "stack": cmd_stack, "buyer-hotzones": cmd_buyer_hotzones,
     "walkthrough": cmd_walkthrough, "bid": cmd_bid}[args.cmd](args)


if __name__ == "__main__":
    main()
