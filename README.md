# NJ Wholesale Engine

An autonomous, zero-budget deal-finding and underwriting system for real estate wholesaling in
New Jersey (Middlesex, Union, Essex, Hudson, Passaic, Bergen, Camden). It harvests distressed and
motivated-owner leads from free public NJ data, finds the cash buyers who are actively buying,
underwrites every top lead (comps → ARV → rehab → MAO → opening offer), ranks them, and prints a
daily action briefing so the operator spends their time on the phone and at the kitchen table.

> **Read `CLAUDE.md` first** — it is the architecture memory and the NJ legal rulebook the code
> enforces (attorney review, tax sale certificates, sheriff-sale adjournments, the Foreclosure Rescue
> Fraud Prevention Act 82%-of-FMV floor, Consumer Fraud Act disclosures, TCPA/DNC, Daniel's Law).

## What it does today (Sprint 1)

| Component | Source (all free) | Output |
|---|---|---|
| `agents/scrapers/sheriff_sale_agent.py` | CivilView SalesWeb — every NJ county sheriff | Upcoming foreclosure auctions: sheriff #, docket, plaintiff, defendant (owner), upset/judgment amount, block/lot, occupancy, adjournment history; matched to the MOD-IV parcel |
| `agents/scrapers/nj_tax_assessment_agent.py` → `TaxAssessmentAgent` | NJGIN statewide parcels + MOD-IV (ArcGIS REST) | Out-of-state and long-tenure absentee owners in focus towns, with mailing address, tenure, likely-free-and-clear flag, value and equity estimates |
| `agents/scrapers/nj_tax_assessment_agent.py` → `CashBuyerAgent` | NJGIN recorded deeds (last 24 months of data) | Active investors clustered by owner mailing address: purchases, volume, buy box (towns, price range, building age), reliability score |
| `agents/scrapers/stale_listing_distress_agent.py` | homeharvest (Realtor.com listing data) | Active listings with 90+ days on market, price cuts, or distress language ("as-is", "cash only", "estate sale", …) + listing agent contact |
| `engine/underwriter.py` | homeharvest sold comps + NJGIN recorded sales | ARV (top-30% $/sqft), as-is (bottom 30%), NJ tax-drag haircut, rehab tier, MAO 70%/75%, anchor offer, assignment fee, verdict, confidence |
| `engine/scoring.py` | everything above | Motivation / viability / priority scores with plain-English reasons |
| `swarm/orchestrator.py` | — | Parallel scrape phase, pipeline order, cron daemon, run bookkeeping |
| `swarm/self_improvement.py` | agent_runs + DB | Health/coverage/spread audit → `logs/swarm_intelligence.md`, tunes scoring weights, auto-adds high-spread towns |
| `manage.py briefing` | DB | The Daily Action Briefing (top 15 leads with ARV, MAO, fee and *why*) |

## Quickstart

```bash
pip install -e ".[dev]"
pip install homeharvest          # optional: listing comps + stale listings (needs pandas<3)
python manage.py init            # creates wholesaling.db
python -m swarm.orchestrator run-all      # live pipeline: scrape → score → underwrite → self-improve
python manage.py briefing        # today's call list
python manage.py lead 42         # one lead in depth (signals, comps, offer math, strategy)
python manage.py buyers          # who is buying right now
python -m swarm.orchestrator daemon       # keep it running on the cron schedules in config/settings.yaml
pytest                           # 39 tests; parsers run against real saved CivilView pages
```

Run a single agent: `python -m swarm.orchestrator run sheriff_sales --opt limit=50`,
`python -m agents.scrapers.nj_tax_assessment_agent absentee --county MIDDLESEX --muni "WOODBRIDGE TWP"`.

## Configuration

`config/settings.yaml` holds every tunable: counties and focus towns, MAO percentages, fee band,
anchor discount, comp radius/window, rehab $/sqft by tier and age bumps, scoring weights, cron
schedules. Put personal overrides in `config/settings.local.yaml` (git-ignored) and secrets in `.env`.
Set `WHOLESALING_DATABASE_URL` to a Postgres URL (e.g. a free Supabase project) to move off SQLite.

## Honest limitations

- **Owner names**: NJ's GIS parcel layer redacts owner names (Daniel's Law). Sheriff-sale leads carry
  the defendant's name; absentee leads carry the mailing address until the owner-records enrichment
  agent attaches names (Sprint 2 — see `docs/nj_data_sources.md`).
- **Public-record sales lag** about a year (MOD-IV list vintage); they're time-adjusted and used as
  a cross-check. Fresh comps come from homeharvest, which scrapes Realtor.com — their terms forbid
  scraping, so it runs low-volume, cached, and internal-only, and the engine falls back to public
  records when it's unavailable.
- **Valuations are screening estimates**, not appraisals. Confidence scores say how much to trust
  each one; walk the property (or get photos) before signing anything.
- **Legal**: this software encodes conservative readings of NJ law but is not legal advice. Have a NJ
  real estate attorney review your contract templates, your scripts, and whether the Foreclosure
  Rescue Fraud Prevention Act applies to your deals. Register as a telemarketer and subscribe to the
  National DNC registry before cold calling.

## Roadmap

Sprint 2 — owner-name enrichment, skip-trace assist, outcome-trained scoring · Sprint 3 — CRM, call
script engine, deal-package PDFs · Sprint 4 — buyer outreach, NJ contract templates, closing coordinator.
See `CLAUDE.md`.
