# Swarm Intelligence Log

Written by `swarm/self_improvement.py` after every pipeline run (newest first). Contains aggregate metrics only — no personal data.

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
