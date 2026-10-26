# Recon: free comparable-sales and listing data for NJ

Run on 2026-10-26 (UTC, about 19:10 to 19:25) from the Claude Code sandbox. Outbound HTTPS went through
the agent proxy with CA bundle `/root/.ccr/ca-bundle.crt` (`REQUESTS_CA_BUNDLE`). Python 3.13.17.
Every number below comes from code that was actually run. Raw dumps and scripts are in the session
scratchpad, not the repo.

## Verdicts

| Source | What we got | Verdict |
|---|---|---|
| **homeharvest 0.8.18** (scrapes Realtor.com's internal GraphQL API) | 30 of 30 zip sweeps succeeded and no throttling was seen in 41 calls. Sold: 1,893 rows (180 days) across 15 zips. Active: 1,803 rows (1,468 unique homes). Description text, list date, DOM, MLS id, AVM. | **USE** (primary, low volume, with a fallback ready; the ToS caveat below applies) |
| Redfin `stingray/api/gis-csv` | HTTP 200 CSV for zip 07095: 129 sold (180 days) and 48 active. You need Redfin's internal region id (07095 = 2592). The autocomplete endpoint returned a **403 CloudFront block**, but the id is embedded in the public zip page. No description text. | **FALLBACK** |
| Zillow | HTTP 403 "Access to this page has been denied" from **PerimeterX (px-captcha)** on both the search page and the `async-create-search-page-state` API. | **BLOCKED** |
| Realtor.com direct HTML | HTTP 429 with a **Kasada (kpsdk)** challenge on `/realestateandhomes-search/07095/show-recently-sold`. The GraphQL endpoint that homeharvest uses still works. | **BLOCKED** (HTML). Use homeharvest. |
| RentCast API | Without a key, `401 auth/api-key-invalid`. Free Developer plan: 50 requests/month, then $0.20 per request, and there is **no hard cap**. Has comps, AVM and listings endpoints. | **FALLBACK** (paid at scale; not signed up) |
| NJ SR1A (NJ Treasury) | Public deed-sale files. The YTD 2026 zip returned HTTP 200, 10,984,774 bytes, last-modified 2026-08-12. | **USE** as the authoritative closed-sale backfill (lagged, not real-time) |

---

## 1. homeharvest

### Install and version

```
$ pip install -U homeharvest
Successfully installed homeharvest-0.8.18 pandas-2.3.3 pytz-2026.5 tenacity-9.2.1 tzdata-2026.5
```

- Version **0.8.18**, the latest on PyPI (`pip index versions homeharvest`). Author Zachary Hampton (Bunsly).
- Dependencies: `pandas>=2.3.1,<3.0.0`, `pydantic>=2.11.7,<3`, `requests>=2.32.4,<3`, `tenacity>=9.1.2,<10`.
  **Installing it downgraded the system pandas from 3.0.5 to 2.3.3.** Pin it in its own venv or extra if the engine wants pandas 3.
- How it works (read from source): it posts to `https://www.realtor.com/frontdoor/graphql` with
  browser-like headers (`rdc-client-name: RDC_WEB_SRP_FS_PAGE`, `x-is-bot: false`, Chrome 135 UA). A 403
  raises `homeharvest.exceptions.AuthenticationError`. There is no built-in rate limiting. `tenacity` retries location
  lookup 3 times. In 0.8.18 `extra_property_data` is **hard-disabled** (`self.extra_property_data = False  # TODO: temporarily disabled`),
  so `assessed_value`, `tax`, `tax_history` and `nearby_schools` are always empty in search results.

### Exact signature (`inspect.signature(homeharvest.scrape_property)`)

```
(location: str, listing_type: str | list[str] | None = None, return_type: str = 'pandas',
 property_type: Optional[List[str]] = None, radius: float = None, mls_only: bool = False,
 past_days: int | datetime.timedelta = None, proxy: str = None,
 date_from: datetime.datetime | datetime.date | str = None, date_to: datetime.datetime | datetime.date | str = None,
 foreclosure: bool = None, extra_property_data: bool = True, exclude_pending: bool = False,
 limit: int = 10000, offset: int = 0, past_hours: int | datetime.timedelta = None,
 updated_since: datetime.datetime | str = None, updated_in_past_hours: int | datetime.timedelta = None,
 beds_min: int = None, beds_max: int = None, baths_min: float = None, baths_max: float = None,
 sqft_min: int = None, sqft_max: int = None, price_min: int = None, price_max: int = None,
 lot_sqft_min: int = None, lot_sqft_max: int = None, year_built_min: int = None, year_built_max: int = None,
 sort_by: str = None, sort_direction: str = 'desc', parallel: bool = True)
 -> Union[pandas.DataFrame, list[dict], list[Property]]
```

### Calls used

```python
# sold, per zip (sequential, 2.5 s pause between calls)
scrape_property(location="07095", listing_type="sold", past_days=180, return_type="raw", parallel=False)
# active, per zip (3 s pause)
scrape_property(location="07095", listing_type="for_sale", past_days=None, return_type="raw", parallel=False)
# radius around an address ("comps" mode)
scrape_property(location="186 Newark Ave, Bloomfield, NJ 07003", radius=0.5, listing_type="sold",
                past_days=180, return_type="raw", parallel=False)
# single address (no radius) -> GetHomeDetails
scrape_property(location="225 Freeman St, Woodbridge, NJ 07095", return_type="raw")
```

We fetched with `return_type="raw"` so that `flags`, `tags`, `details` and `source` were kept. The pandas frame was then
rebuilt **offline** with homeharvest's own `process_property` and `process_result` and the `ordered_properties` column list, which gives
the same frame `return_type="pandas"` would return, without a second request per zip.

### Returned columns (pandas, 66)

`property_url, property_id, listing_id, permalink, mls, mls_id, status, mls_status, text, style, formatted_address,
full_street_line, street, unit, city, state, zip_code, beds, full_baths, half_baths, sqft, year_built, days_on_mls,
list_price, list_price_min, list_price_max, list_date, pending_date, sold_price, last_sold_date, last_sold_price,
last_status_change_date, last_update_date, assessed_value, estimated_value, tax, tax_history, new_construction,
lot_sqft, price_per_sqft, latitude, longitude, neighborhoods, county, fips_code, stories, hoa_fee, parking_garage,
agent_id, agent_name, agent_email, agent_phones, agent_mls_set, agent_nrds_id, broker_id, broker_name, builder_id,
builder_name, office_id, office_mls_set, office_name, office_email, office_phones, nearby_schools, primary_photo, alt_photos`

The raw search rows have 31 top-level keys: `advertisers, current_estimates, description, details, flags, hoa, href, last_sold_date,
last_sold_price, last_status_change_date, last_update_date, list_date, list_price, list_price_max, list_price_min,
listing_id, location, mls_status, open_houses, pending_date, permalink, pet_policy, photos, price_per_sqft,
primary_photo, property_id, source, status, tags, tax_record, units`. `details` holds the MLS fields as
`"Key: value"` strings, such as `Annual Tax Amount`, `Original List Price`, `Price Change Timestamp`, `Short Sale` and `Possession`.

### Row counts and latency per zip (all calls `ok`)

| zip | area | sold rows (180 d) | latency s | for_sale rows (raw) | unique homes | latency s |
|---|---|---|---|---|---|---|
| 07095 | Woodbridge | 110 | 1.74 | 112 | 81 | 1.50 |
| 08817 | Edison | 188 | 1.94 | 167 | 129 | 1.76 |
| 08820 | Edison | 186 | 1.99 | 143 | 109 | 1.64 |
| 08901 | New Brunswick | 135 | 2.50 | 93 | 76 | 1.59 |
| 07201 | Elizabeth | 79 | 1.40 | 53 | 51 | 1.11 |
| 07202 | Elizabeth | 79 | 1.36 | 55 | 49 | 1.33 |
| 07104 | Newark | 95 | 1.71 | 150 | 110 | 3.03 |
| 07106 | Newark | 101 | 1.65 | 123 | 98 | 1.41 |
| 07107 | Newark | 87 | 1.58 | 119 | 91 | 1.54 |
| 07305 | Jersey City | 249 | 2.21 | 272 | 222 | 3.13 |
| 07306 | Jersey City | 229 | 4.79 | 223 | 188 | 2.15 |
| 07501 | Paterson | 104 | 1.96 | 111 | 91 | 1.23 |
| 07502 | Paterson | 61 | 1.37 | 35 | 29 | 1.30 |
| 08104 | Camden | 111 | 1.27 | 95 | 92 | 1.67 |
| 08105 | Camden | 79 | 0.76 | 52 | 52 | 1.14 |
| **total** | | **1,893** | median 1.71 | **1,803** | **1,468** | median 1.54 |

- The sold window covered 2026-04-29 to 2026-10-26. Every row's `postal_code` matched the zip queried. 07305 and 07306 needed 2 pages (page size 200), fetched sequentially.
- **Active results contain duplicates.** The same home comes back once per MLS feed (GSNJ, MCNJ, HCNJ, BCNJ, PHPA, MONJ): 1,803 rows map to 1,468 unique `property_id`s.
  The sold results have 1,893 unique property ids, but the same closing can also appear twice. For example, 924 Monroe Ave, Elizabeth closed 2026-10-19
  for $1,050,000 and shows up under both GSNJ and MCNJ, with city "Elizabeth City" in one and "Elizabeth" in the other.
- `for_sale` **includes pending and contingent listings**: 1,326 FOR_SALE, 300 PENDING and 177 CONTINGENT raw rows.
- Zips cross town lines. For example, 08901 returned East Brunswick addresses, and Woodbridge rows use the city names "Woodbridge Proper" and "Woodbridge".

### Sample sold rows (real output)

| address | sold price | sold date | sqft | beds | full/half baths | year built | lat, lon | style | MLS |
|---|---|---|---|---|---|---|---|---|---|
| 184 Green St, Woodbridge Proper, NJ 07095 | 599,900 | 2026-10-15 | n/a | 6 | 2/n/a | 1916 | 40.557500, -74.285685 | SINGLE_FAMILY | MCNJ |
| 614 Maple Hill Dr, Woodbridge Proper, NJ 07095 | 489,000 | 2026-10-15 17:22:57 | n/a | 2 | 2/1 | 1981 | 40.551960, -74.302481 | TOWNHOMES | MCNJ |
| 7 Candy Ct, Edison, NJ 08817 | 515,000 | 2026-10-22 | 1506 | 3 | 2/1 | 1902 | 40.509369, -74.370186 | SINGLE_FAMILY | MONJ |
| 43 Heathcote Ave, Edison, NJ 08817 | 430,000 | 2026-10-21 18:41:31 | 1518 | 3 | 1/1 | 1956 | 40.528257, -74.372216 | SINGLE_FAMILY | MCNJ |
| 4 Taylor Dr, New Brunswick, NJ 08901 | 600,000 | 2026-10-20 | 1711 | 4 | 2/1 | 1952 | 40.484435, -74.422073 | SINGLE_FAMILY | MCNJ |
| 36-38 Kenmore Ave, Newark, NJ 07106 | 675,000 (list 650,000) | 2026-10-16 | n/a | 6 | 4/n/a | 1911 | 40.749065, -74.233330 | MULTI_FAMILY | GSNJ |
| 333 Danforth Ave, Jersey City, NJ 07305 | 650,000 (list 699,000) | 2026-10-26 18:56:36 | n/a | 6 | 4/n/a | n/a | 40.702360, -74.096762 | MULTI_FAMILY | HCNJ |
| 1097 Morton St, Camden, NJ 08104 | 140,000 (list 165,000) | 2026-10-20 | 1280 | 4 | 1/n/a | 1920 | 39.926165, -75.107135 | TOWNHOMES | PHPA |
| 1345 Whitman Ave, Camden, NJ 08104 | 63,000 | 2026-10-16 | 922 | n/a | n/a | 1908 | 39.928872, -75.100237 | SINGLE_FAMILY | public record |
| 65 Beech St, Paterson, NJ 07501 | 600,000 | 2026-10-13 | 1859 | n/a | n/a | **1700** (bad record) | 40.912294, -74.164240 | SINGLE_FAMILY | public record |

### Field fill rates (% non-null, homeharvest pandas columns)

| field | sold (n=1,893) | sold, MLS rows (n=1,006) | sold, public-record rows (n=887) | active (n=1,803 raw) |
|---|---|---|---|---|
| sqft | 70.4 | 53 | 90 | 45.2 |
| beds | 53.2 | 99 | 1 | 97.0 |
| full_baths | 52.7 | 99 | 1 | 96.5 |
| half_baths | 17.1 | n/a | n/a | 24.2 |
| year_built | 80.3 | 75 | 86 | 58.3 |
| lot_sqft | 73.4 | 63 | 85 | 57.2 |
| sold_price | **100** | 100 | 100 | 61.6 (prior sale) |
| last_sold_date | **100** | 100 | 100 | 61.5 (prior sale) |
| list_price | 53.1 | 100 | 0 | 99.9 |
| list_date | 53.1 | 100 | 0 | **100** |
| days_on_mls | 50.8 | 96 | 0 | **100** |
| latitude / longitude | 98.8 | n/a | n/a | 99.6 |
| style (property type) | 99.5 | n/a | n/a | 100 |
| text (description) | 52.1 | 98 | 0 | **95.1** |
| price_per_sqft | 70.0 | 53 | 89 | 44.5 |
| neighborhoods | **0.0** | | | **0.0** |
| county | 99.8 | | | 100 |
| mls_status | 53.1 | | | 100 |
| estimated_value (Realtor AVM) | 88.2 | 87 | 89 | 82.4 |
| assessed_value / tax | 0.0 (feature disabled in 0.8.18) | | | 0.0 |

Notes:
- **About 47% of sold rows are public-record sales with no MLS source** (`mls` null). They have a price, a date, sqft (90%) and year built, but
  no beds or baths, list price, DOM or description. They also include non-arm's-length and bulk transfers: max sold_price was $79,000,000, and
  `style=OTHER` covers commercial buildings such as 1155 McCarter Hwy. On the other hand, these rows are where off-market cash deals show up.
  **MLS sold rows have beds and baths at 99%, but sqft only 53%.** NJ MLS feeds often leave square footage out.
- On sold MLS rows, `sold_price` equals `list_price` in 133 of 1,006 rows. Sold-to-list ratio is usable for the others.
- Multi-family MLS rows often report `beds = 0`, which means unknown, not a studio. 924 Monroe Ave shows 10 beds in GSNJ and 0 in MCNJ.
- `days_on_mls` is **computed by homeharvest** from list_date to sold date or to today. It is not the MLS DOM. Relisted or stale listings inflate it.
  For example, 14 Seymour Ave (07095) has list_date 2022-01-30 and DOM 1,730. A separate MLS "Days on Market" value is in `details` for MCNJ, HCNJ and MONJ rows (800 of 1,803),
  but it is a snapshot from the last update. Median absolute difference from the computed value is 10 days; p90 is 93 days.
- Sold days on market (MLS rows): median 83 days, range 0 to 761. Active rows with status FOR_SALE: median 46 days, and 25% have been listed more than 90 days.

### Days on market, price reductions, price history, description text (active)

- **List date and DOM: yes.** list_date fill is 100%, and DOM can be derived for every row. `past_days` on `for_sale` filters by list date, checked with
  `active_listings("07305", past_days=90)`: min list_date 2026-07-28.
- **Price reductions: partial.** The search query's `flags` contain only `is_contingent`, `is_pending` and `is_new_construction`. There is
  no `is_price_reduced`, and the tags show no price-reduced value. The only signal is `details → "Original List Price"`, which **only the Hudson County MLS
  (HCNJ) feed provides**: 401 of 401 HCNJ rows (plus 5 of 5 STNY rows) and 0 rows from GSNJ, MCNJ, BCNJ or PHPA. In practice that means Jersey City (07305: 211 of 272, 07306: 182 of 223).
  Across the 406 rows with an original price, 104 were reduced, 7 raised and 295 unchanged. Median cut was 5.4%.
  Two rows had typo originals (789,900,000 against a 789,900 list), so the provider ignores ratios outside 0.5 to 2. `Price Change Timestamp` exists for
  HCNJ, PHPA and MONJ but doesn't say which direction the price moved.
- **Price history: no.** The single-address `GetHomeDetails` call (225 Freeman St, 2.22 s) returned `taxHistory` (2025 tax
  $11,455, assessment $80,200), `estimates` and `nearbySchools`, but **no `property_history`**. Search rows don't include it either.
- **Description text: yes.** 1,715 of 1,803 rows have text, median 862 characters. Keyword hits across the 1,468 de-duplicated active homes, from the provider regexes:

| keyword | homes |
|---|---|
| as-is | 190 |
| investor (any mention) | 462 |
| investor special | 7 |
| needs TLC / "tlc" | 32 |
| tenant occupied | 32 |
| fixer / needs work / rehab | 20 |
| short sale | 11 |
| bank owned / REO / foreclos* | 8 |
| handyman | 5 (mostly false positives: "24-hour handyman service") |
| estate sale | 4 |
| cash only | 1 |
| at least one keyword | 587 of 1,468 |

Real hits:
```
154 Talmadge Rd, Edison 08817 | $525,000 | listed 2026-10-01 | DOM 25 | for_sale | ['as-is','tenant occupied','fixer']
  "...Home is being sold AS IS currently tenant occupied on Month to Month. Needs work but great opportunity..."
105 N Talmadge St, New Brunswick 08901 | $450,000 | listed 2026-08-10 | DOM 77 | for_sale | ['short sale']
  "...Short sale subject to a third-party approval. Buyer will be responsible for all ..."
216 Handy St, New Brunswick 08901 | $525,000 | listed 2026-08-27 | DOM 60 | contingent | ['estate sale']
225 Freeman St, Woodbridge 07095 | $499,900 | listed 2026-04-20 | DOM 189 | "Calling all Investors/Contractors!..."
```
Use regex hits as a cheap pre-filter, then classify in context with an LLM, because of false positives like "handyman service" and "investors welcome".
Realtor `tags` also carry `investment_opportunity` (173 raw active rows) and `fixer_upper` (102). The MLS `details → "Short Sale"` field
(155 rows: Standard 148, Bank Owned/REO 3, ShortSale 2, ThirdPartyApproval 1, Yes 1) is a structured distress flag where the feed provides it.

### Radius search

`scrape_property(location="186 Newark Ave, Bloomfield, NJ 07003", radius=0.5, listing_type="sold", past_days=180)` returned
**33 rows in 0.89 s** (07003: 27, 07109 Belleville: 6). The search is centered on the geocoded address. Example rows: 46 Watsessing Ave #B12, Belleville, $340,000 on 2026-10-14 (condo);
113 Harrison St, Bloomfield, $905,000 on 2026-09-30. 73 Berkeley Ave appears twice, once with city Belleville and once with Bloomfield and a different style, so it's another duplicate.

### Throttling

- **No throttling, 403 or 429 was seen.** In total we made **41 `scrape_property` calls**, roughly 85 HTTP POSTs because each call is one location lookup plus 1 or 2 search pages, over
  about 12 minutes from one IP. That includes **30 back-to-back zip sweeps at 2.5 to 3 s spacing**, all OK, with median latency about 1.6 s and max 4.79 s.
- The ceiling is unknown. We did not push harder on purpose, as instructed. homeharvest's own code expects blocks: on a 403 without
  a proxy it raises `AuthenticationError`, and its token helper's message says "use a proxy/vpn or wait a moment and try again". The provider
  wraps that case as `ProviderBlocked`.
- Recommended for production: at least 2 s between calls, sequential (`parallel=False`), cache by (zip, listing_type, date) for 24 h,
  and a nightly batch rather than calls per user request.

### Terms of use: read before relying on it

- homeharvest is unofficial. It calls Realtor.com's private GraphQL API with spoofed browser headers (`x-is-bot: false`).
- The Realtor.com Terms of Service (fetched HTTP 200 from `/terms-of-service/`) say: *"you shall not ... modify, copy, distribute, ...
  scrape, publish, license ..."* and *"Except with the express written permission of Move, you shall not ... access, collect, text mine
  or data mine, by automated means ... robot, spider, script ... any Content"*. The first line of `robots.txt` is: *"LEGAL NOTICE: Per https://www.realtor.com's
  Terms of Service, scraping data from this website is unauthorized without the express written permission from Move Sales, Inc."*
- So using homeharvest **violates Realtor.com's ToS**. The practical risk is IP blocking and the library breaking without warning. There is also legal and contract risk,
  which is higher if the data is redistributed. MLS data is licensed, and the listings carry MLS copyright. Keep usage internal, low-volume and
  cached, and don't republish raw listing text or photos. Have a fallback ready: SR1A for sold data, and RentCast or MLS/IDX access for listings.

---

## 2. Alternatives

### Redfin `gis-csv` (stingray): FALLBACK

```
GET https://www.redfin.com/stingray/do/location-autocomplete?location=07095&v=2
  -> HTTP 403 text/html (CloudFront "Request blocked")          # bot rule on autocomplete
GET https://www.redfin.com/zipcode/07095
  -> HTTP 200, 569 KB HTML; contains region_id=2592&region_type=2
GET https://www.redfin.com/stingray/api/gis-csv?al=1&num_homes=350&ord=redfin-recommended-asc&page_number=1
    &region_id=2592&region_type=2&sold_within_days=180&status=9&uipt=1,2,3,4&v=8
  -> HTTP 200 text/csv, 31,978 B, 0.72 s: 129 PAST SALE rows (all 07095)
GET ...same with status=1 (no sold_within_days)
  -> HTTP 200 text/csv, 12,685 B: 48 rows (47 MLS Listing, 1 For-Sale-by-Owner)
```
- Columns: `SALE TYPE, SOLD DATE, PROPERTY TYPE, ADDRESS, CITY, STATE OR PROVINCE, ZIP OR POSTAL CODE, PRICE, BEDS, BATHS, LOCATION,
  SQUARE FEET, LOT SIZE, YEAR BUILT, DAYS ON MARKET, $/SQUARE FEET, HOA/MONTH, STATUS, NEXT OPEN HOUSE START TIME, NEXT OPEN HOUSE END TIME,
  URL, SOURCE, MLS#, FAVORITE, INTERESTED, LATITUDE, LONGITUDE`. The first data line is a disclaimer: "In accordance with local MLS rules, some MLS listings are not included in the download".
- Sold fill: SOLD DATE 73.6%, BEDS and BATHS 73.6%, sqft 45.7%, year built 93.8%, **DOM 0%**, lat/lon 100%. Active fill: DOM 100%, sqft 54.2%.
- Sample: `585 Bamford Ave, Woodbridge | sold July-10-2026 | $515,000 | 3bd/2.0ba | 1,121 sqft | 1944 | 40.5607182,-74.2707661`.
- No description text and no price history. We requested `num_homes=350` per call. Region ids have to be scraped from HTML. Redfin's Terms of Use say: *"You may
  not automatedly crawl or query the Services for any purpose or by any means (including ... screen and database scraping, spiders,
  robots, crawlers ...)"*. That's the same ToS problem as homeharvest, and we also saw an active bot rule on autocomplete. Good as a cross-check or backup for sold comps.
  An unrelated guess, region_id=35596, returned Shoshone, ID 83352, so ids have to be resolved, not guessed.

### Zillow: BLOCKED
```
GET https://www.zillow.com/woodbridge-nj-07095/sold/                  -> HTTP 403, "Access to this page has been denied", px-captcha (PerimeterX)
PUT https://www.zillow.com/async-create-search-page-state (JSON body) -> HTTP 403, same PerimeterX page
```
We made no attempt to solve the challenge.

### Realtor.com direct: BLOCKED (HTML), GraphQL works
```
GET https://www.realtor.com/realestateandhomes-search/07095/show-recently-sold -> HTTP 429, Kasada challenge (kpsdk)
POST https://www.realtor.com/frontdoor/graphql (via homeharvest)               -> works (41/41 calls)
```

### RentCast API: FALLBACK (needs a key; we did not sign up)
- Unauthenticated `GET https://api.rentcast.io/v1/avm/value?address=...` returned `401 {"error":"auth/api-key-invalid","message":"No API key provided in request. An API key must be provided in the 'X-Api-Key' header"}`.
- Signup, per https://developers.rentcast.io/reference/getting-started-guide: create an account at https://app.rentcast.io/app/api, then **Select Plan**
  to activate a billing plan, then **Create API Key**, then send it as the `X-Api-Key` header.
- Pricing, from https://www.rentcast.io/api and the billing docs, fetched 2026-10-26:

  | plan | price per month | included requests | overage |
  |---|---|---|---|
  | Developer | $0 | 50 | $0.20 per request |
  | Foundation | $74 | 1,000 | $0.06 |
  | Growth | $199 | 5,000 | $0.03 |
  | Scale | $449 | 25,000 | $0.015 |

  The docs say: *"We do not currently support hard usage caps or automatic API request blocking"*. Overages are billed to the saved payment method.
  Email alerts go out at 85% and 100% of quota. Rate limit is 20 requests per second per key.
- Relevant endpoints:
  - `GET /v1/avm/value`: value estimate plus **comparables** (`compCount` 5 to 25, `maxRadius`, `daysOld`). The comps are **listings last seen on market**, not
    recorded closings.
  - `GET /v1/avm/rent/long-term`: rent AVM plus rental comps, useful for buy-and-hold exit math.
  - `GET /v1/properties`: property records with tax assessments, **sale history** and owner details. Up to 500 per page.
  - `GET /v1/listings/sale`: active and inactive sale listings by address, city, zip or radius.
  - `GET /v1/listings/rental/long-term`: rental listings.
  - `GET /v1/markets`: zip-level market statistics.
- At 50 free calls per month it only works for spot checks. For per-deal AVM and comps, Foundation ($74) is the realistic tier.

### NJ county SR1A sales: USE as the authoritative sold-data backfill
- NJ Division of Taxation, Property Administration "Statistical data" page, https://www.nj.gov/treasury/taxation/lpt/statdata.shtml (HTTP 200), links
  `statdata/YTDSR1A2026.zip` (year-to-date), `statdata/Sales2020.zip` through `Sales2026.zip`, and the record layout `../pdf/lpt/SR1Afilelayout.pdf`.
- `HEAD .../statdata/YTDSR1A2026.zip` returned HTTP 200, application/zip, 10,984,774 bytes, last-modified Wed, 12 Aug 2026.
- These are deed-recorded sales (SR-1A forms) for every NJ county, including non-usable sale codes. The data is free and public, with no ToS issue. Lag runs from weeks to months,
  and the file has no beds, baths or description. Joining with MOD-IV assessment data adds building details. We did not download it in this pass.

---

## 3. Provider module: `engine/homeharvest_provider.py`

```python
def sold_comps(location: str, radius_miles: float | None = None, past_days: int = 180,
               property_types: list[str] | None = None) -> list[dict]
def active_listings(location: str, past_days: int | None = None) -> list[dict]
```
- Each function makes one throttled call: `MIN_INTERVAL_S = 2.0`, a process-wide lock, `return_type="raw"` and `parallel=False`.
  A 403 or auth failure raises `ProviderBlocked`. Other failures raise `ProviderError`. Both are subclasses of `RuntimeError`.
- Output keys: `address, city, state, zip, lat, lon, property_type, beds, baths (full + 0.5*half), sqft, lot_sqft, year_built,
  sold_price, sold_date (YYYY-MM-DD), list_price, list_date (YYYY-MM-DD), days_on_market, price_per_sqft (sold or list price / sqft),
  description, url, mls_status, source="homeharvest", price_reduced (bool|None), raw (JSON-safe original row)`.
  Extras: `property_id, mls (None = public-record sale), status (sold / for_sale / pending / contingent), original_list_price,
  last_sale_price, last_sale_date, estimated_value (Realtor best AVM), distress_keywords (list)`.
- Behavior:
  - When `property_types` is None, results are filtered client-side to residential types, dropping land, other, mobile and null. When it's given, it is passed to homeharvest as a server-side filter.
  - `sold_comps` de-duplicates on (address, zip, sold_date, sold_price) and keeps the richest row.
  - `active_listings` de-duplicates by `property_id`. It still includes pending and contingent listings; filter on `status`.
  - `beds = 0` on multi-family becomes None.
  - NaN and inf become None.
  - `price_reduced` is None unless an `is_price_reduced` flag or a plausible `Original List Price` exists.

### Demo output (`python3 engine/homeharvest_provider.py`, final run)

```
homeharvest 0.8.18 | location=07095 | 2026-10-26 19:23:38Z

=== sold_comps('07095', past_days=180): 105 rows in 1.86s 
fill %: sqft=59, beds=76, baths=77, year_built=99, lot_sqft=73, sold_price=100, sold_date=100, list_price=76, list_date=76, days_on_market=72, lat=100, price_per_sqft=59, description=74, price_reduced=0, mls=76
property_type: {'townhomes': 5, 'single_family': 65, 'condos': 24, 'condo': 9, 'multi_family': 2}
MLS-backed: 80, public-record only: 25; median sold_price: 480,000
   {"address": "614 Maple Hill Dr", "city": "Woodbridge Proper", "zip": "07095", "sold_price": 489000, "sold_date": "2026-10-15", "sqft": null, "beds": 2, "baths": 2.5, "year_built": 1981, "lat": 40.55196, "lon": -74.302481, "property_type": "townhomes", "mls": "MCNJ", "days_on_market": 57, "price_per_sqft": null}
   {"address": "184 Green St", "city": "Woodbridge Proper", "zip": "07095", "sold_price": 599900, "sold_date": "2026-10-15", "sqft": null, "beds": 6, "baths": 2.0, "year_built": 1916, "lat": 40.5575, "lon": -74.285685, "property_type": "single_family", "mls": "MCNJ", "days_on_market": 59, "price_per_sqft": null}
   {"address": "468 Gorham Ave", "city": "Woodbridge Proper", "zip": "07095", "sold_price": 558600, "sold_date": "2026-10-09", "sqft": 1550, "beds": 3, "baths": 2.5, "year_built": 1952, "lat": 40.556631, "lon": -74.289706, "property_type": "single_family", "mls": "MCNJ", "days_on_market": 152, "price_per_sqft": 360.39}

=== active_listings('07095'): 81 rows in 3.71s 
fill %: sqft=57, beds=96, baths=100, year_built=98, lot_sqft=77, sold_price=0, sold_date=0, list_price=100, list_date=100, days_on_market=100, lat=100, price_per_sqft=57, description=99, price_reduced=0, mls=100
property_type: {'single_family': 41, 'condos': 34, 'multi_family': 3, 'townhomes': 3}
status: {'for_sale': 44, 'contingent': 30, 'pending': 7} | price_reduced True/False/None: 0 0 81
distress keyword hits: {'as-is': 4, 'investor': 3, 'needs tlc': 1}
   {"address": "1027 Cricket Ln Unit 1027", "zip": "07095", "list_price": 249000, "original_list_price": null, "list_date": "2026-09-23", "days_on_market": 33, "status": "pending", "price_reduced": null, "distress_keywords": ["as-is"], "url": "https://www.realtor.com/realestateandhomes-detail/1027-Cricket-Ln_Woodbridge_NJ_07095_M63383-67873"}
   {"address": "621 Cricket Ln Unit 621", "zip": "07095", "list_price": 265000, "original_list_price": null, "list_date": "2026-09-01", "days_on_market": 55, "status": "contingent", "price_reduced": null, "distress_keywords": ["investor"], "url": "https://www.realtor.com/realestateandhomes-detail/621-Cricket-Ln_Woodbridge_NJ_07095_M65561-13997"}
JSON-serialisable: ok; raw keys: 31
```

Other module runs, all real:
```
sold_comps(radius 0.5mi Bloomfield): 33 rows 2.41s zips={'07109': 6, '07003': 27}
sold_comps('07095', property_types=['single_family']): 65 rows 4.72s types={'single_family': 65} mls-backed=49
active_listings('07305', past_days=90): 144 rows 5.34s min list_date=2026-07-28
  price_reduced: {False: 104, None: 15, True: 25}
   {"address": "119 Woodlawn Ave", "zip": "07305", "list_price": 575000, "original_list_price": 599000, "list_date": "2026-10-06", "days_on_market": 20, "status": "for_sale", "mls": "HCNJ"}
   {"address": "93 Pearsall Ave Unit 1-2", "zip": "07305", "list_price": 599000, "original_list_price": 619000, "list_date": "2026-10-03", "days_on_market": 23, "status": "for_sale", "mls": "HCNJ"}
```
(That 07305 run happened before the plausibility guard was added, and it listed 369 Van Nostrand Ave, original 789,900,000, as reduced. After the guard,
an offline re-normalization of the saved 07305 rows gives `{False: 159, None: 63, True: 50}` and marks both typo rows None.)
Across all 15 zips, after de-duplication and the residential filter: **1,769 sold comps and 1,468 active homes**. In the provider output, active `description` is 97% filled
and `price_reduced` is known for 26% of rows (382 of 1,468), essentially all from rows sourced from the Hudson County MLS (HCNJ) feed.

---

## 4. Recommendation for the underwriting engine

1. **Use homeharvest as the primary live source for both sold comps and active listings**, through `homeharvest_provider.py`, as an
   optional dependency in its own extra so its `pandas<3` pin doesn't affect the core. Run it in nightly per-zip batches with at least 2 s spacing and a 24 h cache,
   not per request. On `ProviderBlocked`, fall back and alert.
2. **Comp selection rules:** weight MLS-backed sold rows (`mls` not null) over public-record rows, or require them. Drop outliers with
   sold_price below $20k, more than 3x the zip median, or year_built below 1800, and drop non-residential types. Sqft is missing on about 47% of MLS sold rows. When sqft is missing,
   match comps on beds, baths, type and distance, and fill sqft from public-record rows or MOD-IV/SR1A for the same address.
3. **Distress and motivation scoring for active listings:** combine `distress_keywords` (then an LLM check of the text), DOM over 90 days, `status`,
   `price_reduced` (only reliable in Hudson County), `tags` (`fixer_upper`, `investment_opportunity`) and MLS `Short Sale` details from `raw`.
   Price history is not available from this source.
4. **Backfill and verification:** ingest NJ SR1A yearly and YTD files as the record of closed sales. They are free, have no ToS issues, and cover the off-MLS
   transactions wholesalers care about. Keep Redfin gis-csv as the secondary sold-comps fallback. Only consider RentCast Foundation ($74/mo) if a
   supported API is required, and keep in mind its comps are based on listings.
5. **Legal:** homeharvest and Redfin both violate the sites' ToS. Keep the data internal, don't republish listing text or photos, and plan for
   licensed MLS/IDX access (via a broker partner) or a paid API if the business scales.
