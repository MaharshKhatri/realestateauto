# CLAUDE.md — System Brain for the NJ Wholesaling Engine

This file is the permanent architectural memory for this repository. Read it at the start of
every session. Keep it current: when you change architecture, data sources, or legal rules,
update this file in the same commit.

## Mission

An autonomous, zero-budget deal-finding and underwriting system for a New Jersey real estate
wholesaler (18-year-old operator, **laptop and phone only — no car**, no paid data). The software does
the data harvesting, distress detection, owner lookup, comps, ARV/MAO underwriting, lead ranking,
outreach drafting and disposition prep. The operator spends their time on the phone and e-mail with
listing agents, sellers and buyers, negotiating and closing.

Target counties (priority order): **Middlesex, Union, Essex, Hudson, Passaic, Bergen, Camden, Mercer, Monmouth**
(Mercer and Monmouth added in Phase 5: Trenton, Hamilton, Ewing; Asbury Park, Neptune, Long Branch, Freehold).
Target outcome: assignment fees of $15K–$40K per deal.

## 100% Virtual / No-Car Operation Protocol (operator update 2026-10-27)

The operator works only from a laptop and phone. Nothing in code, scripts or coaching may require
driving for dollars, door-knocking or an in-person visit by the operator.

- **Condition & occupancy, remotely:** listing photos/description, Google Street View + satellite links,
  FEMA flood and NJ parcel links, and a photo / 3-minute video-walkthrough request to the seller,
  tenant or listing agent (`engine/virtual_inspector.py`). Recorded answers go to `crm.confirm_occupancy`.
- **Reaching owners without a phone:** letters (`engine/direct_mail.py`, `manage.py mail-merge`) and the
  listing agent when the house is listed (stacked foreclosure + listing → agent phone, no skip trace).
- **Listing agents first:** B2B offers on stale/distressed listings are the default first call of the day
  (`READY_TO_CALL` queue, `engine/agent_outreach.py`); they need no skip-tracing and no driving.
- **Buyers are the boots on the ground:** during the inspection period a matched cash buyer / JV partner
  walks the property with the listing agent (lockbox showing) or seller and submits a bid
  (`engine/disposition.py`, `buyer_bids`). Introduce them **truthfully** — "my buying partner / the
  investor I may assign the contract to, as our agreement allows" — never as a "contractor" or
  "renovation crew" when they are the assignee (Consumer Fraud Act: no misrepresentation). Our PSA lets
  "Buyer and its contractors, lenders and assignees" inspect, and nothing is marketed before attorney
  review ends.
- Wording to avoid in generated text: "drive by", "door-knock", "stop by the house", "I'll come see it".

## Sprint Master Architecture

| Sprint | Scope | Status |
|---|---|---|
| **1. Data & Valuation Swarm** | DB layer, live NJ scrapers (sheriff sales, MOD-IV absentee/out-of-state, mailing-cluster cash buyers, stale listings, tax-sale import), owner/deed import + flip detection, comps/ARV/MAO underwriter with NJ tax drag + FRFPA floor, explainable scoring, swarm orchestrator, self-improvement log, daily briefing CLI | **Done (Phase 1)** |
| **2. Contacts, FRFPA Tracks, Call Scripts, Deal Docs & CRM Cockpit** | Contacts table + enrichment (listing agents, foreclosure defendants/heirs/executors, municipal ArcGIS owner layers, mailing cross-reference, entity lookup links), skip-trace export/paste/import, DNC guard (FTC/internal/Daniel's Law, calling hours), FRFPA occupancy classifier + covered-deal tracks (82% floor / equity-rescue referral), battle cards (playbooks A–E, 12+ objections), LOI/PSA/assignment/JV/deal-sheet generator with compliance gates, CRM (call log → stage → follow-up → rescore → coach), local web cockpit, buyer matcher, contact-readiness audit | **Done (Phase 2)** |
| **3. Virtual B2B Deal Machine, Signal Stacking, Virtual Inspection & Remote Disposition** | PII scrub of tracked files (`pii-scan`), all stale listings underwritten (listing-only condos via MLS type), list-to-MAO gap + striking distance, READY_TO_CALL queue, listing-agent LOI blast (email/SMS/LOI, SMTP 25/day, 48h follow-up), signal stacker (listed distress, portfolio owners, tax-sale local match) with [Nx STACKED] badges, virtual inspector dossier, direct-mail merge (Click2Mail CSV, Daniel's Law suppression), buyer hotzones, remote walkthroughs + buyer_bids → assignment/JV auto-fill, briefing A/B/C, four cockpit tabs | **Done (Phase 3)** |
| **4. Sanity Guard, Listed-Foreclosure Fast Track, Closing Coordinator, Inbound Funnel & Daily Execution** | Comp hygiene + ARV outlier guard (package deeds, price/$-sqft/ratio bands, ask/assessment/$-sqft caps, non-residential PASS) and the `outlier_guard` agent over every valuation; listed-foreclosure sniper queue with honest tracks + urgent agent packets, portfolio-owner scripts/letters; NJ contract-to-close milestones, sourced title/attorney directory, title order e-mail, ledger + 28% tax reserve; `site/` seller + buyer funnel with consent capture and inbound ingest; `manage.py today` + cockpit Today home + `PLAYBOOK.md` | **Done (Phase 4)** |
| **5. Command Center, Guided Autopilot, Human Copy, Clerk Filings & OPRA, Mercer/Monmouth** | `engine/human_copy.py` (plain 3-5 sentence e-mails, disclosures under `--- Offer Summary ---`, linter) across agent / fast-track / buyer / inbound / inspector messages; command center (KPI pills, My Setup → settings.local.yaml + .env, one-click swarm refresh with progress, step-by-step deal cards with send/outcome logging, kanban); desktop launchers; lis pendens agent (Union UCPA, Essex PRESS, Monmouth OPRS live + drop-in imports, Newark snapshot name lookup); OPRA requests + tax-list importer + Records page; Mercer/Monmouth harvest; GitHub Pages deploy behind `build-site --check` | **Done (Phase 5)** |
| **6. Quant Pricing, Funding Partners, Two-Step Funnel, Reply Negotiator, Item 14 & Double Close, Fast Skip-Trace** | `engine/quant_pricing.py` (end-buyer solve: ≥15% ROI and ≥$45K after rehab + contingency, closing, 6-month taxes/carry/capital, commission + NJ RTF/graduated fee; alpha ∈ [0.68, 0.80] calibrated from 1,162 recorded investor purchases → `config/market_calibration.yaml`; buyer_max / mao_target / mao_walkaway / opening_offer on every valuation; `manage.py calibrate` / `recalibrate`); `engine/jv_partner_vault.py` (jv_partners seeded from the top 30 named repeat buyers, Phase 0 cards, buy-box matching, proof-of-funds upload → "verified local capital partner" line); two-step funnel (Touch 1 opener, 48-h nudge, Touch 2 offer on reply; urgent listed foreclosures skip it); `engine/reply_negotiator.py` (paste the agent's reply → intents, counter, risk buffers, GREEN/YELLOW/RED/INFO, 3-sentence reply, AGENT_REPLIED + follow-up); `engine/deal_structure.py` (Section 37 / "Item 14" attorney-review rider with CPEA disclosure + tank-sweep inspection, Correa acknowledgment in the assignment, disclosed single-purpose-LLC option, assignment vs double-close net check); `engine/clipboard_skiptrace.py` (queue → manual links → one-click paste → DNC scrub → best mobile → READY_TO_CALL); cockpit: first-touch toggle, paste-reply panel, 🤝 Partners & POF, ⚡ Skip Trace, quant breakdown, structure card | **Done (Phase 6)** |
| **7. Google Calendar Auto-Connector & Real-Time Event Sync** | `engine/gcal_sync.py` (OAuth Desktop client → `setup-calendar` once → `config/token.json`; `calendar_events` outbox with natural keys = deterministic Google event ids); 🔥 agent follow-ups exactly 48 h after each e-mail (moved into calling hours), 📞 call-backs, ⚠️ attorney review 5 PM on the 3rd NJ business day (+ FRFPA cancellation end), 📋 closing milestones, 🚨 priority sheriff sales that move with adjournments, 🎯 10:00 daily goal; SQLAlchemy commit hooks → background push in the cockpit / CLI / swarm, offline queue, two-way pull (deletes respected, moved follow-ups move the CRM, legal dates restored); `sync-calendar`, `calendar_sync` swarm agent, 📅 cockpit button, `/leads/<id>` links | **Done (Phase 7)** |
| **8. True Zero-Click Autonomous Mode** | `engine/gmail_autopilot.py`: the My Setup Gmail address + app password read agent replies over IMAP (INBOX read-only, BODY.PEEK, only new UIDs, bodies downloaded only for matched mail: thread headers → known agent address → street in the subject), quoted history stripped, out-of-office / bounce handling, `inbound_replies` + `interactions.email_message_id`; a plain "still available" sends Touch 2 + LOI by itself in the agent's thread (In-Reply-To / References) after a human-paced delay inside send hours; everything else waits in 🔥 Action Needed for one click (`approve()`); 🚀 paced daily batch (25-60 s apart, 25 / 24 h); 5-minute background poller + `gmail_replies` swarm agent + `manage.py gmail`; actionable-only deck (agent e-mail or callable phone; Phase 0 partners only when reachable, the rest in Skip Trace); ✅ Interested turns the card into Touch 2; 🔥 calendar alert for replies; simple home (one button, live counts, Action Needed, optional card player) with the 12 tabs under Advanced Tools ▾ | **Done (Phase 8)** |
| **8½. Claude-Native Operation** | No dashboard needed: `.claude/commands/` (/inbox, /daily, /status, /reply) drive `manage.py inbox` / `gmail` from a Claude Code chat on the laptop; `autopilot-daemon` + `install-daemon` (macOS launchd) run the inbox hands-free 8 AM-9 PM; the 25/day cap is cold-only (`engaged` replies use `reply_daily_limit`); Gmail Sent is imported as the record (`sync_sent`); Touch 2 answers "buyer or realtor?", "want to see it?", "call me", backup offers; EXPIRED; wrong-parcel listings on HOLD | **Done** |
| **8¾. Firm Offers & 100 Calls a Day** | `engine/offer_math.py` (one firm number: buyer max − $10K, best and final buyer max − $5K, firm / ask-first / skip tiers, certainty checks, no re-trade, live firm-up), negotiator one move and no-number far-apart answers, `PRICING_VERSION` re-price in the cloud, `engine/call_sheet.py` (100 calls in four frozen blocks fitted around classes, chat codes via sealed notes), 40 cold e-mails / day in two opener waves, wider listing harvest (104 ZIPs) | **Done** |
| **9. Lead Sources & Learning at Scale** | NJ permits (demolition/alteration → flipper + condition signals), paid skip-trace provider adapters, older municipal owner layers with deed-match rule (Middlesex 2022, Passaic City 2023, Essex/Passaic 2017), Mercer sheriff list import once its format is verified, outcome-driven weight re-fits once calls are logged | Next |
| **10. Disposition at Scale** | Buyer list growth (more county deed exports + registered web buyers), deal sheets to matched buyers after attorney review, attorney-reviewed templates replacing the drafts, proof-of-funds tracking on bids | Planned |

## Directory structure

```
CLAUDE.md                     ← you are here
.claude/commands/             ← /inbox, /daily, /status, /reply: the operator runs the desk from a Claude Code chat on the
                                laptop (no web dashboard); Claude Code asks once per manage.py command ("always allow")
manage.py                     ← operator CLI (rich): briefing (A ready-to-call / B sheriff+estate spreads / C stacked),
                                lead, underwrite, buyers, stats, server, queue, script, call, occupancy, contract, docs,
                                skiptrace, dnc (+ --suppress-address), match, pii-scan, agent-blast, mail-merge,
                                inspect, stack, buyer-hotzones, walkthrough, bid, today, fast-track, closing, finance,
                                partners, inbound, site, build-site, install-shortcut, calibrate, recalibrate, vault,
                                skiptrace-fast, reply, structure, preflight, db-sync, setup-calendar, sync-calendar,
                                gmail, inbox, call-sheet, autopilot-daemon, install-daemon, swarm passthrough
Launch_Dashboard.command / launch_dashboard.sh / Launch_Dashboard.bat ← double-click launchers (generated by
                                scripts/install_desktop_shortcut.py; path-independent): start the server if needed,
                                wait for /api/health, open the browser
PLAYBOOK.md                   ← operator's step-by-step guide: setup, daily 2-hour routine, first agent calls, contract →
                                attorney review → assignment → paid, taxes
config/settings.yaml          ← all tunables (counties/towns, MAO %, fees, rehab $/sqft, scoring weights, cron)
config/sources/               ← source directories (realauction_nj.yaml: 91 tax-sale sites in target counties)
config/opra_towns.yaml        ← OPRA channel per town (clerk e-mail / portal / call first, source URL each) + the
                                statutory rules and GRC precedents the requests rely on
config/nj_closing_partners.yaml ← NJ title agencies / closing attorneys checked on their own pages (source URL each),
                                county → region, verification resources, quarterly-tax dates and rates (with sources)
config/nj_deal_costs.yaml     ← NJ RTF brackets + graduated fee (P.L.2025 c.69, seller pays; Treasury schedules cited),
                                end-buyer economics (ROI/profit floors, closing, holding, capital), alpha band, fee targets,
                                double-close and negotiation buffers
config/credentials.json / token.json ← the operator's Google OAuth Desktop client and saved sign-in (git-ignored;
                                token chmod 600); tests point both at paths that never exist
config/market_calibration.yaml ← `manage.py calibrate`: per-town / county / statewide lower-quartile of investor purchase
                                price ÷ equalized value + investor share of sales (aggregates only, no parcels or names)
core/                         ← mail.py (Gmail TLS context from certifi, app-password cleanup, plain-English SMTP / IMAP
                                errors), config loader, polite HTTP client (per-host rate limit, retries, disk cache,
                                host stats), text/address utils (postal_title), NJ business days, geo, ZIP (ZCTA)
                                resolver; pii_scan.py (DB names in git-tracked files → `manage.py pii-scan`)
db/database.py                ← engine/session factory, create + additive migrations, init lock (thread-safe)
db/models.py                  ← ORM: properties, distress_signals, valuations, leads (+ occupancy, FRFPA track, CRM
                                fields, ready_to_call), contacts, interactions, dnc_entries, mail_suppressions,
                                buyer_matches, buyer_bids, closing_milestones, financial_ledger, generated_documents,
                                cash_buyers (+ web registration fields), jv_partners, deed_transfers, listings, agent_runs,
                                scoring_weights, swarm_state, calendar_events, inbound_replies (agent e-mails read from
                                Gmail); interactions.email_message_id threads replies to their lead
db/repository.py              ← idempotent upserts keyed on natural keys; parcel_id = COUNTY-MUN-BLOCK-LOT[-QUAL]
db/snapshot.py                ← `manage.py db-sync`: VACUUM INTO → xz → AES-256-GCM (PBKDF2-HMAC-SHA256, 600k) →
                                data/snapshots/ (< 45 MB parts + manifest); unpack verifies tag, SHA-256, quick_check;
                                ensure_database() (server + launchers) restores a missing / empty DB on a fresh clone
agents/base.py                ← BaseAgent: agent_runs bookkeeping, checkpoints, fault isolation
agents/scrapers/
  njgis_client.py             ← NJGIN MOD-IV parcel API client (queries, radius sales, municipality codes)
  sheriff_sale_agent.py       ← CivilView sheriff sales → parcels → SHERIFF_SALE / VACANT / ESTATE_OWNER
  nj_tax_assessment_agent.py  ← TaxAssessmentAgent (absentee/out-of-state) + CashBuyerAgent (mailing clusters)
  stale_listing_distress_agent.py ← homeharvest active listings: 60+ DOM, price cuts, distress keywords in the
                                `stale_listings:` ZIPs (+ auto-expansion towns); retires listings that left the market
  tax_sale_agent.py           ← RealAuction municipal tax lien sales (live if allowed, else saved-page import)
  owner_records_agent.py      ← imports county owner/deed exports, Newark 2020 snapshot; names buyers; flips
  lis_pendens_agent.py        ← clerk lis pendens / foreclosure filings: Union UCPA, Essex PRESS, Monmouth OPRS live;
                                data/imports/lis_pendens/<county>/ (saved .html / .csv) for the rest → clerk_filings →
                                LIS_PENDENS (+ ESTATE_OWNER) by block/lot, address or unique owner-name match
agents/enrichment/
  municipal_owner_layers.py   ← owner names from Jersey City / New Brunswick / Plainfield ArcGIS layers (PAMS_PIN join,
                                explicit outFields, skip records older than the last deed)
  listing_agent_enricher.py   ← listing-agent contacts (name, brokerage, phones, email) for stale MLS leads
  public_owner_resolver.py    ← foreclosure defendants → OWNER / HEIR_EXECUTOR / RELATIVE / DECEASED; mailing
                                cross-reference (owner lists, deed grantees); entity lookup links
  skiptrace_pipeline.py       ← export top N (CSV+HTML with manual people-search links), import CSV, paste parser
  signal_stacker.py           ← listed distress (foreclosure/absentee/tax lead also on the MLS → ACTIVE_LISTING + agent),
                                portfolio owners (shared mailing / entity name → PORTFOLIO_OWNER), condo-unit ambiguity
engine/
  market.py                   ← per-town sale/assessment ratio, median price, pre-1980 share (from NJGIN)
  equity.py                   ← purchase-money mortgage amortization → likely equity / free-and-clear
  comps.py                    ← public-record comps (equalized assessment ratio method) + percentile math;
                                CompHygiene (package deeds, price / $-sqft / sale-to-assessment bands)
  listing_comps.py            ← homeharvest sold comps ($/sqft method) + subject record lookup, disk cache
  homeharvest_provider.py     ← normalized wrapper around homeharvest (throttled; ProviderBlocked fallback)
  rehab.py                    ← NJ rehab tiers ($25/$50/$85 per sqft + age bumps + roof/HVAC buffer)
  underwriter.py              ← ARV/as-is → outlier guard → tax drag → rehab → fee → MAO 70/75 → anchor → FRFPA →
                                verdict; listing_gap (ask ÷ MAO, striking distance, ARV-vs-ask valuation conflict)
  valuation_guard.py          ← OutlierGuardAgent: re-cleans stored comps + subject guard on every valuation offline;
                                live re-underwrite for thin comp sets (COMPS_NEED_REFRESH)
  fast_track.py               ← listed foreclosures (sheriff sale + MLS): honest tracks, urgent agent script/packet,
                                countdown; portfolio owners: summaries, multi-address script, one letter per owner
  closing_coordinator.py      ← 8 NJ contract-to-close milestones (+ FRFPA recording), business-day deadlines, review
                                gate, disapproval, title order e-mail, partner picker, ClosingAlertsAgent
  finance.py                  ← ledger (income/expense categories), pipeline weighting, 28% reserve, quarterly dates
  inbound.py                  ← web seller / buyer submissions (local API, Formspree/webhook export, mailto text) →
                                lead + consent + underwrite + reply draft / cash_buyers + matching; site config
  today.py                    ← the 2-hour daily checklist (deadlines, 4 blocks) for CLI + cockpit Today tab
  human_copy.py               ← plain e-mail/SMS composition (subject variants, signature, Offer Summary block) + lint
                                (forbidden phrases, em-dashes, markdown, bullets, >110 words above the sign-off)
  autopilot.py                ← step-by-step deal deck (listed foreclosures → due follow-ups → MLS fixers), what/why/
                                action, send (SMTP / mailto / SMS) with edits, outcome buttons, daily goal, KPI pills
  offer_math.py               ← the one firm offer per lead (offer / best and final / tier / certainty checks / locked / firm_up)
  call_sheet.py               ← the daily 100-call sheet: tiers, frozen codes, block script, apply_codes
  quant_pricing.py            ← buyer-implied pricing: RTF / graduated fee, resale costs, buyer P&L, closed-form buyer_max
                                solve, local alpha from the calibration file, quant_offer (buyer_max, walk-away, target,
                                opening), counter_economics, harvest/aggregate/calibrate, recalibrate_all + metrics
  jv_partner_vault.py         ← jv_partners: seed from named repeat buyers (velocity), Phase 0 outreach (plain e-mail /
                                SMS / script), buy-box best_partner, POF upload (data/partners/, chmod 600), pof_language
  reply_negotiator.py         ← agent reply → amounts/intents (counter, available, under contract, POF, bidding war,
                                oil tank, tenants, CO, opt-out) → traffic light vs buyer_max/walk-away (risk buffers) →
                                3-sentence reply + SMS → AGENT_REPLIED, counter, stage, follow-up
  deal_structure.py           ← Item 14 / Section 37 attorney-review rider (assignment, CPEA disclosure, inspection +
                                tank sweep, access, investor disclosure), Correa acknowledgment, disclosed LLC option,
                                compare_structures (assignment vs same-day double close, RTF on leg B, C-9600)
  partner_principals.py       ← partner mailing-address families, likely principals (evidence + confidence), rank,
                                lookup links (people search by name/address, OpenCorporates, NJ business search)
  preflight.py                ← GO / VERIFY / HOLD per deck card, comp talking points, cheat sheet
  gcal_sync.py                ← Google Calendar: OAuth setup (credentials.json → token.json), desired events per lead
                                (follow-up / call-back / attorney review / FRFPA / milestones / sheriff sale / daily
                                goal), calendar_events outbox, pull → reconcile → push, commit hooks + background
                                worker, CalendarSyncAgent
  gmail_autopilot.py          ← Phase 8: read-only IMAP poller (new UIDs, headers first, bodies only for matched mail),
                                match (thread / agent e-mail / street), strip quoted text, auto-reply + bounce handling,
                                reply_negotiator → auto_queued Touch 2 (in-thread, LOI, human delay, send hours) or
                                needs_approval → approve() / dismiss(); paced outbound batch (pick_cards, start_campaign,
                                25-60 s, 25 / 24 h); background poller; status() for the simple home; GmailRepliesAgent
  clipboard_skiptrace.py      ← Fast Skip-Trace Mode: queue (exempt estates → vacant sheriff → lis pendens → absentee),
                                parse pasted people-search pages (never fetched), rank, DNC scrub, save, READY flip
  operator_setup.py           ← My Setup: validate + write settings.local.yaml and .env (password never returned)
  opra_generator.py           ← OPRA tax-list requests (commercial-purpose certified), sent / due / received tracking
  tax_list_import.py          ← town tax-delinquent spreadsheet (CSV/XLSX) → TAX_DELINQUENT signals by block/lot
  site_build.py               ← static funnel build + publish gate (operator details, disclosures, no foreign contacts)
  scoring.py                  ← motivation / viability / priority with reasons (+ CRM engagement, stack bonus,
                                striking-distance boost); ready_to_call(); strategy brief
  briefing.py                 ← briefing sections shared by CLI + cockpit (ready, high-spread, stacked, counts)
  agent_outreach.py           ← B2B listing-agent packets: two cash options, unrepresented-buyer language, LOI, mailto/
                                SMS copy/SMTP (25/day), OFFER_SENT_TO_AGENT + 48h follow-up, opt-out → DNC
  virtual_inspector.py        ← Street View/satellite/FEMA/listing links, condition flags from MLS text, NJ remote
                                checklist, photo / video-walkthrough request messages
  direct_mail.py              ← pre-auction / estate / landlord letters → deals/mail_batches/<ts>/ (PDF, HTML,
                                Click2Mail CSV, manifest); proof watermark; suppression; mark_mailed
  disposition.py              ← buyer hotzones (town + product + price band), walkthrough window/scripts, buyer_bids
                                (invite → walked → bid → accepted/backup), accept → assignment / JV auto-fill
  frfpa_classifier.py         ← occupancy facts → FRFPA track (exempt vs covered) + covered-deal monetization
  compliance.py               ← DNC (FTC 31-day data, internal, Daniel's Law), calling hours, can_call()
  scripts.py                  ← battle cards: playbooks A–E, objection matrix, never-say lists, resources
  contracts.py                ← LOI / PSA draft / assignment / JV / deal sheet → deals/<property_id>/ md+html+pdf, gated
  crm.py                      ← log_call, follow-up cadence, confirm_occupancy (re-price), record_contract, queues
  debrief.py / ai.py          ← rule-based post-call coach; optional Claude note (redacted payload, opt-in)
  buyer_matcher.py            ← top-5 recorded cash buyers per top deal (geography, price, units, rehab, recency)
web/app.py + web/static/      ← local cockpit (FastAPI, 127.0.0.1 only, Host/Origin guard): 🏠 Home (Phase 8: 🚀 one
                                button, live counts, 🔥 Action Needed approvals, optional card player; /api/simple/*,
                                /api/replies/<id>/approve|dismiss); everything below sits under "Advanced Tools ▾":
                                🎯 Step-by-Step Deck (#/autopilot: command bar with KPI pills / My Setup / swarm
                                refresh / OPRA, step-by-step player, kanban),
                                🏛️ Records (OPRA, uploads, clerk filings), 🤝 Partners & POF (vault), ⚡ Skip Trace,
                                Today, dashboard,
                                battle station, Fast Track, Ready to Call, Agent LOI Blast, Inspector & Mail,
                                Walkthroughs & Bids, Closing & Money; serves site/ at /site/* (web.public_hosts may
                                reach only /site/* and POST /api/inbound/*)
site/                         ← static seller page + cash-buyer registration (site.js reads generated config.js;
                                `manage.py site --build` for GitHub Pages / Netlify); config.js + site_dist/ ignored
swarm/orchestrator.py         ← `python -m swarm.orchestrator` (list | run | job | run-all | daemon | status)
swarm/runner.py               ← dashboard refresh: quick (offline re-analysis) | full (daily pipeline) in a background
                                thread, one at a time, per-step progress for GET /api/swarm/status
swarm/self_improvement.py     ← audits health/coverage/spreads → logs/swarm_intelligence.md; tunes weights;
                                auto-adds high-spread towns (swarm_state.auto_expansion)
docs/nj_data_sources.md       ← verified data-source report (what works, how, limits, manual checklist)
docs/recon/                   ← raw recon: legal_rules.md, comps_listings.md, assessment_owner_deeds.md,
                                tax_sale_clerk_opendata.md, public_owner_layers.md, nj_owner_records_client.py
data/imports/                 ← operator-downloaded files (owner_lists/, deeds/, tax_sales/<Town>/) — git-ignored
data/exports/                 ← skip-trace queues (CSV/HTML) — git-ignored
data/snapshots/               ← the ENCRYPTED database snapshot (wholesaling.db.xz.enc[.partNN] + manifest.json) —
                                git-ignored while the repository is public (sent to the laptop as a private download)
deals/<property_id>/          ← generated LOI/PSA/assignment/JV/deal sheets — git-ignored (names sellers)
deals/agent_blast/, deals/mail_batches/ ← agent review pages and letter batches — git-ignored
logs/                         ← swarm_intelligence.md (committed); *.log (ignored)
scripts/install_desktop_shortcut.py ← writes the three launchers + a Desktop copy (`manage.py install-shortcut`)
.github/workflows/            ← ci.yml (pytest); desk.yml (the cloud desk, self-chaining) + night.yml (overnight hand-off:
                                5:17 AM refresh, 7:00 start) + kicker.yml (cron net, unreliable here); deploy_site.yml (GitHub Pages: pii-scan + site tests + build-site
                                --check, from repository variables; skipped until SITE_FORM_ENDPOINT is set)
tests/                        ← pytest; parsers run against saved real pages in tests/fixtures/ (clerk/ = UCPA,
                                PRESS, OPRS pages with every party name replaced by an invented one)
wholesaling.db                ← SQLite database (git-ignored: contains names/addresses)
```

## Data-source access rules (learned 2026-10-26; details in docs/nj_data_sources.md)

- Automated: NJGIN parcels, CivilView sheriff sales, Census ZCTA, City of Newark ArcGIS snapshot,
  homeharvest (low volume, cached; Realtor.com ToS caveat). A refusal (403 → `ProviderBlocked`) is honored: the
  process sends Realtor.com nothing for `stale_listings.blocked_cooldown_hours` (`homeharvest_provider.mark_blocked`,
  stored in swarm_state `listing_provider_blocked` for the next process), and listing comps fall back to public records.
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
- County clerks (learned 2026-10-28, `docs/nj_data_sources.md` §6.2): Union UCPA, Essex PRESS and Monmouth OPRS
  are plain HTTP forms with no robots.txt and no CAPTCHA → automated, one search per document type per run.
  Passaic PRESS (robots `Disallow: /`), Hudson Acclaim (click-through terms), Bergen / Middlesex / Camden BrowserView
  (reCAPTCHA) and Mercer (login modal) are manual: the operator saves results into data/imports/lis_pendens/.
  A filing becomes a signal only on block/lot, address or a **unique** owner-name match — never on a guess; Newark
  name lookups in the City's 2020 snapshot count only when the deed book/page is unchanged in today's MOD-IV.
- Mercer sheriff sales are not on CivilView and mercercounty.org returns Akamai 403 to automated requests
  (robots.txt included): manual, never worked around.
- People-search pages in Fast Skip-Trace Mode (`engine/clipboard_skiptrace.py`, learned 2026-10-29): the operator opens the
  manual link, copies the page and pastes it; the code parses the clipboard text only and never requests those sites.
- OPRA (`engine/opra_generator.py`): the operator sends each request; every request certifies the commercial
  purpose (true for house buying, required by 47:1A-5(f)); one agency per request; follow the town's own channel
  (portal-only towns get the text to paste); never OPRAmachine (it publishes replies with owner names).

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

### Phase 3 operating rules (virtual B2B machine) — enforced in code
- **READY_TO_CALL** (`scoring.ready_to_call`): an underwritten lead with a callable contact (phone not on a DNC
  list, or a listing agent's e-mail) and an offer path. Listed leads must be within striking distance
  (75% MAO ≥ 75% of ask, no ARV-vs-ask conflict > 1.6×); unlisted leads need a deal verdict. Never
  EQUITY_RESCUE_REFERRAL / FRFPA_BLOCKED / SHORT_SALE_ONLY.
- **Listing-agent offers** (`engine/agent_outreach.py`): we are an *unrepresented buyer*; the agent may act as a
  disclosed dual agent or transaction broker only as the listing agreement and NJ rules allow — never imply we
  pay them or ask for part of their commission. Offers never exceed ask; the anchor is ≤ 90% of ask; listings
  below 75% ask-to-ceiling are not contacted. Every e-mail carries the investor/assignment disclosure, the
  operator's postal address (CAN-SPAM — SMTP refuses to send without it) and an opt-out line; ≤ 25 personalized
  e-mails per rolling 24 h; SMS is copied and sent by the operator, never automated. FRFPA-gated or
  occupancy-unconfirmed foreclosures get a no-price *inquiry*. An opt-out puts the agent on the internal DNC list.
- **Stacking**: distinct signal families (foreclosure, listing, absentee, tax, estate, vacant, code, portfolio)
  add +12 / +18 / +25 motivation for 2 / 3 / 4+ families; portfolio owners group only on absentee mailing
  addresses (no servicer, city-hall or care-of mailings) and entity names — never on a person's name alone.
- **Direct mail** (`engine/direct_mail.py`): letters never promise to stop, postpone or negotiate a foreclosure;
  they list HUD counseling / Legal Services of NJ, the investor + assignment disclosure, attorney review and
  (for an occupied home) the FRFPA protections; leads we can already call are skipped; suppressed addresses
  (`dnc --suppress-address`, Daniel's Law source `daniels_law`) are never mailed; without operator name, phone
  and postal address a batch is a watermarked PROOF that `--mark-mailed` refuses.
- **Remote disposition** (`engine/disposition.py`): no buyer invitation, walkthrough or bid before the seller
  contract's attorney review (and, if FRFPA-covered, the 10-business-day cancellation) has ended. Buyers are
  introduced as "my buying partner — the investor I may assign the contract to"; they may bring their own
  contractor. Walkthroughs start the next business day (tenant notice), one shared block per house.
  `bid_amount` is the buyer's total price (contract + fee); a bid below our contract price can't be accepted;
  accepting fills the assignment (or JV) with the buyer's legal name — unnamed mailing-cluster buyers must
  give one first; other open bids become backups.

### Phase 4 operating rules — enforced in code
- **Valuation guard** (`engine/comps.py`, `engine/underwriter.py::outlier_guard`, `engine/valuation_guard.py`):
  comps drop package deeds (one price + deed date on 2+ parcels — NJGIN's sales code shows "A" on them, so it
  can't be trusted), prices outside $40K–$2.5M (cap lifted when the subject's equalized value or ask is > $1.2M),
  $/sqft outside $60–850 when the area is stated, and sale-to-equalized-assessment outside 0.5–2.0. The subject
  ARV is capped to 1.25× a live ask when above 1.6× ask, else to 1.25× the equalized assessment when above 3.5×
  it, and always to $850/sqft of known living area (OUTLIER_CAPPED, confidence ≤ 40). Non-class-2 parcels are
  NON_RESIDENTIAL: PASS when unlisted (valued at the equalized assessment), unit-of-a-larger-parcel when listed.
  Capped / non-residential valuations are never READY_TO_CALL and never get an agent offer.
- **Listed foreclosures** (`engine/fast_track.py`): tracks follow the numbers — CASH_OFFER (exempt, deal verdict,
  best ≥ 60% of ask), FRFPA_FLOOR_OFFER (≥ 82% FMV, attorney-drafted), CONFIRM_OCCUPANCY (presumed exemption),
  SHORT_SALE (a cash offer the agent submits to the lender — the short-sale negotiation stays with the agent and the
  seller's attorney), EQUITY_MONITOR (no lowball), NO_OFFER. Scripts and packets never offer to stop, postpone or
  negotiate the sale, never imply paying the agent, always disclose investor / assignment.
- **Closing** (`engine/closing_coordinator.py`): recording a contract opens 8 milestones (+ FRFPA recording) with
  NJ-business-day deadlines; attorney review concludes the business day after its deadline unless a disapproval is
  recorded (which voids the contract and cancels the file); walkthrough / assignment steps refuse to complete while
  review is open. Closing books the fee once (idempotent ledger key).
- **Money** (`engine/finance.py`): 28% of net profit is reserved; due dates, thresholds and SE-tax factors come from
  `config/nj_closing_partners.yaml` with their IRS / NJ sources — update them every January. Not tax advice.
- **Inbound** (`engine/inbound.py`, `site/`): pages make only provable claims (no testimonials, no timelines, the
  seller pays normal seller costs incl. the realty transfer fee) and carry the investor / assignment / attorney-review
  / foreclosure-help disclosures. A phone is called only with the form's consent checkbox; that consent covers a
  National-registry number for this inquiry, but internal / Daniel's Law stops always win. Honeypot field rejects bots.
  `web.public_hosts` (a tunnel) can reach only `/site/*` and `POST /api/inbound/*`. Static mode sends submissions to
  a third-party form service (Formspree) — the operator's account holds them until exported and ingested.
- **Partners** (`config/nj_closing_partners.yaml`): only firms verified on their own page with a source URL; "investor"
  claims are quoted evidence, not endorsements — confirm assignment / double-close insurance by phone and check
  licenses (NAIC / DOBI) before use.

### Phase 5 operating rules — enforced in code
- **Human copy** (`engine/human_copy.py`): every e-mail is a 3-5 sentence plain note (aim < 85 words, hard limit 110
  above the sign-off), first name + `Full Name | Company` + phone signature, no stock phrases ("I hope this email
  finds you well", "opportunity", "excited", "formal letter of intent", …), no em-dashes, no markdown, no bullets
  above the signature. Everything required sits under `--- Offer Summary ---` (`--- Details ---` for a no-price
  inquiry): terms, unrepresented-buyer wording, investor / may-assign disclosure, postal address, opt-out. Copy
  says "I'm an investor buying for myself or a partner investor", never "we're cash buyers"; "off-market" only when
  the house isn't listed. tests/test_human_copy.py lints every track; the live packets are linted too.
- **Autopilot** (`engine/autopilot.py`): an edited e-mail must keep the postal address and the opt-out line or it
  isn't sent; texts are copied, never automated; "Interested" generates the written offer only through the LOI
  gates (no price on a presumed / covered FRFPA lead); one send = one logged interaction + 48-hour follow-up.
- **My Setup** (`engine/operator_setup.py`): writes only git-ignored files (settings.local.yaml, .env chmod 600),
  never returns the app password; `WHOLESALING_LOCAL_SETTINGS` / `WHOLESALING_ENV_FILE` point tests and demos at
  scratch files. "Live mode" = name, company, 10-digit phone, e-mail, postal address with ZIP.
- **Public site** (`engine/site_build.py`): published only when `build-site --check` passes (operator details from
  SITE_* repository variables, every disclosure present, no e-mail / phone but the operator's and the HUD / LSNJ
  help lines, no stray files) after pii-scan.

### Phase 6 operating rules — enforced in code
- **Pricing model** (`deal.pricing_model: quant`, `engine/quant_pricing.py`, `config/nj_deal_costs.yaml`): the end buyer's max
  price is the lower of (a) the closed-form solve where a rehabber still clears ≥ 15% net ROI and ≥ $45,000 after rehab + 10%
  contingency, 1.5% buyer closing, six months of taxes / insurance / utilities, 11% + 2 points capital (interest on half the rehab),
  a 5% commission + 0.5% seller closing and the seller's NJ Realty Transfer Fee (+ graduated fee over $1M) on the resale, and
  (b) alpha × ARV − rehab, where alpha ∈ [0.68, 0.80] = 0.70 + 0.5 × (local q25 − statewide q25) ± a density nudge, q25 being the
  lower quartile of recorded investor purchase price ÷ equalized value per town (≥ 8 purchases), county (≥ 20) or state
  (`config/market_calibration.yaml`, refreshed by `manage.py calibrate`). `mao_walkaway` = buyer_max − $10K (stored as
  `mao_aggressive`), `mao_target` = price at 16% ROI − the $15K / $20K / $25K fee by ARV band (stored as `mao_conservative`),
  `opening_offer` = target × 0.88, never above 90% of a live ask (stored as `anchor_offer`); the breakdown lives in
  `assumptions_json["quant"]`. The FRFPA 82% floor, guard flags and verdict rules are unchanged; `covered_track` uses the
  effective alpha. `fixed_pct` restores the classic 70% / 75% math. Measured on 2026-10-29 across all 2,042 valuations: striking
  distance 253 → 234 at the 75% gate and 180 → 171 at 78% (losses are alpha-bound thin markets and sub-$250K deals where the
  $45K floor binds; gains in Edison / New Brunswick / Union towns), end-buyer ROI at buyer max median 16.4%, min 15.0%.
- **Funding partners first** (`engine/jv_partner_vault.py`): until `autopilot.partners_needed` (3) partners are ACTIVE with a
  proof of funds on file, the deck opens with Phase 0 cards. Partner copy says what is true (the public deed fact, the
  operator's own intro line from `operator.intro`), proposes a JV with both as principals and no referral fee, and carries the
  investor / attorney-review lines, postal address and STOP. Offers say "acquiring with our verified local capital partner
  (proof of funds attached | available on request)" only when `best_partner(need_pof=True)` finds one whose buy box covers the
  deal; the document is attached only with `pof_share_ok`. POF files live in `data/partners/<id>/` (git-ignored, chmod 600).
- **Two-step funnel** (`autopilot.first_touch: opener`): an agent we have never written to gets Touch 1, the two-line
  availability question (no price, no link, no attachment; only the operator's name, phone, one company + postal line and the
  one-clause "no more" opt-out). Silence → one-line nudge at 48 hours. A reply (logged as AGENT_REPLIED) brings the lead back as
  "They replied: send the offer" with the priced note and the full Offer Summary (Touch 2). Listed foreclosures inside
  `urgent_days_to_sale` (14) skip the opener. `first_touch: offer` sends the priced note first.
- **Reply negotiator** (`engine/reply_negotiator.py`): the counter is checked against the buffered walk-away (oil tank $12K,
  tenants $9K, CO $2.5K from `negotiation:`); GREEN accept at ≤ walk-away (fee = buyer_max − counter), YELLOW counter at
  buyer_max − $12K when that is ≥ 92% of their number (the reply states the rehab, tax and carry figures), else RED hold at the
  walk-away with a 14-day drip; best-and-final is never above the walk-away; a counter below the FRFPA floor or a PASS /
  referral / short-sale verdict yields no price; "available" hands off to Touch 2; a proof-of-funds ask routes to the vault
  (or "call your top 3 matched buyers"); an opt-out suppresses the agent immediately. Every priced reply carries the Offer
  Summary and passes the copy linter (≤ 3 sentences, < 90 words above the sign-off).
- **Item 14 rider & structure** (`engine/deal_structure.py`): the rider is gated like the LOI, references the "No Assignment"
  section as numbered in the parties' contract (Section 37 on the current Form 118; Item 14 on the old NJAR form), asks for the
  CPEA property-condition statement (P.L.2024, c.32, N.J.S.A. 45:15-16.87(e)) and the flood addendum, a 10-business-day inspection
  with a tank sweep and escrow holdback, access for inspectors / partners / assignees, and tells the seller the buyer is an
  investor who may resell or assign for profit and may take title in an entity (CFA: nothing hidden). The assignment carries the
  Correa / Weintraub acknowledgment. Double close is recommended when the seller refuses the rider or the spread exceeds $30K;
  the net check charges transactional funding, both closings and the RTF (+ graduated fee) on the full B-to-C price; taking title
  in an LLC adds the C-9600 (10 business days) and GIT/REP-3 notes. The LLC membership-interest route is always disclosed.
- **Fast skip-trace** (`engine/clipboard_skiptrace.py`): only unlisted, underwritten leads with nobody to call enter the queue;
  pasted numbers are classified before they are saved (DNC numbers stay visible with confidence 0 and never callable;
  unscrubbed = not callable until fresh FTC data is loaded); wireless + recently reported ranks first; relatives are saved only
  for estates / deceased owners; READY_TO_CALL is recomputed on save.

### Day-1 deal-desk rules (learned 2026-10-29) — enforced in code
- **Partner families** (`engine/partner_principals.py`, `manage.py vault --unmask [--gis]`): cash buyers are grouped by a
  mailing key recomputed from the stored address (older rows kept the state in the key); untouched duplicate partners of
  one family are merged. Rank = 3 × purchases in 36 months + 6 × flips + $ deployed / $500K (≤ 12) + 4 per priority
  county + recency + 3 for a multi-LLC operator. A principal is named only from records we hold (imported owner lists /
  deed exports, parcels, deed transfers, contacts, cash-buyer links) at the same mailing address or owning the parcel at
  it; > 4 people at one address = a shared office (no principal). Every name carries basis + confidence; suppressed
  (Daniel's Law / stop) addresses are never shown or linked. Partner phones from a pasted people-search page are DNC-
  classified like any other number (a JV pitch is still a sales call) and never saved when on a list.
- **Comp kinds** (`engine/comps.py`): a detached house is never valued off condo units, townhouses, units in a 4+ story
  building, an address that sold more than once in the window, or 3+ sales on one street with an identical assessment
  (a townhouse row); condos / townhouses only off condos / townhouses (MOD-IV "COND", qualifier C…, MLS type); no
  new construction (built in the last 4 years) for a subject older than 30 years; listing comps beyond 2.5 × the radius
  or outside 0.6–1.6 × the median $/sqft are dropped; a thin listing set (< 6) more than 50% from the recorded sales
  yields to the recorded sales. The outlier guard re-applies all of it to stored comps.
- **FRFPA fair market value** (`engine/underwriter.py::frfpa_fmv`): the 82% floor uses the highest of our as-is
  estimate, the equalized assessment and 85% of a live ask — never our distressed-percentile number alone.
- **Pre-flight** (`engine/preflight.py`, `manage.py preflight --out data/exports/...`): every deck card is GO / VERIFY /
  HOLD. HOLD (comps flagged for refresh, < 3 comps, mismatched comp kinds, confidence < 45, ARV > 1.5 × ask, capped or
  non-residential) never reaches the deck. Listed foreclosures whose payoff exceeds our walk-away (SHORT_SALE track) come
  after the MLS fixers, labeled "lender short sale (low odds)"; a SHORT_SALE or CONFIRM_OCCUPANCY listing whose
  walk-away is under 60% of the ask is NO_OFFER.
- **Open at the payoff** (learned 2026-10-29, `engine/fast_track.py::offer_numbers`): a listed foreclosure whose payoff
  (sheriff upset) is above our opening but within our walk-away opens at the payoff (next $1,000), never above the
  walk-away or 90% of the ask; e-mail, text and the attached LOI share the number. Below the payoff the seller needs
  the lender's short-sale approval, which doesn't come before a sale date. The payoff still excludes the seller's
  commission and closing costs: ask the agent what the seller must net.
- **Partner copy** (`engine/jv_partner_vault.py`) says "distressed NJ deals", never "off-market": most deals the desk
  finds are listed (stale MLS fixers, listed foreclosures).
- **Sheriff payoff** (learned 2026-11-01, `agents/scrapers/sheriff_sale_agent.py::parse_detail_page`): CivilView's
  "Approx. Upset" field is often the old amount due (Bergen's sheriff prints "the approximate upset price listed below is
  not accurate"). The notice's own figure wins as `upset_amount` ("the upset price … is $530,000.00", "ESTIMATED UPSET …
  BID AMOUNT", "estimated upset price is … which includes interest, costs"); `payoff_estimate` = the highest of that,
  the field and the amount due (a plaintiff may also bid below its debt) and is stored as `amount_owed`;
  "subject to approximately $X … taxes, water" is `prior_liens`. `known_debt` = payoff + prior liens, and the outlier
  guard re-prices any valuation whose known debt moved, offline, on the next pass (quick or full refresh).

### Cloud-to-laptop database snapshot (learned 2026-10-29) — enforced in code
- `db/snapshot.py` carries the database between the cloud session and the operator's laptop as one encrypted file:
  AES-256-GCM (a wrong passphrase or one changed byte fails before anything is written)
  under a key derived by PBKDF2-HMAC-SHA256 (600,000 iterations, random salt) from a random 32-character passphrase
  (57-symbol alphabet, ~186 bits). (Fernet was considered and not used: it is AES-128-CBC + HMAC.)
- The passphrase lives only in `.env` as `DB_SYNC_KEY` (git-ignored, chmod 600) — never in the snapshot, the manifest,
  a commit or a log. manifest.json holds sizes, SHA-256 sums and table counts, nothing else.
- The repository is public, so `data/snapshots/` is git-ignored and pushing a snapshot was refused (2026-10-29): the file
  goes to the operator as a private download (the app's file card) and is dropped into `data/snapshots/` on the laptop.
  Committing one is a deliberate `git add -f` after the repository is made private, with pii-scan run first (it reads
  the snapshot bytes like any tracked file and must find 0 names). If the passphrase may have leaked, re-pack with
  `--new-key`.
- Unpack never overwrites a database that has data without `--force`, and `--force` moves the old copy to
  `data/backups/` first. `manage.py server` and the launchers unlock a missing / empty database (DB_SYNC_KEY from
  .env, or one prompt) and refuse to start on an empty database while a snapshot is waiting.

### Phase 7 operating rules — Google Calendar (learned 2026-10-29) — enforced in code
- **Access** (`engine/gcal_sync.py`): one data scope, `calendar.events` (events only: no e-mail, contacts or calendar
  settings), plus `openid` + `userinfo.email` at sign-in only to learn which account signed in. The operator's own
  "Desktop app" client in `config/credentials.json`; `setup-calendar` (or 📅 Connect in the cockpit) runs the browser
  consent once and writes `config/token.json` chmod 600. Both files are git-ignored.
- **Which calendar** (learned 2026-10-29): `setup-calendar --account <email>` pre-selects that account (`login_hint`)
  and refuses a sign-in as anyone else (nothing saved); `--calendar <email>` writes to another calendar the signed-in
  account may edit ("Make changes to events"), the fallback when a school / work Google Workspace blocks unverified
  apps (that calendar's default notifications then apply: reminders on an event belong to the account that made it).
  The choice lives in settings.local.yaml (`calendar.account`, `calendar.calendar_id`), never in a committed file.
  Switching account or calendar deletes this app's events (private property `njw=1`, nothing else) from the old
  calendar with the old token, resets the outbox and re-creates everything on the new one.
- **7-day sign-in** (OAuth app left in "Testing"): every connect stores `connected_at`; a 🔑 "Re-connect Google Calendar
  sync" event lands 6 days later at 10:15 with the exact `setup-calendar` command (`calendar.reconnect_reminder_days`,
  0 = off once the app is published), because the operator only reads the calendar and an expired sign-in is silent.
  An app left in "Testing" loses its refresh token after 7 days: the sync then reports "sign in again" and keeps
  everything queued; publishing the app (unverified is fine for one user) avoids it.
- **Events**: agent follow-up = exactly `calendar.followup_hours` (48) after the e-mail's logged time, moved to 10:00
  the next calling day when that lands outside 8am–9pm Eastern or on a Sunday (the CRM queue keeps its own 10:00 slot);
  call-backs at the CRM follow-up time; attorney review at 5:00 PM on the 3rd NJ business day after the contract date
  (`nj_business_days_after`, holidays skipped), FRFPA cancellation end at 5:00 PM when later; pending closing
  milestones at 9:00 on their due date; sheriff sales as all-day events for at most `sheriff_sale_limit` (25) leads
  (listed foreclosures with an offer path, then ready-to-call, then priority); one recurring 10:00 daily-goal event.
  Leads under contract get milestone events instead of follow-ups. Nothing is created in the past.
- **Sync**: `calendar_events` rows are keyed (`followup:<lead>:i<interaction>`, `review:<lead>:<date>`, `sale:<lead>`,
  `daily_goal`, …) and the Google event id is derived from the key, so re-runs, lost rows and a second machine update
  instead of duplicating. Commit hooks (after_flush → after_commit) queue the touched leads and a background thread
  syncs them; sync sessions carry `session.info["gcal_worker"]` so they never re-trigger. A sync commits after pull +
  reconcile and before any push, so no SQLite write lock is held while Google is called (a slow / offline Google
  never stalls the cockpit's own writes). Offline / 5xx / rate
  limits leave rows queued (attempts + last_error); the cockpit re-syncs every `background_minutes`, the swarm runs
  `calendar_sync` every 15 minutes and at the end of every pipeline.
- **Two-way**: pull first. An event the operator deleted becomes `dismissed` and is never re-created; a moved
  follow-up / call-back moves `lead.next_followup_at`; a moved daily goal keeps the operator's time; a moved attorney
  review, FRFPA date, milestone or sheriff sale is put back (legal facts). Our own cancellations (a newer touch, a
  disapproval, a closed lead) delete future events only; past events stay as history.
- **Privacy**: events may carry the street, county, ask, upset, our offer / walk-away, the listing agent's business
  contact and the dashboard link. Never a homeowner's or defendant's name or phone (call-backs say "the number is on
  the lead card", where the DNC + calling-hours checks run); no agent phone when it is on the internal or Daniel's
  Law list; an address under a Daniel's Law / stop suppression (property or owner mailing address) appears only as
  "lead #<id>", without the CivilView link.

### Phase 8 operating rules — Gmail autopilot & the one-button home (learned 2026-11-02) — enforced in code
- **Reading the inbox** (`engine/gmail_autopilot.py`): the My Setup Gmail address + app password (SMTP_USER /
  SMTP_APP_PASSWORD in .env) on imap.gmail.com:993. INBOX is selected **read-only** and every fetch is BODY.PEEK, so
  nothing in the operator's Gmail is marked read, moved or deleted. Each check reads only UIDs above the last one
  seen (per account + UIDVALIDITY in swarm_state); the first check looks back `lookback_days` (14). Headers come first.
  A body is downloaded only for a bounce or a message matched to one of our leads:
  1. In-Reply-To / References hold a Message-ID we sent or received (`interactions.email_message_id`); else
  2. the sender is a listing agent we wrote to (their street in the text picks the listing, else the one we wrote to
     last); else
  3. an unknown sender's subject names a street we e-mailed about (this one always waits for approval).

  Unmatched mail is never downloaded or stored. A matched reply keeps only the agent's own words: quoted history,
  Outlook header blocks and "Sent from my iPhone" are stripped, capped at 4,000 characters. Out-of-office and
  automatic replies are ignored. A bounce takes the dead address off the agent's card (noted, phone kept). One
  e-mail per Message-ID (unique `inbound_replies.message_id`), so a second poller in another process never
  processes or sends twice (the auto-send claims its row with a conditional UPDATE).
- **What goes out by itself** (`auto_send_touch2`, default on): only Touch 2, and only when every one of these holds:
  - the reply says available, with no number and no other topic (tank, tenants, CO, POF, bidding war, estate,
    short sale, under contract, stop);
  - its only question, if any, is "what's your offer?";
  - it matched by thread or by the agent's own address;
  - the priced packet builds with no warning and its LOI generates;
  - pre-flight shows no price blocker: FRFPA, payoff, condo / townhouse unit, unit count, sheriff sale inside 10
    days, outlier / non-residential, ARV above every comp, confidence under 50;
  - a listed foreclosure is on the CASH_OFFER track.

  It goes out in the agent's own Gmail thread (In-Reply-To / References, "Re:" subject) with the one-page LOI, 4-11
  minutes after the reply and only inside `send_hours` (8 AM to 8 PM Eastern), logged as OFFER_SENT_TO_AGENT with
  the 48-hour follow-up. Nothing newer may have happened on the lead; otherwise it waits for approval.
- **What waits for the operator**: every counter, accept, question, POF ask, bidding war, risk item or under-contract
  note goes to 🔥 Action Needed with the light (🟢 DEAL WORKS AT $X — Your Est. Profit = buyer_max − their price;
  🟡 counter; 🔥 hold) and the negotiator's drafted answer.
  - One click (`approve()`) sends it in their thread. An edited answer must keep the postal address and the opt-out
    line (CAN-SPAM).
  - A priced answer attaches an LOI at that price through the normal gates (`contracts.generate`).
  - An opted-out agent gets nothing.
  - Nothing negotiates by itself.
  - An opt-out reply ("no more", "remove me") suppresses the agent at once.
  - A 🔥 calendar alert (key `reply:<lead>:r<id>`, color sage) lands ~10 minutes after a reply that waits (inside
    calling hours). It carries the street, the light and the numbers, never the agent's message (it may name the
    seller). It disappears when the reply is approved or dismissed.
- **The 🚀 batch** (`start_campaign` / `run_daily_outbound_autopilot`): today's deck in money order (urgent listed
  foreclosures, due follow-ups, MLS fixers). It picks only cards that have the agent's e-mail and pre-flight GO or
  VERIFY. Short sales, cards with warnings and priced notes with a pre-flight blocker stay for the card player. Each
  send goes through `autopilot.send` (same gates and logging), 25-60 s apart (`pace_seconds`), inside the rolling
  25-per-24-hours limit and `send_hours`, one batch at a time, ⏹ Stop between e-mails. Texts are never automated.
- **Actionable-only deck** (`autopilot.actionable`): a card appears only with the agent's e-mail or a phone that is
  on no stop list (internal / Daniel's Law always win). Leads whose reply waits in Action Needed or the Touch 2 queue
  are off the deck. Phase 0 partner cards show only partners with an e-mail or a DNC-clear phone (a JV pitch to a
  person's phone is a sales call). The rest wait in ⚡ Skip Trace (`needs_lookup`). A phone / e-mail that reaches the
  linked cash-buyer record later is paired onto the partner (`pair_contacts`).
- **✅ Interested on a call** (`agent_outreach.last_touch`): the listing agent saying "send it" on the phone
  (SPOKE_NEEDS_OFFER with a listing-agent contact) counts as their answer. The card turns into Touch 2 (priced
  note, LOI on SMTP) right away, even when they were called before any e-mail.
- **The home page** (`#/`): the giant button, live counts (e-mails sent today, replies found, offers delivered, next
  auto-check), Action Needed and the optional card player. The 12 tabs and the lead search sit under "Advanced
  Tools ▾" (`#/autopilot` is the full step-by-step deck with the kanban). The UI copy says "AI" (operator branding).
  The reply reader and negotiator are rule-based: no model is called and nothing leaves the laptop but Gmail.
- **Send failures are sentences, not 500s** (learned 2026-11-02: the Send button showed a bare "Internal Server Error"
  because `autopilot.send` only converted RuntimeError, so smtplib / ssl exceptions escaped):
  - `core/mail.py`:
    - certifi certificates for SMTP (465 SSL / 587 STARTTLS) and IMAP (the python.org macOS build has no CA store
      until "Install Certificates.command");
    - `clean_app_password()` drops spaces / hyphens;
    - `explain()` turns sign-in, certificate, bad-recipient, network and limit errors into one actionable
      sentence, never containing the password;
    - `GmailSendError(fatal=…)`: sign-in / certificate / network failures stop a batch after the first card.
  - `operator_setup.sync_from_disk()` (called by `smtp_ready()` and `gmail_autopilot.credentials()`) picks up
    settings.local.yaml / .env edits made outside the running server by mtime.
  - The send endpoint returns:
    - 409 for Gmail failures;
    - 503 for "database is locked", naming the refresh step and warning that a sent e-mail may already be in Sent;
    - for anything else, the app-wide handler answers JSON and writes the traceback to `logs/server.log`
      (`manage.py server`, rotated, git-ignored).
  - A failing offer PDF becomes a warning (the e-mail no longer claims "attached"). The calendar commit hooks log
    and never break a save.
  - ⏹ Stop refresh (`swarm/runner.stop()`, `POST /api/swarm/stop`) skips the remaining steps.
  - 🔌 Test Gmail (`POST /api/setup/test-gmail`, `manage.py gmail --test [--send-test]`) signs in to both servers
    and says which side fails and why.
  - SQLite runs WAL with busy_timeout 120 s (agents checkpoint per lead, so locks are short).
  - Gmail hanging up (learned 2026-11-02: "SMTPServerDisconnected: Connection unexpectedly closed" on the 4th offer
    of the first `inbox` run): `deliver` tries again after 5 s and 20 s (`outreach.smtp_retry_waits`) on a transient
    failure only (`core/mail.is_transient`: hang-up, 421 / 4xx, connection reset; never a refused sign-in,
    certificate, refused address or blocked port). When the line dropped while the message itself was going out, the
    Sent folder is searched for its Message-ID first (found = sent; can't look = stop and say "check Sent"), so an
    agent never gets two offers. One inbox run sends its offers 8-20 s apart (`reply_pace_seconds`) and stops at the
    first failure every e-mail would share; the rest stay queued, the failed one is re-queued by the next check.
- **First live replies** (learned 2026-11-02):
  - A broker's iPhone signature ("Sample Real Estate", "Over 20 Million Dollar Sales Volume") was read as a $20M
    counter plus an estate sale. Fixes:
    - `strip_quoted` now cuts at a sign-off line ("Sincerely," / "Thanks," / "--") and at "sent from my iPhone"
      anywhere on a line;
    - ESTATE ignores "real estate";
    - a counter outside 0.2-2.5x the larger of ask and ARV is dropped with a warning.
  - A short reply naming the attorney ("Yes. Attorney") answers our opener's "or already in attorney review?". It
    reads as UNDER_CONTRACT (attorney_ambiguous): the draft asks them to confirm and asks for the first call (no
    congratulations), with a 7-day follow-up.
  - Touch 2 after a reply opens "Thanks for getting back to me on <street>." When the LOI is generated it says
    "The 1-page written offer is attached"; if the PDF fails, it falls back to "I'll send over the written offer
    today".
  - A company name equal to the operator's own name is printed once in the signature.
  - One sure way to send the offer (a logged "still available" with no offer out yet):
    - The lead page's ✉️ Next e-mail to the agent box (`GET /api/autopilot/<id>/card`) and the reply panel's
      ✉️ Send this offer from Gmail now both go through `autopilot.send`. Touch 2 goes in the agent's thread
      (`gmail_autopilot.reply_thread`), and any queued / waiting reply for the lead is closed
      (`resolve_open_replies`), so the queue never sends a second offer.
    - ⏩ Send the offer now (`POST /api/replies/<id>/send-now`) skips the 4-11 minute wait with every other check.
    - "✉️ Send my offer instead" sits on Action Needed cards with no number ("Yes. Attorney").
    - The box defers to Action Needed when the waiting reply named a number.
    - Nothing else goes out while a row is "sending".
    - Every inbox check re-reads replies still waiting with the current parser (`refresh_open_replies`). A
      counter the new reading drops is cleared on the lead.
### Claude-native operation (learned 2026-11-02, operator's 25 openers → 8 replies in 90 minutes) — enforced in code
- **The interface is a Claude Code chat on the laptop**, opened in `~/realestateauto`. The slash commands in
  `.claude/commands/` run the CLI:
  - `/inbox` → `manage.py inbox --json`;
  - `/daily` → refresh, `gmail --dry-run`, `gmail --run`;
  - `/status`;
  - `/reply <house> <words>` → `gmail --find` + `gmail --reply LEAD --body-file F [--price N]`.

  The web dashboard is optional (`pkill -f "manage.py server"`). A cloud Claude session has no route to the laptop's
  Gmail (SMTP 465 / IMAP 993 are blocked there, and the app password lives only in the laptop's .env), and its
  Gmail connector may be signed in to another account: answer agents from the laptop.
- **Hands-free** (`manage.py autopilot-daemon`, installed in macOS launchd by `manage.py install-daemon` as
  `com.khatri.wholesale.autopilot`, log in logs/daemon.log): every 5 minutes between 8 AM and 9 PM Eastern it runs
  `run_inbox`:
  1. import Gmail Sent;
  2. read new replies;
  3. send every "still available" offer;
  4. keep the calendar in sync.
- **The 25/day cap is for cold e-mail only** (`outreach.daily_email_limit`: openers, first offers, nudges to agents
  who never wrote back). Anything to an agent who replied (`engaged`: Touch 2, approved answers, /reply, in-thread
  follow-ups) counts only against `reply_daily_limit` (150, under Gmail's ~500). The 25 openers of 2026-11-02 had
  silently queued every Touch 2 ("daily limit reached").
- **Gmail is the record** (`gmail_autopilot.sync_sent`): replies the operator types in Gmail itself (phone, web,
  another computer) in a lead's thread are imported from the \Sent folder (found by its special-use flag, any
  language):
  - an offer when the text says "Option 1: $X" or "offer … $X", else a follow-up;
  - the waiting / queued reply is closed, so the autopilot never sends a second offer on top.
- **What the agent asked shapes Touch 2's first line** (`reply_negotiator` intents, `agent_outreach.build_packet(reply_flags=…)`):
  - ASKS_ROLE ("buyer or a realtor?") → "I'm the buyer: an investor … not a realtor …";
  - ASKS_SHOWING ("interested in seeing it?") → photos / video first, walked during the inspection period (no car,
    no claim of a partner we don't have);
  - CALL_ME → "I'll give you a call; … here they are in writing", follow-up at the next call slot (~3 h, 9 AM-8 PM);
  - BACKUP ("contract but not sure it's going through", and a bare "Yes. Attorney") → "a clean backup offer in case
    the current contract doesn't go through";
  - EXPIRED ("Listing expired") → a no-price "if the owner still wants to sell" note, which waits for approval.

  A leading "yes" answers our "is it still available?". A leading "no," / "sorry," never negates what follows ("No,
  sorry it sold" is sold). These four flags are auto-send eligible; numbers, risk topics and other questions still
  wait.
- **Wrong parcel = HOLD** (`preflight.listing_parcel_miles`): when the MLS listing's own coordinates are more than a
  mile from the parcel it was linked to (2026-11-02: "22 Grand Ave, Iselin" sat on a South River parcel 10.4 miles
  away; 25 of 1,410 listings), the card is HOLD, so it is off the deck, never auto-priced, and the negotiator drafts a
  no-price "confirm beds / baths / sqft / condition" note. `stale_listing_distress_agent` no longer links a parcel
  more than `MAX_PARCEL_MILES` (1.0) from the listing (a county-wide fallback hit the same street in another town).
- **Second-morning replies** (learned 2026-11-03):
  - "Never happen… We are at 470.00" is a REJECT with a $470,000 counter: a 3-digit number after "we are at" /
    "wants" / "under contract at" is thousands even with decimals ("469.9" = $469,900); a bare "$150.00" (HOA, fee)
    stays literal and "HOA is $X" is never a price. When they're under contract with someone else (this reply or an
    earlier BACKUP), the RED answer keeps us the backup at the walk-away, never above it.
  - **Facts before a price** (`reply_negotiator.fact_check_issue`): when pre-flight says the price rests on the wrong
    comps (a condo unit priced off other units' sales, a 3-family off 1- and 2-family sales) or on the wrong house (HOLD:
    wrong parcel, outlier, low confidence), "still available" gets a no-price answer: it answers their question (buyer
    or realtor, want to see it, call me, backup) and asks for the one fact (same-size unit sales + HOA; the rent roll;
    beds / baths / sqft / condition / net). `kind = "fact_check"`, no LOI, logged AGENT_FOLLOWUP_SENT. It goes out by
    itself (`auto_send_fact_check`, default on) under the Touch 2 conditions, and `approve()` never attaches an offer
    LOI to it (before this, approving the wrong-parcel note attached an LOI priced off the other house).
  - `gmail --approve / --dismiss / --send-offer / --reply` take the street ("108 S Park") as well as the id: unique
    match only, streets that start with the words first, leads we e-mailed first.
  - `autopilot-daemon --daily-openers N [--openers-at 10:30]` (and `install-daemon` with the same flags) sends the 🚀
    batch once per weekday after that time, inside send hours, under the 25 / 24 h cap; the day is claimed in
    swarm_state (`daemon_daily_openers`) before the first e-mail. Off unless the flag is given.
- **Hands-off operation** (learned 2026-11-03, the operator wants no dashboard and updates in the cloud chat):
  - `manage.py set-gmail [--email]` saves a new app password typed at a hidden prompt in the operator's own Terminal
    (never pasted into a chat), keeps the rest of .env (DB_SYNC_KEY), and tests both Gmail servers.
  - 📊 `desk:<YYYYMMDD>` (gcal_sync.desk_status_event, `calendar.desk_status_event`, color graphite, all day, free, no
    reminder): the Mac's daily report on the calendar, rebuilt on every full sync (the daemon kicks one each cycle):
    sent / replies / waiting counts in the title; WAITING ON YOU with the exact `gmail --approve "<street>"` command,
    going out next, Gmail failures, offers sent, answered without a price, calls, cold e-mails in 24 h. Streets and
    numbers only (no agent message text, no homeowner names; suppressed addresses as "lead #<id>"). A cloud Claude
    session can't reach the Mac, so its scheduled check-ins read this event (and the 🔥 reply / follow-up events) through
    the Google Calendar connector and report in the chat.
  - The daily-goal event no longer points at the dashboard ("🎯 Daily outreach: N agent e-mails (autopilot)").
  - The daemon reads the inbox 8 AM-9 PM but sends only inside `send_hours` (8 AM-8 PM): run_inbox(send=in_send_hours()).
  - `inbox` prints progress on stderr while it paces the sends ("next one in 14 s"), so a pause never looks like a hang.
  - A 🔥 reply alert is timed from when the Mac READ the reply (the later of `received_at`, Eastern, and `created_at`,
    UTC → Eastern), not from when the agent sent it: replies read hours late (Mac asleep, no daemon) used to get no
    alert at all because it landed in the past.
- **One yes for the whole inbox** (learned 2026-11-03, the operator came back to a full inbox):
  - `gmail --approve-all [--yes]` (`gmail_autopilot.approve_all`) lists every drafted answer waiting on the operator,
    asks once, and sends each through `approve()` (LOI at the drafted price, CAN-SPAM lines, stop list), 8-20 s
    apart, stopping at a Gmail failure every e-mail would share.
  - Rows matched only by a street in the subject, rows with no draft, and agents who asked to stop are left for a look.
  - `/go` (`.claude/commands/go.md`) is the one command for a Claude Code chat on the Mac: pull, inbox, one question,
    approve-all, install the daemon if missing, today's batch, calendar sync, a short report.
  - A cloud session can't use a Mac folder unless the Mac offers it under Remote Control; that isn't available to every
    account yet, so the desk runs in a Claude Code session on the Mac itself.
- **The Mac runs itself** (learned 2026-11-03, the operator handles calls only; `engine/mac_desk.py`):
  - **Self-update:** every 30 minutes (`autopilot-daemon --update-every`) the daemon fetches its branch. New commits
    are pulled with `pull --rebase` (local commits kept), and `pip install -e .` runs when pyproject.toml changed;
    then the daemon restarts on the new code (os.execv).
    - Uncommitted edits are parked in a named stash ("autopilot self-update …: local edits parked"), never merged
      half-way into running code.
    - A conflict aborts the rebase and the Mac keeps the code it had.
    - **A Claude session on the Mac commits and pushes its own code fixes right away, without asking** (the
      operator asked never to be asked to commit), so nothing gets parked.
  - **Unattended desk agent** (`.claude/commands/autopilot.md`): while replies with a draft wait, inside 9 AM-7 PM,
    at most every `--agent-every` (90) minutes, the daemon runs `claude -p <the autopilot prompt>`. That is headless
    Claude Code on the operator's own sign-in, found by `mac_desk.find_claude` or recorded by `install-daemon`.
    - Its `--allowedTools` are only manage.py inbox / gmail / lead / script and Read: no edits, no other shell.
    - It approves drafts that fit, writes its own short answer when a draft doesn't fit (`gmail --reply`), and
      leaves accepts, call requests and legal / tenant / foreclosure questions for the operator (their 🔥 event
      stays).
    - Its report goes to logs/agent.log.
  - `gmail --reply --price N` refuses N above the lead's walk-away (`mao_aggressive`) unless `--over-walkaway`
    (the operator only), so no unattended run can be talked above it.
  - A bounced agent e-mail with a phone on the card sets the next call slot, so the call lands on the calendar.
  - Signature IDs are not prices: `reply_negotiator._ID_NUMBER` drops "License #: 0897105", "Lic#", "MLS #",
    "NMLS" numbers before amounts are read (a Redfin license number had read as an $897K counter).
  - The 🔑 calendar re-connect event says how to publish the OAuth app, so the 7-day sign-in stops for good.
  - "Please call me" answered by e-mail: the follow-up the answer set (the call slot, under 20 h after the e-mail) is
    the calendar event, titled "📞 Call <agent> about <street> (they asked you to call)", not a 48-hour follow-up.
- **The cloud desk** (learned 2026-11-03, the operator can't keep a laptop open; `engine/cloud_desk.py`,
  `.github/workflows/desk.yml`):
  - **Runtime:** GitHub Actions every 15 minutes, which reaches Gmail directly. It reads replies 8 AM-9 PM and sends
    8 AM-8 PM: Touch 2, fact checks, `auto_approve`, the 10:30 openers. Then it syncs the calendar. The data refresh
    (run-all) runs at ~5:17 AM, and an encrypted backup goes out after 8 PM. One run at a time (concurrency group).
  - **Turning it on:** three repository secrets, SMTP_USER, SMTP_APP_PASSWORD and DESK_KEY (≥ 16 random characters).
    Until they exist, every run exits in its first step.
  - **State:** `data/cloud/state.enc` in the Actions cache: database + settings.local.yaml + token.json +
    credentials.json, AES-256-GCM under HKDF(DESK_KEY). It is restored with prefix `desk-state-` and saved under a new
    key every run.
    - The repository is public: runs print one JSON line of counts (stderr to /dev/null). No message text,
      addresses or names ever reach the log.
  - **Handoff, no command:** the first run with no state mails itself "[njw-cloud] ready for the handoff" with its
    X25519 public key, derived from DESK_KEY.
    - The Mac daemon checks Sent every 30 minutes (`mac_handoff`). It seals its database + config to that key and
      mails them to itself as "[njw-bundle] db <stamp> i/n" parts (12 MB each, SHA-256 in the body).
    - It then writes `data/runtime_cloud.json` and goes idle (self-update and config hand-over only).
    - A later `setup-calendar` on the Mac sends a "config" bundle the cloud applies on its next run.
    - Bundles open only with DESK_KEY; the nightly backups use the same format, so a lost cache restores from the
      newest one (up to a day old).
  - **What goes out without a person** (`gmail_autopilot.auto_approve`, `auto_approve: true`):
    - no-price notes (facts first, numbers on hold, listing expired, under contract);
    - holds / counters / backups at or under the walk-away, only when the agent's number was stated plainly
      (`ParsedReply.counter_explicit`: right after "wants" / "is at" / "would take" ...).
    - Accepts, unclassified questions and signature numbers stay for the operator.
    - Their calendar alert is a call ("📞 COUNTER-OFFER RECEIVED: call <agent> about <street>" / "📞 Call <agent>
      about <street>: they replied"), since the operator handles calls only.
  - **Acting from a cloud Claude session:** `workflow_dispatch` (the inputs are public: ids only, never text or a
    price):
    - task `approve` + reply #: send the drafted answer;
    - task `dismiss` + reply #: a call settled it;
    - task `offer` + lead #: the lead's next e-mail, the same as `gmail --send-offer` (Touch 2 at the desk's own
      price, with the Offer Summary and the LOI, in the agent's thread).
    - task `note` + a **sealed note** (learned 2026-11-03: the operator reports calls in the chat): JSON items
      `{"lead", "reply", "price", "over_walkaway", "call", "channel", "notes", "followup_at", "agent_phone",
        "close_replies"}` ("followup_at" with no "call" sets the next touch after the reply) sealed with
      `manage.py cloud --seal <pub>` to the desk's X25519 public key. Every run prints that key as `pub`.
      - The public run page shows only ciphertext, and only repository writers can dispatch.
      - `apply_note`: a reply goes through `send_custom_reply` (signature, address, opt-out; price → LOI, refused
        above the walk-away unless the operator chose `over_walkaway`, e.g. when the stored valuation is for the
        wrong parcel); a call goes through `crm.log_call` (debrief source `chat_note`) after the reply, so its
        follow-up is the next call.
      - Calendar: a missed call becomes "📞 Try <agent> again … (no answer last time)"; a conversation becomes
        "📞 Call <agent> back … (check in)" carrying the chat note's facts. Only `chat_note` notes reach the
        calendar: Claude keeps them to business facts, never a homeowner's name or phone.
      - Every call script ends with a voicemail line.
      - `street` (in place of `lead`: a unique street match) and `agent_email` + `agent_name` (learned 2026-11-04: "please
        contact Geri, who I've CC'd" while the agent travelled): the colleague is saved as the listing agent's e-mail and
        the item's `reply` goes to her in the same thread (`send_custom_reply(to=...)`).
      - `close_replies` (learned 2026-11-04, Claude reads the business inbox through the Gmail connector, read-only):
        a reply Claude settled (a polite no, "sharing with the sellers") is dismissed with the reason, so the desk
        sends nothing more on it and its call-sheet row says "SKIP TODAY: settled by e-mail". A no ("no where
        close", "best of luck") is a REJECT even on a HOLD listing: never a second fact-finding note.
  - **After the handoff the Mac never writes to the calendar** (learned 2026-11-03: its background sync overwrote
    the cloud's 📊 report with its stale copy). `gcal_sync.handed_off()` reads `data/runtime_cloud.json`: then
    `configured()` is False and `sync()` talks to no one. `setup-calendar` on the Mac saves the new sign-in and
    mails it to the cloud at once. When the Mac's Gmail app password no longer works, it first asks for a new one
    at a hidden prompt (`set-gmail`). The Mac's original password had been pasted into a chat, so it gets revoked.
  - **Every call event carries a word-for-word call script** (`gcal_sync.call_brief`, "--- Call script (word for
    word) ---"): reply calls, "call me" follow-ups and 48-hour follow-ups. The operator is new to real estate
    (2026-11-03: "give me full scripts for everything"), so every script, here or in the chat, is spoken lines, not
    hints. It holds:
    - the house (multi-family, beds / baths / sqft / year / days on market / ask);
    - our open / aim / walk-away, or "Don't name a price" with the pre-flight reason, never above an offer
      already sent; an ask-first house whose own number needs a check (no living area, old comps: "agree to
      nothing" on the call sheet) gets no yes on the call either, only "let me run it" and the square-footage question;
    - the exact opener (name, phone, purpose) and a "bad time" line;
    - who we are, and the questions for that fact kind (condo, units, basics);
    - the answers to "what's your offer?" (priced, too low, their number under / over the walk-away; or no price),
      "wholesaler / assign?", "proof of funds?" and "want to see it?";
    - "let me check and get back to you" for anything else;
    - the closing line and the never-say rules;
    - "tell Claude in the chat what they said".

    48-hour follow-ups say the call is optional (the morning batch e-mails the nudge). From the cloud, events
    carry no dashboard link, and the 📊 report says "by the cloud desk".
- **Google Voice** (learned 2026-11-03, the operator's number is Google Voice on the business Gmail):
  - Voice has no API for software to send texts or place calls; automating its web app would break Google's terms
    and "never impersonate a browser". Automated texts / AI calls to cell phones are also off-limits (TCPA; texts are
    never automated here).
  - So the desk **reads** Voice: with "Forward messages to email" and "Get voicemail via email" on in Voice settings,
    texts (`…@txt.voice.google.com`, "New text message from (xxx) xxx-xxxx") and voicemails (`voice-noreply@google.com`,
    "New voicemail from …") land in the inbox the desk already reads.
  - `gmail_autopilot.voice_kind / voice_phone`: a notification is read only when its number is a listing agent we
    contacted (`Known.phones`); any other number is never downloaded. `strip_voice` keeps the agent's words (no footer,
    links or "Transcript:").
  - The reply is analyzed like an e-mail (`match_method` voice_text / voice_mail, channel sms_in / voicemail) but
    never answered by itself: `auto_touch2_block` / `approve()` refuse it.
  - `from_email` is stored empty: replying to the Voice relay address would send an SMS from the operator's number.
    `reply_thread` skips voice rows, so later e-mails never thread onto them.
  - Its calendar event is "📞 Call <agent> about <street>: they texted you / left you a voicemail", with the drafted
    text (`sms_text`) to send by hand.
- **One firm offer, final and on the record** (learned 2026-11-03, the operator: "every offer we are giving are complete
  lowballs" and "when we give the offers, they are final"; $5K-$10K fees are fine). `engine/offer_math.py` is the only
  place a price is decided; the e-mail, LOI, call scripts (battle card, calendar, call sheet), strategy brief, pre-flight,
  fast track and negotiator all read `firm_offer()` / `locked()`:
  - buyer_max from `quant_pricing` (unchanged floors); **offer = buyer_max − $10K** (`fees.target_fee`), ≤ 90% of the ask;
    **best and final = buyer_max − $5K** (`fees.min_fee`), ≤ the ask; no anchor discount, no ranges, one move only;
  - FRFPA 82% floor raises the offer (a floor above the final = no offer); a listed foreclosure's payoff between offer and
    final becomes the offer (next $1K); a payoff above the final = lender short sale, no firm number;
  - tiers: **firm** (final ≥ 80% of ask and every certainty check passes) states the number; **ask_first** (65-80%, or a
    certainty issue) asks what the seller needs and says yes at or under the final; **skip** (< 65%, HOLD, no-offer
    verdict, broken valuation) is not contacted;
  - certainty checks: pre-flight not HOLD, living area on record, median comp sold ≤ 12 months ago (`firm.max_comp_age_months`),
    ARV ≤ 1.05 × the top comp, no move-in-ready retail wording without distress words, unit count / condo comps confirmed,
    FRFPA occupancy confirmed;
  - `locked()`: nothing later states less than a written offer, nothing ever exceeds the final;
  - negotiator: their number ≤ final (less tank $3.5K / tenants $9K / CO $1.5K buffers, never under a sent offer) =
    accept (with "Before you accept, confirm: …" for any certainty issue); within 92% = "my best and final is $X";
    more than 25% apart and no number in writing yet = "too far apart", **no number**, 30-day check-in;
  - auto-answers: priced ones only when the agent stated the number plainly and the lead passes every certainty check;
    no-number ones ("too far apart", "not now", ask-first Touch 2) go out by themselves;
  - rehab: the listing description sets the tier (`rehab.apply_description`: "as is" / "needs work" → MEDIUM / HEAVY,
    $30 / $55 / $110 per sqft + age bumps, 0 / 5 / 10% extra contingency); `reprice()` re-reads it;
  - `offer_math.PRICING_VERSION`: the cloud desk re-prices every stored valuation (~20 s) on its first run under new
    rules, on every refresh and on task `recalibrate`; waiting drafts are rebuilt when their numbers change;
  - firm-up (task `firmup` does only this, today's call-sheet houses first whatever their reach, ~13 s each, ≤ 45 in
    15 minutes): the daily refresh re-values up to `call_sheet.firm_up_daily` (30) ask-first listings whose only gap is stale
    data live with recent sold MLS comps; a number an agent names on a call that fits the final is written only after
    the same live re-value clears the certainty checks.

  Nov 3 data after the re-price: 4 firm, 226 ask-first, 1,042 skip among 1,272 listed leads (447 of the skips are
  under 65% reach). tests/conftest.py pins the classic gates for the shared fixtures; `@pytest.mark.production_gates`
  tests run the live rules.
- **100 calls a day** (learned 2026-11-03/04, the operator: "I can make ... like 100 a day"; `engine/call_sheet.py`):
  - each weekday the desk builds one sheet (≤ `crm.call_queue_size` 100; 25 on Saturday, none Sunday) and freezes it
    (swarm_state `call_sheet`), so "A07" means the same call all day: T0 replies waiting, T1 sheriff sale ≤ 21 days, T2
    due follow-ups (≤ 4 tries), T3 firm offers, T4 ask their number; one row per phone (≤ 3 streets), never a fax or a
    stopped number, nothing re-called within 2 days (T0-T2 excepted);
  - calendar: four `callblock:<YYYYMMDD>:<A-D>` events (25 rows each, one word-for-word script), fitted around the
    operator's own events (classes) read with `CalendarAPI.busy` (a block shrinks to a ≥ 40-minute gap, never past
    8:30 PM); a lead on the day's sheet gets no separate follow-up event that day;
  - reporting: "A01 vm, A02 340 roof, A03 send, A04 stop, A05 cb thu 2pm" in the chat → Claude seals one note of
    `{"code", "result", "notes"}` items (`manage.py cloud --seal <pub>`), task `note` → `call_sheet.apply_codes`: every
    listing on that phone is logged; "send" e-mails the firm offer; a number ≤ final is written only with certain
    numbers (else the live re-value first, else held for the call-back); "stop" = internal DNC; `manage.py call-sheet
    [--script | --report "A01 vm, …"]` does the same locally;
  - e-mail volume: 40 cold / 24 h (25 → 40; 60 only after two weeks with < 2% bounces and no spam flags), 30 openers a
    weekday in two waves (`DAILY_OPENERS`, `OPENER_WAVES` 10:30, 14:00), 20-45 s apart, one new cold e-mail per agent
    address per 72 h (`gmail_autopilot.agent_cold_gap_hours`), and a lead on today's call sheet is called before it
    is e-mailed.
- **Speed and purpose** (learned 2026-11-04, the operator: "make the entire process more efficient ... everything has
  purpose"):
  - **Every ~6 minutes, 7 AM-9 PM**: desk.yml runs only by dispatch, and its last step starts the next run
    (`CYCLE_SECONDS` 360; GITHUB_TOKEN may dispatch workflow_dispatch). It dispatches only when no desk run is queued,
    so a note or approval dispatched meanwhile goes first and carries the chain. GitHub's own cron had fired twice in
    a day and, sharing desk.yml's concurrency group, could cancel a queued note: the safety net is now
    `.github/workflows/kicker.yml` (every 20 min + the 5:17 AM refresh), which dispatches only when no desk run is
    running or queued.
  - **No call or cold e-mail on a house an investor can't win**: `offer_math.condition()` reads the listing text;
    "retail" (move-in-ready / renovated / new construction, no distress words) is tier skip (`firm.skip_retail`).
    Nov 4: every "no work needed / new kitchen / new construction" conversation was a dead end. Replies and follow-ups
    (T0 / T2) are still answered. Nov 3 data: 422 listings skip as retail; 121 remain (2 firm, 73 fixer ask-first).
  - **Fixers first**: the sheet ranks T3 / T4 by condition (fixer before unknown) then reach, and each row shows the
    listing's own words ("listing says: estate sale, sold as-is, needs tlc").
  - **Only deals that convert** (`offer_math.convertible`, the same test for calls, cold e-mails and 48-hour nudges;
    replies are always answered): a firm number, a listed foreclosure with a number, a listing that reads as a fixer, or
    a neutral listing 120+ days on the market / price-cut with our final at 80%+ of the ask. Not: retail / new
    construction, condo / townhouse units without fixer words (today's A13 / A18 / A21: HOA, unwarrantable financing,
    retail buyers), anything else. Nov 3 data: 81 listings qualify. Task `sheet` (`call_sheet.rebuild_unstarted`)
    rebuilds the blocks nobody has called yet with today's rules; started blocks keep their codes.
  - **Volume without wasted touches** (learned 2026-11-04, the operator: "send the most e-mails possible to the deals
  that could work"; the first funnel: 1,447 listed, 68 convertible, 46 never e-mailed, 17 of 40 cold e-mails used,
  28 replies to 75 e-mails in 7 days, 3 bounces):
  - GitHub's cron is not relied on (kicker.yml fired 0 times in its first 95 minutes): after 9 PM the last desk run
    dispatches night.yml (own concurrency group, waits ≤ 5.5 h per hop), which starts the 5:17 AM refresh; a run
    before 7 AM hands off again (wake 07:00) to start the daytime chain. The hourly Claude check-in restarts a broken
    chain from 7:41 AM;
  - cold call-sheet rows (T1 / T3 / T4) get their opener e-mail the same morning (the call follows it); only follow-up
    rows (T0 / T2) wait for their call (`gmail_autopilot.held_for_calls`);
  - each daytime run re-values up to 3 listings flagged `comps_need_refresh` (fixers first, ≤ 40 s, 150 / day:
    `offer_math.trickle`) and 4 unvalued listings (`value_new_listings` cap 4, 1 minute), so supply grows all day;
  - the first cloud harvest (2026-11-04): 104 ZIPs, 10,252 listings seen in 41.6 minutes, not blocked; listed leads
    1,447 → 2,796 with 1,567 unvalued. So after 9 PM the last desk run first dispatches task `value`
    (`value_new_listings`, cap 600, 70 minutes at night / 30 in the day), whose own hand-off then starts night.yml;
  - `DAILY_OPENERS` 35;
  - the refresh's run-all may take 80 minutes before 7 AM and 40 in the day (job timeout 100); a cut-off is not a
    failure (agents commit as they go; the state is saved in `finally`) and the rest of the run still happens;
  - three opener waves (`OPENER_WAVES` 10:30, 14:00, 16:30); each sends its share of what is LEFT of the day's
    `DAILY_OPENERS` (swarm_state `sent_total`), so a short morning supply is made up after the calls and the firm-up;
  - a call-sheet lead is held from e-mail only until it is dialed: a reported no-answer / voicemail
    (`gmail_autopilot.unanswered_today`) gets the e-mail the same day;
  - desk.yml checks for a queued run before its ~6-minute wait, so a note or approval starts at once;
  - every hand-off (desk.yml's next run, night.yml's hop / wake) goes through `dispatch()`: a GitHub 5xx is retried
    after 15 / 45 / 90 / 180 s, and a run that got queued despite the error counts as done (2026-11-06 7:38 PM: one
    HTTP 502 on the dispatch had stopped the evening chain);
  - task `stats` (`engine/outreach_stats.py::funnel`) prints counts only: listed, valued, tiers + top skip reasons,
    convertible, reachable, never e-mailed, cold / all e-mail in 24 h, replies and bounce % in 7 days;
  - a note item's "agent_phone" + "agent_name" saves a different person the office named as the listing agent (her own
    contact, not the office's e-mail); a note or approval run before 5:17 AM hands back to night.yml for the 5:17 refresh
    (it used to hand off with wake 07:00, which would have replaced the sleeper and skipped the morning refresh);
  - follow-ups due any time today are T2 rows on the 5:17 sheet (a no-answer from last night is due at 10:00; left off the
    sheet, each had become its own calendar event: 40 "Try again" events at 10:00 on 2026-11-05); a number reported on a
    call that is far above our final (final < 80% of it) sets the next touch to the ~30-day check-in, not tomorrow;
  - a due follow-up whose house is now tier skip (retail, too far, HOLD) takes no sheet slot (it would only print "SKIP
    TODAY"), and due follow-ups are ranked conversations first (the agent replied or a call reached them), then oldest
    due (`call_sheet._talked`): 2026-11-09, 19 of 100 rows were skips while 19 fresh follow-ups and three promised
    call-backs got separate 10:00 events;
  - the ask-first note ("what would the seller need to net?") goes out once per lead (`asked_net_before`): an agent who
    answers "please submit an offer in writing" waits for a number or the one fact the number needs (2026-11-05, Tracy);
  - a lead's fact question (beds / baths / sqft, rent roll, unit sales + HOA) goes out once: `asked_facts_before` keeps
    auto-answers and `auto_approve` from asking it again after the agent answered (2026-11-05, 12 Tasley Court);
  - a bounced address is remembered (swarm_state `bounced_emails`, `agent_outreach.record_bounce`) and `deliver` refuses
    it from then on, whichever card still carries it (2026-11-04: a 48-hour nudge went to an "inbox full" address that
    had bounced that morning; first-week bounce rate 3.8%, 5 of 130);
  - a sealed note item `{"operator": {"phone": ...}}` changes the operator's details on the desk (validated, saved in
    its settings.local.yaml): every e-mail, LOI, call script and voicemail line follows.
  - Gmail dropping the line (2026-11-05: a TimeoutError and an IMAP "abort" each failed a whole run) costs one inbox
    check, not the run: `_work` reports `gmail_error` with the exception name, the calendar still syncs, the run stays
    green, and the next run picks up from the last UID it saw. The config-bundle check at the start of every run is
    guarded the same way (`config_check` = the exception name); a calendar error reaches the public log only as a
    short code (`cloud_desk.calendar_code`: busy-403 / busy-429, offline-<Exception>, auth, error-<status>).
  - A config bundle from the Mac (a calendar re-connect) brings the Mac's settings.local.yaml, but operator details
    changed on the desk by a sealed `operator` note stay (`set_operator` lists them under `cloud_desk.operator_keys`;
    `apply_bundle` keeps them). 2026-11-06: the Mac's re-connect had put the old phone back into every script.
  - "Proof of funds?" in every call script (block events, call briefs, `call-sheet --script`) and the battle card is
    answered from the vault (`jv_partner_vault.pof_answer`): "Yes, it comes with the written offer" only when an
    active partner's proof of funds is verified, else "Not a letter today. I'm lining up the funding with a partner
    investor ..." (CFA: every claim true). The battle card no longer offers to close in our company's name.
  - A reply we already answered is never answered again (`gmail_autopilot.answered_since`): when one of our e-mails
    to that lead went out after the reply came in (a sealed note, an approval, the operator's Gmail), `approvable`
    holds it and `auto_approve` closes it ("answered already"). 2026-11-06: the desk read Jean's 7:37 reply only
    after Claude's 7:51 answer and sent a second, contradicting note at 8:04.
  - "We just arip'd it" (ARIP = attorney review in progress) reads as under contract, and "feel free to show for
    backup" / "as a backup" as BACKUP (which wins, so the backup offer can go out). 2026-11-06, 5 Linden St.
    "Are you interested in a tour?" is ASKS_SHOWING (photos / video first), not an open question.
    "Just started review" / "currently in review" is under contract (it answers our opener's "or already in attorney
    review?"); "your offer is in review with the seller" is not. "I anticipate it not being anything the seller would
    consider" / "won't even consider" is a no. A call slot set by a bounce is titled "📞 Call <agent> about <street>
    (your e-mail bounced)" and its script opens "my e-mail to you bounced", never "they asked you to call".
    "I was under contract a month ago, but it's back on the market" / "it WAS under contract" is available, not under
    contract (2026-11-06, 36 Curtis Ave: the desk had congratulated the agent). "We have a few offers" with nothing of
    ours in writing gets a written best and final only on a firm number: an ask-first house or one with a certainty
    issue (no living area, old comps) gets "facts first" (the one fact, or the seller's net), no number (4 Richard Ter).
    "It is still aviilable" / "availble" (typos) read as available (2026-11-06, 492 Booth Ct had waited as a question).
    "It is sold as is" is the condition, never SOLD (the desk had congratulated an agent on a contract that
    didn't exist); "the asking price is $549k" / "listing price is 525,000" / "asking is 395, make an offer" is
    the list price quoted back, not the seller's number (`_LIST_BEFORE`), unless "firm" / "net" follows.
    "It is near asking price" / "close to asking" / "around list price" / "just under asking" (2026-11-07, 5 Fitch Rd, the
    answer to "what would the seller need to net?") is their number = about the ask, never stated plainly
    (`reply_negotiator.near_ask_asked`; not "nowhere near" / "not close to", never a question).
    "They would want at least asking $375,000" / "nothing less than asking" / "seller wants asking" (2026-11-09, 8 Japril Dr)
    is their number = the ask, stated plainly (`_FULL_PRICE`: "at least" / "no less than" before "asking"; bare "asking").
  - A sealed note item's "sqft" / "beds" / "baths" (an agent's e-mail or a call) is saved on the property
    (`sqft_source` "agent") and the house is re-valued live (`cloud_desk.save_facts` → `offer_math.firm_up`): most
    "agree to nothing (the living area isn't on record)" rows wait only on the size. The run log adds
    `facts_saved` / `now_firm` counts.
- **Done is visible** (learned 2026-11-04, the operator: "anytime I confirm I did something it gets a check mark or a
  color … I will have class and miss some calls"):
  - call blocks: the title counts what is left ("📞 Call block B: 9 to call · ✅ 16 done"); reported rows are ✅ in a
    "Done" part, "SKIP TODAY" rows ⏭; nothing left = "✅ Call block B: all done", graphite (`colors.done` 8), no reminder;
  - a passed block keeps updating ("⏩ … moved to Catch-up calls") and its unreached rows go to one
    `callblock:<day>:catchup` event (tangerine), same codes: the first free 30+ minutes after the last block with
    calls, else (an evening class, 2026-11-04) the first free 30+ minutes from now; it stays put (calendar state
    `catchup`) until its time passes, then moves forward; anything still left is on tomorrow's sheet by itself; row
    status uses the sheet's own date (`call_sheet.refreshed(..., day)`);
  - with no free 30 minutes left today (2026-11-04: class, blocks C-D, an evening reception), missed rows join the
    next block that hasn't ended ("📞 Call block C: 20 to call + 16 from earlier", "--- Also from earlier blocks ---");
    after the last block they go on tomorrow's sheet by themselves;
  - a plain thank-you ("Sounds good! Thank you!", `reply_negotiator.is_ack`) after a no-price note is dismissed with
    nothing to answer and no call; after a priced offer it is read like any reply (it may be an acceptance);
  - a short yes to our "mind if I check back in about 30 days?" ("Sure, you can absolutely", `rn.is_checkin_ok`,
    only when our last answer asked to check back: `gmail_autopilot.asked_to_check_back`) is dismissed and the check-in
    stands; no Touch 2 goes out by itself once the agent gave a number on the house (`earlier_counter`); the answer
    greets the person who wrote (a colleague the agent handed the thread to), not the card's agent; "Prob 550" /
    "around 480" / a bare "550" read as thousands, never "stated plainly" (2026-11-04: Geri, Juliet);
    "Full price" / "seller wants asking" / "firm on list price" is their number = the ask, stated plainly
    (`reply_negotiator.full_price_asked`; not after "below" / "under" / "not", never "what's the asking price?");
    their number above our final while our own number still needs a check (`certainty_issues`: no living area, old
    comps) gets no ceiling or best-and-final in writing: "facts first" asks for the one fact (`_doubt_ask`) instead;
  - a row whose agent e-mailed back today and was answered says so ("THEY E-MAILED BACK (…; the desk answered by
    e-mail)"), or "SKIP TODAY: they e-mailed back (off the table for now)" when the door is closed;
  - follow-up / call-back / reply events handled today (`gcal_sync.handled`: the operator's call or an approved /
    dismissed reply; or a desk e-mail when no newer event replaced it) stay in place as "✅ …" graphite with
    "✅ DONE <time>: <what happened>" and no reminder; one that was only rescheduled (a new call slot, a moved
    call-back) becomes "⏩ … (moved to 5:15 PM)", never ✅; handled ones later in the week are removed as before;
  - events Claude made by hand with the Calendar connector are marked the same way (✅ + colorId 8) when the operator
    says they're done.
- **Rows follow the day**: block events re-read every row on every sync (`call_sheet.refreshed`): "DONE 11:00
    (voicemail): skip" once reported in the chat, "THEY E-MAILED BACK: read it in Gmail first" when an agent wrote back,
    the current firm words, or "SKIP TODAY". Codes stay frozen.
- **Wider listing supply, safely** (learned 2026-11-03, goal ~100 listing-agent calls a day; Nov 2 data: 648 of 1,110
  foreclosure / estate / vacant signals sat outside the 71 harvested ZIPs, 177 listing leads had no valuation, 37
  agent "phones" were fax lines). Rules now:
  - **Where**: `stale_listings:` in config/settings.yaml (104 quoted ZIPs, `min_days_on_market: 60`,
    `max_list_price: 900000`); `DEFAULT_ZIPS` in the agent is only the fallback. Towns in swarm_state.auto_expansion
    add the ZIPs our own parcels show there (≥ 5 parcels and ≥ 10% of the town, ≤ 4 per town); a town with no parcels
    yet is noted, never guessed.
  - **A refusal**: the harvest stops at the first `ProviderBlocked`, keeps every ZIP already harvested (committed per
    ZIP), records it in the run notes, metrics and swarm_state, and asks nothing until the pause ends (see the data-source
    rules). Never retried around.
  - **Off the market = out of outreach**: a pending / contingent / sold listing retires its STALE_LISTING signal at once;
    a listing missing from its ZIP's last 3 successful harvests (`deactivate_after_missed_harvests`; an empty answer is
    not a harvest; history in swarm_state `stale_listing_harvests`) becomes `off_market` and its signal is retired; the
    stacker never matches an off-market listing (its ACTIVE_LISTING retires). Back on the market = active again.
    `scoring.listing_off_market()`: its listing agent stops counting for READY_TO_CALL and the striking-distance bonus,
    the call queue's cold part (`best_phone(live_listing_only=True)`) and the autopilot deck skip it (unless the agent
    already replied); agent_outreach.candidates / fast_track already require an active listing signal.
  - **Fax lines are never callable**: `core.text.is_fax_label`; advertiser fax numbers are never saved as a phone (an
    agent with a fax + e-mail is an e-mail contact), the harvest stores the first non-fax number (+ `listing_agent_fax`),
    `listing_agent_enricher.scrub_fax_contacts` moves old fax numbers off contacts (raw_json["fax"]), and
    `compliance.callable_phone` / `can_call`, `crm.best_phone` / call queue, `scoring.ready_to_call` and the deck card
    refuse a phone labelled fax.
  - **Every listing lead gets a number**: `value_new_listings` (engine/underwriter.py `NewListingValuationAgent`, after the
    top-N underwriter in run-all and the dashboard's full refresh) values open leads on a live listing with no current
    valuation, highest priority then newest, `underwrite.new_listing_cap` (300) per run inside `new_listing_max_minutes`
    (20, ~90 valuations at the ~13 s measured live: the cloud desk's run-all has a 45-minute limit); the daemon's nightly
    2:10 run gets 120 minutes (`agents.schedule_options`, passed only to scheduled runs). A try with no comps waits
    `new_listing_retry_days` (3). Unmatched listing parcels carry the listing's own coordinates, so they can be valued.
- **The flat-fee AI Deal Desk** (2026-11-09 pivot, `engine/deal_desk.py`; the operator chose software over a license):
  buyer boxes from recorded deeds per buyer family (towns by mun_code, 36-month purchases, q25-q75 price band without
  package deeds, 1- vs 2-4 family); `select_buyers` = every named-principal family (≤ 34) then the top multi-buy LLCs
  (≤ 20, ≥ 3 purchases). A one-page landscape **Weekly Deal Digest** per buyer: up to 10 deals in their towns (then
  counties) whose quant buyer max fits their band, a mix of MLS fixers / listings (buyer max ≥ 75% of the ask, never
  retail, HOLD, outlier or off-market; the listing agent's business e-mail and a phone on no stop list), sheriff sales
  with room over the upset (an upset under 25% of buyer max is a partial figure: skipped) and pre-foreclosures (FRFPA
  note). No owner / defendant names; suppressed addresses never appear. Footer: $500 30-day trial, then $1,000/month
  flat, zero commission or per-deal pay, the client is the buyer from their own inbox and approves every offer, "not a
  licensed broker ... paid the flat fee only, never on a deal" (N.J.S.A. 45:15-3: one act for a fee is brokerage), the
  operator's contact, postal address and "no more". Desk task `digest` mails them to the operator's own inbox
  ("[njw-digest] <date> i/n", 10 PDFs each); the run log shows `digests` / `digest_rows` / `digest_mails` only.
  "Named" = a person on record behind the family (a deed contact that isn't the entity again, or the vault's likely
  principal); fewer named families leave room for more LLCs (54 in all). "Owner @ <address>" cluster buyers (no name)
  and buyers with fewer than 3 deals in their box this week get no page.
- **Prospects** (`prospects` table, `deal_desk.seed_prospects` / `update_prospect`): one row per digest buyer, stages
  NEW → CONTACTED → SAMPLE_SENT → CALL_BOOKED → TRIAL → PAYING (or LOST), with public business contacts (website,
  business e-mail / phone, LinkedIn URL) and their source. A sealed note item `{"prospect": {"entity", "stage",
  "email", "website", "phone", "linkedin", "source", "notes", "next_touch", "fee", "alias"}}` updates it; "alias" (the
  deed name, e.g. "Owner @ <mailing>") lets the company found for an unnamed cluster buyer name its digest. Contacts come from
  business sources only (never people-search sites, OpenCorporates or a guessed e-mail pattern).
- **Flipper-connected agents** (`deal_desk.flipper_agents`, sent with every digest run and alone by task `agents` as
  "[njw-agents] <date>" + CSV): +5 per listing of a house a recorded cash buyer owns (a flip resale), +0.5 per active
  cash-buyer family in the town for each fixer listing (0.15 otherwise, capped at 12 so volume can't win), +3 if the
  agent already wrote back; shared office inboxes (info@, office@, ...), bounced / stopped agents and stop-listed phones
  are left out.
- **✅ Today's ten** (`deal_desk.daily_actions`, `gcal_sync.actions_event`, key `actions:<YYYYMMDD>`, blueberry):
  replaces the 100-call blocks. One weekday block of at most ten actions, fitted into the first free 45 minutes
  from 11:00 around classes: agent replies waiting (≤ 4, "send #id"), call-backs due with agents who talked to us
  (≤ 3), then flipper pitches (prospect follow-ups due, prospects with a business e-mail to forward the digest to; only
  when nobody has a contact, "find a business e-mail for ..."). With DAILY_OPENERS "0" the recurring 🎯 daily-goal
  event is not desired and is deleted from the calendar.
- **The desk's own number in a note** (2026-11-09, warm threads answered from the chat): a sealed note item with
  `"price": "firm" | "final" | "sent"` and `{price}` in its "reply" gets the desk's locked number written in and the LOI
  attached at it (`offer_math.note_price`): "firm" = the one offer (on a house whose only gap is the reach, the same
  offer math, `FirmOffer.opening`), never under a number already sent; "final" = best and final; "sent" = restated.
  HOLD, retail, FRFPA, verdict skips, certainty issues (no living area, old comps) and an offer already above today's
  final are refused and nothing is sent, unless the item has a no-price `"fallback"` text, which goes instead;
  `"at_least"` keeps an older two-option offer's higher option. Run log: `desk_priced` / `desk_fallback` counts only.
- **Paused 2026-11-09 (operator pivot away from cold MLS wholesaling)**: desk.yml `DAILY_OPENERS: "0"` (no new cold openers or
  48-hour nudges) and `CALL_SHEET: "off"` (`cloud_desk._work` builds no call sheet and clears today's frozen one, so the call
  blocks leave the calendar). Replies to agents who already wrote back, the data harvest, valuations and the calendar keep
  running. Set `"35"` / `"on"` to restore.
- **The 10-stream research firm** (2026-11-09, the operator: "route every listing, sheriff sale, deed and tax record
  through 10 revenue streams"; `engine/swarm_firm.py`, `engine/firm_outbox.py`, `engine/streams/`):
  - **Contract** (`engine/streams/base.py`): each `sN_*.py` defines `STREAM` (a `Stream` with `key`, `cadence_hours`,
    `run_hours`); `run(session, ctx)` returns a `StreamResult` (counts, PDFs written to `ctx.out_dir`, proposed
    `Outbound` e-mails). Streams never send. Flat-fee research only: no referral fees or pay tied to a deal or a client
    (N.J.S.A. 45:15-3; NJ RPC 7.2(c) / Joint Opinion 741: per lead fine, never per retained client); no opinion of
    value (N.J.S.A. 45:14F: an appraisal needs a license); no private individual's name in a product; every address
    through `address_ok` (Daniel's Law); public records / listing text / our math only, never private e-mail content.
  - **Outbox** (`firm_outbox`): `enqueue` stores each Outbound under its natural key (never twice) after the stop
    lists (bounced, OPTED_OUT / LOST target, a listing agent's dnc contact, our own address) and the pitch gap (one cold
    pitch per target per `pitch_gap_days` 6, `max_touches` 2). `send_due` sends by priority inside rolling-24 h caps
    (`research_firm.caps`: p1 replies 150, p2 deal-desk pitches 15, p3 specialty 8; conservative because Gmail's
    program policies forbid unsolicited commercial mail from a personal account: move cold B2B to a Workspace domain
    with SPF / DKIM / DMARC before raising them), cold mail weekdays 9-5 Eastern, `per_run` 6, 8-20 s apart, under the
    account-wide `reply_daily_limit`. Every message: signature, postal address, "no more" opt-out; pitches also the
    commercial-message line and a `List-Unsubscribe` mailto header. A failing send retries up to 3 times.
  - **B2B replies**: the inbox check (`gmail_autopilot.poll_gmail_replies`) downloads a message matched to a pitch's
    Message-ID, or from a pitched address that isn't in a lead thread, into `firm_replies` (`firm_outbox.handle_reply`):
    `classify` -> opt_out (target OPTED_OUT at once) / not_interested (LOST) / interested (INTERESTED, a warm action) /
    question (REPLIED); a reply ends cold touches and skips queued pitches. A bounced pitch is remembered and the target
    LOST. Sealed-note items: `{"firm_reply": id, "reply": "...", "stage": "TRIAL"}` answers in their thread (priority
    1, no ad line) or closes it; `{"firm_target": {...}}` adds / updates a target.
  - **Orchestrator** (`swarm_firm.tick`, every daytime desk run with `FIRM=on`): seeds `config/b2b_targets.yaml` once a
    day, runs due streams one transaction each inside `budget_seconds` (150; a crash is recorded by name and never stops
    the others; `firm_stream_runs`), then `send_due`. Task `firm` prints counts only (`sN_found` / `_products` / `_sent`
    / `_queued` / `_error`, `firm_mrr`, `firm_waiting`, `firm_warm`); task `firm-run` runs every stream now (900 s).
    `manage.py firm-status [--json]` / `firm-run [--only s2,s6] [--dry-run]`; `/firm-status`, `/check-in`. Recurring
    revenue counts only targets in TRIAL / PAYING (no projected pipeline money).
  - **s1 acquisitions** (`s1_acquisitions.py`, 6 h): the desk's funnel counts + every lead with a live agent conversation
    matched to its top 3 recorded cash buyers (buyer_matches, internal: nothing is marketed before a contract's attorney
    review). **s2 AI Deal Desk SaaS** (`s2_deal_desk_saas.py`, hourly 9-4 weekdays): flippers with a business e-mail
    (prospects) and the top 150 flipper-connected agents become targets; each run pitches up to 4 due targets with a
    digest built for them (flippers: their deed towns; agents: the towns they list in, never their own listings) and one
    follow-up after the gap; never an agent we are in deal talks with (a reply or a lead e-mail in 30 days); greetings
    use a person's first name only (a brokerage in the name field gets "there").
  - **s3 surplus research** (`s3_surplus_overages.py`, daily): sold-to-third-party sheriff sales (status / history "Purchased
    - 3rd Party", or a recorded sheriff's deed NU code 12 to a non-plaintiff grantee) and a 60-day watchlist where value
    minus known debt >= $75K (association / municipal plaintiffs and upsets under 25% of value left out). CivilView
    carries no winning bid, so surplus = bid - payoff - prior liens only when a bid is on record; otherwise "winning bid:
    from the recorded sheriff's deed or the sheriff (OPRA)". One-page county PDFs (docket, sheriff #, plaintiff lender,
    upset, liens, formula; never a defendant / person name), "for attorneys only", law footer (N.J.S.A. 2A:50-37, R.
    4:64-3, R. 4:57-2, 46:30B-106 finder limits, Zanolini). Pitched to `foreclosure_surplus_attorney` targets: $400/month
    or $150 a county report, never a percentage or per retained client (Joint Opinion 741).
  - **s4 contractor rehab triggers** (`s4_contractor_leads.py`, daily): a negation-aware classifier over live listing text
    (OIL_TANK / ROOF / HVAC / STRUCTURAL / FULL_GUT; "new roof", "no oil tank", "tank removed", home-warranty "roof leak
    protection" don't fire) + entity purchases in 120 days (FRESH_FLIP); per-county one-page reports quoting <= 12 words of
    the public listing (no phones / e-mails / names), pitched to the five contractor kinds with their own trade's rows
    first, $500/month per county.
  - **s5 hard-money borrower feed** (`s5_hard_money_feed.py`, daily): entity buyer families only (no person names, no
    "Owner @" clusters, no suppressed mailing addresses), >= 2 purchases in 36 months, in-rehab counts (bought <= 9 months)
    and resales listed; one-page feed of the top 40, "not a consumer or credit report", pitched to lenders, $750/month.
  - **s6 tax-appeal evidence** (`s6_tax_appeals.py`, daily): official Chapter 123 ratios for all 267 target-county towns in
    `config/streams/nj_chapter123_ratios.yaml` (tax year 2027 certification of 2026 ratios, promulgated 2026-10-01; refresh
    every October and check in January for Tax Court amendments). Internal Chapter 123 screen (assessment above 1.15 x R on
    similar recorded sales, >= 15% reduction) picks the properties; the PDF prints only recorded facts, per-sale
    "assessment / sale price", the range and "If the true value were V, the Chapter 123 assessment would be R x V" with V
    blank, the deadline (April 1; Monmouth January 15; May 1 in revaluation towns) and, at the top in body type, "Public-record
    data; not an appraisal, BPO or CMA; no opinion of value" (N.J.S.A. 45:14F). No value, savings or "over-assessed".
    Pitched to tax-appeal attorneys: $100 a packet or $500/month.
  - **s7 graduated-fee cliff net sheets** (`s7_mansion_tax_cliff.py`, daily): dead zones solved on a $500 grid from the YAML
    tiers via quant_pricing (above $1M to $1,010,500; $2M to $2,021,000; $2.5M to $2,513,000; $3M to $3,016,000; $3.5M to
    $3,518,500); a listing priced inside one gets a one-page net sheet and a free courtesy note to its agent (one per
    listing ever, one per agent per 30 days, none to a lead e-mailed in 30 days). Supply is thin: harvested listings stop at
    $900K and agents mostly price under the line.
  - **s8 contract fallout** (`s8_contract_fallout.py`, hourly 8-8): pending / contingent -> for_sale transitions (snapshot in
    swarm_state `s8_listing_status`) and under-contract / backup replies 14-60 days old with the listing still live; a
    non-commercial note in the agent's thread ("my offer of $X still stands" only when our written offer <= today's final,
    else "what would the seller need now"), at most 5 per run, only agents we already talked to (`cold_ok: false`);
    detections kept in swarm_state `s8_fallout` {"items": [...]} for the digest.
  - **s9 cost-seg / 1031 feed** (`s9_cpa_1031_feed.py`, daily): Table A entity purchases of 2+ unit / class 4 property
    $500K-$5M in 18 months (deed exports, cash-buyer purchases, MOD-IV); Table B listed multi-family / class 4 with a last
    recorded sale 8+ years ago and ask >= 1.5x it (implied growth > 12%/yr dropped as nominal deeds); no owner names; "may
    qualify; the client's CPA decides" (100% bonus only for <= 20-year components acquired after 2025-01-19; NJ decouples).
    Pitched to cost-seg firms, 1031 intermediaries, real-estate CPAs, $500/month.
  - **s10 pre-MLS distress** (`s10_pre_mls_distress.py`, daily): lis pendens, tax certificates / delinquencies, estates and
    sheriff sales with a fresh adjournment (snapshot `s10_adjournments`) on unlisted properties; adjournments left = 5 -
    used (N.J.S.A. 2A:17-36); an INTERNAL one-page queue mailed to the operator's own inbox ("[njw-s10] <date>",
    `StreamResult.internal` -> `swarm_firm.mail_internal`); never an e-mail to a homeowner (FRFPA: letters only, operator
    approved).
  - **B2B targets** (`config/b2b_targets.yaml`, 121 firms, 75 with a published e-mail, each with its source page): tax-appeal
    and surplus attorneys, hard-money lenders, cost-seg firms, 1031 intermediaries, CPAs, oil-tank / roofing / HVAC / GC /
    dumpster companies, investor-focused brokerages. Measured on the Nov 2 snapshot (forced run, 43 s): s1 101 convertible
    of 1,449 listed; s2 4 digests; s3 257 rows / 8 PDFs; s4 97 triggers / 11 PDFs; s5 22 entities; s6 19 packets of 1,795
    high-tax parcels; s7 0 in a dead zone; s9 119 triggers; s10 123 rows.
  - **First live day (2026-11-09), fixed the same afternoon**:
    - targets are seeded whenever `config/b2b_targets.yaml` appears or changes (`swarm_firm.targets_digest`), never
      marked done while it is missing (the first run predated the file and every specialty stream had nobody to pitch);
    - s10's PDF goes in `StreamResult.internal`, so `mail_internal` sends "[njw-s10] <date>" to the operator's inbox;
    - pitch copy states only counts the data shows ("3 fixer listings and 1 investor resale around Elizabeth", never
      "a lot of"); a buyer family filed under a person is "you", never named in a subject; subjects say "week of
      <Monday>" (`streams.base.week_of`);
    - reply reader: an amount beside a value / fixed-up / comps cue with nothing about the seller in between is a value
      opinion, not a counter ("more like $645,000 give or take vs $670,000"); "the ability to take her house off the
      market by going into contract if ..." is hypothetical, not a contract; "tenants will be out before closing" adds
      no tenant buffer;
    - a priced reply sent from a sealed note carries "Who's buying: an investor buying as a principal ... may assign
      the contract" when its text doesn't say it (the attached LOI says it too);
    - run log: `sN_waiting` per stream (s1 = agent replies needing approval);
    - Touch 2 never goes out by itself once an offer is on record (`lead.offer_amount`): 1708 Heck Ave got its opening
      offer e-mail again after three offers, because "no repairs or credits available for the buyer" read as
      "it's available" (the bare pattern had no word boundary: "cred-its available"); "available" after credits /
      repairs / financing / concessions / times is never the house (`reply_negotiator._AVAIL_NOT_HOUSE`);
    - the inbox step is repeated once after a Gmail timeout (60 s per socket operation): 6 of 29 runs had lost it.
  - **Written answers, no stalls** (`reply_negotiator`): a "call me" with no number gets "I'm in classes; e-mail is
    fastest; the seller's as-is bottom line and big repair items?" (or a call-window ask when the agent won't negotiate
    by e-mail); a proof-of-funds ask with no verified partner gets "no letter today; the sqft and condition?" instead of
    the old promise of a letter. Both are `SAFE_NO_PRICE` (sent by themselves).
  - **Calendar** (`calendar.mode: firm`, `gcal_sync.firm_mode`): only the 📊 10-Stream Firm Status (key `desk:<day>`,
    the stream table above the desk's own lines), attorney-review / FRFPA / closing deadlines, the sign-in reminder and
    a ✅ event of at most 5 warm actions (an agent accepted; a B2B prospect said yes). Follow-up / call-back / reply /
    sheriff-sale / call-block / daily-goal / today's-ten events are no longer desired, so their future copies leave the
    calendar on the next full sync. Tests pin the old full mode (`conftest._full_calendar`).
- **Tests never touch Gmail**: tests/conftest.py points IMAP at a closed local port (WHOLESALING_IMAP_HOST / _PORT),
  and tests/test_gmail_autopilot.py serves messages from a fake IMAP connection and records sends on a fake SMTP.

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
- Never commit `wholesaling.db`, caches or exports (personal data). The AES-256-GCM snapshot from
  `manage.py db-sync --pack` is ignored too while the repository is public (rules below). `logs/swarm_intelligence.md`
  is committed because it holds no PII. Recon docs and test fixtures use `[individual]` / `[address]`
  or invented names (e.g. "QUINN Q SAMPLE"); run `python manage.py pii-scan` before pushing. Commits made
  before 2026-10-27 still contain unredacted recon names in git history (not rewritten).

## The autonomous agent runner

- Every agent subclasses `agents.base.BaseAgent` and implements `run(ctx: AgentContext) -> None`.
  `BaseAgent.execute()` wraps it: opens an `agent_runs` row, records counts/errors/notes,
  never lets one agent's crash stop the swarm.
- `swarm/orchestrator.py` holds the registry (`AGENTS`), dependency order and cron schedules
  (from `config/settings.yaml`). Modes:
  - `python -m swarm.orchestrator run-all` — run the full pipeline once (scrape ∥ [sheriff + lis pendens, data refresh] → owner enrichment →
    contact enrichment [municipal_owners → listing_agents → owner_resolver → signal_stacker] → score → underwrite →
    value_new_listings → outlier_guard → re-score → fast_track → buyer_matcher → closing_alerts → self-improve).
  - `python -m swarm.orchestrator run <agent>` — run one agent.
  - `python -m swarm.orchestrator daemon` — APScheduler loop using the cron schedules.
  - `python -m swarm.orchestrator status` — recent runs and health.
- The **Self-Improvement & Alpha Discovery Agent** runs last in every pipeline: audits scraper
  success rates, data coverage, contact readiness (top-100 leads with a callable phone), ready-to-call and
  stacked-distress counts, agent LOIs awaiting follow-up, outlier-guard coverage, listed foreclosures not yet
  contacted, overdue closing milestones, the money picture, buyer-match
  coverage, DNC freshness, call outcomes, comp coverage and spread viability, writes findings to
  `logs/swarm_intelligence.md`, tunes the scoring weights stored in `scoring_weights`,
  and proposes adjacent municipalities with better spread-to-ARV ratios.

## Common commands

```bash
pip install -e ".[dev]"                     # install
python manage.py init                       # create/upgrade the database
python manage.py today                      # the daily checklist (the dashboard's 🎯 Autopilot is the click-through version)
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
python manage.py agent-blast --top 50       # listing-agent LOI packets → deals/agent_blast/<date>/index.html
python manage.py agent-blast --mark-sent <id> [--channel sms]   # log a packet sent by hand (48h follow-up)
python manage.py mail-merge --top 50        # letters + Click2Mail CSV; then --mark-mailed <batch dir>
python manage.py inspect <id>               # virtual inspection dossier
python manage.py stack                      # signal stacker (listed distress, portfolio owners)
python manage.py buyer-hotzones             # where cash buyers pay the most + our deals there
python manage.py walkthrough <id> --invite <buyer>   # after attorney review: remote buyer walkthroughs
python manage.py bid <id> --buyer <b> --amount 470k --emd 5k ; bid <id> --accept <bid> [--assignee "ABC LLC"]
python manage.py fast-track [--packets] [--script <id>] [--portfolios 20] [--portfolio-letters 20]
python manage.py closing [<id> --done EMD_DEPOSITED | --title-email | --close --fee 22.5k | --disapproved]
python manage.py finance [--add-expense 120 --category mail]   # ledger, pipeline, 28% tax reserve, due dates
python manage.py partners --county Essex    # sourced NJ title agencies / closing attorneys
python manage.py inbound --ingest export.json   # Formspree / webhook / mailto submissions → leads / buyers
python manage.py site --build site_dist --endpoint <form URL>   # static funnel for free hosting
python manage.py build-site --out site_dist --check   # same build + the publish gate (CI: deploy_site.yml)
python manage.py install-shortcut           # Desktop launcher: double-click → server up → browser open
python -m swarm.orchestrator run lis_pendens --opt days=30   # clerk filings (Union, Essex, Monmouth + imports)
python -m swarm.orchestrator run outlier_guard  # re-check every valuation (comp hygiene + ARV guard)
python -m swarm.orchestrator run value_new_listings --opt cap=500 --opt max_minutes=0   # value every unvalued listing lead
python manage.py pii-scan                   # before every commit: DB names in tracked + new files → exit 1
python manage.py calibrate                  # local investor pricing (alpha) from recorded buyer purchases → config/market_calibration.yaml
python manage.py recalibrate                # re-price every valuation under the quant model + striking-distance report
python manage.py vault --seed 30            # JV partner & POF vault; --script <id>, --touch <id>, --pof <id> --file pof.pdf [--share]
python manage.py skiptrace-fast --links     # fast skip-trace queue; --lead <id> --paste page.txt saves a copied people-search page
python manage.py reply <id> --text '...'    # agent reply → traffic light, math, 3-sentence answer (--dry-run to preview)
python manage.py structure <id>             # assignment vs double close net check (--contract 250k --buyer 295k works without a lead)
python manage.py docs <id> --type rider     # Item 14 / Section 37 attorney-review assignment rider
python manage.py vault --unmask --gis      # partner families + likely principals, re-rank, top 10 (--out data/exports/x.json)
python manage.py preflight --top 10        # GO / VERIFY / HOLD cheat sheet for the first deck cards (--out data/exports/x.md)
python manage.py db-sync --pack            # encrypted DB snapshot → data/snapshots/ (key: DB_SYNC_KEY in .env; --new-key rotates)
python manage.py db-sync --unpack [KEY]    # laptop: restore wholesaling.db (--force replaces data, backed up first); --status
python manage.py setup-calendar            # once: config/credentials.json → browser consent → config/token.json → first sync
python manage.py sync-calendar             # two-way Google Calendar sync (--dry-run lists the events, --status shows the queue)
python manage.py gmail --dry-run            # Phase 8: today's paced batch (no send); --run [N] sends it; --check reads agent replies
python manage.py gmail --status             # today's counts, 🔥 Action Needed, queued Touch 2; --approve <id> / --dismiss <id>
python manage.py gmail --test               # sign in to smtp.gmail.com + imap.gmail.com with the saved app password (--send-test: note to yourself)
python manage.py inbox                      # /inbox: Gmail Sent import → agent replies → "still available" offers out now → table (--json, --no-send)
python manage.py gmail --find "22 Grand"    # leads by street / town / agent; --reply LEAD --text "..." [--price N]; --send-offer LEAD
python manage.py call-sheet --script        # today's 100 calls in blocks of 25; --report "A01 vm, A02 340" logs results
python manage.py autopilot-daemon           # hands-free inbox loop 8 AM-9 PM; install-daemon puts it in macOS launchd (--uninstall)
python -m swarm.orchestrator run gmail_replies  # one inbox check (the dashboard also checks every 5 minutes)
pytest                                      # tests
```
