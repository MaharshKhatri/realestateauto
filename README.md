# NJ Wholesale Engine

An autonomous, zero-budget deal-finding and underwriting system for real estate wholesaling in
New Jersey (Middlesex, Union, Essex, Hudson, Passaic, Bergen, Camden). It harvests distressed and
motivated-owner leads from free public NJ data, finds the cash buyers who are actively buying,
underwrites every top lead (comps → ARV → rehab → MAO → opening offer), ranks them, finds who to call,
writes the call script, logs the call, drafts the paperwork and matches the deal to buyers — so the operator
spends their time on the phone and at the kitchen table.

> **Read `CLAUDE.md` first** — it is the architecture memory and the NJ legal rulebook the code
> enforces (attorney review, tax sale certificates, sheriff-sale adjournments, the Foreclosure Rescue
> Fraud Prevention Act 82%-of-FMV floor, Consumer Fraud Act disclosures, TCPA/DNC, Daniel's Law).

## What it does today (Phases 1–2)

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
| `engine/frfpa_classifier.py` | signals, owner/defendant, confirmed occupancy | FRFPA track per foreclosure lead (exempt vs covered) and how a covered deal can still work: 82%-of-FMV floor, equity-rescue referral, or blocked |
| `agents/enrichment/*` | municipal ArcGIS owner layers, listing payloads, sheriff defendants, owner/deed exports | Contacts: owners, heirs/executors, relatives, listing agents, entity lookups; skip-trace queue (CSV/HTML with manual search links) and paste/CSV import |
| `engine/compliance.py` | FTC DNC downloads, internal + Daniel's Law lists | Callable / unscrubbed / DNC per number, 8am–9pm window |
| `engine/scripts.py` | lead facts + valuation | Battle cards: playbooks A (sheriff sale), B (estate), C (tired landlord), D (listing agent), E (cash buyer); 12+ objections; never-say lists |
| `engine/contracts.py` | lead + valuation | LOI, NJ purchase-agreement draft, assignment, JV, 1-page deal sheet → `deals/<id>/` (md, html, pdf) behind FRFPA / attorney-review gates |
| `engine/crm.py` + `web/` | operator | Call logging → stage → follow-up → re-score → post-call coach; local cockpit at http://127.0.0.1:8000 |
| `engine/buyer_matcher.py` | recorded cash buyers | Top 5 buyers per top deal with reasons |
| `manage.py briefing` | DB | The Daily Action Briefing (top leads with ARV, MAO, fee, FRFPA track, contact status, top buyers and *why*) |

## Quickstart

```bash
pip install -e ".[dev]"
pip install homeharvest          # optional: listing comps + stale listings (needs pandas<3)
python manage.py init            # creates wholesaling.db
python -m swarm.orchestrator run-all      # live pipeline: scrape → score → underwrite → self-improve
python manage.py briefing        # today's call list
python manage.py lead 42         # one lead in depth (signals, comps, offer math, strategy)
python manage.py buyers          # who is buying right now
python manage.py server          # CRM cockpit: http://127.0.0.1:8000 (local only)
python manage.py skiptrace --export-top 50   # who to look up, with search links; then --import the CSV
python manage.py dnc --load 973.txt          # National DNC data for an area code (free for 5 codes)
python manage.py script 42       # pre-call battle card
python manage.py call 42 --outcome SPOKE_NEEDS_OFFER --occupancy VACANT --ask 300k
python manage.py docs 42 --type loi          # LOI / psa / assignment / jv / deal_sheet / all
python -m swarm.orchestrator daemon       # keep it running on the cron schedules in config/settings.yaml
pytest                           # 98 tests; parsers run against real saved pages
```

Optional AI coach after calls: `pip install -e ".[ai]"` and set `ANTHROPIC_API_KEY`; tick "AI coach" in the
cockpit (or `--ai`). Only the town, deal numbers and redacted notes are sent.

Run a single agent: `python -m swarm.orchestrator run sheriff_sales --opt limit=50`,
`python -m agents.scrapers.nj_tax_assessment_agent absentee --county MIDDLESEX --muni "WOODBRIDGE TWP"`.

## Configuration

`config/settings.yaml` holds every tunable: counties and focus towns, MAO percentages, fee band,
anchor discount, comp radius/window, rehab $/sqft by tier and age bumps, scoring weights, cron
schedules. Put personal overrides in `config/settings.local.yaml` (git-ignored) and secrets in `.env`.
Set `WHOLESALING_DATABASE_URL` to a Postgres URL (e.g. a free Supabase project) to move off SQLite.

## Honest limitations

- **Owner names and phones**: NJ's GIS parcel layer redacts owner names (Daniel's Law). Names come from
  sheriff defendants, Jersey City / New Brunswick / Plainfield parcel layers, county owner exports and
  mailing cross-references (~33% of leads today). Phone numbers need skip tracing: the engine prepares the
  lookups, the operator clicks them (people-search sites forbid scraping) and pastes the result back.
- **Documents are drafts.** The purchase agreement, assignment and JV are for a NJ attorney to finalize;
  FRFPA-covered (owner-occupied foreclosure) contracts are never generated — the attorney drafts those.
- **Public-record sales lag** about a year (MOD-IV list vintage); they're time-adjusted and used as
  a cross-check. Fresh comps come from homeharvest, which scrapes Realtor.com — their terms forbid
  scraping, so it runs low-volume, cached, and internal-only, and the engine falls back to public
  records when it's unavailable.
- **Valuations are screening estimates**, not appraisals. Confidence scores say how much to trust
  each one; get photos or a video walkthrough (engine/virtual_inspector.py) before signing anything.
- **Legal**: this software encodes conservative readings of NJ law but is not legal advice. Have a NJ
  real estate attorney review your contract templates, your scripts, and whether the Foreclosure
  Rescue Fraud Prevention Act applies to your deals. Register as a telemarketer and subscribe to the
  National DNC registry before cold calling.

## Roadmap

Sprint 3 — clerk lis pendens, NJ permits, paid skip-trace adapters, outcome-trained scoring · Sprint 4 —
deal blasts to matched buyers, attorney-reviewed templates, closing coordinator. See `CLAUDE.md`.
