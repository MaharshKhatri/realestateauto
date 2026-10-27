# CLAUDE.md — System Brain for the NJ Wholesaling Engine

This file is the permanent architectural memory for this repository. Read it at the start of
every session. Keep it current: when you change architecture, data sources, or legal rules,
update this file in the same commit.

## Mission

An autonomous, zero-budget deal-finding and underwriting system for a New Jersey real estate
wholesaler (18-year-old operator, has a laptop, phone and car, no paid data). The software does
the data harvesting, distress detection, owner lookup, comps, ARV/MAO underwriting and lead
ranking. The operator spends their time talking to sellers, negotiating and closing.

Target counties (priority order): **Middlesex, Union, Essex, Hudson, Passaic, Bergen, Camden**.
Target outcome: assignment fees of $15K–$40K per deal.

## 4-Sprint Master Architecture

| Sprint | Scope | Status |
|---|---|---|
| **1. Data & Valuation Swarm** | DB layer, live NJ scrapers (sheriff sales, MOD-IV absentee/out-of-state, mailing-cluster cash buyers, stale listings, tax-sale import), owner/deed import + flip detection, comps/ARV/MAO underwriter with NJ tax drag + FRFPA floor, explainable scoring, swarm orchestrator, self-improvement log, daily briefing CLI | **Done (Phase 1)** |
| **2. Contacts, FRFPA Tracks, Call Scripts, Deal Docs & CRM Cockpit** | Contacts table + enrichment (listing agents, foreclosure defendants/heirs/executors, municipal ArcGIS owner layers, mailing cross-reference, entity lookup links), skip-trace export/paste/import, DNC guard (FTC/internal/Daniel's Law, calling hours), FRFPA occupancy classifier + covered-deal tracks (82% floor / equity-rescue referral), battle cards (playbooks A–E, 12+ objections), LOI/PSA/assignment/JV/deal-sheet generator with compliance gates, CRM (call log → stage → follow-up → rescore → coach), local web cockpit, buyer matcher, contact-readiness audit | **Done (Phase 2)** |
| **3. Lead Sources & Learning at Scale** | Union + Essex clerk lis-pendens agents (names → parcels via owner lists), NJ permits (demolition/alteration → flipper + condition signals), paid skip-trace provider adapters, older municipal owner layers with deed-match rule (Middlesex 2022, Passaic City 2023, Essex/Passaic 2017), outcome-driven weight re-fits once calls are logged | Next |
| **4. Disposition & Closing Coordinator** | Buyer list growth (county deed exports), deal blasts to matched buyers (only after attorney review), attorney-reviewed templates replacing the drafts, title/closing milestone tracker | Planned |

## Directory structure

```
CLAUDE.md                     ← you are here
manage.py                     ← operator CLI (rich): briefing, lead, underwrite, buyers, stats, server, queue, script,
                                call, occupancy, contract, docs, skiptrace, dnc, match, swarm passthrough
config/settings.yaml          ← all tunables (counties/towns, MAO %, fees, rehab $/sqft, scoring weights, cron)
config/sources/               ← source directories (realauction_nj.yaml: 91 tax-sale sites in target counties)
core/                         ← config loader, polite HTTP client (per-host rate limit, retries, disk cache,
                                host stats), text/address utils, NJ business days, geo, ZIP (ZCTA) resolver
db/database.py                ← engine/session factory, create + additive migrations, init lock (thread-safe)
db/models.py                  ← ORM: properties, distress_signals, valuations, leads (+ occupancy, FRFPA track, CRM
                                fields), contacts, interactions, dnc_entries, buyer_matches, generated_documents,
                                cash_buyers, deed_transfers, listings, agent_runs, scoring_weights, swarm_state
db/repository.py              ← idempotent upserts keyed on natural keys; parcel_id = COUNTY-MUN-BLOCK-LOT[-QUAL]
agents/base.py                ← BaseAgent: agent_runs bookkeeping, checkpoints, fault isolation
agents/scrapers/
  njgis_client.py             ← NJGIN MOD-IV parcel API client (queries, radius sales, municipality codes)
  sheriff_sale_agent.py       ← CivilView sheriff sales → parcels → SHERIFF_SALE / VACANT / ESTATE_OWNER
  nj_tax_assessment_agent.py  ← TaxAssessmentAgent (absentee/out-of-state) + CashBuyerAgent (mailing clusters)
  stale_listing_distress_agent.py ← homeharvest active listings: 90+ DOM, price cuts, distress keywords
  tax_sale_agent.py           ← RealAuction municipal tax lien sales (live if allowed, else saved-page import)
  owner_records_agent.py      ← imports county owner/deed exports, Newark 2020 snapshot; names buyers; flips
agents/enrichment/
  municipal_owner_layers.py   ← owner names from Jersey City / New Brunswick / Plainfield ArcGIS layers (PAMS_PIN join,
                                explicit outFields, skip records older than the last deed)
  listing_agent_enricher.py   ← listing-agent contacts (name, brokerage, phones, email) for stale MLS leads
  public_owner_resolver.py    ← foreclosure defendants → OWNER / HEIR_EXECUTOR / RELATIVE / DECEASED; mailing
                                cross-reference (owner lists, deed grantees); entity lookup links
  skiptrace_pipeline.py       ← export top N (CSV+HTML with manual people-search links), import CSV, paste parser
engine/
  market.py                   ← per-town sale/assessment ratio, median price, pre-1980 share (from NJGIN)
  equity.py                   ← purchase-money mortgage amortization → likely equity / free-and-clear
  comps.py                    ← public-record comps (equalized assessment ratio method) + percentile math
  listing_comps.py            ← homeharvest sold comps ($/sqft method) + subject record lookup, disk cache
  homeharvest_provider.py     ← normalized wrapper around homeharvest (throttled; ProviderBlocked fallback)
  rehab.py                    ← NJ rehab tiers ($25/$50/$85 per sqft + age bumps + roof/HVAC buffer)
  underwriter.py              ← ARV/as-is → tax drag → rehab → fee → MAO 70/75 → anchor → FRFPA → verdict
  scoring.py                  ← motivation / viability / priority with reasons (+ CRM engagement); strategy brief
  frfpa_classifier.py         ← occupancy facts → FRFPA track (exempt vs covered) + covered-deal monetization
  compliance.py               ← DNC (FTC 31-day data, internal, Daniel's Law), calling hours, can_call()
  scripts.py                  ← battle cards: playbooks A–E, objection matrix, never-say lists, resources
  contracts.py                ← LOI / PSA draft / assignment / JV / deal sheet → deals/<property_id>/ md+html+pdf, gated
  crm.py                      ← log_call, follow-up cadence, confirm_occupancy (re-price), record_contract, queues
  debrief.py / ai.py          ← rule-based post-call coach; optional Claude note (redacted payload, opt-in)
  buyer_matcher.py            ← top-5 recorded cash buyers per top deal (geography, price, units, rehab, recency)
web/app.py + web/static/      ← local cockpit (FastAPI, 127.0.0.1 only, Host/Origin guard): dashboard, battle station
swarm/orchestrator.py         ← `python -m swarm.orchestrator` (list | run | job | run-all | daemon | status)
swarm/self_improvement.py     ← audits health/coverage/spreads → logs/swarm_intelligence.md; tunes weights;
                                auto-adds high-spread towns (swarm_state.auto_expansion)
docs/nj_data_sources.md       ← verified data-source report (what works, how, limits, manual checklist)
docs/recon/                   ← raw recon: legal_rules.md, comps_listings.md, assessment_owner_deeds.md,
                                tax_sale_clerk_opendata.md, public_owner_layers.md, nj_owner_records_client.py
data/imports/                 ← operator-downloaded files (owner_lists/, deeds/, tax_sales/<Town>/) — git-ignored
data/exports/                 ← skip-trace queues (CSV/HTML) — git-ignored
deals/<property_id>/          ← generated LOI/PSA/assignment/JV/deal sheets — git-ignored (names sellers)
logs/                         ← swarm_intelligence.md (committed); *.log (ignored)
tests/                        ← pytest; parsers run against saved real pages in tests/fixtures/
wholesaling.db                ← SQLite database (git-ignored: contains names/addresses)
```

## Data-source access rules (learned 2026-10-26; details in docs/nj_data_sources.md)

- Automated: NJGIN parcels, CivilView sheriff sales, Census ZCTA, City of Newark ArcGIS snapshot,
  homeharvest (low volume, cached; Realtor.com ToS caveat).
- **Manual download → automatic import** (the agents parse the files): county tax-board owner/deed
  exports (taxrecords-nj.com: robots.txt `Disallow: /`), RealAuction tax-sale pages (firewall serves
  browsers only), Essex TaxDataHub (reCAPTCHA). OPRA requests to tax collectors are the scalable route.
- Never: impersonate a browser, solve/bypass CAPTCHAs, accept click-through terms for the operator, or
  crawl paths a robots.txt disallows. If a source starts refusing us, record it in agent_runs and fall back.
- Municipal ArcGIS owner layers (learned 2026-10-27, `docs/recon/public_owner_layers.md`): query only our
  leads' PAMS_PINs with an **explicit field list** — the Plainfield vendor layer carries raw SSN columns that
  must never be requested. Skip the Middlesex `mcgisportal` parcels service (looks like an accidental exposure).
- People-search sites (TruePeopleSearch, FastPeopleSearch, CyberBackgroundChecks) and entity search
  (NJ Business Name Search, OpenCorporates) are **manual links the operator clicks**; results come back via
  paste/CSV import. No scraping (ToS), no OpenCorporates API (key + share-alike licence). Voter-file lookups
  are not used (use restrictions).
- National DNC data is a manual download from the operator's FTC account (first 5 area codes free), loaded
  with `manage.py dnc --load`; it is stale after 31 days.

## NJ Legal & Market Rules (encode these in logic and scripts)

Operating rules, not legal advice — have a NJ real estate attorney review the contract templates
once. Full citations, statute text and open VERIFY items: `docs/recon/legal_rules.md`.

### Attorney Review (3 business days)
- Source: *N.J. State Bar Ass'n v. N.J. Ass'n of Realtor Boards*, 93 N.J. 470, modified 94 N.J. 449
  (1983); codified for licensees at N.J.A.C. 11:5-6.2(g). Covers 1–4 family / vacant one-family lot
  contracts **prepared by a licensee with a fee interest**; either party's attorney may disapprove
  for any reason.
- Count: delivery of the fully signed contract to **both** parties = day 0; the next 3 days that are
  not Sat/Sun/NJ legal holidays (N.J.S.A. 36:1-1, incl. Lincoln's Birthday, Good Friday, Election
  Day) end the period. Disapproval may go by fax, e-mail, personal delivery or overnight mail
  (*Conley v. Guerrero*, 2017). Code: `core/dates.py::nj_business_days_after()`.
- A wholesaler-as-principal contract is **not legally required** to have it — we include it
  voluntarily (protects the seller, reduces CFA exposure). Nothing is "binding", marketed to buyers,
  or charged for (title, inspection) until review ends with no disapproval.

### Tax Sale Certificates (N.J.S.A. 54:5) — verified, with corrections
- Interest is **bid down from 18%**; below 1% bidders bid a **premium** (earns no interest, returned
  on redemption). So 18% is the *maximum bid rate*, not a cap on the holder's return.
- On redemption the holder also gets a **redemption penalty on the certificate amount: 2% if >$200,
  4% if >$5,000, 6% if >$10,000** (54:5-61), interest on subsequent taxes at the town's delinquency
  rate (8% on first $1,500 / 18% above), and capped costs.
- There is **no redemption deadline**: the owner can redeem **until final judgment**. "2 years" is
  only the earliest a third-party holder may **file** foreclosure (municipality: 6 months; abandoned
  property: anytime). Judgments can be reopened only within 3 months (jurisdiction/fraud).
- Since **P.L.2024, c.39** (July 10, 2024) and *Roberto* (N.J. 2025), holders can't keep surplus equity;
  the owner may demand a sheriff sale/auction before final judgment to preserve equity.
- Why prime targets: equity-rich, cash-poor owners facing loss of the property; the certificate is
  redeemed from sale proceeds at closing. Aged certificates + stacked municipal liens = urgency.
  Always get the collector's certified redemption figure before offering.

### Judicial Foreclosure timeline (NJ is a judicial-foreclosure state)
1. **Notice of Intention to Foreclose** (Fair Foreclosure Act, N.J.S.A. 2A:50-56): 30–180 days before filing.
2. **Complaint + Lis Pendens** with the county clerk (N.J.S.A. 2A:15-6); **35 days** to answer (R. 4:6-1).
   Owner-occupied 1–4 family owners may use **Foreclosure Mediation**. Debtor may cure until judgment.
3. Uncontested → **Office of Foreclosure** → **Final Judgment** → **Writ of Execution**; the sheriff must
   hold the sale within **150 days** of the writ.
4. **Sheriff Sale** (salesweb.civilview.com). **Adjournments, N.J.S.A. 2A:17-36 as amended by P.L.2019,
   c.71 (eff. July 28, 2019): five total — 2 at the debtor's request, 2 at the lender's, 1 by
   agreement — each up to 30 calendar days**; more only by court order for cause. (Correction: the
   "two 28-day adjournments" figure has no statutory basis; 14 days was the pre-2019 rule. Court
   stays can use up the debtor's free adjournments — *Lazarus v. Tawil*, 2025.) Scripts never advise
   an owner on postponing a sale (that's a regulated "distressed property service").
5. After the sale: **10 days** to object/redeem (R. 4:65-5; 60 days with a bankruptcy filing). Surplus
   funds go to the owner by court motion. CWPP (P.L.2023, c.255) gives the owner/next of kin/tenant a
   right of first refusal at the upset price.
- Wholesale angle: listings give defendant, address, docket, plaintiff and **upset amount** (≈ payoff).
  Equity = value − upset − other liens. Typical NJ timeline from first notice ≈ 3–4.5 years.

### Foreclosure Rescue Fraud Prevention Act (N.J.S.A. 46:10B-53 to -68) — enforced in code
- Covers **owner-occupied 1–4 family homes in foreclosure or 90+ days delinquent**. Whether a plain
  wholesale purchase is covered is unsettled → **we treat these leads as covered**.
- Requirements if covered: notarized contract in **14-pt bold** with statutory notices; **10-business-day
  cancellation** (no deed signed before it ends); contract recorded within 10 business days; owner
  receives **≥ 82% of fair market value**; closing at an attorney/title office; lienholders notified.
  Penalties: treble damages, civil penalties, and it can be a crime.
- `engine/frfpa_classifier.py::classify()` decides coverage from occupancy facts. **Exempt**: not in
  foreclosure / not 1–4 family, entity owner (trusts are *not* exempt), confirmed tenant-occupied or vacant;
  **presumed exempt** (screening only): VACANT signal, absentee estate, absentee owner. **Covered**:
  presumed owner-occupied, estate with unknown occupancy (an heir living there is an owner-occupant),
  operator-confirmed owner/heir occupancy. Servicer tax-bill addresses are not evidence of absence.
- Covered leads are priced at ≥ 82% of as-is FMV. When 70% MAO is below that floor, `covered_track()` picks
  **WORKABLE_82_PCT_FLOOR** (a 75%-of-ARV buyer still leaves ≥ $10K), else **EQUITY_RESCUE_REFERRAL**
  (0.9·FMV − debt > $75K: refer to a listing agent/counselor — no referral fee while unlicensed,
  N.J.S.A. 45:15-3), else **FRFPA_BLOCKED**.
- Gates in `engine/contracts.py`: never generate a PSA for a covered lead (attorney drafts the statutory
  form); LOIs for covered leads ≥ the floor; a *presumed* exemption needs operator-confirmed occupancy
  (`crm.confirm_occupancy`, which re-prices) before any offer below the floor; no offer documents on
  EQUITY_RESCUE_REFERRAL / FRFPA_BLOCKED. `crm.record_contract` uses the later of attorney review and the
  10-business-day FRFPA cancellation before anything is marketed.

### Consumer Fraud Act & wholesale transparency (N.J.S.A. 56:8-1 et seq.)
- Unconscionable practices or misrepresentations → **treble damages + attorney fees** (56:8-19); AG
  penalties $10K/$20K. *D'Agostino v. Maldonado*, 216 N.J. 168 (2013): an "I buy houses" investor was
  liable under the CFA. Any violation of another law is a CFA violation in an AG action (2022 amendment).
- Every script, ad and contract says: we are **investors/principals, not licensed agents**; we **may
  assign the contract** to another investor; the seller should have an **attorney** review; no pressure.
  Never claim to be a cash buyer if we will assign; every marketing claim must be true and provable.
- Licensing risk: NJ has no wholesaling statute; repeatedly assigning contracts for fees can look like
  unlicensed brokerage (N.J.S.A. 45:15-1/-3: $5K/$10K per transaction; fee may be uncollectible).
  Bill **A4278 (2026, pending)** would require a license and a 30-day cancellation right. Market only
  the contract/equitable interest, only after attorney review ends; never pay unlicensed referral fees.
  Taking title (double close) is the most conservative structure.

### Calls, texts and personal data
- TCPA/TSR: call 8am–9pm recipient time; scrub against the National DNC registry (data ≤ 31 days old;
  first 5 area codes free, $85 each after); honor internal DNC requests within 10 business days and keep
  them 5 years; no prerecorded/AI/ringless voicemail to cell phones without consent. *Coffey v. Fast
  Easy Offer* (9th Cir. 2026): "we buy houses" outreach can be telemarketing.
- NJ (N.J.S.A. 56:8-119 et seq.): telemarketers must **register annually** with the Division of Consumer
  Affairs; within the first 30 seconds state name, company, phone number and purpose; no caller-ID spoofing.
- Enforced in code (`engine/compliance.py`, `engine/crm.py`): the cockpit shows a number as callable only
  inside 8am–9pm Eastern and not on any DNC list; numbers not covered by fresh FTC data are flagged
  "unscrubbed"; a DNC call outcome adds the number to the internal list immediately; Daniel's Law requests
  go in with source `daniels_law`. Listing agents' advertised numbers are B2B but still honor stop requests.
  Two-party-consent states (CA, FL, PA, MD, MA, WA, …): ask before recording. The optional AI coach sends
  only the town, deal numbers and redacted notes — never names, phones or street addresses.
- **Daniel's Law** (N.J.S.A. 56:8-166.1): on a nondisclosure request, stop disclosing that person's home
  address/phone (including mailing lists and vendor sharing) within **10 business days**; strict
  liability, $1,000+ per violation (N.J. Supreme Court, Aug. 2026). It is why NJGIN owner names are blank.

## Coding standards

- **No placeholders, mock data or TODO stubs in shipped code.** Every scraper is tested live from
  the terminal before it is called done. Parsers have pytest tests against saved real HTML/JSON
  fixtures in `tests/fixtures/`.
- **Free public sources first** (NJGIN MOD-IV, CivilView sheriff sales, county tax board records).
  When a site blocks automated access (403/Cloudflare/CAPTCHA), **pivot to another public source**;
  do not build bot-detection evasion. Respect robots.txt and terms of use, identify ourselves in the
  User-Agent, rate-limit per host (`core/http.py`), and cache responses on disk.
- **Idempotent ingestion.** Every record has a natural key (parcel_id, sheriff_number, source+ref).
  Re-running an agent updates rather than duplicates. Use `db/repository.py` helpers.
- **Explainable scoring.** Every score stores its component breakdown (`score_breakdown_json`)
  so the briefing can say *why* a lead ranks high.
- Python 3.11+, type hints, `from __future__ import annotations`, small pure functions for math
  (easy to test), SQLAlchemy 2.0 typed ORM. Keep settings in `config/settings.yaml`, secrets in `.env`.
- Never commit `wholesaling.db`, caches or exports (personal data). `logs/swarm_intelligence.md`
  is committed because it holds no PII.

## The autonomous agent runner

- Every agent subclasses `agents.base.BaseAgent` and implements `run(ctx: AgentContext) -> None`.
  `BaseAgent.execute()` wraps it: opens an `agent_runs` row, records counts/errors/notes,
  never lets one agent's crash stop the swarm.
- `swarm/orchestrator.py` holds the registry (`AGENTS`), dependency order and cron schedules
  (from `config/settings.yaml`). Modes:
  - `python -m swarm.orchestrator run-all` — run the full pipeline once (scrape ∥ → owner enrichment →
    contact enrichment [municipal_owners → listing_agents → owner_resolver] → score → underwrite → re-score →
    buyer_matcher → self-improve).
  - `python -m swarm.orchestrator run <agent>` — run one agent.
  - `python -m swarm.orchestrator daemon` — APScheduler loop using the cron schedules.
  - `python -m swarm.orchestrator status` — recent runs and health.
- The **Self-Improvement & Alpha Discovery Agent** runs last in every pipeline: audits scraper
  success rates, data coverage, contact readiness (top-100 leads with a callable phone), buyer-match
  coverage, DNC freshness, call outcomes, comp coverage and spread viability, writes findings to
  `logs/swarm_intelligence.md`, tunes the scoring weights stored in `scoring_weights`,
  and proposes adjacent municipalities with better spread-to-ARV ratios.

## Common commands

```bash
pip install -e ".[dev]"                     # install
python manage.py init                       # create/upgrade the database
python -m swarm.orchestrator run-all        # full live pipeline (scrape ∥ → enrich → score → underwrite → re-score → improve)
python manage.py briefing                   # daily action briefing (top 15 leads)
python manage.py underwrite --top 15        # (re)underwrite the top leads
python manage.py lead <id>                  # full detail for one lead
python manage.py server                     # CRM cockpit → http://127.0.0.1:8000
python manage.py queue                      # today's dial list (follow-ups first)
python manage.py script <id>                # pre-call battle card (playbook auto-chosen)
python manage.py call <id> --outcome SPOKE_NEEDS_OFFER --occupancy VACANT --ask 300k
python manage.py docs <id> --type loi       # LOI / psa / assignment / jv / deal_sheet / all (gated)
python manage.py skiptrace --export-top 50  # then --import <csv> or --lead <id> --paste <file>
python manage.py dnc --load <ftc_file>      # National DNC data (re-load every 31 days)
python manage.py match                      # top-5 cash buyers for the top 50 deals
pytest                                      # tests
```
