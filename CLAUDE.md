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

Target counties (priority order): **Middlesex, Union, Essex, Hudson, Passaic, Bergen, Camden**.
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
| **5. Lead Sources & Learning at Scale** | Union + Essex clerk lis-pendens agents (names → parcels via owner lists), NJ permits (demolition/alteration → flipper + condition signals), paid skip-trace provider adapters, older municipal owner layers with deed-match rule (Middlesex 2022, Passaic City 2023, Essex/Passaic 2017), outcome-driven weight re-fits once calls are logged | Next |
| **6. Disposition at Scale** | Buyer list growth (more county deed exports + registered web buyers), deal sheets to matched buyers after attorney review, attorney-reviewed templates replacing the drafts, proof-of-funds tracking on bids | Planned |

## Directory structure

```
CLAUDE.md                     ← you are here
manage.py                     ← operator CLI (rich): briefing (A ready-to-call / B sheriff+estate spreads / C stacked),
                                lead, underwrite, buyers, stats, server, queue, script, call, occupancy, contract, docs,
                                skiptrace, dnc (+ --suppress-address), match, pii-scan, agent-blast, mail-merge,
                                inspect, stack, buyer-hotzones, walkthrough, bid, today, fast-track, closing, finance,
                                partners, inbound, site, swarm passthrough
PLAYBOOK.md                   ← operator's step-by-step guide: setup, daily 2-hour routine, first agent calls, contract →
                                attorney review → assignment → paid, taxes
config/settings.yaml          ← all tunables (counties/towns, MAO %, fees, rehab $/sqft, scoring weights, cron)
config/sources/               ← source directories (realauction_nj.yaml: 91 tax-sale sites in target counties)
config/nj_closing_partners.yaml ← NJ title agencies / closing attorneys checked on their own pages (source URL each),
                                county → region, verification resources, quarterly-tax dates and rates (with sources)
core/                         ← config loader, polite HTTP client (per-host rate limit, retries, disk cache,
                                host stats), text/address utils (postal_title), NJ business days, geo, ZIP (ZCTA)
                                resolver; pii_scan.py (DB names in git-tracked files → `manage.py pii-scan`)
db/database.py                ← engine/session factory, create + additive migrations, init lock (thread-safe)
db/models.py                  ← ORM: properties, distress_signals, valuations, leads (+ occupancy, FRFPA track, CRM
                                fields, ready_to_call), contacts, interactions, dnc_entries, mail_suppressions,
                                buyer_matches, buyer_bids, closing_milestones, financial_ledger, generated_documents,
                                cash_buyers (+ web registration fields), deed_transfers, listings, agent_runs,
                                scoring_weights, swarm_state
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
  today.py                    ← the 2-hour daily checklist (deadlines, 4 blocks) for CLI + cockpit home
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
web/app.py + web/static/      ← local cockpit (FastAPI, 127.0.0.1 only, Host/Origin guard): Today (home), dashboard,
                                battle station, Fast Track, Ready to Call, Agent LOI Blast, Inspector & Mail,
                                Walkthroughs & Bids, Closing & Money; serves site/ at /site/* (web.public_hosts may
                                reach only /site/* and POST /api/inbound/*)
site/                         ← static seller page + cash-buyer registration (site.js reads generated config.js;
                                `manage.py site --build` for GitHub Pages / Netlify); config.js + site_dist/ ignored
swarm/orchestrator.py         ← `python -m swarm.orchestrator` (list | run | job | run-all | daemon | status)
swarm/self_improvement.py     ← audits health/coverage/spreads → logs/swarm_intelligence.md; tunes weights;
                                auto-adds high-spread towns (swarm_state.auto_expansion)
docs/nj_data_sources.md       ← verified data-source report (what works, how, limits, manual checklist)
docs/recon/                   ← raw recon: legal_rules.md, comps_listings.md, assessment_owner_deeds.md,
                                tax_sale_clerk_opendata.md, public_owner_layers.md, nj_owner_records_client.py
data/imports/                 ← operator-downloaded files (owner_lists/, deeds/, tax_sales/<Town>/) — git-ignored
data/exports/                 ← skip-trace queues (CSV/HTML) — git-ignored
deals/<property_id>/          ← generated LOI/PSA/assignment/JV/deal sheets — git-ignored (names sellers)
deals/agent_blast/, deals/mail_batches/ ← agent review pages and letter batches — git-ignored
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
  is committed because it holds no PII. Recon docs and test fixtures use `[individual]` / `[address]`
  or invented names (e.g. "QUINN Q SAMPLE"); run `python manage.py pii-scan` before pushing. Commits made
  before 2026-10-27 still contain unredacted recon names in git history (not rewritten).

## The autonomous agent runner

- Every agent subclasses `agents.base.BaseAgent` and implements `run(ctx: AgentContext) -> None`.
  `BaseAgent.execute()` wraps it: opens an `agent_runs` row, records counts/errors/notes,
  never lets one agent's crash stop the swarm.
- `swarm/orchestrator.py` holds the registry (`AGENTS`), dependency order and cron schedules
  (from `config/settings.yaml`). Modes:
  - `python -m swarm.orchestrator run-all` — run the full pipeline once (scrape ∥ → owner enrichment →
    contact enrichment [municipal_owners → listing_agents → owner_resolver → signal_stacker] → score → underwrite →
    outlier_guard → re-score → fast_track → buyer_matcher → closing_alerts → self-improve).
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
python manage.py today                      # START HERE every day: deadlines + 4 blocks from live records
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
python -m swarm.orchestrator run outlier_guard  # re-check every valuation (comp hygiene + ARV guard)
python manage.py pii-scan                   # before every commit: DB names in tracked + new files → exit 1
pytest                                      # tests
```
