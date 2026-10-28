# Swarm Intelligence Log

Written by `swarm/self_improvement.py` after every pipeline run (newest first). Contains aggregate metrics only — no personal data.

<!-- report -->
## Swarm intelligence report — 2026-10-28 16:47 UTC

### Bottlenecks & findings
- Owner names known on only 23% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- 1 offer-verdict valuations still above $1.8M ARV — review their comps by hand.
- 22 listed foreclosures with an agent haven't been contacted (7 with a sale within 14 days) — python manage.py today, Block 1.
- 142 lis pendens filings aren't tied to a parcel yet — attach addresses on the dashboard's Records page, or import owner lists to raise the name-match rate.
- Only 14% of the top 100 leads have a callable number. 86 can be skip-traced right now (python manage.py skiptrace --export-top 50); 0 need an owner name first (owner exports / OPRA).
- National DNC data is missing: download the free NJ area codes from telemarketing.donotcall.gov and run python manage.py dnc --load <file> before dialing owners.

### Actions taken automatically
- Scoring weights v7 → v8: viability prior by source from underwriting verdicts: sheriff_sale=0.13, stale_listing=0.95, out_of_state_owner=0.50, absentee_owner=0.83, lis_pendens=0.28

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| buyer_matcher | success | 8/8 | 50 | 0 | 0 | 0% | 0.4s |
| cash_buyers | success | 2/2 | 294 | 0 | 0 | 0% | 1841.8s |
| closing_alerts | success | 2/2 | 0 | 0 | 0 | 0% | 0.0s |
| fast_track | success | 2/2 | 57 | 0 | 0 | 0% | 2.6s |
| lead_scoring | success | 16/16 | 10042 | 0 | 0 | 0% | 24.6s |
| lis_pendens | success | 3/3 | 96 | 38 | 0 | 0% | 68.8s |
| listing_agents | success | 4/4 | 1448 | 0 | 0 | 0% | 2.1s |
| municipal_owners | success | 3/3 | 994 | 0 | 0 | 0% | 17.3s |
| outlier_guard | success | 4/4 | 2042 | 0 | 0 | 0% | 98.8s |
| owner_enrichment | success | 4/4 | 65670 | 0 | 0 | 0% | 13.9s |
| owner_resolver | success | 9/9 | 10042 | 0 | 0 | 0% | 14.8s |
| sheriff_sales | success | 7/8 | 89 | 89 | 0 | 0% | 89.8s |
| signal_stacker | success | 6/6 | 115 | 0 | 0 | 0% | 10.9s |
| stale_listings | success | 3/4 | 1712 | 313 | 0 | 0% | 235.3s |
| tax_assessment | success | 3/3 | 15851 | 1745 | 0 | 0% | 344.2s |
| tax_sales | success | 2/2 | 0 | 0 | 0 | 0% | 0.7s |
| underwriter | success | 12/13 | 405 | 0 | 0 | 0% | 3779.4s |

### Yield & coverage
- Open leads: **10042** (3711 new in 24h) — by source: absentee_owner 5348, out_of_state_owner 2386, stale_listing 1287, sheriff_sale 980, lis_pendens 41
- Active sheriff sales tracked: 947; upset/judgment amount captured on 100%
- Owner name known: 23% · parcel matched: 97% · living area known: 11%
- Top-50 leads underwritten: 100%

### Contact readiness & CRM
- Top-100 contact-ready: **14%** — owner/heir phone 0, listing agent only 14; skip-traceable now (name known, no phone) 86; need a name first 0
- Top deals with 5 matched cash buyers: 49/50 (98%); buyer_matches rows: 418
- National DNC data: not loaded
- Calls (7 days): 0; conversations 0 (0% connect); none logged; overdue follow-ups 0
- Ready to call now (deal + live phone / agent e-mail): **173**; agent LOIs awaiting follow-up 0; stacked distress 2x 1716, 3x 58, 4x+ 0

### Guardrails, fast track & money
- Outlier guard: 2042/2042 valuations checked; 236 ARVs capped to a benchmark, 105 non-residential parcels (PASS / unit-of-a-larger-parcel); 1 offer-verdict ARVs above $1.8M
- Listed foreclosures: 57 (22 actionable with an agent, 22 not yet contacted)
- Closing: 0 open files, 0 milestones due soon (0 overdue)
- Money 2026: fees 0, net 0, tax reserve 0, weighted pipeline 0

### Clerk filings, OPRA & outreach copy
- Lis pendens filings (open): Essex 3/88 matched, Monmouth 46/52 matched, Union 0/51 matched — python -m swarm.orchestrator run lis_pendens
- OPRA tax-list requests: 0 logged, 0 overdue
- Outreach copy: 25/25 top agent packets pass the plain-copy linter

### Spread viability
- Valuations: 2042 — STRONG 780, WORKABLE 490, FRFPA_BLOCKED 209, SHORT_SALE_ONLY 178, EQUITY_RESCUE_REFERRAL 176, PASS 107, WORKABLE_82_PCT_FLOOR 93, TIGHT 9
- absentee_owner: 17/18 STRONG/WORKABLE
- lis_pendens: 8/31 STRONG/WORKABLE
- out_of_state_owner: 3/5 STRONG/WORKABLE
- sheriff_sale: 101/795 STRONG/WORKABLE
- stale_listing: 1141/1193 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): WEST LONG BRANCH BORO 67% (n=1), SADDLE RIVER BORO 67% (n=1), RUMSON BORO 66% (n=1), MONTVALE BORO 66% (n=1), HARRINGTON PARK BORO 65% (n=1), RAMSEY BORO 65% (n=1), RIVERVALE TWP 65% (n=1), CLIFFSIDE PARK BORO 64% (n=5)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

### Alpha discovery — spread potential by town (top 10)
| town | county | score | p75/p25 price-to-assessment gap | median sale | pre-1980 | in harvest |
|---|---|---|---|---|---|---|
| PATERSON CITY | Passaic | 65.39 | 1.47x | $480,000 | 91% | yes |
| NEWARK CITY | Essex | 55.67 | 1.41x | $517,000 | 84% | yes |
| PASSAIC CITY | Passaic | 49.76 | 1.34x | $550,000 | 95% | yes |
| LINDENWOLD BORO | Camden | 46.68 | 1.33x | $220,000 | 90% | yes |
| NEW BRUNSWICK CITY | Middlesex | 46.38 | 1.34x | $435,000 | 85% | yes |
| CAMDEN CITY | Camden | 46.36 | 1.63x | $135,000 | 96% | yes |
| GLOUCESTER CITY | Camden | 46.07 | 1.31x | $212,500 | 97% | yes |
| WEST NEW YORK TOWN | Hudson | 42.55 | 1.33x | $500,000 | 78% | yes |
| GUTTENBERG TOWN | Hudson | 40.06 | 1.31x | $350,000 | 81% | yes |
| ELMWOOD PARK BORO | Bergen | 39.56 | 1.29x | $580,000 | 87% | yes |

<!-- report -->
## Swarm intelligence report — 2026-10-27 20:13 UTC

### Bottlenecks & findings
- Owner names known on only 28% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- 21 listed foreclosures with an agent haven't been contacted (7 with a sale within 14 days) — python manage.py today, Block 1.
- Only 12% of the top 100 leads have a callable number. 88 can be skip-traced right now (python manage.py skiptrace --export-top 50); 0 need an owner name first (owner exports / OPRA).
- National DNC data is missing: download the free NJ area codes from telemarketing.donotcall.gov and run python manage.py dnc --load <file> before dialing owners.

### Actions taken automatically
- Scoring weights v6 → v7: viability prior by source from underwriting verdicts: sheriff_sale=0.13, stale_listing=0.94, out_of_state_owner=0.50, absentee_owner=0.83

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| buyer_matcher | success | 7/7 | 50 | 0 | 0 | 0% | 0.4s |
| cash_buyers | success | 2/2 | 294 | 0 | 0 | 0% | 1841.8s |
| closing_alerts | success | 1/1 | 0 | 0 | 0 | 0% | 0.0s |
| fast_track | success | 1/1 | 54 | 0 | 0 | 0% | 2.8s |
| lead_scoring | success | 15/15 | 7854 | 0 | 0 | 0% | 20.4s |
| listing_agents | success | 3/3 | 1132 | 0 | 0 | 0% | 2.3s |
| municipal_owners | success | 3/3 | 994 | 0 | 0 | 0% | 17.3s |
| outlier_guard | success | 3/3 | 1679 | 0 | 0 | 0% | 2.3s |
| owner_enrichment | success | 4/4 | 65670 | 0 | 0 | 0% | 13.9s |
| owner_resolver | success | 8/8 | 7854 | 0 | 0 | 0% | 15.1s |
| sheriff_sales | success | 6/7 | 858 | 14 | 0 | 0% | 986.6s |
| signal_stacker | success | 5/5 | 95 | 0 | 0 | 0% | 10.1s |
| stale_listings | success | 2/3 | 4789 | 16 | 0 | 0% | 1103.4s |
| tax_assessment | success | 2/2 | 56647 | 1493 | 0 | 0% | 783.5s |
| tax_sales | success | 2/2 | 0 | 0 | 0 | 0% | 0.7s |
| underwriter | success | 11/12 | 3 | 0 | 0 | 0% | 52.9s |

### Yield & coverage
- Open leads: **7854** (1785 new in 24h) — by source: absentee_owner 4393, out_of_state_owner 1596, stale_listing 974, sheriff_sale 891
- Active sheriff sales tracked: 858; upset/judgment amount captured on 100%
- Owner name known: 28% · parcel matched: 97% · living area known: 10%
- Top-50 leads underwritten: 100%

### Contact readiness & CRM
- Top-100 contact-ready: **12%** — owner/heir phone 0, listing agent only 12; skip-traceable now (name known, no phone) 88; need a name first 0
- Top deals with 5 matched cash buyers: 50/50 (100%); buyer_matches rows: 415
- National DNC data: not loaded
- Calls (7 days): 0; conversations 0 (0% connect); none logged; overdue follow-ups 0
- Ready to call now (deal + live phone / agent e-mail): **127**; agent LOIs awaiting follow-up 0; stacked distress 2x 1227, 3x 51, 4x+ 0

### Guardrails, fast track & money
- Outlier guard: 1679/1679 valuations checked; 199 ARVs capped to a benchmark, 105 non-residential parcels (PASS / unit-of-a-larger-parcel); 0 offer-verdict ARVs above $1.8M
- Listed foreclosures: 54 (21 actionable with an agent, 21 not yet contacted)
- Closing: 0 open files, 0 milestones due soon (0 overdue)
- Money 2026: fees 0, net 0, tax reserve 0, weighted pipeline 0

### Spread viability
- Valuations: 1679 — STRONG 632, WORKABLE 365, FRFPA_BLOCKED 187, SHORT_SALE_ONLY 160, EQUITY_RESCUE_REFERRAL 148, PASS 103, WORKABLE_82_PCT_FLOOR 75, TIGHT 9
- absentee_owner: 17/18 STRONG/WORKABLE
- out_of_state_owner: 3/5 STRONG/WORKABLE
- sheriff_sale: 92/722 STRONG/WORKABLE
- stale_listing: 885/934 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): SADDLE RIVER BORO 67% (n=1), MONTVALE BORO 66% (n=1), HARRINGTON PARK BORO 65% (n=1), RAMSEY BORO 65% (n=1), RIVERVALE TWP 65% (n=1), CLIFFSIDE PARK BORO 64% (n=5), MAHWAH TWP 63% (n=5), SOUTH PLAINFIELD BORO 63% (n=2)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

### Alpha discovery — spread potential by town (top 10)
| town | county | score | p75/p25 price-to-assessment gap | median sale | pre-1980 | in harvest |
|---|---|---|---|---|---|---|
| PATERSON CITY | Passaic | 65.39 | 1.47x | $480,000 | 91% | yes |
| NEWARK CITY | Essex | 55.67 | 1.41x | $517,000 | 84% | yes |
| PASSAIC CITY | Passaic | 49.76 | 1.34x | $550,000 | 95% | yes |
| LINDENWOLD BORO | Camden | 46.68 | 1.33x | $220,000 | 90% | yes |
| NEW BRUNSWICK CITY | Middlesex | 46.38 | 1.34x | $435,000 | 85% | yes |
| CAMDEN CITY | Camden | 46.36 | 1.63x | $135,000 | 96% | yes |
| GLOUCESTER CITY | Camden | 46.07 | 1.31x | $212,500 | 97% | yes |
| WEST NEW YORK TOWN | Hudson | 42.55 | 1.33x | $500,000 | 78% | yes |
| GUTTENBERG TOWN | Hudson | 40.06 | 1.31x | $350,000 | 81% | yes |
| ELMWOOD PARK BORO | Bergen | 39.56 | 1.29x | $580,000 | 87% | yes |

<!-- report -->
## Swarm intelligence report — 2026-10-27 17:19 UTC

### Bottlenecks & findings
- Owner names known on only 33% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- Only 8% of the top 100 leads have a callable number. 92 can be skip-traced right now (python manage.py skiptrace --export-top 50); 0 need an owner name first (owner exports / OPRA).
- National DNC data is missing: download the free NJ area codes from telemarketing.donotcall.gov and run python manage.py dnc --load <file> before dialing owners.

### Actions taken automatically
- Scoring weights v5 → v6: viability prior by source from underwriting verdicts: sheriff_sale=0.17, stale_listing=0.95, out_of_state_owner=0.50, absentee_owner=0.83

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| buyer_matcher | success | 6/6 | 50 | 0 | 0 | 0% | 0.3s |
| cash_buyers | success | 1/1 | 293 | 0 | 0 | 0% | 1136.0s |
| lead_scoring | success | 12/12 | 6331 | 0 | 0 | 0% | 19.2s |
| listing_agents | success | 2/2 | 1112 | 0 | 0 | 0% | 1.5s |
| municipal_owners | success | 2/2 | 990 | 0 | 0 | 0% | 1.7s |
| owner_enrichment | success | 3/3 | 65670 | 0 | 0 | 0% | 11.5s |
| owner_resolver | success | 7/7 | 6331 | 0 | 0 | 0% | 11.7s |
| sheriff_sales | success | 5/6 | 414 | 25 | 0 | 0% | 472.2s |
| signal_stacker | success | 4/4 | 93 | 0 | 0 | 0% | 8.7s |
| stale_listings | success | 1/2 | 4793 | 950 | 0 | 0% | 730.4s |
| tax_assessment | success | 1/1 | 44216 | 4496 | 0 | 0% | 238.3s |
| tax_sales | success | 1/1 | 0 | 0 | 0 | 0% | 0.6s |
| underwriter | success | 9/10 | 48 | 0 | 0 | 0% | 222.9s |

### Yield & coverage
- Open leads: **6331** (6331 new in 24h) — by source: absentee_owner 3382, out_of_state_owner 1114, stale_listing 958, sheriff_sale 877
- Active sheriff sales tracked: 853; upset/judgment amount captured on 99%
- Owner name known: 33% · parcel matched: 97% · living area known: 12%
- Top-50 leads underwritten: 100%

### Contact readiness & CRM
- Top-100 contact-ready: **8%** — owner/heir phone 0, listing agent only 8; skip-traceable now (name known, no phone) 92; need a name first 0
- Top deals with 5 matched cash buyers: 50/50 (100%); buyer_matches rows: 395
- National DNC data: not loaded
- Calls (7 days): 0; conversations 0 (0% connect); none logged; overdue follow-ups 0
- Ready to call now (deal + live phone / agent e-mail): **144**; agent LOIs awaiting follow-up 0; stacked distress 2x 911, 3x 50, 4x+ 0

### Spread viability
- Valuations: 1679 — STRONG 692, WORKABLE 340, FRFPA_BLOCKED 184, SHORT_SALE_ONLY 164, EQUITY_RESCUE_REFERRAL 149, WORKABLE_82_PCT_FLOOR 76, PASS 60, TIGHT 14
- absentee_owner: 17/18 STRONG/WORKABLE
- out_of_state_owner: 3/5 STRONG/WORKABLE
- sheriff_sale: 119/722 STRONG/WORKABLE
- stale_listing: 893/934 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): RAMSEY BORO 68% (n=1), SADDLE RIVER BORO 67% (n=1), HARRINGTON PARK BORO 67% (n=1), SOUTH PLAINFIELD BORO 67% (n=2), SUMMIT CITY 66% (n=1), MONTVALE BORO 66% (n=1), RIVERVALE TWP 65% (n=1), CLIFFSIDE PARK BORO 64% (n=5)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

### Alpha discovery — spread potential by town (top 10)
| town | county | score | p75/p25 price-to-assessment gap | median sale | pre-1980 | in harvest |
|---|---|---|---|---|---|---|
| PATERSON CITY | Passaic | 65.39 | 1.47x | $480,000 | 91% | yes |
| NEWARK CITY | Essex | 55.67 | 1.41x | $517,000 | 84% | yes |
| PASSAIC CITY | Passaic | 49.76 | 1.34x | $550,000 | 95% | yes |
| LINDENWOLD BORO | Camden | 46.68 | 1.33x | $220,000 | 90% | yes |
| NEW BRUNSWICK CITY | Middlesex | 46.38 | 1.34x | $435,000 | 85% | yes |
| CAMDEN CITY | Camden | 46.36 | 1.63x | $135,000 | 96% | yes |
| GLOUCESTER CITY | Camden | 46.07 | 1.31x | $212,500 | 97% | yes |
| WEST NEW YORK TOWN | Hudson | 42.55 | 1.33x | $500,000 | 78% | yes |
| GUTTENBERG TOWN | Hudson | 40.06 | 1.31x | $350,000 | 81% | yes |
| ELMWOOD PARK BORO | Bergen | 39.56 | 1.29x | $580,000 | 87% | yes |

<!-- report -->
## Swarm intelligence report — 2026-10-27 04:33 UTC

### Bottlenecks & findings
- Owner names known on only 33% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- Only 0% of the top 100 leads have a callable number. 100 can be skip-traced right now (python manage.py skiptrace --export-top 50); 0 need an owner name first (owner exports / OPRA).
- National DNC data is missing: download the free NJ area codes from telemarketing.donotcall.gov and run python manage.py dnc --load <file> before dialing owners.

### Actions taken automatically
- None this run.

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| buyer_matcher | success | 5/5 | 50 | 0 | 0 | 0% | 0.3s |
| cash_buyers | success | 1/1 | 293 | 0 | 0 | 0% | 1136.0s |
| lead_scoring | success | 9/9 | 6331 | 0 | 0 | 0% | 11.5s |
| listing_agents | success | 2/2 | 1112 | 0 | 0 | 0% | 1.5s |
| municipal_owners | success | 2/2 | 990 | 0 | 0 | 0% | 1.7s |
| owner_enrichment | success | 3/3 | 65670 | 0 | 0 | 0% | 11.5s |
| owner_resolver | success | 7/7 | 6331 | 0 | 0 | 0% | 11.7s |
| sheriff_sales | success | 5/6 | 414 | 25 | 0 | 0% | 472.2s |
| stale_listings | success | 1/2 | 4793 | 950 | 0 | 0% | 730.4s |
| tax_assessment | success | 1/1 | 44216 | 4496 | 0 | 0% | 238.3s |
| tax_sales | success | 1/1 | 0 | 0 | 0 | 0% | 0.6s |
| underwriter | success | 7/7 | 0 | 0 | 0 | 0% | 12.7s |

### Yield & coverage
- Open leads: **6331** (6331 new in 24h) — by source: absentee_owner 3382, out_of_state_owner 1114, stale_listing 958, sheriff_sale 877
- Active sheriff sales tracked: 853; upset/judgment amount captured on 99%
- Owner name known: 33% · parcel matched: 97% · living area known: 12%
- Top-50 leads underwritten: 100%

### Contact readiness & CRM
- Top-100 contact-ready: **0%** — owner/heir phone 0, listing agent only 0; skip-traceable now (name known, no phone) 100; need a name first 0
- Top deals with 5 matched cash buyers: 50/50 (100%); buyer_matches rows: 355
- National DNC data: not loaded
- Calls (7 days): 0; conversations 0 (0% connect); none logged; overdue follow-ups 0

### Spread viability
- Valuations: 722 — FRFPA_BLOCKED 184, SHORT_SALE_ONLY 164, EQUITY_RESCUE_REFERRAL 149, STRONG 104, WORKABLE_82_PCT_FLOOR 76, PASS 16, WORKABLE 15, TIGHT 14
- sheriff_sale: 119/722 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): RAMSEY BORO 68% (n=1), SADDLE RIVER BORO 67% (n=1), HARRINGTON PARK BORO 67% (n=1), SOUTH PLAINFIELD BORO 67% (n=2), SUMMIT CITY 66% (n=1), MONTVALE BORO 66% (n=1), RIVERVALE TWP 65% (n=1), CLIFFSIDE PARK BORO 64% (n=5)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

<!-- report -->
## Swarm intelligence report — 2026-10-27 04:29 UTC

### Bottlenecks & findings
- Owner names known on only 33% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- Only 0% of the top 100 leads have a callable number. 100 can be skip-traced right now (python manage.py skiptrace --export-top 50); 0 need an owner name first (owner exports / OPRA).
- National DNC data is missing: download the free NJ area codes from telemarketing.donotcall.gov and run python manage.py dnc --load <file> before dialing owners.

### Actions taken automatically
- Expansion: added ELMWOOD PARK BORO (Bergen) to harvest list — renovated/distressed price gap 1.29x, median sale $580,000, 87% pre-1980 stock
- Expansion: added HILLSIDE TWP (Union) to harvest list — renovated/distressed price gap 1.27x, median sale $474,000, 96% pre-1980 stock

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| buyer_matcher | success | 3/3 | 50 | 0 | 0 | 0% | 0.2s |
| cash_buyers | success | 1/1 | 293 | 0 | 0 | 0% | 1136.0s |
| lead_scoring | success | 7/7 | 6331 | 0 | 0 | 0% | 12.7s |
| listing_agents | success | 2/2 | 1112 | 0 | 0 | 0% | 1.5s |
| municipal_owners | success | 2/2 | 990 | 0 | 0 | 0% | 1.7s |
| owner_enrichment | success | 2/2 | 65670 | 0 | 0 | 0% | 13.5s |
| owner_resolver | success | 6/6 | 6331 | 0 | 0 | 0% | 12.2s |
| sheriff_sales | success | 5/6 | 414 | 25 | 0 | 0% | 472.2s |
| stale_listings | success | 1/2 | 4793 | 950 | 0 | 0% | 730.4s |
| tax_assessment | success | 1/1 | 44216 | 4496 | 0 | 0% | 238.3s |
| tax_sales | success | 1/1 | 0 | 0 | 0 | 0% | 0.6s |
| underwriter | success | 6/6 | 0 | 0 | 0 | 0% | 13.9s |

### Yield & coverage
- Open leads: **6331** (6331 new in 24h) — by source: absentee_owner 3382, out_of_state_owner 1114, stale_listing 958, sheriff_sale 877
- Active sheriff sales tracked: 853; upset/judgment amount captured on 99%
- Owner name known: 33% · parcel matched: 97% · living area known: 12%
- Top-50 leads underwritten: 100%

### Contact readiness & CRM
- Top-100 contact-ready: **0%** — owner/heir phone 0, listing agent only 0; skip-traceable now (name known, no phone) 100; need a name first 0
- Top deals with 5 matched cash buyers: 50/50 (100%); buyer_matches rows: 355
- National DNC data: not loaded
- Calls (7 days): 0; conversations 0 (0% connect); none logged; overdue follow-ups 0

### Spread viability
- Valuations: 722 — FRFPA_BLOCKED 184, SHORT_SALE_ONLY 164, EQUITY_RESCUE_REFERRAL 149, STRONG 104, WORKABLE_82_PCT_FLOOR 76, PASS 16, WORKABLE 15, TIGHT 14
- sheriff_sale: 119/722 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): RAMSEY BORO 68% (n=1), SADDLE RIVER BORO 67% (n=1), HARRINGTON PARK BORO 67% (n=1), SOUTH PLAINFIELD BORO 67% (n=2), SUMMIT CITY 66% (n=1), MONTVALE BORO 66% (n=1), RIVERVALE TWP 65% (n=1), CLIFFSIDE PARK BORO 64% (n=5)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

### Alpha discovery — spread potential by town (top 10)
| town | county | score | p75/p25 price-to-assessment gap | median sale | pre-1980 | in harvest |
|---|---|---|---|---|---|---|
| PATERSON CITY | Passaic | 65.39 | 1.47x | $480,000 | 91% | yes |
| NEWARK CITY | Essex | 55.67 | 1.41x | $517,000 | 84% | yes |
| PASSAIC CITY | Passaic | 49.76 | 1.34x | $550,000 | 95% | yes |
| LINDENWOLD BORO | Camden | 46.68 | 1.33x | $220,000 | 90% | yes |
| NEW BRUNSWICK CITY | Middlesex | 46.38 | 1.34x | $435,000 | 85% | yes |
| CAMDEN CITY | Camden | 46.36 | 1.63x | $135,000 | 96% | yes |
| GLOUCESTER CITY | Camden | 46.07 | 1.31x | $212,500 | 97% | yes |
| WEST NEW YORK TOWN | Hudson | 42.55 | 1.33x | $500,000 | 78% | yes |
| GUTTENBERG TOWN | Hudson | 40.06 | 1.31x | $350,000 | 81% | yes |
| ELMWOOD PARK BORO | Bergen | 39.56 | 1.29x | $580,000 | 87% | yes |

<!-- report -->
## Swarm intelligence report — 2026-10-27 04:24 UTC

### Bottlenecks & findings
- Owner names known on only 33% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- Only 3% of the top 100 leads have a callable number. 97 can be skip-traced right now (python manage.py skiptrace --export-top 50); 0 need an owner name first (owner exports / OPRA).
- National DNC data is missing: download the free NJ area codes from telemarketing.donotcall.gov and run python manage.py dnc --load <file> before dialing owners.

### Actions taken automatically
- Scoring weights v4 → v5: viability prior by source from underwriting verdicts: sheriff_sale=0.17

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| buyer_matcher | success | 2/2 | 50 | 0 | 0 | 0% | 0.3s |
| cash_buyers | success | 1/1 | 293 | 0 | 0 | 0% | 1136.0s |
| lead_scoring | success | 5/5 | 6331 | 0 | 0 | 0% | 8.4s |
| listing_agents | success | 1/1 | 1112 | 0 | 0 | 0% | 219.1s |
| municipal_owners | success | 1/1 | 990 | 0 | 0 | 0% | 17.4s |
| owner_enrichment | success | 2/2 | 65670 | 0 | 0 | 0% | 13.5s |
| owner_resolver | success | 5/5 | 0 | 0 | 0 | 0% | 10.7s |
| sheriff_sales | success | 5/6 | 414 | 25 | 0 | 0% | 472.2s |
| stale_listings | success | 1/2 | 4793 | 950 | 0 | 0% | 730.4s |
| tax_assessment | success | 1/1 | 44216 | 4496 | 0 | 0% | 238.3s |
| tax_sales | success | 1/1 | 0 | 0 | 0 | 0% | 0.6s |
| underwriter | success | 5/5 | 780 | 0 | 0 | 0% | 379.2s |

### Yield & coverage
- Open leads: **6331** (6331 new in 24h) — by source: absentee_owner 3382, out_of_state_owner 1114, stale_listing 958, sheriff_sale 877
- Active sheriff sales tracked: 853; upset/judgment amount captured on 99%
- Owner name known: 33% · parcel matched: 97% · living area known: 12%
- Top-50 leads underwritten: 100%

### Contact readiness & CRM
- Top-100 contact-ready: **3%** — owner/heir phone 0, listing agent only 3; skip-traceable now (name known, no phone) 97; need a name first 0
- Top deals with 5 matched cash buyers: 50/50 (100%); buyer_matches rows: 250
- National DNC data: not loaded
- Calls (7 days): 0; conversations 0 (0% connect); none logged; overdue follow-ups 0

### Spread viability
- Valuations: 722 — FRFPA_BLOCKED 184, SHORT_SALE_ONLY 164, EQUITY_RESCUE_REFERRAL 149, STRONG 104, WORKABLE_82_PCT_FLOOR 76, PASS 16, WORKABLE 15, TIGHT 14
- sheriff_sale: 119/722 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): RAMSEY BORO 68% (n=1), SADDLE RIVER BORO 67% (n=1), HARRINGTON PARK BORO 67% (n=1), SOUTH PLAINFIELD BORO 67% (n=2), SUMMIT CITY 66% (n=1), MONTVALE BORO 66% (n=1), RIVERVALE TWP 65% (n=1), CLIFFSIDE PARK BORO 64% (n=5)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

<!-- report -->
## Swarm intelligence report — 2026-10-26 21:30 UTC

### Bottlenecks & findings
- Owner names known on only 24% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- Only 30% of the top 50 leads are underwritten — raise underwriter --top.

### Actions taken automatically
- Expansion: added WEST NEW YORK TOWN (Hudson) to harvest list — renovated/distressed price gap 1.33x, median sale $500,000, 78% pre-1980 stock
- Expansion: added GUTTENBERG TOWN (Hudson) to harvest list — renovated/distressed price gap 1.31x, median sale $350,000, 81% pre-1980 stock

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| cash_buyers | success | 1/1 | 293 | 0 | 0 | 0% | 1136.0s |
| lead_scoring | success | 5/5 | 6331 | 0 | 0 | 0% | 8.4s |
| owner_enrichment | success | 2/2 | 65670 | 0 | 0 | 0% | 13.5s |
| sheriff_sales | success | 5/6 | 414 | 25 | 0 | 0% | 472.2s |
| stale_listings | success | 1/2 | 4793 | 950 | 0 | 0% | 730.4s |
| tax_assessment | success | 1/1 | 44216 | 4496 | 0 | 0% | 238.3s |
| tax_sales | success | 1/1 | 0 | 0 | 0 | 0% | 0.6s |
| underwriter | success | 3/3 | 28 | 0 | 0 | 0% | 197.9s |

### Yield & coverage
- Open leads: **6331** (6331 new in 24h) — by source: absentee_owner 3382, out_of_state_owner 1114, stale_listing 958, sheriff_sale 877
- Active sheriff sales tracked: 853; upset/judgment amount captured on 99%
- Owner name known: 24% · parcel matched: 97% · living area known: 12%
- Top-50 leads underwritten: 30%

### Spread viability
- Valuations: 59 — FRFPA_BLOCKED 38, STRONG 9, SHORT_SALE_ONLY 6, WORKABLE 4, TIGHT 2
- sheriff_sale: 13/59 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): SOUTH ORANGE VILLAGE TW 59% (n=1), BAYONNE CITY 57% (n=1), EDGEWATER BORO 57% (n=1), MAYWOOD BORO 55% (n=1), CLOSTER BORO 53% (n=1), PLAINFIELD CITY 52% (n=3), RAHWAY CITY 50% (n=2), BLOOMFIELD TWP 47% (n=1)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

### Alpha discovery — spread potential by town (top 10)
| town | county | score | p75/p25 price-to-assessment gap | median sale | pre-1980 | in harvest |
|---|---|---|---|---|---|---|
| PATERSON CITY | Passaic | 65.39 | 1.47x | $480,000 | 91% | yes |
| NEWARK CITY | Essex | 55.67 | 1.41x | $517,000 | 84% | yes |
| PASSAIC CITY | Passaic | 49.76 | 1.34x | $550,000 | 95% | yes |
| LINDENWOLD BORO | Camden | 46.68 | 1.33x | $220,000 | 90% | yes |
| NEW BRUNSWICK CITY | Middlesex | 46.38 | 1.34x | $435,000 | 85% | yes |
| CAMDEN CITY | Camden | 46.36 | 1.63x | $135,000 | 96% | yes |
| GLOUCESTER CITY | Camden | 46.07 | 1.31x | $212,500 | 97% | yes |
| WEST NEW YORK TOWN | Hudson | 42.55 | 1.33x | $500,000 | 78% | yes |
| GUTTENBERG TOWN | Hudson | 40.06 | 1.31x | $350,000 | 81% | yes |
| ELMWOOD PARK BORO | Bergen | 39.56 | 1.29x | $580,000 | 87% |  |

<!-- report -->
## Swarm intelligence report — 2026-10-26 21:30 UTC

### Bottlenecks & findings
- Owner names known on only 24% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- Only 30% of the top 50 leads are underwritten — raise underwriter --top.

### Actions taken automatically
- Expansion: added LINDENWOLD BORO (Camden) to harvest list — renovated/distressed price gap 1.33x, median sale $220,000, 90% pre-1980 stock
- Expansion: added GLOUCESTER CITY (Camden) to harvest list — renovated/distressed price gap 1.31x, median sale $212,500, 97% pre-1980 stock

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| cash_buyers | success | 1/1 | 293 | 0 | 0 | 0% | 1136.0s |
| lead_scoring | success | 5/5 | 6331 | 0 | 0 | 0% | 8.4s |
| owner_enrichment | success | 2/2 | 65670 | 0 | 0 | 0% | 13.5s |
| sheriff_sales | success | 5/6 | 414 | 25 | 0 | 0% | 472.2s |
| stale_listings | success | 1/2 | 4793 | 950 | 0 | 0% | 730.4s |
| tax_assessment | success | 1/1 | 44216 | 4496 | 0 | 0% | 238.3s |
| tax_sales | success | 1/1 | 0 | 0 | 0 | 0% | 0.6s |
| underwriter | success | 3/3 | 28 | 0 | 0 | 0% | 197.9s |

### Yield & coverage
- Open leads: **6331** (6331 new in 24h) — by source: absentee_owner 3382, out_of_state_owner 1114, stale_listing 958, sheriff_sale 877
- Active sheriff sales tracked: 853; upset/judgment amount captured on 99%
- Owner name known: 24% · parcel matched: 97% · living area known: 12%
- Top-50 leads underwritten: 30%

### Spread viability
- Valuations: 59 — FRFPA_BLOCKED 38, STRONG 9, SHORT_SALE_ONLY 6, WORKABLE 4, TIGHT 2
- sheriff_sale: 13/59 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): SOUTH ORANGE VILLAGE TW 59% (n=1), BAYONNE CITY 57% (n=1), EDGEWATER BORO 57% (n=1), MAYWOOD BORO 55% (n=1), CLOSTER BORO 53% (n=1), PLAINFIELD CITY 52% (n=3), RAHWAY CITY 50% (n=2), BLOOMFIELD TWP 47% (n=1)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

### Alpha discovery — spread potential by town (top 10)
| town | county | score | p75/p25 price-to-assessment gap | median sale | pre-1980 | in harvest |
|---|---|---|---|---|---|---|
| PATERSON CITY | Passaic | 65.39 | 1.47x | $480,000 | 91% | yes |
| NEWARK CITY | Essex | 55.67 | 1.41x | $517,000 | 84% | yes |
| PASSAIC CITY | Passaic | 49.76 | 1.34x | $550,000 | 95% | yes |
| LINDENWOLD BORO | Camden | 46.68 | 1.33x | $220,000 | 90% | yes |
| NEW BRUNSWICK CITY | Middlesex | 46.38 | 1.34x | $435,000 | 85% | yes |
| CAMDEN CITY | Camden | 46.36 | 1.63x | $135,000 | 96% | yes |
| GLOUCESTER CITY | Camden | 46.07 | 1.31x | $212,500 | 97% | yes |
| WEST NEW YORK TOWN | Hudson | 42.55 | 1.33x | $500,000 | 78% |  |
| GUTTENBERG TOWN | Hudson | 40.06 | 1.31x | $350,000 | 81% |  |
| ELMWOOD PARK BORO | Bergen | 39.56 | 1.29x | $580,000 | 87% |  |

<!-- report -->
## Swarm intelligence report — 2026-10-26 21:30 UTC

### Bottlenecks & findings
- Owner names known on only 24% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- Only 30% of the top 50 leads are underwritten — raise underwriter --top.

### Actions taken automatically
- Scoring weights v3 → v4: viability prior by source from underwriting verdicts: sheriff_sale=0.23
- Expansion: added LINDENWOLD BORO (Camden) to harvest list — renovated/distressed price gap 1.33x, median sale $220,000, 90% pre-1980 stock
- Expansion: added GLOUCESTER CITY (Camden) to harvest list — renovated/distressed price gap 1.31x, median sale $212,500, 97% pre-1980 stock

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| cash_buyers | success | 1/1 | 293 | 0 | 0 | 0% | 1136.0s |
| lead_scoring | success | 5/5 | 6331 | 0 | 0 | 0% | 8.4s |
| owner_enrichment | success | 2/2 | 65670 | 0 | 0 | 0% | 13.5s |
| sheriff_sales | success | 5/6 | 414 | 25 | 0 | 0% | 472.2s |
| stale_listings | success | 1/2 | 4793 | 950 | 0 | 0% | 730.4s |
| tax_assessment | success | 1/1 | 44216 | 4496 | 0 | 0% | 238.3s |
| tax_sales | success | 1/1 | 0 | 0 | 0 | 0% | 0.6s |
| underwriter | success | 3/3 | 28 | 0 | 0 | 0% | 197.9s |

### Yield & coverage
- Open leads: **6331** (6331 new in 24h) — by source: absentee_owner 3382, out_of_state_owner 1114, stale_listing 958, sheriff_sale 877
- Active sheriff sales tracked: 853; upset/judgment amount captured on 99%
- Owner name known: 24% · parcel matched: 97% · living area known: 12%
- Top-50 leads underwritten: 30%

### Spread viability
- Valuations: 59 — FRFPA_BLOCKED 38, STRONG 9, SHORT_SALE_ONLY 6, WORKABLE 4, TIGHT 2
- sheriff_sale: 13/59 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): SOUTH ORANGE VILLAGE TW 59% (n=1), BAYONNE CITY 57% (n=1), EDGEWATER BORO 57% (n=1), MAYWOOD BORO 55% (n=1), CLOSTER BORO 53% (n=1), PLAINFIELD CITY 52% (n=3), RAHWAY CITY 50% (n=2), BLOOMFIELD TWP 47% (n=1)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

### Alpha discovery — spread potential by town (top 10)
| town | county | score | p75/p25 price-to-assessment gap | median sale | pre-1980 | in harvest |
|---|---|---|---|---|---|---|
| PATERSON CITY | Passaic | 65.39 | 1.47x | $480,000 | 91% | yes |
| NEWARK CITY | Essex | 55.67 | 1.41x | $517,000 | 84% | yes |
| PASSAIC CITY | Passaic | 49.76 | 1.34x | $550,000 | 95% | yes |
| LINDENWOLD BORO | Camden | 46.68 | 1.33x | $220,000 | 90% | yes |
| NEW BRUNSWICK CITY | Middlesex | 46.38 | 1.34x | $435,000 | 85% | yes |
| CAMDEN CITY | Camden | 46.36 | 1.63x | $135,000 | 96% | yes |
| GLOUCESTER CITY | Camden | 46.07 | 1.31x | $212,500 | 97% | yes |
| WEST NEW YORK TOWN | Hudson | 42.55 | 1.33x | $500,000 | 78% |  |
| GUTTENBERG TOWN | Hudson | 40.06 | 1.31x | $350,000 | 81% |  |
| ELMWOOD PARK BORO | Bergen | 39.56 | 1.29x | $580,000 | 87% |  |

<!-- report -->
## Swarm intelligence report — 2026-10-26 21:25 UTC

### Bottlenecks & findings
- Owner names known on only 24% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- Only 42% of the top 50 leads are underwritten — raise underwriter --top.

### Actions taken automatically
- Scoring weights v2 → v3: viability prior by source from underwriting verdicts: sheriff_sale=0.29
- Expansion: added LINDENWOLD BORO (Camden) to harvest list — renovated/distressed price gap 1.33x, median sale $220,000, 90% pre-1980 stock
- Expansion: added GLOUCESTER CITY (Camden) to harvest list — renovated/distressed price gap 1.31x, median sale $212,500, 97% pre-1980 stock

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| cash_buyers | success | 1/1 | 293 | 0 | 0 | 0% | 1136.0s |
| lead_scoring | success | 4/4 | 6331 | 0 | 0 | 0% | 8.1s |
| owner_enrichment | success | 2/2 | 65670 | 0 | 0 | 0% | 13.5s |
| sheriff_sales | success | 5/6 | 414 | 25 | 0 | 0% | 472.2s |
| stale_listings | success | 1/2 | 4793 | 950 | 0 | 0% | 730.4s |
| tax_assessment | success | 1/1 | 44216 | 4496 | 0 | 0% | 238.3s |
| tax_sales | success | 1/1 | 0 | 0 | 0 | 0% | 0.6s |
| underwriter | success | 2/2 | 11 | 0 | 0 | 0% | 107.6s |

### Yield & coverage
- Open leads: **6331** (6331 new in 24h) — by source: absentee_owner 3382, out_of_state_owner 1114, stale_listing 958, sheriff_sale 877
- Active sheriff sales tracked: 853; upset/judgment amount captured on 99%
- Owner name known: 24% · parcel matched: 97% · living area known: 12%
- Top-50 leads underwritten: 42%

### Spread viability
- Valuations: 36 — FRFPA_BLOCKED 24, STRONG 6, WORKABLE 4, SHORT_SALE_ONLY 1, TIGHT 1
- sheriff_sale: 10/36 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): EDGEWATER BORO 57% (n=1), CLOSTER BORO 53% (n=1), PLAINFIELD CITY 52% (n=1), BLOOMFIELD TWP 47% (n=1), HILLSIDE TWP 47% (n=4), ENGLEWOOD CITY 46% (n=1), NEW MILFORD BORO 45% (n=1), ROSELLE BORO 42% (n=2)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

### Alpha discovery — spread potential by town (top 10)
| town | county | score | p75/p25 price-to-assessment gap | median sale | pre-1980 | in harvest |
|---|---|---|---|---|---|---|
| PATERSON CITY | Passaic | 65.39 | 1.47x | $480,000 | 91% | yes |
| NEWARK CITY | Essex | 55.67 | 1.41x | $517,000 | 84% | yes |
| PASSAIC CITY | Passaic | 49.76 | 1.34x | $550,000 | 95% | yes |
| LINDENWOLD BORO | Camden | 46.68 | 1.33x | $220,000 | 90% | yes |
| NEW BRUNSWICK CITY | Middlesex | 46.38 | 1.34x | $435,000 | 85% | yes |
| CAMDEN CITY | Camden | 46.36 | 1.63x | $135,000 | 96% | yes |
| GLOUCESTER CITY | Camden | 46.07 | 1.31x | $212,500 | 97% | yes |
| WEST NEW YORK TOWN | Hudson | 42.55 | 1.33x | $500,000 | 78% |  |
| GUTTENBERG TOWN | Hudson | 40.06 | 1.31x | $350,000 | 81% |  |
| ELMWOOD PARK BORO | Bergen | 39.56 | 1.29x | $580,000 | 87% |  |

<!-- report -->
## Swarm intelligence report — 2026-10-26 20:38 UTC

### Bottlenecks & findings
- Owner names known on only 24% of open leads (NJGIN redacts names). Biggest unlock: owner-record enrichment / skip tracing for absentee leads.
- Upset amount missing on 43% of sheriff sales — extend detail parsers.
- Only 30% of the top 50 leads are underwritten — raise underwriter --top.

### Actions taken automatically
- Scoring weights v1 → v2: viability prior by source from underwriting verdicts: sheriff_sale=0.33
- Expansion: added LINDENWOLD BORO (Camden) to harvest list — renovated/distressed price gap 1.33x, median sale $220,000, 90% pre-1980 stock
- Expansion: added GLOUCESTER CITY (Camden) to harvest list — renovated/distressed price gap 1.31x, median sale $212,500, 97% pre-1980 stock

### Scraper health (7 days)
| agent | last status | ok runs | processed | new leads | errors | http fail | duration |
|---|---|---|---|---|---|---|---|
| cash_buyers | success | 1/1 | 293 | 0 | 0 | 0% | 1136.0s |
| lead_scoring | success | 2/2 | 6297 | 0 | 0 | 0% | 11.0s |
| owner_enrichment | success | 1/1 | 65670 | 0 | 0 | 0% | 19.2s |
| sheriff_sales | success | 2/2 | 856 | 3 | 0 | 0% | 1740.5s |
| stale_listings | success | 1/2 | 4793 | 950 | 0 | 0% | 730.4s |
| tax_assessment | success | 1/1 | 44216 | 4496 | 0 | 0% | 238.3s |
| tax_sales | success | 1/1 | 0 | 0 | 0 | 0% | 0.6s |
| underwriter | success | 1/1 | 25 | 0 | 0 | 0% | 220.0s |

### Yield & coverage
- Open leads: **6297** (6297 new in 24h) — by source: absentee_owner 3382, out_of_state_owner 1114, stale_listing 958, sheriff_sale 843
- Active sheriff sales tracked: 853; upset/judgment amount captured on 57%
- Owner name known: 24% · parcel matched: 96% · living area known: 12%
- Top-50 leads underwritten: 30%

### Spread viability
- Valuations: 25 — FRFPA_BLOCKED 17, WORKABLE 4, STRONG 4
- sheriff_sale: 8/25 STRONG/WORKABLE
- Median MAO/ARV by town (higher = more room): HILLSIDE TWP 48% (n=3), BLOOMFIELD TWP 47% (n=1), ENGLEWOOD CITY 46% (n=1), JERSEY CITY CITY 45% (n=2), NEW MILFORD BORO 45% (n=1), EDISON TWP 41% (n=1), LINDEN CITY 38% (n=1), BELLEVILLE TWP 38% (n=1)

### Learning
- Outcome learning waits for 20 labeled leads (have 0). Log call outcomes to feed it.

### Alpha discovery — spread potential by town (top 10)
| town | county | score | p75/p25 price-to-assessment gap | median sale | pre-1980 | in harvest |
|---|---|---|---|---|---|---|
| PATERSON CITY | Passaic | 65.39 | 1.47x | $480,000 | 91% | yes |
| NEWARK CITY | Essex | 55.67 | 1.41x | $517,000 | 84% | yes |
| PASSAIC CITY | Passaic | 49.76 | 1.34x | $550,000 | 95% | yes |
| LINDENWOLD BORO | Camden | 46.68 | 1.33x | $220,000 | 90% | yes |
| NEW BRUNSWICK CITY | Middlesex | 46.38 | 1.34x | $435,000 | 85% | yes |
| CAMDEN CITY | Camden | 46.36 | 1.63x | $135,000 | 96% | yes |
| GLOUCESTER CITY | Camden | 46.07 | 1.31x | $212,500 | 97% | yes |
| WEST NEW YORK TOWN | Hudson | 42.55 | 1.33x | $500,000 | 78% |  |
| GUTTENBERG TOWN | Hudson | 40.06 | 1.31x | $350,000 | 81% |  |
| ELMWOOD PARK BORO | Bergen | 39.56 | 1.29x | $580,000 | 87% |  |
