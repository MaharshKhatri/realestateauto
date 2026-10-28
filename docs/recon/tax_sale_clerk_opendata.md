# Recon: Tax sale lists, county clerk records (lis pendens), open data — NJ target counties

- **Date of recon:** 2026-10-26 (all results below were fetched live that day unless marked otherwise)
- **Counties:** Middlesex, Union, Essex, Hudson, Passaic, Bergen, Camden
- **Method:** `curl` / Python `requests` from the sandbox (egress proxy), plus WebSearch/WebFetch. ≤1 request/second per host. No CAPTCHA was solved or bypassed. No click-through terms were accepted on the operator's behalf.
- **Verdict legend:** **USE** = free, no login, plain HTTP, build a scraper. **FALLBACK** = free and machine-readable but irregular, partial or PDF-only. **MANUAL** = a person must do it in a browser (JS app, CAPTCHA, terms gate, robots disallow). **BLOCKED** = paid, login-only, or the bot wall refused us.

> **Bottom line:** We can automate for free: **(1)** RealAuction's NJ tax-sale sites (`*.newjerseytaxsale.com`), which cover 91 municipalities in our 7 counties, including Newark, Paterson, Elizabeth, Camden, Clifton, East Orange, Irvington, Linden, New Brunswick, Perth Amboy, Plainfield and Union City. **(2)** The Union County clerk index, which returns lis pendens and foreclosure filings by date range over plain HTTP. **(3)** The Essex County Register (PRESS), which does the same. **(4)** The statewide NJ construction-permit dataset on Socrata (it includes demolitions). Everything else is manual or blocked: Jersey City, Bayonne, Hackensack, Woodbridge and Edison tax-sale lists; the Bergen, Middlesex, Camden and Hudson clerk systems; NJ Courts; and the newspaper notice site.

---

## Part 1 — Tax sale and tax delinquency lists

### 1.0 How NJ publishes tax sale lists (legal frame, verified)

- Every municipality must hold a tax lien sale each year for prior-year delinquent taxes, water and sewer charges. The Jersey City taxes FAQ (`https://www.jerseycitynj.gov/cityhall/finance/taxes`) says: *"All the State of New Jersey municipalities are required to hold a tax sale for all liens … that remain unpaid at the end of the yearly cycle, November 11th. The Tax Sale is held in December."* Many towns hold an "accelerated" sale in December. Others sell the next spring or fall.
- **Online sales are regulated by N.J.A.C. 5:33-1.1.** Fetched from `https://law.cornell.edu/regulations/new-jersey/N-J-A-C-5-33-1-1`:
  - (d)(1): *"The tax lien sale line items shall be available to the public online on the vendor's website … without requiring registration, membership, or payment prior to viewing."*
  - (e)(3): the ad must say that *"the listing of all parcels and delinquencies and costs … are available online for viewing at no cost"*.
  - (e)(5): the ad must give *"The full website link where the tax lien sale line items may be viewed."*
  - The display ad runs *"once a week for four weeks, prior to the week of the sale."*

  **Consequence:** for every town that sells online, the line items must legally be public and free. In practice that means RealAuction (§1.1).
- Towns that sell **in person** (Jersey City, Bayonne, Edison, Hackensack, Garfield) only have to publish the list in a newspaper. The list then appears as a PDF on the town website (sometimes), as a newspaper legal notice (always), or only on request.

### 1.1 RealAuction / "RealTaxLien" — `https://<Town>.newjerseytaxsale.com/` → **USE (top priority)**

**Vendor:** Realauction.com. The `newjerseytaxsale.com` sites are RealAuction's. `https://www.newjerseytaxsale.com/` itself redirects to realauction.com. Tax Sale Resources and Zeus Auction are covered in §1.4.

**Discovery endpoint (full list of NJ towns):**
```
POST https://manalapan.newjerseytaxsale.com/jumpMenu/flyoutmenu.cfm
     body: state=NJ&auctionsToList=all        # "all" = 275 NJ sites; omit auctionsToList for "current" = 182
→ HTML; parse:  aria-label="<Town>" … jump_vendor_id="<id>" url="//<Host>.newjerseytaxsale.com/"
```
Any NJ subdomain serves this endpoint. 275 NJ sites in total. **91 of them are in our 7 counties** (table below).

**Per-site endpoints (no login, plain server-rendered HTML, ColdFusion):**

| Purpose | Request | Notes |
|---|---|---|
| Upcoming list ("Preview Items for Sale") | `POST https://<Host>/index.cfm?folder=previewItems` body `pageNum=<n>&orderBy=AdvNum&orderDir=asc` | 50 rows per page. Header shows `of N (Displaying 50 of <total> records)`. Rows are `<tr id="cert<certID>">`. |
| Sold liens ("Results") | `POST https://<Host>/index.cfm?folder=auctionResults&mode=preview` body as above | Same paging. |
| Sale date / rules | `GET https://<Host>/index.cfm?folder=showDocument&documentName=biddingRules` | Regex `held via the Internet (on )?MM/DD/YYYY` and `(20\d\d) Tax (Lien )?Sale`. |
| Parcel detail (address + owner) | `POST https://<Host>/popup/popup-certificates.cfm` body `certID=<certID>&page=certpop-property-details.cfm` | Returns ADV num, Block-Lot-Qual, **Property Address**, Owner Name, Face Value, number of bids, status, and *"information … current as of MM/DD/YYYY"*. |

- **robots.txt** (`https://clifton.newjerseytaxsale.com/robots.txt`): for `User-agent: *` it disallows only `/test/ /common/ /home/ /legal/ /login/ /myAccount/ /privacy/ /register/`. `/index.cfm` and `/popup/` are not disallowed. The `*.cfm` disallow applies to Googlebot only.
- **Fields:**
  - Preview: `ADV NUM, Tax Year, Parcel ID (block - lot [- qual]), Owner Name, Charge Type (T=tax, W=water, S=sewer, O=other), Face Amount, Status (Active/Unavailable)`.
  - Results: `ADV NUM, Tax Year, Notices, Block-Lot-Qual, Face Amount, Winning Bid ($ premium or % rate), Sold To (bidder number)`.
  - Popup adds `Property Address`, `Owner Name`, `Alternate Parcel ID` and an "as of" date.
- **Status semantics:** *Unavailable* most likely means the charge was paid or withdrawn before the sale. Evidence: Clifton has 342 "Active" preview rows, and its Results list has exactly 342 records.
- **Freshness:** the popup's "current as of" date. Examples: East Orange 09/29/2026, Clifton 09/15/2026, Paterson 11/12/2025.
- **Caveats:**
  - Some sites go "currently offline" between sales: Newark, Irvington, Union City, Plainfield, Roselle, Westfield, Passaic City, North Bergen and others (list below).
  - Some sites hide Preview after the sale (Camden: *"Preview Items for Sale for the 2026 City of Camden Tax Lien Auction is currently unavailable"*). The Results list stays up.
  - Results lists carry **no owner name**. Fetch the popup per cert to get owner and address.

**Tested — real downloads (full pagination):**

*Clifton (Passaic). Sale held 10/15/2026. `previewItems`: 14 pages, **659 rows parsed of 659**. 342 Active / 317 Unavailable. Charge types: S 563, T 46, "T S" 41, O 5. Total face $1.22M, max $56,856, 68 rows over $5K.*
```
certID   ADV     Yr    Block-Lot   Owner                              Type  Face        Status
5698101  000001  2025  1.01 - 11   [individual]                       S     $1,074.89   Active
5698106  000006  2025  1.13 - 21   [individual]                       S     $8,668.62   Active
5698107  000007  2025  1.22 - 9    385 LAKEVIEW AVE CLIFTON LLC       T S   $20,457.41  Active
popup(5698101) → Property Address "[address] Clifton, NJ", current as of 09/15/2026
```

*East Orange (Essex). **Sale is 10/29/2026 (3 days out).** `previewItems`: 9 pages, **408 rows parsed of 408**. 290 Active / 118 Unavailable. Charge types: W 228, T 157, "T W" 23. Total face $3.54M, max $759,231, 107 rows over $5K. Top Active tax items, with addresses from the popup:*
```
ADV   Block-Lot   Owner                                Type  Face         Address (popup)
0278  563 - 20    KHUBANI ENTERPRISES                  T     $182,428.92  250 PROSPECT ST.
0164  371 - 2     GUIDING STAR UNITED HOLY CHURCH OF   T     $97,821.66   400 DR. MLK JR., BLVD.
0283  580 - 41    132 EAST ORANGE PORTFOLIO LLC        T     $74,040.45   444-50 PROSPECT STREET
0002  12 - 2.06   [individual]                         T     $12,285.87   [address] (as of 09/29/2026)
```

*Paterson (Passaic). Sale held 12/10/2025. `auctionResults`: 20 pages, **956 rows parsed of 956** (liens sold, now under a 2-year redemption clock):*
```
ADV   Yr    Block-Lot  Face       WinningBid  SoldTo
0001  2025  101 - 7    $828.16    $1,500.00   0332     popup → [address], owner [individual] (as of 11/12/2025)
0017  2025  108 - 10   $2,506.23  $15,900.00  0566
0027  2025  116 - 8    $535.38    $34,000.00  0566
```
*Camden City Results (sale 04/06/2026): **2,309** records. Sample: `000006 2025 9 - 45 $163.08 16.00% 40869`. A % in the bid column means the bid was on the interest rate, not a premium.*

**Survey of all 91 target-county RealAuction sites (2026-10-26).** "preview" = upcoming-list rows; "results" = sold-lien rows; date = sale date from biddingRules.

| County | Sites | Preview rows | Results rows | Offline now |
|---|---|---|---|---|
| Essex | 16 | 2,438 | 873 | 4 (Cedar Grove, Irvington, Newark, Verona) |
| Union | 11 | 132 | 1,525 | 3 (Plainfield City, Roselle, Westfield) |
| Hudson | 6 | 13 | 312 | 2 (North Bergen, Union City) |
| Passaic | 6 | 859 | 1,383 | 1 (Passaic City) |
| Bergen | 19 | 104 | 266 | 5 (Edgewater, Fort Lee, Oakland, Ramsey, River Edge) |
| Middlesex | 16 | 956 | 1,549 | 5 (East Brunswick, Highland Park, Sayreville, South Amboy, South River) |
| Camden | 17 | 1,833 | 5,661 | 2 (Gibbsboro, Oaklyn) |
| **Total** | **91** | **6,335** | **11,569** | 22 |

Target big cities:

| City | Host | Status |
|---|---|---|
| Newark | `Newark.newjerseytaxsale.com` | "City of Newark Tax Sale website is currently offline". An unverified search snippet (samsearch.co, behind a Cloudflare gate) mentions a Newark RFP for "Online Tax Sales" with responses due 2026-07-28. |
| Paterson | `Paterson.newjerseytaxsale.com` | 956 results (sale 12/10/2025) |
| Elizabeth | `Elizabeth.newjerseytaxsale.com` | 278 results (sale 06/08/2026) |
| Camden | `Camden.newjerseytaxsale.com` | 2,309 results (04/06/2026) |
| Clifton | `Clifton.newjerseytaxsale.com` | 659 preview, 342 results (10/15/2026) |
| East Orange | `EastOrange.newjerseytaxsale.com` | 408 preview (sale **10/29/2026**) |
| Irvington | `Irvington.newjerseytaxsale.com` | offline |
| Linden | `Linden.newjerseytaxsale.com` | 351 results (06/05/2026) |
| New Brunswick | `NewBrunswick.newjerseytaxsale.com` | 455 results (12/23/2025) |
| Perth Amboy | `PerthAmboy.newjerseytaxsale.com` | 483 results (12/17/2025) |
| Plainfield | `PlainfieldCity.newjerseytaxsale.com` | offline |
| Union City | `UnionCity.newjerseytaxsale.com` | offline |
| Jersey City, Bayonne, Edison, Woodbridge, Hackensack | not in RealAuction's 275-site list | see §1.2 |

Other big lists in the target counties:
- Results: Gloucester Twp 1,572; Hillside 847; Pennsauken 458; Orange City 452.
- Preview: Pennsauken 969; West Orange 867 (sale 11/20/2026); Piscataway 515; Runnemede 278; Montclair 267 (11/17/2026); Bloomfield 267; Carteret 266.

Upcoming sales after today: Bloomingdale and Stratford (10/27), Washington Twp-Bergen (10/28), East Orange (10/29), Glen Ridge (10/30), Magnolia (11/04), Park Ridge (11/05), Montclair and Cranford (11/17), Essex Fells (11/18), Caldwell (11/19), West Orange (11/20), Dunellen (11/24).

**Verdict: USE.** Scrape all 91 target-county sites weekly; scrape daily in the 30 days before a sale. Key every row on `(municipality, block, lot, qual, tax_year)`.
- **Upcoming list (preview):** the owner is delinquent now and can still pay before the sale. Strongest wholesaling signal.
- **Results:** a private certificate now sits on title. Foreclosure becomes possible 2 years after the sale date.
- Repeated `Sold To` bidder IDs (e.g., 0332 and 0566 in Paterson) identify large lien buyers.

### 1.2 Municipal tax-collector web pages (PDF lists) → **FALLBACK / MANUAL**

| Town | URL (fetched) | What we found | Rows | Verdict |
|---|---|---|---|---|
| **Bayonne** (Hudson) | News page `https://www.bayonnenj.org/News/View/11976/tax-sale-notice` → list `https://www.bayonnenj.org/_Content/pdf/notices/2024-TAX-SALE-ADVERTISEMENT-LIST.pdf` | "NOTICE OF 2024 TAX SALE, Date of Sale: 4/16/2025". Excel-to-PDF, 5 pages, text layer OK. Fields: `BLOCK, LOT, QUALIFIER, OWNER NAME, PROPERTY LOCATION, TAX, INTEREST, SALE-COST, TOTAL`. The 2026 notice (`.../2026-04-15-Tax-Sale-Notice.pdf`, sale 4/15/2026, private sale at the collector's office) is a 1-page notice with **no list**, and no 2026 list link was found. | **157** | FALLBACK (yearly PDF; find the link by hand) |
| **Teaneck** (Bergen) | `https://www.teanecknj.gov/media/uozdtmsa/2025-tax-sale-list.pdf` | Edmunds GovTech report "Tax Sale Listing By Block/Lot/Qual", printed 12/20/2024, sale 01/21/25, 4 pages. Fields: `Block, Lot, Qual, Owner Name, Amount, type code, Property Location`. Teaneck has since moved to RealAuction (58 results for its 01/21/2026 sale). | **194** | FALLBACK (Edmunds layout is common; one parser fits many towns) |
| Garfield (Bergen) | `https://www.garfieldnj.org/_Content/pdf/notices/2025-10-02-Tax-Sale-Property-Notice.pdf` | 1-page notice for the 10/02/2025 in-person sale. The "following list" is **not in the PDF**. | 0 | MANUAL |
| Edison (Middlesex) | `https://www.edisonnj.org/departments/finance/accelerated_tax_sale.php` | In-person sale 12/15/2025 at Town Hall. *"The Tax Sale List and Bidder information packages could be requested by email to: TaxCollector@edisonnj.gov"*. Published in the Home News Tribune. | — | MANUAL (email/OPRA, newspaper) |
| Hackensack (Bergen) | `https://www.hackensack.org/tax-collector` | *"Our 2026 Accelerated Tax Lien Sale will be held on December 21, 2026"*. Proceedings begin Nov 11. No list link; bidder registration by email. | — | MANUAL (list appears in the newspaper late Nov/Dec) |
| Jersey City (Hudson) | `https://www.jerseycitynj.gov/cityhall/finance/taxes` | FAQ only: *"The Tax Sale is held in December."* No list or link. `/cityhall/finance/tax_collector` returns 404. | — | MANUAL (Jersey Journal legal notice via §1.3) |
| Woodbridge (Middlesex) | `https://www.twp.woodbridge.nj.us/948/Tax-Payer-Information` (via WebFetch; curl got connection resets) | No tax-sale information. Payment via Edmunds WIPP `https://wipp.edmundsassoc.com/Wipp1225/`. | — | MANUAL |
| Waldwick, Old Tappan, Nutley | URLs from search results: `https://www.waldwicknj.gov/media/departments/clerk/Legal Notices/11-25-2025  Tax Sale Ad 2024  12-17-25.pdf`, `https://oldtappan.net/media/attachments/2025/11/26/2025-borough-of-old-tappan-tax-sale-notice-week-3.pdf`, `https://www.nutleynj.org/taxsale` | Found by search (Bergen/Essex 2025 lists with block, lot, owner, amount, location). **Not downloaded.** Nutley is now also on RealAuction (96 results). | ? | FALLBACK |

**Edmunds WIPP (per-parcel tax balance).** Edison `https://wipp.edmundsassoc.com/Wipp1205/` → `https://wipp.edmundsassoc.com/Wipp/?wippid=1205`. Woodbridge uses `Wipp1225`; Camden's revenue page links a "Wipp Portal". It is a GWT single-page app (`wipp/wipp.nocache.js`, GWT-RPC). It is not plain HTTP, and it does one parcel at a time. **MANUAL.** It is good for checking one owner's current delinquency before calling, not for building lists.

### 1.3 Newspaper legal notices — NJ Press Association `https://www.njpublicnotices.com/` → **MANUAL**

- **Search works without login.** ASP.NET WebForms with a cookieless session in the path `/(S(<sid>))/Search.aspx`. Fields: `txtSearch`, `rdoType` (AND/OR/EXACT), `txtExclude`, County checkboxes (all 21 counties), City list, Publication list (Star-Ledger, Jersey Journal, The Record, Courier-Post, Home News Tribune, Courier News, etc.), and date range (last N days or From/To). Tested: "tax sale", last 60 days, statewide → **42 pages × 10 results**.
- **Results list shows only a snippet (~300 characters).** Real results seen on 2026-10-26:
  - northjersey.com 10/26/2026: *"NOTICE OF IN REM FORECLOSURE OF TAX LIEN TITLES … CHANCERY DIVISION-HUDSON COUNTY DOCKET NUMBER F-9018-26 By: PRO CAP 8 FBO FIRSTRUST BANK"*
  - courierpostonline.com 10/26/2026: *"NOTICE TO ABSENT DEFENDANTS … CAMDEN COUNTY DOCKET NO. F-010287-26 …"*
  - app.com: *"THE BOROUGH OF ATLANTIC HIGHLANDS … 2026 TAX SALE … NOVEMBER 13, 2026 … HTTPS://ATLANTICHIGHLANDS.NEWJERSEYTAXSALE.COM"*
  - northjersey.com 10/25/2026: Township of Little Falls (Passaic) *"NOTICE OF TAX SALE"*.
- **Full text is gated.** `Details.aspx?SID=…&ID=<id>` requires a reCAPTCHA. The page says: *"I understand and agree that I may not use the content of this site in any database, compilation, archive or cache and that I may not engage in any unauthorized screen scraping … or use of any other automated means to collect information from the site. You must complete the challenge in order to continue."*
- **Verdict: MANUAL.** The terms forbid automated collection, and full text sits behind a CAPTCHA. Use it by hand each December for Jersey City, Bayonne, Hackensack, Woodbridge and Edison lists. Use it for in-rem and tax-lien foreclosure notices (Notice to Absent Defendants gives docket F-numbers, which feed §2.8). Its "Smart Search" email alerts (free sign-up) are an allowed way to get notices pushed to the operator.

### 1.4 Third-party lien and tax-sale data vendors

| Source | URL (fetched) | Finding | Verdict |
|---|---|---|---|
| Tax Sale Resources | `https://www.taxsaleresources.com/counties/newark-city-essex-new-jersey`; sale page `https://cloud.taxsaleresources.com/public/sales/new-jersey/chester-township-morris/lien/09-08-2026--evQSz9bw` | Directory pages give only contacts. Sale pages show parcel count and opening bid, but **addresses are hidden ("Click to see the address")**. Paid plan: $1.99 trial, then $97/month. | BLOCKED (paid) |
| TaxDataHub | `https://www.taxdatahub.com/6229fbf0ce4aef911f9de7bc/Essex%20County?town=0714` (linked from the Newark RealAuction site; Edison links the Middlesex hub) | "Essex County Property Assessment Search Hub": MOD-IV assessment data with Excel export and a download cap. Loads Google reCAPTCHA v3. No tax-sale data. | Not a delinquency source. Assessment data belongs to a different recon. |
| Zeus Auction | search only | No NJ municipality found using it (CO and LA results only). | n/a |
| NJ DLGS online-sale rule | see §1.0 | The legal basis for the free public RealAuction line items. | reference |

### 1.5 Municipal-held certificates and in-rem foreclosure lists → **FALLBACK (high value)**

- **Camden City, assignment of municipal-held tax sale certificates.** These are legal-notice PDFs the city posts before Council votes (N.J.S.A. 54:5-114.2).
  - **Less than face value:** `https://www.camdennj.gov/wp-content/uploads/2026/03/LFV-March-2026-Legal-Notice.pdf`. Council vote 03/10/2026. 16 pages, Word-to-PDF, text layer OK. Fields: `Cert#, Property Address, Block, Lot, Property Owner, Winning Bid, Redemption Amount as of 03/10/2026`. **About 370 rows.** Sample:
    ```
    89-62     [address]             88   51   [individual]                     $7,000.00   22,104.06
    89-157    [address]             244  17   [individual]                     $15,000.00  180,105.61
    960132    [address]             244  31   [estate of an individual]        $5,995.00   201,284.37
    ```
    These are certificates from as far back as 1989 with redemption figures far above value. They are essentially abandoned or zombie properties, and some owners are estates.
  - **Full face value:** `https://www.camdennj.gov/wp-content/uploads/2026/08/August-2026-Full-Value-Publishing-1.pdf`. Vote 08/11/2026. 4 pages. Fields: `Cert#, Property Location, Block, Lot, Dimensions, Successful Bidder, Amount`. **About 63 rows.** Sample: `20-00542 408 WEBSTER STREET 467 19 28X95 ZION INVESTMENT GROUP LLC $28,739.60`. Owners are not listed; join on block/lot.
  - The revenue page `https://www.camdennj.gov/revenue-collections/` offers a "Request for Assignment of Municipal Held Lien" form (`.../2021/08/REQUEST-FOR-ASSIGNMENT-OF-MUNICIPAL-HELD-LIEN.pdf`). This means anyone can ask to buy a city-held lien.
  - **Verdict: FALLBACK.** Publication is irregular. Watch `camdennj.gov/wp-content/uploads/<yyyy>/<mm>/` links from the City Council and Revenue pages, then parse with `pdftotext -layout`.
- **In-rem lists (search-found, not downloaded):** West Milford council resolutions authorizing in-rem foreclosure of "Tax Foreclosure List In Rem #39/#39B" (`https://www.westmilford.org/FCpdf/2026%2D107%2DAuthorizeIN%2DREM%2039B%20With%20LIST%20and%20WITHOUT%20Names%2Epdf`). No 2026 in-rem list was found for Newark, Jersey City, Paterson or Camden. In-rem filings do show up as **"LIS PENDENS IN REM"** in the Essex clerk index (§2.2) and as in-rem notices on njpublicnotices (§1.3).

---

## Part 2 — County clerk records (lis pendens, deeds, mortgages)

Search window used: **09/26/2026 – 10/26/2026.** Indexes lag about 1–2 weeks: Union was indexed through 10/19/2026, Essex through 10/14/2026.

### Summary

| County | System / vendor | URL | Lis pendens by doc type + date, free, no login? | Plain HTTP? | Property address in index? | Verdict |
|---|---|---|---|---|---|---|
| **Union** | In-house Java app "UCPA" v2.0.15 (Tomcat) | `https://clerk.ucnj.org/UCPA/DocIndex?s=date` | **Yes**: dt=7 Lis Pendens, dt=17 Foreclosure | **Yes** (form POST, no CAPTCHA, no robots.txt) | No (party names and book/page only; PDF image has the property) | **USE** |
| **Essex** | "PRESS" (ASP.NET WebForms) | `https://press.essexregister.com/ProdPRESS/clerk/ClerkHome.aspx?op=basic` | **Yes**: 23 LP Foreclosure, 25 LP Foreclosure & Recovery, 21 LP In Rem, 24 LP Recovery, 22 LP Discharge | **Yes** (VIEWSTATE postback, no CAPTCHA) | Town yes; Block/Lot column exists but is "N/A" on lis pendens | **USE** |
| **Passaic** | "PRESS" (older ASP.NET build) | `https://records.passaiccountynj.org/press/clerk/ClerkHome.aspx?op=basic` | Yes: 9 Foreclosure LP, 8 LP, 2TAX Tax Sale Certificate | Technically yes, **but robots.txt is `Disallow: /`** | Town yes; Block/Lot "N/A" | **MANUAL** (respect robots; ask the clerk) |
| **Hudson** | Harris Recording Solutions **Acclaim** | `https://acclaim.hcnj.us/AcclaimWeb/` (`/search/SearchTypeDocType`, `/search/SearchTypeRecordDate`) | Doc Type and Record Date searches exist | Behind a **click-through disclaimer** (`POST /AcclaimWeb/Search/Disclaimer`, `disclaimer=true`) | not tested | **MANUAL** until the operator accepts the terms |
| **Bergen** | New Vision Systems **BrowserView** (ClickOnce "SearchNG" for power users) | `https://bclrs.co.bergen.nj.us/landrecords/` → `http://bclrs.co.bergen.nj.us/browserview/` | Doc type + town + date range exist | **No**: AngularJS app, reCAPTCHA widgets, request parameters encrypted client-side | not tested | **MANUAL** |
| **Middlesex** | New Vision Systems BrowserView | `https://mcrecords.co.middlesex.nj.us/recordssearch/` → `https://mcrecords.co.middlesex.nj.us/publicsearch1/` | Same as Bergen | **No** (same app). The server also sends an **incomplete TLS chain**, fixed by adding the DigiCert Global G3 TLS ECC SHA384 2020 CA1 intermediate from its AIA URL. County website returns Akamai "Access Denied" 403 from our egress. | not tested | **MANUAL** |
| **Camden** | New Vision Systems BrowserView | Free: `http://camden.newvisionsystems.com/clerksearch/` → `/SearchAnywhere/`. Info: `https://camdencounty.com/service/county-clerk/online-property-records` | **No**: county says lis pendens are **Premium only ($25/month)**; free tier is deeds, mortgages, discharges, cancellations | No (same app) | — | **BLOCKED** for lis pendens (deeds are MANUAL) |

### 2.1 Union County — **USE**
```
GET  https://clerk.ucnj.org/UCPA/DocIndex?s=date           (sets JSESSIONID)
POST https://clerk.ucnj.org/UCPA/DateResults
     dt=17&from=09%2F26%2F2026&to=10%2F26%2F2026&rpp=500&pOpt=1     # dt=17 Foreclosure, dt=7 Lis Pendens; pOpt 1=Grantor/Defendant side
detail: GET https://clerk.ucnj.org/UCPA/NameDetails?dt=17&sd=09/28/2026&ip=&im=2859&is=R
image:  GET https://clerk.ucnj.org/UCPA/Viewer?id=<docid>&dt=Foreclosure&in=<inst>&bp=<book/page>
        → HTML that embeds https://clerk.ucnj.org/UCPA/rvi/<JSESSIONID>_<docid>.pdf (direct download, same session)
```
- Document types available: Deed, Mortgage, Assignment, Discharge, Release, Notice of Settlement, Federal Tax Lien, **Lis Pendens (7)**, **Foreclosure (17)**, Construction Lien, NUB, and others. Coverage: 06/01/1977 – 10/20/2026.
- **30-day result:** **39 Foreclosure** filings, **7 Lis Pendens**, and 34 Lis Pendens Discharges, recorded 09/28–10/19/2026. Sample rows:
  ```
  Foreclosure  Defendant [individual]                  09/28/2026  Complainant PENNY MAC LOAN SERVICES LLC   92/281
  Foreclosure  Defendant [estate of an individual]     09/28/2026  Complainant U S BANK TRUST COMPANY N.A.   92/285
  Foreclosure  Defendant [individual]                  09/30/2026  Complainant GB EQUITY LLC                 92/301
  Lis Pendens  Defendant 742 WESTFIELD REAL ESTATELLC  10/08/2026  Complainant FCR TL TRUST                  62/2594
  ```
  "FCR TL TRUST" is a tax-lien holder, so that row is a tax-lien foreclosure. "ESTATE OF …" defendants are probate leads.
- **Fields:** document type, party role, party name, recorded date, reverse party, book/page. The detail page adds instrument # (e.g., `2859-R`). **No address, block or lot in the index.**
- **The PDF has the property details.** Tested: instrument 2891-R, a 5-page scanned image (CCITT, no text layer). The cover sheet says "Lis Pendens Foreclosure". Page 2 shows *"SUPERIOR COURT OF NEW JERSEY CHANCERY DIVISION UNION COUNTY DOCKET NO.: F-008821-26 … NOTICE OF LIS PENDENS"* and the mortgage book/page. Getting the address and docket automatically needs OCR. **`tesseract` is not installed in this sandbox.**

### 2.2 Essex County Register — **USE**
```
GET  https://press.essexregister.com/ProdPRESS/clerk/ClerkHome.aspx?op=basic     (collect __VIEWSTATE etc.)
POST same URL with:
  ctl00$ContentPlaceHolder1$ddlMunTab2     = COUNTY WIDE   (or NEWARK, EAST ORANGE, IRVINGTON, ...)
  ctl00$ContentPlaceHolder1$ddlDocTypeTab2 = 23            (23 LP FORECLOSURE, 25 LP FORECLOSURE AND RECOVERY, 21 LP IN REM, 24 LP RECOVERY, 22 LP DISCHARGE, 1 DEED, 2 MORTGAGE)
  ctl00$ContentPlaceHolder1$txtFromTab2 = 09/26/2026 ; txtToTab2 = 10/26/2026
  ctl00$ContentPlaceHolder1$ddlShowRecTab2 = 100 ; ddlTotalRecTab2 = 750 ; btnSearchTab2 = Search ; hidDiv = basic
→ results grid id ctl00_ContentPlaceHolder1_dgdDeedMort (one row per party pair; page links "1 2 …" via __doPostBack)
```
- **Coverage:** recordings since 05/01/2001 are online (from the PRESS home page). Also offers block-and-lot and name searches by doc type.
- **30-day result (COUNTY WIDE):**
  - Type 23 (LP Foreclosure): **12 instruments** (57 party rows). Irvington 22, Newark 20, East Orange 11, Nutley 3, Orange 1 (party rows). Recorded 9/28–10/14/2026.
  - Type 25 (LP Foreclosure & Recovery): **≥30 instruments on page 1** (100 rows; more pages exist). Newark 32, Orange 19, Belleville 16, West Orange 12 (party rows).
  - Type 21 (LP In Rem): 2 instruments, both Orange. Example: `FWI IRONWORKS NJ LLC` v. `FIG 25, LLC`, 2026065022, 10/2/2026.
  - Type 24 (LP Recovery): 5 instruments.
- **Sample rows:**
  ```
  LIS PENDENS FORECLOSURE | AUGUSTINE ESTATES LLC | WILMINGTON SAVINGS FUND SOCIETY, FSB (TRUSTEE) | 2026063996 | 9/28/2026 | NEWARK | N/A
  LIS PENDENS FORECLOSURE | [individual] (ESTATE OF) | FINANCE OF AMERICA REVERSE LLC | 2026067317 | 10/14/2026 | NUTLEY | N/A
  LIS PENDENS FORECLOSURE AND RECOVERY | [individual] | WELLS FARGO BANK, N.A. | 2026063750 | 9/28/2026 | NEWARK | N/A
  ```
- **Fields:** Type, Direct party, Indirect party, Instrument #, Recorded date, **Town**, Block, Lot (N/A on lis pendens), Book, Page.
- **Terms (`/ProdPRESS/TermsOfUse.aspx`):** traffic is monitored; no explicit scraping ban was seen. No robots.txt (404). Keep the rate low: one search per doc type per day.

### 2.3 Passaic County — **MANUAL** (works, but robots.txt disallows all)
- `https://records.passaiccountynj.org/press/clerk/ClerkHome.aspx?op=basic`. Same PRESS pattern, with field prefix `_ctl0:PageContent:`. The search button's onclick sets hidden field `hidDiv=NewSearch`. Doc types include **9 FORECLOSURE LIS PENDENS, 8 LIS PENDENS, 21 DISCHARGE OF LISPENS, 2TAX TAX SALE CERTIFICATE, 31 NOTICE OF SALE**.
- **Tested once (30 days):**
  - Foreclosure LP: page 1 of 2 = 100 party rows / **28 instruments**. Clifton 35, Paterson 31, Ringwood 6, Wayne 6 (party rows). Sample: `FORECLOSURE LIS PENDENS | [individual] | MIDFIRST BANK | 2026033641 | 9/28/2026 | CLIFTON CITY`.
  - Plain LP: 2 instruments.
  - 2TAX (Tax Sale Certificate) from 01/01/2025 to 10/26/2026 returned **0**, so the type is unused or indexed differently.
- `https://records.passaiccountynj.org/robots.txt` = `User-agent: * / Disallow: /`. **Do not automate.** Run it by hand weekly, or write to the County Clerk for permission or a bulk extract. `passaiccountynj.org` itself returns Akamai 403 to our egress.

### 2.4 Hudson County Register — **MANUAL (pending operator)**
- Acclaim at `https://acclaim.hcnj.us/AcclaimWeb/`. Nav lists Name, Book/Page, Instrument #, Legal Description, **Doc Type**, Consideration, **Record Date**. Any search URL first redirects to `/AcclaimWeb/Search/Disclaimer?st=...`, a click-through form (`disclaimer=true`).
- We did **not** accept the disclaimer on the operator's behalf, so no searches were run. If the operator reads and accepts the terms, Acclaim's Doc Type + Record Date search is usually plain HTTP. That needs to be verified before building on it.
- The Register's site (`https://hudsoncountyregister.org/property-search-2/`, per search) says lis pendens are in the public search room and offers a free Recording Notification Service.

### 2.5 Bergen, Middlesex, Camden (New Vision Systems BrowserView) — **MANUAL / BLOCKED**
- All three landing pages offer a Windows ClickOnce "Advanced/SearchNG" app and a browser search (`btnLegacyApp` postback):
  - Bergen → `/browserview/`
  - Middlesex → `/publicsearch1/`
  - Camden → `/SearchAnywhere/`
- The browser search is an AngularJS app. Its tabs include **Document Type** with Town and Date Range ("7 Days / 30 Days / 90 Days"), Party Name, and Town/Lot/Block. Each tab includes a `vc-recaptcha` widget (rendered according to a runtime flag), and the app encrypts query parameters client-side (`r.Encrypt(...)`) before calling `api/search`.
- We stopped there: probing the internal API is out of bounds (CAPTCHA protection). Use it by hand in a real browser.
- **Camden:** lis pendens are Premium only ($25/month), per the county page.
- **Middlesex:** coverage is deeds since 1929, mortgages since 1950, everything else since 1958 (landing page).
- **Bergen:** coverage since 1970.

### 2.6 What a lis pendens record gives you (all counties)
- The index gives **party names, recorded date, instrument, book/page**. Essex and Passaic also give **town**. **No street address.**
- The address and the **F-docket number** are inside the scanned document image, which needs OCR, or in the court case jacket (§2.8).
- **Practical join:** defendant name + town → MOD-IV owner name match (same town) → block/lot and mailing address. "ESTATE OF …" defendants and "TL TRUST" or tax-lien-fund complainants mark probate and tax-lien foreclosures. Both are high-motivation.

### 2.7 Deeds and mortgages
- **Free and automatable:** Union (dt=1 Deed, dt=2 Mortgage) and Essex (types 1 and 2, by town). Same endpoints as above. Use them for cash-buyer detection (deed with no mortgage) and absentee-owner flips.
- **Free, by hand:** Camden (free tier), Bergen and Middlesex (BrowserView).

### 2.8 NJ Courts — eCourts Public Access Case Jacket → **BLOCKED for automation / MANUAL per docket**
- App: `https://portal.njcourts.gov/webcivilcj/CIVILCaseJacketWeb/pages/publicAccessDisclaimer.faces` (from the official guide `https://www.njcourts.gov/sites/default/files/attorneys/ecourts/training/publicaccesscasejacket.pdf`, last modified 2022-09-22).
- Foreclosure is docket type **F** under General Equity. Searches: by docket number, party name (individual or business) with "Case Initiation Date", or by judgment.
- The guide says reCAPTCHA *"must be completed by a user for every search for a case"* and *"will trigger … after every 5 search attempts"*.
- The current njcourts page (`https://www.njcourts.gov/ar/node/954811`) also says *"First-time users must register with the New Jersey Courts to search civil and foreclosure cases"*. Login: `https://portal-cloud.njcourts.gov/prweb/PRAuth/CloudSAMLAuth?AppName=ESSO`.
- **Verdict:** a person can look up specific F-dockets we already know from §2.1, §2.2 or §1.3 to get the plaintiff attorney, filing date and status. No bulk access.
- **Statewide counts only:** NJ DOBI foreclosure statistics `https://www.nj.gov/dobi/division_banking/foreclosuredstats.htm` (aggregate, not leads). No public list of pending NJ foreclosures was found.

---

## Part 3 — Code violations, vacant/abandoned registries, open data

### 3.1 NJ Construction Permit Data (statewide, includes demolitions) — **USE**
- Socrata: `https://data.nj.gov/resource/w9se-dmra.json`. Metadata: `https://data.nj.gov/api/views/w9se-dmra.json`.
- **Row count:** `$select=count(*)` → **2,678,341**.
- **Freshness:** rowsUpdatedAt **2026-08-13**. Description: *"Data received as of 08/07/2026 … updated monthly … purged after 60 months"*. `max(processdate)` = 2026-08-07. Beware garbage dates (`max(permitdate)` = 2925-08-15).
- **Fields:** `comu, treasurycode, muniname, munitype, county, recordid, block, lot, permitno, status, permitstatusdesc, permitdate, certdate, permittype, permittypedesc (Alteration/New/Demolition/Addition), certtype, certtypedesc, buildfee…totalfee, cubic, squarefeet, constcost, salegained, rentgained, usegroup, usegroupdesc, censusnumber, censusdesc, public, storage, manufactured, hudseal, source, sourcedesc, version, processdate, pk`.
- **Demolitions with permitdate ≥ 2026-01-01:** Bergen 595, Essex 423, Union 360, Middlesex 323, Passaic 206, Hudson 105, Camden 103.
- **Tested query:**
  ```
  GET https://data.nj.gov/resource/w9se-dmra.json?$select=muniname,block,lot,permitno,permitdate,permittypedesc,usegroupdesc,constcost
      &$where=county='ESSEX' AND permittypedesc='Demolition' AND permitdate>='2026-01-01' AND permitdate<'2026-12-31'&$order=permitdate DESC&$limit=4
  → NEWARK 3689/41 26CP001623 2026-07-31 Demolition "Accessory buildings…" $2000
    NEWARK 4208/12.01 26CP001622 2026-07-31 …; SOUTH ORANGE VILLAGE TWP 411/7 20260655 2026-07-30 …
  ```
  (An unbounded `$where` with `$order` gave HTTP 500. Always bound the date range.)
- **Use:** block/lot keyed signals:
  - demolition (vacant lot or teardown);
  - no alteration permits in 5 years on old housing stock (deferred maintenance; derived);
  - a recent "New" permit next to a target (gentrification comps).

### 3.2 Paterson — Abandoned Properties registry (official city ArcGIS) — **FALLBACK (USE as a static seed)**
- Item `0d3094a3db5d4a11a356cb87696c1544` (owner PatcityNJ, public). Service: `https://services7.arcgis.com/24MTNeTGsw9FVZm6/arcgis/rest/services/City_of_Paterson__Abandoned_Properties_by_year_WFL1/FeatureServer/0`.
- **Count:** `/0/query?where=1%3D1&returnCountOnly=true&f=json` → **522**.
- **Fields:** `Location_Address, Application_Number, Registration_Type, Status_Type, Sub_Type_1, Sub_Type_2, Is_Active, Application_Date, Abandoned_Year, Ward, Last_Inspection_Date, Date_as_Foreclosure, Date_as_Abandoned, Date_Removed_From_List, Registry_Total_Due, Registry_Total_Paid, Registry_Total_Balance, Block, Lot, Qualifier, Agent_Name, Full_Address, Latitude, Longitude`.
- **Breakdown:**
  - Abandoned_Year: 2017 31, 2018 95, 2019 90, 2020 57, 2021 15, 2022 97, **2025 137** (2025 rows have null application data).
  - Is_Active TRUE 385. Sub_Type_1 "Foreclosed Property" 382.
  - `max(Application_Date)` = 2022-12-28. The item was last modified 2025-07-07. It is a snapshot, not a live feed.
- **Sample:** `23 GOULD AVE | FIN00008 | FIN | Active | Foreclosed Property | 2017-06-07 | Ward 6 | Block 6805 Lot 17 | Agent RALPH BARRONE | 40.8927,-74.1611`.
- **Companion:** "City of Paterson – Abandoned Properties" Web Experience `https://experience.arcgis.com/experience/aa51721f9d814d4895683a2be89db3c6`.
- **City-owned property 2026** (item `0c71514d81fb4c889b32d696c9ac9885`): `https://services7.arcgis.com/24MTNeTGsw9FVZm6/arcgis/rest/services/City_Of_Paterson_City_Owned_Property_2026_WFL1/FeatureServer`. Layer 1 "Property Information" has **240** rows (full MOD-IV columns: Owner, Location, Block_1/Lot_1, PropertyCl, LandValue, …). Layer 2 has **242** rows (`Block, Lot, BL_KEY, Qual, Class, Location`). FALLBACK: these are city-owned, not seller leads. Use them to exclude parcels, or as targets for city land sales.
- **Paterson Hub:** `https://city-of-paterson-patersonnj.hub.arcgis.com`.

### 3.3 Jersey City — Opendatasoft `https://data.jerseycitynj.gov/` → **SKIP (no relevant data)**
- Catalog API `GET /api/explore/v2.1/catalog/datasets?where="vacant"` → 27 hits; `"violation"` → 7 hits. All are Planning, Zoning or HPC application *document* datasets (`records_count` 0) plus "Parking Violations 2017" (276 rows). `"demolition"` → 61 HPC/Planning document datasets.
- No code-enforcement, vacant-registry or tax dataset found. The host also reset our connections repeatedly ("ws_closed_mid_exchange").

### 3.4 Newark — `https://data.newarknj.gov/` → **BLOCKED/MANUAL**
- It is a Tableau Server portal (`vizportal`) behind Incapsula. Dashboards only, no data API.
- Newark's GIS org on ArcGIS Online (owner `dikidjievai_newgin`, 211 items) has `newark_parcels_2023`, zoning and redevelopment layers. **No vacant/abandoned registry or code-violation layer was found.**
- "Vacant Lots Newark" (`https://services1.arcgis.com/ze0XBzU1FXj94DJq/arcgis/rest/services/Vacant_Lots_Newark_WFL1`) is a Rutgers student layer. It is unofficial; do not rely on it.

### 3.5 Others
- No Socrata portals exist for Paterson, Elizabeth, Camden, Edison or Woodbridge (Socrata discovery API search).
- ArcGIS Online keyword searches for Elizabeth, Plainfield, Irvington, East Orange and Camden City vacant/abandoned/code layers found only student or consultant layers. One exception: "City of New Brunswick 3D Vacant Lots" dashboard (owner NBWater_Manager, 2024-09-24), which has no FeatureServer URL in search results.
- **Statewide:** data.nj.gov has no "vacant" or "abandoned" datasets. Searching "code enforcement" there only returns DCA/Sandy grant program data.

---

## Build next (ranked: free and realistically automatable)

1. **RealAuction NJ tax-sale scraper** (`agents/scrapers/nj_realauction_taxsale.py`). Covers 91 target-county towns, including Newark, Paterson, Elizabeth, Camden, Clifton, East Orange, Irvington, Linden, New Brunswick, Perth Amboy, Plainfield and Union City.
   - Discover: `POST https://manalapan.newjerseytaxsale.com/jumpMenu/flyoutmenu.cfm` with `state=NJ&auctionsToList=all`. Filter to the 91 target-county hosts (list in §1.1).
   - Per host:
     - `POST https://<Host>/index.cfm?folder=previewItems` with `pageNum=N&orderBy=AdvNum&orderDir=asc`
     - `POST https://<Host>/index.cfm?folder=auctionResults&mode=preview` with the same body
     - `GET https://<Host>/index.cfm?folder=showDocument&documentName=biddingRules` for the sale date
   - Address and owner: `POST https://<Host>/popup/popup-certificates.cfm` with `certID=<id>&page=certpop-property-details.cfm`. Only for Active rows above an amount threshold, or for all rows slowly.
   - Schedule: weekly; daily within 30 days of a sale. Rows: `tax_sale_items(muni, block, lot, qual, tax_year, charge_type, face_amount, status, owner, address, sale_date, sold_to, winning_bid, as_of)`.
   - Legal basis for public access: N.J.A.C. 5:33-1.1(d)(1).
2. **Union County clerk, lis pendens and foreclosure, daily** (`nj_union_clerk.py`). `POST https://clerk.ucnj.org/UCPA/DateResults` with `dt=17` (Foreclosure) and `dt=7` (Lis Pendens), `from/to` = trailing 14 days, `rpp=500`, `pOpt=1`. Parse party pairs and book/page. Optionally fetch `Viewer` → `rvi/*.pdf` and OCR page 2–3 for the docket and property address (needs `tesseract`). The same endpoint with `dt=1/2` gives deeds and mortgages for cash-buyer detection.
3. **Essex Register (PRESS), lis pendens, daily** (`nj_essex_press.py`). ASP.NET postback to `https://press.essexregister.com/ProdPRESS/clerk/ClerkHome.aspx?op=basic` with `ddlMunTab2=COUNTY WIDE`, `ddlDocTypeTab2 ∈ {23,25,21}`, `txtFromTab2/txtToTab2` = trailing 14 days, `ddlShowRecTab2=100`, `ddlTotalRecTab2=750`. Follow pager postbacks. Gives town (Newark, Irvington, East Orange, …) per filing.
4. **NJ construction permits, monthly** (`nj_permits_socrata.py`). `GET https://data.nj.gov/resource/w9se-dmra.json` with `$where=county in('ESSEX','UNION','HUDSON','PASSAIC','BERGEN','MIDDLESEX','CAMDEN') AND processdate>'<last_run>'`, paged with `$limit/$offset`. Flag demolitions and long permit gaps by block/lot.
5. **PDF list parsers** (`nj_taxsale_pdf.py`), FALLBACK. One parser for the Edmunds "Tax Sale Listing By Block/Lot/Qual" layout (Teaneck) and one for the Excel layout (Bayonne `BLOCK LOT QUALIFIER OWNER NAME PROPERTY LOCATION TAX INTEREST SALE-COST TOTAL`). Plus the Camden municipal-held-lien assignment notices (`camdennj.gov/wp-content/uploads/<yyyy>/<mm>/*LFV*.pdf` and `*Full-Value*.pdf`). The URLs are entered by hand (from the operator's December newspaper or njpublicnotices check); the parsing is automated.
6. **Paterson abandoned registry, one-shot plus monthly change check.** `GET https://services7.arcgis.com/24MTNeTGsw9FVZm6/arcgis/rest/services/City_of_Paterson__Abandoned_Properties_by_year_WFL1/FeatureServer/0/query?where=Is_Active%3D'TRUE'%20OR%20Abandoned_Year%3D2025&outFields=*&returnGeometry=false&f=json`. 522 rows with address and block/lot.

**Manual playbook (no automation; operator in a browser):**
- **njpublicnotices.com:** saved searches or Smart Search alerts for "tax sale" and "in rem" in Hudson, Bergen and Middlesex. This is how to get the Jersey City, Bayonne, Hackensack, Woodbridge and Edison lists each Nov–Dec.
- **Clerk systems:**
  - Passaic PRESS: weekly, by hand (robots.txt disallows bots).
  - Hudson Acclaim: after the operator accepts the disclaimer, re-test and possibly automate.
  - Bergen and Middlesex BrowserView: Document Type + 30 Days.
  - Camden lis pendens: needs the $25/month Premium tier. Skip unless volume justifies it.
- **NJ Courts Case Jacket:** look up specific F-dockets only.
- **Edmunds WIPP / city tax portals:** check one parcel's current balance before an offer.

**Main blockers:**
- Jersey City, Bayonne, Edison, Hackensack and Woodbridge do not use RealAuction and post no machine-readable list.
- Newark's RealAuction site is offline. The next Newark sale's list will appear there when it reopens, so poll it weekly.
- Clerk indexes have no property address; it is only in scanned PDFs, so OCR is needed.
- Four of the 7 clerks use JS, CAPTCHA, terms-gated or robots-disallowed systems.
- NJ Courts requires CAPTCHA and registration.
- The njpublicnotices terms forbid scraping.
- No city in scope publishes code violations as open data. Paterson's abandoned registry is the only one, and it is a 2025 snapshot.
