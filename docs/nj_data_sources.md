# NJ Data Sources — Verified Findings (Phase 1 recon 2026-10-26; Phase 2 additions 2026-10-27)

Every source below was tested from this environment with real requests on 2026-10-26. Verdicts:
**USE** (automated in production), **FALLBACK** (works, secondary), **MANUAL** (human-in-the-loop),
**BLOCKED** (bot protection or login; we do not attempt to bypass it).

Raw reconnaissance notes, sample rows and test transcripts live in `docs/recon/`:
`comps_listings.md` (MLS/comps), `legal_rules.md` (law), `assessment_owner_deeds.md` (owner names,
SR1A deeds), `tax_sale_clerk_opendata.md` (tax sales, clerks, open data).

## Summary

| # | Source | What we get | Access | Verdict | Used by |
|---|---|---|---|---|---|
| 1 | NJGIN Parcels + MOD-IV composite (ArcGIS FeatureServer) | All ~3.9M NJ parcels: block/lot, class, location, owner **mailing** address, assessed land/improvement/total, last-year tax, building description, year built, dwellings, last deed date + price, parcel polygon/centroid | Free REST API, no key | **USE** | tax_assessment, cash_buyers, sheriff matching, public-record comps, market stats |
| 2 | CivilView SalesWeb sheriff sales | Every upcoming foreclosure auction in 17 NJ counties incl. 8 of our 9 targets (not Mercer, §11): sheriff #, docket, sale date, plaintiff, defendant, address, upset/judgment, block/lot, occupancy, status/adjournment history | Free HTML, session cookie | **USE** | sheriff_sales |
| 3 | Census TIGERweb ZCTA polygons | Property ZIP for parcels (NJGIN only has the owner's mailing ZIP) | Free REST API | **USE** | tax_assessment |
| 4 | homeharvest → Realtor.com | Sold comps (radius or ZIP, last N days) with sqft/beds/baths/year; active listings with list date, DOM, description, tags, listing agent | pip library (unofficial API) | **USE (low volume)** — ToS prohibits scraping | underwriter, stale_listings |
| 5 | Redfin `gis-csv` | Sold/active CSV per region | Unofficial endpoint; region id lookup 403 | FALLBACK | — |
| 6 | Zillow | — | PerimeterX CAPTCHA (403) | **BLOCKED** | — |
| 7 | Realtor.com web pages | — | Kasada challenge (429) | **BLOCKED** (the API homeharvest uses still works) | — |
| 8 | RentCast API | AVM, comps (listings), rents | Key required; 50 free calls/month, then $0.20/call | FALLBACK (paid beyond free tier) | — |
| 9 | NJ Treasury SR1A sales files | Statewide deed sales (grantor, grantee, price, NU code) — 2026 YTD file reachable (11 MB, updated 2026-08-12) | Free download | see §5 | planned: owner/buyer names, fresh comps |

## 1. NJGIN statewide parcels + MOD-IV (the backbone)

- Endpoint: `https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/Parcels_Composite_NJ_WM/FeatureServer/0/query`
- `maxRecordCount` 2,000; pages with `resultOffset`. `returnCentroid=true&outSR=4326` gives WGS84 centroids.
- Supports statistics with `groupByFieldsForStatistics` + `havingClause` → server-side aggregation.
  This powers cash-buyer detection: group recent deeds by owner mailing address, `COUNT >= 2`.
- Supports point + `distance` queries (`units=esriSRUnit_StatuteMile`) → radius comps.
- **Owner names are blank statewide** (`OWNER_NAME = ''` for all 3.9M rows — NJ Daniel's Law redaction).
  Mailing address (`ST_ADDRESS`, `CITY_STATE`, `ZIP_CODE`) is intact → absentee and out-of-state detection.
- Sale data vintage: tax-year-2025 list. Essex class-2 deeds per year: 2019 7,588 · 2020 8,588 ·
  2021 10,634 · 2022 9,087 · 2023 7,625 · 2024 7,684 · 2025 25. → comps window is anchored to the
  data vintage and time-adjusted (5%/yr default).
- `DEED_DATE` is `YYMMDD` text; date filtering enumerates `YYMM%` prefixes. Century: ≤ current year+1 → 2000s.
- `SALES_CODE` letters (`A`, `Z`, …) are not the SR1A NU codes; we filter non-arm's-length sales by
  price/assessment outliers (IQR) and a minimum price instead.
- Assessments are on each town's own base (e.g. Woodbridge ≈ 1/6.5 of market, Newark ≈ 1/2.7,
  Bloomfield ≈ 1/1.67). `engine/market.py` measures each town's median sale/assessment ratio from
  recent recorded sales and uses it to convert any assessment to a market estimate.
- Package deeds (one deed for several lots) record the package price on every lot — detected when
  price > 2.2× estimated value and excluded from equity math.
- Live tests: Newark class-2 parcels 28,326; out-of-state owners 2,442; Woodbridge out-of-state 256;
  radius 0.5 mi around Bloomfield returned 145 sales (2023–24).

## 2. CivilView SalesWeb (sheriff sales)

- List: `https://salesweb.civilview.com/Sales/SalesSearch?countyId={id}` · Detail:
  `/Sales/SaleDetails?PropertyId={id}` (requires the session cookie set by the list page; without it the
  server redirects). No robots.txt (404).
- County ids (NJ): Middlesex 73, Union 15, Essex 2, Hudson 10, Passaic 17, Bergen 7, Camden 1;
  also Atlantic 25, Burlington 3, Cape May 52, Cumberland 6, Gloucester 19, Hunterdon 32, Monmouth 8,
  Morris 9, Ocean 85, Salem 20.
- Listings on 2026-10-26: Middlesex 206, Union 84, Essex 178, Hudson 70, Passaic 123, Bergen 108,
  Camden 158 (927 total; 855 open within 180 days).
- Quirks handled by `parse_list_page` / `parse_detail_page` (tests against saved real pages):
  - column order differs (Hudson: Address before Plaintiff); only Middlesex shows Status, and its
    "Open" filter still returns old "Purchased – 3rd Party" rows → filter by status and date;
  - amount labels: `Approx. Upset*`, `Good Faith Upset*`, `Judgment`, `Approx. Judgment*`, or only in the
    description text (Union uses a space as thousands separator: `$585 004.93`);
  - block/lot in `Parcel #` or description: `LOT 95.08 FKA 95, BLOCK 152.01 FKA 152`,
    `Tax Lot(s): 1 in Block: 229`, `TAX LOT 36, TAX BLOCK 3406`, `LOT: 78 BLOCK: 335`;
  - occupancy phrases (`OCCUPANCY STATUS: OCCUPIED`, `Property is occupied by unknown`);
  - status history table → adjournment counts (e.g. Hudson `DEFENDANT TOTAL 8 WEEK ADJ`).
- Parcel matching: municipality (description or postal city) + block/lot against NJGIN, falling back
  to street-address matching. Test run: 15/15 matched.
- Estate foreclosures are detectable from the defendant line (`…, HER HEIRS, BENEFICIARIES…`,
  `UNKNOWN HEIRS OF…`, `ESTATE OF…`) → `ESTATE_OWNER` signal.

## 3. Census TIGERweb ZCTA

- `https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1/query`
- Envelope query returns generalized polygons; `core/zcta.py` does point-in-polygon locally so a
  municipality's worth of parcels costs a handful of requests.

## 4. Comps and listings (homeharvest)

Full evidence: `docs/recon/comps_listings.md`. Highlights:
- homeharvest 0.8.18 (requires pandas < 3). 30/30 ZIP sweeps succeeded, no throttling at 2.5–3 s spacing.
- Sold, last 180 days: 1,893 rows across 15 target ZIPs; 0.5-mile radius around 186 Newark Ave,
  Bloomfield → 33 sales. Fill: price/date 100%, lat/lon 99%, year built 80%, sqft 70%, beds 53%.
  ~47% of sold rows are deed-only (no MLS): no beds/description, some non-arm's-length → the engine
  prefers MLS-backed comps when ≥ 3 exist and drops $/sqft outliers.
- Active: 1,803 rows (1,468 unique homes; one row per MLS feed). List date/DOM 100%, description 95%.
  Pending/contingent included → filtered. Price reductions only where the feed exposes original list
  price (mainly Hudson MLS). Listing agent name/phone/email in `advertisers`.
- Single-address lookups resolve the subject's own record (beds/baths, sometimes sqft) when the
  address is exact; unresolved addresses fall back to an area search (capped with `limit=5`).
- Terms: Realtor.com prohibits scraping. We keep it low-volume, cached (24 h), internal-only, and
  never republish listing text/photos. Public-record comps are the automatic fallback.
- **Package deeds in NJGIN sales (learned 2026-10-27):** one bulk deed (e.g. a building of condo units sold for
  $4.725M) is recorded with the full price on every parcel, and `SALES_CODE` reads "A" on all of them, so the code
  can't filter them. The comp engine drops any price + deed date that appears on 2+ parcels, sale prices outside
  $40K–$2.5M for houses, and sales above 2× (or below 0.5×) their equalized assessment. Before this, 45 valuations
  were above $1.8M ARV (worst: $215M on a commercial tower, $12.8M on a 1,884 sqft house); after the guard, no class-2
  house above $1.8M carries an offer verdict.
- Listing-only parcels (condo units and addresses that never matched a MOD-IV parcel) have no building
  description, so comps use the listing's own MLS property type (condos / multi_family) — learned 2026-10-27:
  without it, 114 condo/multi listings found no comps.
- Cross-matching the active-listing table against foreclosure/absentee/tax leads (street + ZIP, or ≤ 40 m with
  the same house number) found 93 listed-distress properties (30 listed foreclosures) on 2026-10-27; several
  condo parcels sharing one listing are matched only on an exact unit.

## 5. Owner names and deed (SR1A) records

Full evidence, district-code tables and sample rows: `docs/recon/assessment_owner_deeds.md`.

| Need | Bergen · Camden · Hudson · Middlesex · Passaic · Union | Essex (Newark) |
|---|---|---|
| Owner name + mailing address | **taxrecords-nj.com** county-board search — current (data as of 10/21–10/23/26) | No current free source. Newark 2020 ownership snapshot (City of Newark ArcGIS) names ~54% of parcels — valid only where the deed is unchanged since 2020 |
| Whole-town bulk pull | One form submission with "Excel" output → ZIP/CSV of every parcel (Jersey City 64,836 rows; Woodbridge 29,601; New Brunswick 8,027) incl. **sq ft** and year built | NJ Treasury statewide `modiv-2026.zip` (names redacted) |
| Recent deeds: grantor, grantee, price, date | Deed/Sr1a export with a sale-date range (data as of: Passaic 10/14/26, Union 09/24/26, Hudson 09/11/26, Camden 08/31/26, Bergen 08/26/26). **Middlesex SR1A db is stale (09/24/21).** | NJ Treasury `Sales2026.zip`: price, dates, block/lot, NU code — **no names** (0 of 170,664 rows) |

- Endpoint: `POST https://taxrecords-nj.com/pub/cgi/inf.cgi` with `ms_user=ctb<CC>` (ctb02 Bergen, ctb04
  Camden, ctb09 Hudson, ctb12 Middlesex, ctb16 Passaic, ctb20 Union; **ctb07 Essex has no data**). The old
  `tax1.co.monmouth.nj.us` `monm/data` account serves Monmouth only — that is why earlier Newark queries
  returned 0 rows. `njactb.org` record search was retired 1/1/2023 (Daniel's Law).
- **Verdict: MANUAL + automated import.** Both taxrecords-nj.com and tax1.co.monmouth.nj.us publish
  `robots.txt: Disallow: /`. The search and export are public (county boards link to them) but we do not
  crawl a site whose robots.txt forbids it. The operator downloads each focus town once per refresh
  cycle (≈ 1 minute per town) into `data/imports/owner_lists/` and `data/imports/deeds/`;
  `agents/scrapers/owner_records_agent.py` imports every file, matches parcels by district+block+lot+qual,
  fills owner names and living area, names GIS-clustered cash buyers, and detects flips. Cleaner long-term
  route: ask each county tax board for a periodic file. The recon client
  (`docs/recon/nj_owner_records_client.py`) documents the request format and is kept as evidence only.
- Verified on the real exports: Jersey City owner list → 64,836 rows parsed; Elizabeth deeds
  (Oct 2025–Oct 2026) → 834 deeds, **7 flips** (e.g. bought $750K → resold $1.269M in 244 days;
  $420K → $535K in 74 days). Recon also found 59 flips over 24 months in Paterson.
- **Newark** (largest market): automated via the City of Newark's public ArcGIS layer
  `Newark_Parcels_with_Ownership` (2020 snapshot) — owner name accepted only when its deed book/page equals
  the current MOD-IV deed book/page. For current Newark names a person can export from Essex's TaxDataHub
  (reCAPTCHA-protected, so manual only; up to 75,000 rows).
- Every record carries a **Daniel's Law** notice: honor nondisclosure requests (suppression list) before
  mailing or sharing addresses.
- Blocked/unavailable: `oprs.co.monmouth.nj.us` (connection closed), `njpropertyrecords.com` (Cloudflare),
  Hudson/Middlesex/Passaic board sites (Sucuri/Akamai), Essex Register of Deeds search (unreachable from here).

### 5.1 Municipal ArcGIS layers that still publish owner names (Phase 2 recon, 2026-10-27)

Full evidence (12/12 PINs matched NJGIN `PAMS_PIN`, field lists, counts, vintages, rejected layers):
`docs/recon/public_owner_layers.md`. **Automated** by `agents/enrichment/municipal_owner_layers.py`
(`municipal_owners` agent), querying only our leads' PINs in batches of 80 with explicit `outFields`:

| Town | Layer | Names | Vintage | Live result |
|---|---|---|---|---|
| Jersey City | City of Jersey City `EnerGov_AddressPoints_and_Parcels/FeatureServer/1` (`Parcel_PAMS_PIN`) | 59,641 | updates through Dec 2025 | 433 / 433 leads named |
| New Brunswick | `Tax_Parcels/FeatureServer/0` (`PAMS_PIN`) | 8,038 | MOD-IV 2025-09-18 | 264 / 264 |
| Plainfield | SpatialDataLogic `Plainfield_NJ_Parcels_081825` (`PAMS_PIN`, field `Owner`) | 10,576 | 2025-08-18 | 293 / 293 |

- A layer name is applied only if the record is at least as new as NJGIN's last deed for the parcel; servicer
  mailing addresses defer to the foreclosure defendant and switch off mailing-derived absentee signals.
- **Never request `*`**: the Plainfield vendor layer carries raw MOD-IV SSN columns (`SocialSecu`, `SpouseSoci`).
- Fallback (older; deed book/page must still match): Middlesex 2022 (Woodbridge/Edison/Perth Amboy), Passaic City
  2023, Essex/Passaic 2017, Newark rent-control registry 2021 — queued for Sprint 3.
- No owner-name layer exists for Elizabeth, Linden, Bayonne, West New York, Hackensack, Garfield or the Camden
  County towns → owner exports / OPRA.
- Entity lookups: NJ Business Name Search is free (name, ID, city, type); registered agent costs $5 + $1.25 per
  status report (bulk: $0.0185/record). OpenCorporates' API needs a key and its free tier is personal/open-data
  only → the engine generates manual links, it does not call either service.

### 5.2 Phones (skip tracing) and do-not-call data

- People-search sites (TruePeopleSearch, FastPeopleSearch, CyberBackgroundChecks) forbid automated access.
  `manage.py skiptrace --export-top 50` writes the queue (who to look up — executor > owner > co-defendant — with
  prebuilt search links); the operator pastes results into the cockpit or fills the CSV and imports it.
  Every imported number is DNC-classified.
- National DNC registry: register at telemarketing.donotcall.gov, download the area codes you call (the first 5
  are free; `manage.py dnc --status` recommends the five that cover the most of our contacts), load with
  `manage.py dnc --load`. Data older than 31 days stops counting as a scrub.
- Listing agents: names/phones/emails come with the listing payload (stale-listing agent) — no skip trace needed.
  On 2026-10-27, 144 underwritten deals were ready to call (141 via a listing agent's phone, 3 by agent e-mail).

## 6. Tax sales, tax delinquency, county clerk (lis pendens), open data

Full evidence and per-town survey: `docs/recon/tax_sale_clerk_opendata.md`.

### 6.1 Municipal tax lien sales — RealAuction (`<town>.newjerseytaxsale.com`)
- **91 municipal sites in the 7 target counties** (Bergen 19, Camden 17, Essex 16, Middlesex 16, Union 11,
  Hudson 6, Passaic 6) — directory in `config/sources/realauction_nj.yaml`. On 2026-10-26 they held
  **6,335 upcoming line items** (delinquent owners, with owner name, block/lot, charge type, amount) and
  **11,569 sold liens** (tax sale certificates). Examples: East Orange sale 10/29/2026 (408 items,
  $3.54M face); Clifton 659; Paterson 956 sold; Camden 2,309 sold. Newark, Irvington, Plainfield,
  Union City are on the platform but offline between sales.
- N.J.A.C. 5:33-1.1(d)(1) requires these line items to be public and free; robots.txt allows the paths.
- **Access: the sites' firewall answers 403 to every non-browser client** (our identified User-Agent,
  curl, python-requests). We do not impersonate a browser to get past it. Verdict: **MANUAL + import**.
  `agents/scrapers/tax_sale_agent.py` tries one honest live request per run, then parses pages the operator
  saves from a browser into `data/imports/tax_sales/<Town>/` (Preview Items pages, Results pages, Bidding
  Rules page). Parsers are tested against real pages.
- **Best zero-cost route at scale: OPRA requests.** Tax sale lists are public records under N.J.S.A.
  47:1A; email each target town's tax collector for the current tax sale list and the list of outstanding
  municipal-held certificates as a spreadsheet, and drop the file in `data/imports/`.
- In-person-sale towns (Jersey City, Bayonne, Edison, Woodbridge, Hackensack) publish only a newspaper
  notice / PDF (Edison emails the list on request) → OPRA or manual.

### 6.2 County clerks (lis pendens / foreclosure filings) — `agents/scrapers/lis_pendens_agent.py` (Phase 5)
| County | System | Verdict | Notes |
|---|---|---|---|
| Union | `clerk.ucnj.org/UCPA/DateResults` (`dt=7` lis pendens, `dt=17` foreclosure) | **USE (automated)** | Plain HTTP; live 30 days (2026-10-28): 44 foreclosures + 46 lis pendens entries. Party names, recorded date, instrument; no town, no address (scanned PDF only) |
| Essex | PRESS `press.essexregister.com` doc types 23 / 25 / 21 (+ 22 discharge) | **USE (automated)** | Live 30 days: 13 + 73 + 2 filings, 75 discharges; gives the town. Paged by `__doPostBack` |
| Monmouth | OPRS `oprs.co.monmouth.nj.us/oprs/clerk/ClerkHome.aspx?op=basic` (same PRESS form): LISPENFO / LISPEN / LISINRM (+ LISPENDI discharge) | **USE (automated, added 2026-10-28)** | No robots.txt, no CAPTCHA, terms-of-use popup with no automation clause. Grid headers DEFENDANT / PLAINTIFF and **block / lot filled in** → exact NJGIN parcel match (first run: 96 filings, 38 new leads) |
| Mercer | NewVision `records.mercercounty.org/publicsearch/` (doc type NLP) | MANUAL (export + upload) | JSON API behind an Angular app with a login modal; not automated. Search in a browser and upload the export |
| Passaic | clerk PRESS search | MANUAL | Works over HTTP but robots.txt disallows all → save the results page and upload it |
| Hudson | Acclaim click-through disclaimer | MANUAL | Operator must accept terms |
| Bergen, Middlesex, Camden | New Vision BrowserView JS app | BLOCKED/MANUAL | CAPTCHA + encrypted params; Camden charges $25/mo for lis pendens |
| NJ Courts eCourts | case jacket | MANUAL | CAPTCHA + registration; look up specific F-dockets by hand |

Matching (a filing is never pinned to a house on a guess): block/lot → our parcel or NJGIN; street address →
our parcel or NJGIN address search; otherwise a **unique** owner-name match in the same town (Essex, Monmouth,
imports) or county (Union), skipping co-defendants who are lienholders or the government. For Newark filings the
defendant is also looked up in the City of Newark 2020 ownership snapshot and accepted only when exactly one parcel
matches and its deed book/page still equals today's MOD-IV record. First live run: Union + Essex 253 filings →
3 exact Newark matches; the rest wait in `clerk_filings` (they re-match as owner names arrive) and are listed on the
dashboard's 🏛️ Records page with the clerk link so the operator can read the address and attach it. Discharges
deactivate the earlier filing's signal. Sheriff sales (§2) still cover the late-stage funnel automatically.

### 6.3 Open data
- **NJ construction permits** (data.nj.gov `w9se-dmra`, 2.68M rows, block/lot keyed, monthly): demolition and
  major-alteration permits → **USE (Sprint 2)** for "active flipper" buyer detection and condition signals.
- Paterson abandoned-property registry (ArcGIS, 522 rows, snapshot July 2025) → FALLBACK.
- No city in scope publishes code violations as open data (Jersey City none; Newark Tableau behind bot protection).
- NJ Press Association notices (njpublicnotices.com): CAPTCHA + terms forbid automated collection → MANUAL.

## 7. Operator data-refresh checklist (manual sources, ~30 minutes a month)

1. Owner lists: for each focus town outside Essex, county record search → "Current Owners/Assmt List" →
   Excel output → save the ZIP to `data/imports/owner_lists/`.
2. Deeds: same search → "Deed/Sr1a List" → last 12 months → save to `data/imports/deeds/` (cash buyers, flips).
3. Tax sales: before each town's sale, save its Preview Items / Results / Bidding Rules pages to
   `data/imports/tax_sales/<Town>/` — or send the OPRA request once a year.
4. Newark current owners: TaxDataHub export by hand (reCAPTCHA) → `data/imports/owner_lists/`.
5. National DNC: re-download your area codes every 31 days → `python manage.py dnc --load <file>`.
6. Skip trace: `python manage.py skiptrace --export-top 50` → look up → paste/import.
7. Run `python -m swarm.orchestrator run-all`.

## 9. Closing partners and tax references (added 2026-10-27)

`config/nj_closing_partners.yaml` lists NJ title agencies and closing attorneys by region, each checked on its own
website (source URL recorded; quotes only where the page mentions investors, assignments or double closings), plus
verification resources: NJLTA member directory, NJ DOBI licensee search, NAIC SBS producer lookup, ALTA directory,
NJSBA county bar referral services. Union County had no verifiable title agency office; statewide agencies and the
Cranford/Springfield attorneys cover it. Tax inputs (SE tax 15.3% on 92.35% of net earnings, 2026 federal and NJ
estimated-tax due dates and thresholds) come from the 2026 Form 1040-ES and NJ-1040-ES instructions.

## 10. Inbound web submissions

The seller/buyer pages post to the local cockpit (`/api/inbound/*`), or — when hosted statically — to a form
service such as Formspree, whose JSON/CSV export `manage.py inbound --ingest` reads. Submissions are first-party data
the visitor typed, with an explicit consent checkbox for calls/texts.

## 11. Mercer and Monmouth (added 2026-10-28)
- **Sheriff sales:** Monmouth is CivilView county 8 (automated; first run 89 open sales). Mercer is **not** on
  CivilView, and mercercounty.org answers 403 (Akamai) to every automated request, robots.txt included, so Mercer
  sheriff sales are manual (the list is linked from the Sheriff's Foreclosure Sale page; sales every other
  Wednesday at the Civil Courthouse, 175 S Broad St, Trenton). `sheriff_sale_agent.NOT_ON_CIVILVIEW` says so in
  every run instead of erroring.
- **Parcels / absentee owners / listings:** NJGIN covers both counties; focus towns Trenton, Hamilton, Ewing
  (Mercer) and Asbury Park, Neptune Twp, Long Branch, Freehold Borough (Monmouth); their ZIPs are in the stale-listing
  harvest. NJGIN town names keep their municipal type when matched (`njgis_client.match_municipality`: Freehold
  Borough ≠ Freehold Township, Neptune City ≠ Neptune Township; a bare ambiguous name matches nothing).
- **Clerks:** Monmouth automated (§6.2); Mercer manual.

## 12. OPRA requests for tax lists (`engine/opra_generator.py`, `config/opra_towns.yaml`)
- P.L.2024, c.16 (in force Sept 3, 2024): a letter or e-mail with every item on the town's adopted form is a valid
  request (N.J.S.A. 47:1A-5(f)); one agency per request (5(g)); towns may route requests to a portal (Jersey City
  GovQA, Woodbridge / Garfield GovPilot, New Brunswick / Trenton / Long Branch NextRequest, Linden web form, Asbury
  Park JustFOIA, Hamilton portal).
- **Commercial purpose must be certified, truthfully** (47:1A-5(f)); failing to certify it is a $1,000 / $2,500 /
  $5,000 civil penalty (47:1A-11(b)). Commercial requests get up to 14 business days (47:1A-5(i)(1)); paying up to
  2× cost buys 7 days, which we don't request. Electronic records are free apart from supplies (5(b)); special
  service charges must be quoted, so requests ask for the amount before any fee over $5 (*O'Reilly v. Lawnside*,
  GRC 2021-173, was a $3,360 quote).
- Name existing reports (the custodian need not create a record): the tax sale list, the municipal-held tax sale
  certificate list (Edmunds "Condensed Foreclosure Listing"), the Tax Search Export / delinquency listing. The GRC
  ordered these disclosed (*Hall v. Camden*, GRC 2013-305; *McBride v. Camden*, GRC 2014-54; *Wolosky v. Washington*,
  GRC 2016-19). Camden already posts its municipal lien list (230-page PDF, no owner names); Bayonne posts its tax
  sale ad.
- Expect redactions: personal phones / e-mails are stripped (47:1A-5(a)), and Daniel's Law covered persons' names
  or addresses may be withheld from tax records.
- **Never use OPRAmachine** for these: it publishes requests and responses, i.e. owner names and addresses.
- The spreadsheet that comes back is imported by `engine/tax_list_import.py` (CSV / XLSX, columns found by name)
  → TAX_DELINQUENT signals on exact block / lot / qualifier matches.

