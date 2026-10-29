#!/usr/bin/env python3
"""Operator CLI.

    python manage.py init                     create/upgrade the database
    python manage.py today                    START HERE: the 2-hour daily checklist from live records
    python manage.py briefing [--top 15]      Daily Action Briefing: (A) ready to call (B) sheriff/estate spreads (C) stacked
    python manage.py lead <id>                everything about one lead
    python manage.py underwrite [--top 15]    comps / ARV / MAO for the top leads
    python manage.py calibrate                local investor pricing (alpha) from recorded buyer purchases
    python manage.py recalibrate              re-price every valuation (quant model) + striking-distance report
    python manage.py vault [--seed 30]        JV partner & proof-of-funds vault (Phase 0: 3 funding partners)
    python manage.py skiptrace-fast           fast skip-trace queue; --lead <id> --paste page.txt saves a copied page
    python manage.py reply <id> --text '...'  agent reply → traffic light, math, 3-sentence answer
    python manage.py structure <id>           assignment vs double close net check
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
    python manage.py fast-track [--packets] [--script LEAD] [--portfolios 20] [--portfolio-letters 20]
    python manage.py closing [<lead> [--done EMD_DEPOSITED] [--title-email] [--close --fee 22.5k] [--disapproved]]
    python manage.py finance [--add-income 22.5k --lead ID | --add-expense 120 --category mail --desc ...]
    python manage.py partners [--county Essex]
    python manage.py inbound [--ingest submissions.json]   website seller leads / buyer registrations
    python manage.py site --build site_dist [--endpoint https://formspree.io/f/...]   static funnel for free hosting
    python manage.py swarm <args...>          passthrough to `python -m swarm.orchestrator`
"""
from __future__ import annotations

import argparse
from pathlib import Path
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
from db.models import AgentRun, CashBuyer, Contact, DistressSignal, Lead, Property, SignalType, Valuation, JVPartner

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


def print_listed_foreclosures(rows: list[dict], title: str) -> None:
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold red", title_justify="left", title=title)
    for col, kw in [("Lead", {"justify": "right"}), ("Property", {"max_width": 34}), ("Sale", {}),
                    ("Upset", {"justify": "right"}), ("Ask", {"justify": "right"}), ("DOM", {"justify": "right"}),
                    ("Track", {"max_width": 26}), ("Offer", {"justify": "right"}), ("Listing agent", {"max_width": 30})]:
        t.add_column(col, **kw)
    for r in rows:
        days = r.get("days_to_sale")
        sale = (f"{r['sale_date'][5:]} [bold]{'TODAY' if days == 0 else str(days) + 'd'}[/bold]"
                if r.get("sale_date") else "—")
        agent = r.get("agent") or {}
        t.add_row(str(r["lead_id"]), f"{r['address']}\n[dim]{r['county']}[/dim]", sale, short_money(r.get("upset")),
                  short_money(r.get("list_price")), str(r.get("dom") or "?"), r["track_label"],
                  f"{short_money(r['anchor'])}/{short_money(r['best'])}" if r.get("anchor") else "—",
                  f"{agent.get('name') or ''}\n{agent.get('phone_display') or agent.get('email') or '[dim]no contact[/dim]'}")
    console.print(t)


def print_briefing_sections(s, brief, top: int) -> None:
    lf = brief.listed_rows(s, 10)
    if lf:
        print_listed_foreclosures(lf, f"(0) Listed foreclosures — sheriff sale + MLS listing, call the agent first "
                                      f"({len(lf)} shown)")
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


def cmd_install_shortcut(args: argparse.Namespace) -> None:
    from scripts.install_desktop_shortcut import main as install_main
    install_main(["--desktop", args.desktop] if args.desktop else [])


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
        for col in ("Lead", "Listing", "DOM", "Ask", "Sale / upset", "Anchor / best", "75% ÷ ask", "Agent", "Reach",
                    "Kind"):
            t.add_column(col)
        for p in packets:
            t.add_row(str(p.lead_id), p.address[:38], str(p.dom or "?"), short_money(p.list_price),
                      f"{p.sale_date[5:]} ({p.days_to_sale}d)\n{short_money(p.upset)}" if p.sale_date else "—",
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


def cmd_fast_track(args: argparse.Namespace) -> None:
    from pathlib import Path

    from engine import fast_track as ft
    init_db()
    with session_scope() as s:
        if args.script:
            lead = s.get(Lead, args.script)
            if lead is None or not ft.is_listed_foreclosure(lead.property):
                console.print(f"[red]lead {args.script} is not a listed foreclosure[/red]")
                return
            lf = ft.listed_foreclosure(s, lead)
            sc = ft.agent_script(lf)
            console.print(Panel(sc["opening"], title=f"Listing-agent script — {lf.address} ({ft.TRACK_LABEL[lf.track]})"))
            for q in sc["questions"]:
                console.print(f"  ? {q}")
            console.print(Panel(sc["offer"], title="Offer line", border_style="green"))
            console.print(f"[bold]Close:[/bold] {sc['close']}\n[dim]{sc['disclosure']}[/dim]")
            for n in sc["never_say"]:
                console.print(f"  [red]never:[/red] {n}")
            return
        rows = [lf.to_dict() for lf in ft.listed_foreclosures(s)]
        rows = rows[: args.top]
        print_listed_foreclosures(rows, f"Listed foreclosures — {len(rows)} (sheriff sale + MLS listing)")
        for r in rows:
            if r["reasons"]:
                console.print(f"  #{r['lead_id']} {r['track_label']}: {r['reasons'][0]}")
        if args.packets:
            made = 0
            for r in rows:
                if r["agent"] and r["track"] in ("CASH_OFFER", "FRFPA_FLOOR_OFFER", "CONFIRM_OCCUPANCY", "SHORT_SALE"):
                    pk = ft.build_packet(s, s.get(Lead, r["lead_id"]))
                    made += 1
                    console.print(f"  packet #{r['lead_id']} ({pk.kind}): {pk.subject}"
                                  + (f" → {pk.loi_files.get('pdf')}" if pk.loi_files else ""))
            console.print(f"{made} urgent agent packets ready — send from the cockpit (Fast Track) or "
                          "python manage.py agent-blast --mark-sent <lead>.")
        if args.portfolios:
            for p in ft.portfolios(s, args.portfolios):
                who = p.owner or "owner (name not on record)"
                console.print(f"[bold cyan]{who}[/bold cyan] — {p.size} properties, {p.deal_count} with an offer verdict"
                              + (f", phone {p.phone['phone_display']}" if p.phone else ", mail only"))
                for x in p.properties[:6]:
                    console.print(f"   #{x['lead_id']} {x['address']}, {x['town']} · {', '.join(x['signals'])} · "
                                  f"{x['verdict'] or 'not valued'}")
            if args.top_script:
                top = ft.portfolios(s, 1)
                if top:
                    console.print(Panel(ft.portfolio_script(top[0])["opening"], title="Portfolio call opener"))
        if args.portfolio_letters:
            out, letters = ft.write_portfolio_batch(s, args.portfolio_letters,
                                                    Path(args.out) if args.out else None)
            console.print(f"{len(letters)} portfolio letters → {out} (then mail-merge --mark-mailed {out})")


def cmd_closing(args: argparse.Namespace) -> None:
    from engine import closing_coordinator as cc
    init_db()
    with session_scope() as s:
        if args.id is None:
            for a in cc.alerts(s):
                console.print(f"[{'red' if a.overdue else 'yellow'}]{'OVERDUE' if a.overdue else 'due'} "
                              f"{a.due_date:%a %b %d}[/] lead #{a.lead_id} {a.address}: {a.label}")
            t = Table(box=box.SIMPLE, title="Open closing files")
            for col in ("Lead", "Property", "Stage", "Contract", "Progress", "Next step", "Due"):
                t.add_column(col)
            for r in cc.pipeline(s):
                t.add_row(str(r["lead_id"]), r["address"], r["stage"], money(r["contract_price"]),
                          f"{r['done']}/{r['total']}", r["next"] or "—",
                          (f"[red]{r['next_due']}[/red]" if r["overdue"] else r["next_due"]) or "—")
            console.print(t)
            return
        lead = s.get(Lead, args.id)
        if lead is None:
            console.print(f"[red]lead {args.id} not found[/red]")
            return
        try:
            if args.disapproved:
                cc.record_disapproval(s, lead, args.notes or "")
                console.print("Contract disapproved during attorney review: file closed, lead back to NEGOTIATING.")
                return
            if not cc.milestones(s, lead):
                cc.open_file(s, lead)
            for note in cc.sync(s, lead):
                console.print(f"[green]{note}[/green]")
            if args.done:
                cc.complete(s, lead, args.done.upper(), args.notes or "")
            if args.close:
                if not args.fee:
                    console.print("[red]--close needs --fee (the assignment fee actually received)[/red]")
                    return
                cc.close_deal(s, lead, money_arg(args.fee), date.fromisoformat(args.date) if args.date else None)
                console.print(f"Closed — {money(money_arg(args.fee))} booked as income.")
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        t = Table(box=box.SIMPLE, title=f"Closing file — lead #{lead.id}")
        for col in ("#", "Milestone", "Due", "Status", "Notes"):
            t.add_column(col)
        for m in cc.milestones(s, lead):
            style = {"done": "green", "cancelled": "dim", "waived": "dim"}.get(m.status, "red" if m.due_date and
                                                                                 m.due_date < date.today() else "")
            t.add_row(str(m.sequence), cc.LABELS[m.milestone], f"{m.due_date:%a %b %d}" if m.due_date else "—",
                      f"[{style}]{m.status}[/{style}]" if style else m.status, (m.notes or "")[-60:])
        console.print(t)
        if args.title_email:
            partner = None
            if args.partner:
                data = cc.partners()
                partner = next((p for r in (data.get("regions") or {}).values() for p in r.get("title_agencies") or []
                                if args.partner.lower() in p.get("name", "").lower()), None)
            e = cc.title_order_email(lead, partner, args.structure)
            console.print(Panel(f"To: {e['to']} {e['email_to']}\nSubject: {e['subject']}\n\n{e['body']}",
                                title="Title order e-mail"))
            if e["partner"]:
                console.print(f"[dim]{e['partner'].get('phone') or ''} · {e['partner'].get('website') or ''} — confirm "
                              "they insure assignments / double closes before ordering.[/dim]")


def cmd_finance(args: argparse.Namespace) -> None:
    from engine import finance
    init_db()
    with session_scope() as s:
        try:
            if args.add_income:
                finance.add_entry(s, kind="income", category=args.category or "assignment_fee",
                                  amount=money_arg(args.add_income), description=args.desc, lead_id=args.lead,
                                  entry_date=date.fromisoformat(args.date) if args.date else None)
            if args.add_expense:
                finance.add_entry(s, kind="expense", category=args.category or "other",
                                  amount=money_arg(args.add_expense), description=args.desc, lead_id=args.lead,
                                  entry_date=date.fromisoformat(args.date) if args.date else None)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        sm = finance.summary(s, args.year)
        console.print(Panel.fit(
            f"Income {money(sm.income)} · expenses {money(sm.expenses)} · net [bold]{money(sm.net)}[/bold]\n"
            f"Tax reserve ({sm.reserve_rate:.0%}): [bold yellow]{money(sm.tax_reserve)}[/bold yellow] · "
            f"self-employment tax alone ≈ {money(sm.self_employment_tax_estimate)}\n"
            f"Projected pipeline (probability-weighted): {money(sm.pipeline_projected)}"
            + (f"\nNext estimated-tax due date: {sm.next_due['date']} — set aside {money(sm.next_due['set_aside'])} "
               "from this period" if sm.next_due else ""), title=f"Money — {sm.year}"))
        for q in sm.quarters:
            console.print(f"  due {q.due_date}: period net {money(q.period_net)} → set aside {money(q.set_aside)}")
        for x in sm.pipeline[:10]:
            console.print(f"  #{x['lead_id']} {x['address']} · {x['stage'].lower()} · fee {money(x['expected_fee'])} "
                          f"x {x['probability']:.0%} = {money(x['weighted'])}")
        for n in sm.notes:
            console.print(f"[dim]• {n}[/dim]")


def cmd_partners(args: argparse.Namespace) -> None:
    from engine import closing_coordinator as cc
    data = cc.partners()
    for name, region in (data.get("regions") or {}).items():
        if args.county and cc.region_for(args.county) != name:
            continue
        console.print(f"[bold]{name.title()} NJ[/bold] ({', '.join(region.get('counties') or [])})")
        for kind in ("title_agencies", "closing_attorneys"):
            for p in region.get(kind) or []:
                ev = f" — “{p['investor_evidence']}”" if p.get("investor_evidence") else ""
                console.print(f"  {'title' if kind == 'title_agencies' else 'attorney'}: {p['name']}, {p.get('city', '')} "
                              f"{p.get('phone', '')}{ev}")
    console.print(f"[dim]Verified {data.get('verified_on')} from each firm's own page — call to confirm assignments / "
                  "double closes, and check licenses (NAIC lookup) before you use them.[/dim]")


def cmd_inbound(args: argparse.Namespace) -> None:
    from pathlib import Path

    from engine import inbound
    init_db()
    gis = None
    if not args.offline:
        from agents.scrapers.njgis_client import NJGISClient
        gis = NJGISClient()
    with session_scope() as s:
        if not args.ingest:
            rows = s.execute(select(Lead.id, Property.street_address, Property.city, Lead.status_stage)
                             .join(Property, Lead.property_id == Property.id)
                             .where(Lead.primary_source == inbound.SOURCE).order_by(Lead.id.desc()).limit(25)).all()
            for r in rows:
                console.print(f"#{r[0]} {r[1]}, {r[2]} · {r[3].lower()}")
            buyers = s.scalars(select(CashBuyer).where(CashBuyer.source == inbound.SOURCE)).all()
            console.print(f"{len(rows)} web seller leads, {len(buyers)} registered buyers. "
                          "Ingest a form export: python manage.py inbound --ingest submissions.json")
            return
        out = inbound.ingest_file(s, Path(args.ingest), gis=gis, underwrite=not args.no_underwrite)
        for r in out["sellers"]:
            console.print(f"[green]seller lead #{r['lead_id']}[/green] ({'new' if r['created'] else 'existing'}) "
                          f"parcel {'matched' if r['matched_parcel'] else 'unmatched'} · "
                          f"{r['verdict'] or 'not valued yet'}" + (f" · {'; '.join(r['notes'])}" if r["notes"] else ""))
            console.print(Panel(r["response_email"], title="Draft reply (e-mail)"))
            if r["response_sms"]:
                console.print(f"  SMS: {r['response_sms']}")
        for b in out["buyers"]:
            console.print(f"[cyan]buyer #{b['buyer_id']}[/cyan] {b['name']} ({'new' if b['created'] else 'updated'}) — matched "
                          "against current deals")
        for e in out["errors"]:
            console.print(f"[red]skipped:[/red] {e}")


def cmd_site(args: argparse.Namespace) -> None:
    from pathlib import Path

    from engine.site_build import build
    out = Path(args.build)
    cfg = build(out, args.endpoint, args.buyer_endpoint)
    missing = [k for k in ("company", "phone", "email", "mailing_address") if not cfg[k]]
    console.print(f"Static site → {out} (mode: {cfg['mode']}). Upload the folder to GitHub Pages / Netlify / Vercel.")
    if missing:
        console.print(f"[yellow]Set operator {', '.join(missing)} in config/settings.local.yaml first — the pages show "
                      "your contact details and the forms need somewhere to go.[/yellow]")


def cmd_build_site(args: argparse.Namespace) -> None:
    from pathlib import Path

    from engine.site_build import build, check
    out = Path(args.out)
    cfg = build(out, args.endpoint, args.buyer_endpoint)
    problems = check(out, cfg)
    console.print(f"Static site → {out} (mode: {cfg['mode']}, counties: {', '.join(cfg['counties'])})")
    for p in problems:
        console.print(f"[red]✗[/red] {p}")
    if args.check:
        console.print("[green]Publish check passed.[/green]" if not problems else
                      f"[red]{len(problems)} problem(s): not publishing.[/red]")
        raise SystemExit(1 if problems else 0)


def cmd_today(args: argparse.Namespace) -> None:
    from engine.today import today
    init_db()
    with session_scope() as s:
        t = today(s)
        console.print(Panel.fit(f"[bold]Today — {t['date']}[/bold]: 2 focused hours. Work the blocks in order; log "
                                "every touch (cockpit → lead → Log call, or manage.py call).", border_style="cyan"))
        d = t["deadlines"]
        for a in d["closing"]:
            console.print(f"[{'red' if a['overdue'] else 'yellow'}]DEADLINE[/] {a['due_date']} lead #{a['lead_id']} "
                          f"{a['address']}: {a['label']}")
        for f in d["followups"]:
            console.print(f"[yellow]FOLLOW-UP[/yellow] lead #{f['lead_id']} {f['address']} ({f['stage'].lower()}, last: "
                          f"{(f['last'] or '—').lower()})")
        b1, b2, b3, b4 = t["blocks"]
        console.print(f"\n[bold red]BLOCK 1 ({b1['minutes']} min) — {b1['title']}[/bold red]")
        for r in b1["rows"]:
            a = r["agent"] or {}
            console.print(f"  #{r['lead_id']} [bold]{r['address']}[/bold] · sale {r['sale_date']} "
                          f"([bold]{r['days_to_sale']}d[/bold]) · upset {short_money(r['upset'])} · ask "
                          f"{short_money(r['list_price'])} · {r['dom'] or '?'} DOM · {r['track_label']}")
            console.print(f"     agent {a.get('name')} {a.get('phone_display') or ''} {a.get('email') or ''} · "
                          f"offer {short_money(r['anchor'])}/{short_money(r['best'])} · script: {r['cli']}")
        if not b1["rows"]:
            console.print(f"  [dim]{b1['empty']}[/dim]")
        console.print(f"\n[bold green]BLOCK 2 ({b2['minutes']} min) — {b2['title']}[/bold green]")
        for r in b2["rows"]:
            console.print(f"  #{r['lead_id']} {r['address']}, {r['town']} · ask {short_money(r.get('ask'))} vs MAO 70 "
                          f"{short_money(r['mao_70'])} (gap {short_money(r['gap_to_ask'])}) · {r.get('who') or ''} "
                          f"{r.get('phone_display') or r.get('email') or ''}")
        if not b2["rows"]:
            console.print(f"  [dim]{b2['empty']}[/dim]")
        console.print(f"\n[bold yellow]BLOCK 3 ({b3['minutes']} min) — {b3['title']}[/bold yellow]")
        for r in b3["rows"]:
            console.print(f"  #{r['lead_id']} {r['address']} · {r['kind']} · sale {r['sale_date']} · spread "
                          f"{short_money(r['spread'])} · look up: {r['target']} ({r['role'].lower()})")
            for name, url in list(r["links"].items())[:3]:
                console.print(f"     {name}: {url}")
        console.print("  [dim]Paste what you find: python manage.py skiptrace --lead <id> --paste <file> "
                      "(or the cockpit's Skip-trace box).[/dim]" if b3["rows"] else f"  [dim]{b3['empty']}[/dim]")
        console.print(f"\n[bold cyan]BLOCK 4 ({b4['minutes']} min) — {b4['title']}[/bold cyan]")
        for r in b4["rows"]:
            rec = r["recent"]
            console.print(f"  buyer #{r['buyer_id']} [bold]{r['name']}[/bold]{' (flipper)' if r['flipper'] else ''} · "
                          f"{r['purchases_in_towns']} buys there · recent {rec['address']}, {rec['town']} "
                          f"{money(rec['price'])} ({rec['date']}) · {r['phone_display'] or r['email'] or 'find a phone:'}")
            if not (r["phone_display"] or r["email"]):
                for name, url in r["links"].items():
                    console.print(f"     {name}: {url}")
            console.print(f"     script: python manage.py script <lead> --buyer {r['buyer_id']}  ·  “{r['opening'][:150]}…”")
        if not b4["rows"]:
            console.print(f"  [dim]{b4['empty']}[/dim]")
        console.print(Panel(t["reminder"], title="Every call", border_style="yellow"))


def cmd_calibrate(args: argparse.Namespace) -> None:
    """Measure what local investors pay (price / equalized value) from our recorded buyer purchases → market_calibration."""
    from agents.scrapers.njgis_client import NJGISClient
    from engine import quant_pricing as qp
    init_db()
    with session_scope() as s:
        data = qp.calibrate(s, NJGISClient(), progress=lambda m: console.print(f"[dim]{m}[/dim]") if args.verbose else None)
    st = data["statewide"]
    console.print(f"[green]calibrated[/green] {data['purchases_clean']} clean investor purchases of {data['purchases_total']}; "
                  f"statewide q25 {st['q25']:.2f} / q50 {st['q50']:.2f}; {len(data['towns'])} towns, "
                  f"{len(data['counties'])} counties → {qp.CALIBRATION_FILE}")
    t = Table(title="towns with the most evidence")
    for col in ("code", "town", "county", "n", "q25", "q50", "investor share", "alpha"):
        t.add_column(col)
    towns = sorted(data["towns"].items(), key=lambda kv: -kv[1]["n"])[:args.top]
    for code, row in towns:
        la = qp.local_alpha(code, row.get("county"))
        t.add_row(code, row.get("name") or "", row.get("county") or "", str(row["n"]), f"{row['q25']:.2f}",
                  f"{row['q50']:.2f}", f"{(row.get('share') or 0):.1%}", f"{la.alpha:.2f}")
    console.print(t)


def cmd_recalibrate(args: argparse.Namespace) -> None:
    """Re-price every current valuation under deal.pricing_model and report the striking-distance change."""
    from engine import quant_pricing as qp
    init_db()
    with session_scope() as s:
        r = qp.recalibrate_all(s, limit=args.limit, progress=lambda m: console.print(f"[dim]{m}[/dim]"))
    b, a = r["before"], r["after"]
    console.print(f"[green]re-priced {r['repriced']} valuations[/green] ({r['errors']} errors)")
    t = Table(title="striking distance (listed leads our ceiling reaches, no ARV-vs-ask conflict)")
    for col in ("metric", "before", "after", "change"):
        t.add_column(col)
    for key in ("valuations", "listed", "fixers", "foreclosures", "strike_75", "strike_78", "fixers_75", "fixers_78",
                "foreclosures_75", "foreclosures_78", "conflicts", "quant", "buyer_roi_below_min"):
        t.add_row(key, str(b.get(key, 0)), str(a.get(key, 0)), f"{a.get(key, 0) - b.get(key, 0):+d}")
    console.print(t)
    if a.get("buyer_roi_median") is not None:
        console.print(f"end-buyer ROI at buyer max after re-pricing: median {a['buyer_roi_median']:.1%}, "
                      f"min {a['buyer_roi_min']:.1%}; valuations under the ROI floor (not alpha-bound): "
                      f"{a.get('buyer_roi_below_min', 0)}")
    console.print("[dim]Scores and READY_TO_CALL flags refresh on the next `swarm run lead_scoring` (or the dashboard's quick refresh).[/dim]")


def cmd_vault(args: argparse.Namespace) -> None:
    """JV partner & proof-of-funds vault: seed from the deed records, list, log a touch, attach a POF."""
    from engine import jv_partner_vault as vault
    init_db()
    with session_scope() as s:
        if args.seed:
            rows = vault.seed(s, top=args.seed)
            console.print(f"[green]seeded {len(rows)} partners[/green]")
        if args.unmask:
            from agents.scrapers.njgis_client import NJGISClient
            from engine import partner_principals as pp
            pp.refresh(s, gis=NJGISClient() if args.gis else None)
            rows = pp.desk_rows(s, top=10)
            t = Table(title="Top 10 JV partner targets (grouped by mailing address; principals are leads to verify)")
            for col in ("#", "entity / buyer", "principal (basis)", "buys 36m", "$ deployed", "buys in", "icebreaker", "look up"):
                t.add_column(col, overflow="fold")
            for r in rows:
                ice = r["icebreaker"] or {}
                link = r["links"].get("truepeoplesearch") or r["links"].get("truepeoplesearch_address") or ""
                who = (f"{r['principal']} ({int((r['principal_confidence'] or 0) * 100)}%)" if r["principal"] else "—")
                t.add_row(str(r["rank"]), r["entity"] + (f" + {r['llc_count'] - 1} LLCs" if r["llc_count"] > 1 else ""),
                          who, str(r["purchases_36m"]), money(r["deployed"]), ", ".join(r["top_towns"][:3]),
                          f"{ice.get('address', '')}, {ice.get('town', '')} {short_money(ice.get('price'))} ({ice.get('date', '')})",
                          link)
            console.print(t)
            out = Path(args.out) if args.out else None
            if out:
                out.parent.mkdir(parents=True, exist_ok=True)
                import json as _json
                out.write_text(_json.dumps(rows, indent=1, default=str))
                console.print(f"[dim]wrote {out} (git-ignored: names and addresses)[/dim]")
            return
        if args.touch:
            p = s.get(JVPartner, args.touch)
            if p is None:
                raise SystemExit(f"partner {args.touch} not found")
            vault.log_touch(s, p, args.channel, args.notes or "")
            console.print(f"logged {args.channel} for {p.contact_name or p.entity_name} → {p.status}")
        if args.pof:
            p = s.get(JVPartner, args.pof)
            if p is None:
                raise SystemExit(f"partner {args.pof} not found")
            path = Path(args.file or "")
            if not path.exists():
                raise SystemExit("--file <pdf|png|jpg> is required with --pof")
            dest = vault.attach_pof(s, p, path.name, path.read_bytes())
            vault.update(s, p, pof_share_ok=bool(args.share))
            console.print(f"[green]proof of funds on file[/green] → {dest} ({vault.active_count(s)}/{vault.partners_needed()} verified)")
        if args.script:
            p = s.get(JVPartner, args.script)
            if p is None:
                raise SystemExit(f"partner {args.script} not found")
            c = vault.outreach(s, p)
            console.print(f"[bold]{c.subject}[/bold]\n{c.body}\n\n[dim]SMS:[/dim] {c.sms}\n[dim]Call:[/dim] " + " ".join(c.phone_script))
            return
        rows = vault.partners(s)
        t = Table(title=f"JV partner vault — {vault.active_count(s)}/{vault.partners_needed()} verified with proof of funds")
        for col in ("id", "partner", "entity", "status", "POF", "counties", "price band", "rehab", "last touch", "velocity"):
            t.add_column(col)
        for p in rows[:args.top]:
            t.add_row(str(p.id), p.contact_name or "", p.entity_name or "", p.status, "✓" if p.pof_verified else "",
                      ", ".join(str(c) for c in (p.target_counties or [])[:3]),
                      f"{short_money(p.min_price)}–{short_money(p.max_price)}", p.max_rehab or "",
                      p.last_contact_at.strftime("%m/%d") if p.last_contact_at else "", f"{p.velocity_score or 0:.0f}")
        console.print(t)
        if vault.phase0_open(s):
            console.print("[yellow]Phase 0 is open:[/yellow] reach the TARGET partners first (manage.py vault --script <id>).")


def cmd_fast_skiptrace(args: argparse.Namespace) -> None:
    """Fast Skip-Trace Mode from the terminal: the queue with lookup links, or paste a saved page for one lead."""
    from engine import clipboard_skiptrace as cs
    init_db()
    with session_scope() as s:
        if args.paste:
            text = Path(args.paste).read_text(encoding="utf-8", errors="replace")
            out = cs.trace(s, args.lead, text)
            sv = out["saved"]
            console.print(f"[green]saved[/green] best {sv['best_phone']} ({sv['best_status']}) · ready_to_call={sv['ready_to_call']} "
                          f"· {sv['contacts_created']} new contact(s)")
            for h in out["hits"]:
                console.print(f"  {h['display']:16s} {h['kind']:9s} {h['last_reported'] or '':10s} {h['status']:12s} {h['reason']}")
            for n in sv["notes"]:
                console.print(f"  [yellow]{n}[/yellow]")
            if out["next"]:
                console.print(f"next: lead {out['next']['lead_id']} {out['next']['address']}")
            return
        rows = cs.queue(s, limit=args.top)
        t = Table(title="⚡ Fast skip-trace queue (no phone yet, offer path, unlisted)")
        for col in ("lead", "address", "why", "look up", "verdict · fee", "sale"):
            t.add_column(col)
        for r in rows:
            t.add_row(str(r["lead_id"]), f"{r['address']}", ", ".join(r["reasons"]), f"{r['target']['name']} ({r['target']['role']})",
                      f"{r['numbers']['verdict']} · {short_money(r['numbers']['fee'])}",
                      f"{r['days_to_sale']}d" if r.get("days_to_sale") is not None else "")
        console.print(t)
        if rows and args.links:
            r = rows[0]
            for k, v in r["links"].items():
                if v:
                    console.print(f"  {k}: {v}")
        console.print("[dim]Open the links in a browser, copy the result page, then: manage.py skiptrace-fast --lead <id> --paste page.txt[/dim]")


def cmd_reply(args: argparse.Namespace) -> None:
    """Paste an agent's reply: traffic light, the math and the 3-sentence answer (logged unless --dry-run)."""
    from engine import reply_negotiator as rn
    init_db()
    text = Path(args.file).read_text(encoding="utf-8", errors="replace") if args.file else (args.text or "")
    if not text.strip():
        raise SystemExit("give --text '...' or --file reply.txt")
    with session_scope() as s:
        lead = _lead(s, args.id)
        a = rn.analyze(s, lead, text) if args.dry_run else rn.analyze_and_record(s, lead, text)[0]
        light = {"GREEN": "green", "YELLOW": "yellow", "RED": "red", "INFO": "cyan"}[a.verdict]
        console.print(f"[{light}][bold]{a.verdict}[/bold] {a.headline}[/{light}]  intents: {', '.join(sorted(a.parsed.intents))}")
        n = a.numbers
        if n:
            console.print(f"counter {money(n.get('counter'))} · our counter {money(n.get('our_counter'))} · fee at theirs "
                          f"{money(n.get('fee_at_counter'))} · buyer max {money(n.get('buyer_max'))} · walk-away "
                          f"{money(n.get('adjusted_walkaway', n.get('mao_walkaway')))}")
        for x in a.actions:
            console.print(f"  • {x}")
        if a.reply_text:
            console.print(f"\n[bold]{a.reply_subject}[/bold]\n{a.reply_text}\n\n[dim]SMS:[/dim] {a.sms_text}")
        if a.followup_at:
            console.print(f"[dim]follow-up {a.followup_at:%a %b %d %H:%M}[/dim]")


def cmd_structure(args: argparse.Namespace) -> None:
    """Assignment vs double close for a lead (or any two prices), and the rider / entity option."""
    from engine import deal_structure as ds
    if args.contract and args.buyer:
        c = ds.compare_structures(contract_price=args.contract, buyer_price=args.buyer,
                                  seller_refuses_assignment=args.seller_refuses, in_llc=args.llc)
    else:
        init_db()
        with session_scope() as s:
            lead = _lead(s, args.id)
            c = ds.structure_for_lead(lead, buyer_price=args.buyer, seller_refuses_assignment=args.seller_refuses)
    t = Table(title=f"contract {money(c.contract_price)} → buyer {money(c.buyer_price)} (spread {money(c.spread)})")
    for col in ("", "assignment", "double close"):
        t.add_column(col)
    t.add_row("costs", money(c.assignment["costs"]), money(c.double_close["costs"]))
    t.add_row("net", money(c.assignment["net"]), money(c.double_close["net"]))
    t.add_row("out of pocket", "deposit only", money(c.double_close["out_of_pocket"]))
    console.print(t)
    console.print(f"[green]recommended: {c.recommended.replace('_', ' ')}[/green] — {'; '.join(c.reasons)}")
    console.print(f"[dim]{c.double_close['condition']}[/dim]")


def cmd_preflight(args: argparse.Namespace) -> None:
    """Day-1 pre-flight cheat sheet: the first N deck cards with every number, the comps and what to verify."""
    from engine import autopilot as ap
    from engine import preflight
    from engine.scripts import lead_facts
    from engine.underwriter import asking_price
    init_db()
    out: list[str] = [f"# Pre-flight cheat sheet — {date.today():%A, %B %d, %Y}", ""]
    with session_scope() as s:
        d = ap.deck(s, limit=max(args.top, 10))
        held = d.get("held") or []
        for i, c in enumerate(d["cards"][:args.top], 1):
            lead = s.get(Lead, c["lead_id"])
            p, v = lead.property, lead.property.current_valuation
            a = v.assumptions_json or {}
            q = a.get("quant") or {}
            f = lead_facts(lead)
            pf = c.get("preflight") or preflight.check(lead).to_json()
            ask = asking_price(p)
            out += [f"## {i}. {f['address']} ({p.county}) — {c['label']} · pre-flight {pf['status']}",
                    f"- Property: class {p.property_class} · {p.building_desc or '?'} · {p.dwellings or 1} unit(s) · "
                    f"{p.beds or '?'} bd / {p.baths or '?'} ba · {('%s sqft' % format(p.sqft, ',')) if p.sqft else 'sqft unknown (est. %s)' % format(v.sqft_used or 0, ',')} · built {p.year_built or '?'}",
                    f"- Ask {money(ask)}" + (f" · payoff (upset) {money(f.get('upset'))} · sheriff sale {f.get('sale_date')} ({f.get('days_to_sale')} days)" if f.get('upset') else "")
                    + (f" · {f.get('dom')} days on market" if f.get('dom') else ""),
                    f"- ARV {money(v.arv_estimate)} · repairs {money(v.est_repair_cost)} ({(v.rehab_tier or '').lower()}) · "
                    f"buyer max {money(q.get('buyer_max'))} · target {money(q.get('mao_target'))} · walk-away "
                    f"{money(q.get('mao_walkaway'))} · opening {money(q.get('opening_offer'))} · verdict {v.verdict}",
                    f"- Touch {c['touch']} ({c['packet_kind']}): {c['action']}"]
            for x in pf["issues"]:
                out.append(f"  - ⚠ {x}")
            if pf["comps"]:
                out.append("- Comps to quote:")
                for cp in pf["comps"][:5]:
                    out.append(f"  - {cp['address']}{', ' + cp['town'] if cp.get('town') else ''}: {money(cp['price'])} "
                               f"({cp.get('date')}, {cp.get('distance_mi')} mi{', $%s/sqft' % cp['ppsf'] if cp.get('ppsf') else ''}, {cp.get('type') or ''})")
            if pf.get("talking_point"):
                out.append(f"- Say: \"{pf['talking_point']}\"")
            out.append("")
        if held:
            out += ["## Held back (numbers not defensible yet — re-underwrite before contacting)"]
            out += [f"- {h['address']}: {'; '.join(h['issues'])}" for h in held]
        s.rollback()
    text = "\n".join(out)
    console.print(text)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        console.print(f"[dim]wrote {path} (git-ignored)[/dim]")


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
    cal = sub.add_parser("calibrate", help="measure local investor pricing (alpha) from recorded buyer purchases")
    cal.add_argument("--top", type=int, default=25)
    cal.add_argument("--verbose", action="store_true")
    rc = sub.add_parser("recalibrate", help="re-price every valuation under deal.pricing_model; report striking distance")
    rc.add_argument("--limit", type=int)
    va = sub.add_parser("vault", help="JV partner & proof-of-funds vault (Phase 0: lock in 3 funding partners)")
    va.add_argument("--seed", type=int, nargs="?", const=30, help="seed the top N named repeat buyers from the deed records")
    va.add_argument("--top", type=int, default=40)
    va.add_argument("--script", type=int, help="print the outreach e-mail / text / call script for a partner id")
    va.add_argument("--touch", type=int, help="log an e-mail / text / call to a partner id")
    va.add_argument("--channel", default="email", choices=["email", "sms", "phone"])
    va.add_argument("--notes", default="")
    va.add_argument("--pof", type=int, help="attach a proof-of-funds file to a partner id (with --file)")
    va.add_argument("--file")
    va.add_argument("--share", action="store_true", help="the partner lets us attach the POF to offers")
    va.add_argument("--unmask", action="store_true", help="group partners by mailing address, attach likely principals, "
                                                          "re-rank, print the top 10")
    va.add_argument("--gis", action="store_true", help="with --unmask: classify each mailing address from NJGIN")
    va.add_argument("--out", help="with --unmask: write the top 10 rows as JSON here (use data/exports/)")
    sk = sub.add_parser("skiptrace-fast", help="fast skip-trace queue with lookup links; --lead/--paste saves a copied page")
    sk.add_argument("--top", type=int, default=25)
    sk.add_argument("--links", action="store_true", help="print the lookup links for the first lead")
    sk.add_argument("--lead", type=int)
    sk.add_argument("--paste", help="text file with the copied people-search page")
    rp = sub.add_parser("reply", help="analyze a listing agent's reply: traffic light, math, 3-sentence answer")
    rp.add_argument("id", type=int)
    rp.add_argument("--text")
    rp.add_argument("--file")
    rp.add_argument("--dry-run", action="store_true", help="analyze without logging it on the lead")
    pfp = sub.add_parser("preflight", help="pre-flight cheat sheet for the first N autopilot deal cards")
    pfp.add_argument("--top", type=int, default=10)
    pfp.add_argument("--out", help="write the cheat sheet as markdown here (use data/exports/)")
    stp = sub.add_parser("structure", help="assignment vs double close net check (lead id, or --contract/--buyer)")
    stp.add_argument("id", type=int, nargs="?")
    stp.add_argument("--contract", type=money_arg)
    stp.add_argument("--buyer", type=money_arg)
    stp.add_argument("--seller-refuses", action="store_true")
    stp.add_argument("--llc", action="store_true")
    sub.add_parser("stats")
    bs = sub.add_parser("build-site", help="static seller/buyer funnel for GitHub Pages (+ --check publish gate)")
    bs.add_argument("--out", default="site_dist")
    bs.add_argument("--endpoint", help="form endpoint for seller submissions (e.g. Formspree); default $SITE_FORM_ENDPOINT")
    bs.add_argument("--buyer-endpoint", help="separate buyer form endpoint; default $SITE_BUYER_ENDPOINT")
    bs.add_argument("--check", action="store_true", help="exit 1 unless the build is complete and carries no data but yours")
    ish = sub.add_parser("install-shortcut", help="desktop launcher: double-click to start + open the dashboard")
    ish.add_argument("--desktop", help="folder for the shortcut (default ~/Desktop)")
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
    fs = sub.add_parser("fast-track", help="listed foreclosures (sheriff sale + MLS) and portfolio owners")
    fs.add_argument("--top", type=int, default=30)
    fs.add_argument("--script", type=int, metavar="LEAD", help="listing-agent script for one listed foreclosure")
    fs.add_argument("--packets", action="store_true", help="build urgent e-mail/SMS/LOI packets for the queue")
    fs.add_argument("--portfolios", type=int, default=0, metavar="N", help="show the top N portfolio owners")
    fs.add_argument("--top-script", action="store_true", help="with --portfolios: print the call opener for #1")
    fs.add_argument("--portfolio-letters", type=int, default=0, metavar="N", help="one letter per portfolio owner")
    fs.add_argument("--out")
    cl = sub.add_parser("closing", help="contract-to-close milestones, title order e-mail, close a deal")
    cl.add_argument("id", type=int, nargs="?")
    cl.add_argument("--done", metavar="MILESTONE", help="mark a milestone done (e.g. EMD_DEPOSITED)")
    cl.add_argument("--notes")
    cl.add_argument("--disapproved", action="store_true", help="an attorney disapproved during review")
    cl.add_argument("--title-email", action="store_true")
    cl.add_argument("--partner", help="title agency name (default: preferred / best in the county's region)")
    cl.add_argument("--structure", default="assignment", choices=["assignment", "double_close"])
    cl.add_argument("--close", action="store_true", help="closed and paid")
    cl.add_argument("--fee", help="assignment fee received, e.g. 22.5k")
    cl.add_argument("--date", help="YYYY-MM-DD")
    fi = sub.add_parser("finance", help="income, expenses, pipeline and the quarterly tax reserve")
    fi.add_argument("--year", type=int)
    fi.add_argument("--add-income")
    fi.add_argument("--add-expense")
    fi.add_argument("--category")
    fi.add_argument("--desc")
    fi.add_argument("--lead", type=int)
    fi.add_argument("--date")
    pa = sub.add_parser("partners", help="NJ title agencies and closing attorneys (config/nj_closing_partners.yaml)")
    pa.add_argument("--county")
    ib = sub.add_parser("inbound", help="website seller / buyer submissions (list or --ingest an export)")
    ib.add_argument("--ingest", metavar="FILE", help="Formspree/webhook JSON or CSV export, or a 'key: value' e-mail")
    ib.add_argument("--no-underwrite", action="store_true")
    ib.add_argument("--offline", action="store_true", help="skip the NJGIN parcel lookup")
    si = sub.add_parser("site", help="build the static seller/buyer site for free hosting")
    si.add_argument("--build", required=True, metavar="DIR")
    si.add_argument("--endpoint", help="form endpoint for the seller form (e.g. a Formspree URL)")
    si.add_argument("--buyer-endpoint", help="separate endpoint for the buyer form (default: --endpoint)")
    sub.add_parser("today", help="the 2-hour daily checklist (listed foreclosures, ready calls, skip-trace, buyers)")
    args = parser.parse_args(argv)
    {"init": lambda a: (init_db(), console.print("[green]database ready[/green]")),
     "briefing": cmd_briefing, "lead": cmd_lead, "underwrite": cmd_underwrite, "buyers": cmd_buyers,
     "stats": cmd_stats, "server": cmd_server, "install-shortcut": cmd_install_shortcut, "build-site": cmd_build_site, "queue": cmd_queue, "script": cmd_script, "call": cmd_call,
     "occupancy": cmd_occupancy, "contract": cmd_contract, "docs": cmd_docs, "skiptrace": cmd_skiptrace,
     "dnc": cmd_dnc, "match": cmd_match, "pii-scan": cmd_pii_scan, "agent-blast": cmd_agent_blast,
     "mail-merge": cmd_mail_merge, "inspect": cmd_inspect, "stack": cmd_stack, "buyer-hotzones": cmd_buyer_hotzones,
     "walkthrough": cmd_walkthrough, "bid": cmd_bid, "fast-track": cmd_fast_track, "closing": cmd_closing,
     "finance": cmd_finance, "partners": cmd_partners, "inbound": cmd_inbound, "site": cmd_site,
     "today": cmd_today, "calibrate": cmd_calibrate, "recalibrate": cmd_recalibrate, "vault": cmd_vault,
     "skiptrace-fast": cmd_fast_skiptrace, "reply": cmd_reply, "structure": cmd_structure,
     "preflight": cmd_preflight}[args.cmd](args)


if __name__ == "__main__":
    main()
