# NJ owner, assessment and deed-transfer (SR1A) sources: live recon

**Date of tests:** 2026-10-26. Every result below comes from a request made that day with `curl` or Python `requests`, from the sandbox behind the agent proxy. I paced requests at about one per second per host. I did not attempt to get past any CAPTCHA or JavaScript challenge.

**Client module:** `docs/recon/nj_owner_records_client.py`, which needs only `requests` and `bs4`. Its demo output is pasted in section 6.

---

## 1. Bottom line

| Need | Six of seven target counties (Bergen, Camden, Hudson, Middlesex, Passaic, Union) | Essex (Newark) |
|---|---|---|
| (a) Owner name and mailing address by block/lot or street | **USE taxrecords-nj.com.** Owner names are present and current, data as of 10/21 to 10/23/26 | Mailing address only, from NJGIN or MOD-IV. **Owner name is redacted in every free automated source.** About 54% of Newark parcels get a name from a 2020 snapshot, and only when the deed is unchanged since then |
| (b) Bulk pull of a whole municipality | **USE.** A single POST with `out_type=3` returns a ZIP containing a CSV of every parcel. Jersey City came back as 64,836 rows in about 9 s, a 7.6 MB ZIP | **FALLBACK.** The NJ Treasury statewide `modiv-2026.zip` gives 46,675 Newark rows with names redacted. HTTP Range reads fetch only the Essex member |
| (c) Recent deeds with grantor, grantee, price and date | **USE.** The Deed/Sr1a export takes a date range. Data as of: Passaic 10/14/26, Union 09/24/26, Hudson 09/11/26, Camden 08/31/26, Bergen 08/26/26. **Middlesex is the exception:** its SR1A database is stale ("as of 09/24/21"), so the client builds Middlesex deeds from the owner list's last sale plus the statewide SR1A file | **FALLBACK.** NJ Treasury `Sales2026.zip` has price, dates, block/lot and NU code, but **grantor and grantee are blank** (0 of 170,664 rows have a name). It runs through recordings of June 2026, about a 4-month lag |

**The fix for the tax1.co.monmouth.nj.us problem:** the `ms_user=monm&passwd=data` account only returns Monmouth County (13xx) data. Every other county's tables on that host return 0 records. Newark showed "Data as of 05/31/22", Woodbridge 03/04/24 and Jersey City 02/27/24, all with 0 rows. The working credentials live on a different host, which the county boards themselves link to:

```
POST https://taxrecords-nj.com/pub/cgi/inf.cgi
ms_user=ctb<CC>   passwd=   (empty)    # CC = 2-digit county code: ctb02 Bergen, ctb04 Camden,
                                       # ctb09 Hudson, ctb12 Middlesex, ctb16 Passaic, ctb20 Union
srch_type=1|2  select_cc=<district>  district=<district>  adv=1|2  out_type=1|2|3  ms_ln=10..1000
p_loc=  owner=  block=  lot=  qual=    (+ advanced fields, see section 3.1)
```

Union's board page links to `https://taxrecords-nj.com/pub/cgi/prc6.cgi?district=2001&ms_user=ctb20`. Bergen's links to `https://taxrecords-nj.com/pub/cgi/prc6.cgi?district=0200&ms_user=ctb02`. I found the other `ctbXX` values by loading `prc6.cgi?district=<d>&ms_user=ctbXX` for each target county; every page returned that county's district list. **Essex (`ctb07`) has no data at all.** It returns `0 Records Found for District: 0714  Data as of //` for both databases.

**Compliance flags, which need a human decision:**
1. Both `taxrecords-nj.com/robots.txt` and `tax1.co.monmouth.nj.us/robots.txt` say `User-agent: *` / `Disallow: /`. The search form and Excel export are public and the county boards link to them, but automated use goes against robots.txt. The client keeps the request count low: a whole town is two requests (POST, then ZIP), cached for an hour, with at least 1.1 s between requests per host. A cleaner long-term route is to ask each county board for a periodic file.
2. Every page carries a **Daniel's Law** notice: "this record may contain information governed by L. 2015, c. 226 and L. 2020, c. 125, which include civil and criminal penalties for improper disclosure". A wholesaling CRM that stores and mails to these addresses should keep a Daniel's Law suppression list and honor nondisclosure requests. Have counsel confirm this.

---

## 2. District / county code table

The codes are standard NJ county+municipality codes. NJGIN's `PCL_MUN`, the MOD-IV and SR1A files, and taxrecords-nj.com all use the same values.

| Municipality | County (code) | District code | taxrecords `ms_user` | Owner list "Data as of" | Deed/Sr1a db "Data as of" | Rows in the full owner export |
|---|---|---|---|---|---|---|
| Newark | Essex (07) | **0714** | ctb07: **no data** (`Data as of //`) | n/a; MOD-IV 2026 statewide file instead | n/a; statewide SR1A instead | 46,675 (MOD-IV) |
| Jersey City | Hudson (09) | **0906** | ctb09 | 10/23/26 | 09/11/26 | 64,836 |
| Woodbridge | Middlesex (12) | **1225** | ctb12 | 10/23/26 | **09/24/21 (stale)** | 29,601 |
| Edison | Middlesex (12) | **1205** | ctb12 | 10/23/26 | **09/24/21 (stale)** | not pulled |
| New Brunswick | Middlesex (12) | **1213** (1214 is North Brunswick) | ctb12 | 10/23/26 | **09/24/21 (stale)** | 8,027 |
| Elizabeth | Union (20) | **2004** | ctb20 | 10/23/26 | 09/24/26 | not pulled (834 deeds in the 2025-10-01..2026-10-26 window) |
| Paterson | Passaic (16) | **1608** | ctb16 | 10/23/26 | 10/14/26 | not pulled |
| Hackensack (Bergen sample) | Bergen (02) | **0223** | ctb02 | 10/21/26 | 08/26/26 | not pulled |
| Camden City | Camden (04) | **0408** | ctb04 | 10/21/26 | 08/31/26 | not pulled |

All district codes per county, copied from the `<select name=district>` options on each `prc6.cgi` page:

- **Essex 07:** 0701 Belleville, 0702 Bloomfield, 0703 Caldwell, 0704 Cedar Grove, 0705 East Orange, 0706 Essex Fells, 0707 Fairfield, 0708 Glen Ridge, 0709 Irvington, 0710 Livingston, 0711 Maplewood, 0712 Millburn, 0713 Montclair, 0714 Newark, 0715 North Caldwell, 0716 Nutley, 0717 Orange, 0718 Roseland, 0719 South Orange, 0720 Verona, 0721 West Caldwell, 0722 West Orange (0700 = ALL)
- **Hudson 09:** 0901 Bayonne, 0902 East Newark, 0903 Guttenberg, 0904 Harrison, 0905 Hoboken, 0906 Jersey City, 0907 Kearny, 0908 North Bergen, 0909 Secaucus, 0910 Union City, 0911 Weehawken, 0912 West New York (0900 = ALL)
- **Middlesex 12:** 1201 Carteret, 1202 Cranbury, 1203 Dunellen, 1204 East Brunswick, 1205 Edison, 1206 Helmetta, 1207 Highland Park, 1208 Jamesburg, 1209 Metuchen, 1210 Middlesex, 1211 Milltown, 1212 Monroe, 1213 New Brunswick, 1214 North Brunswick, 1215 Old Bridge, 1216 Perth Amboy, 1217 Piscataway, 1218 Plainsboro, 1219 Sayreville, 1220 South Amboy, 1221 South Brunswick, 1222 South Plainfield, 1223 South River, 1224 Spotswood, 1225 Woodbridge (1200 = ALL)
- **Passaic 16:** 1601 Bloomingdale, 1602 Clifton, 1603 Haledon, 1604 Hawthorne, 1605 Little Falls, 1606 North Haledon, 1607 Passaic, 1608 Paterson, 1609 Pompton Lakes, 1610 Prospect Park, 1611 Ringwood, 1612 Totowa, 1613 Wanaque, 1614 Wayne, 1615 West Milford, 1616 Woodland Park (1600 = ALL)
- **Union 20:** 2001 Berkeley Heights, 2002 Clark, 2003 Cranford, 2004 Elizabeth, 2005 Fanwood, 2006 Garwood, 2007 Hillside, 2008 Kenilworth, 2009 Linden, 2010 Mountainside, 2011 New Providence, 2012 Plainfield, 2013 Rahway, 2014 Roselle, 2015 Roselle Park, 2016 Scotch Plains, 2017 Springfield, 2018 Summit, 2019 Union, 2020 Westfield, 2021 Winfield (2000 = ALL)
- **Camden 04:** 0401 Audubon … 0408 Camden City, 0409 Cherry Hill, 0414 Gloucester City, 0415 Gloucester Twp, 0422 Lindenwold, 0427 Pennsauken, 0434 Voorhees, 0436 Winslow, and the rest (0401–0437; 0400 = ALL)
- **Bergen 02:** 0201 Allendale … 0223 Hackensack, 0221 Garfield, 0217 Fair Lawn, 0219 Fort Lee, 0231 Lodi, 0246 Paramus, 0260 Teaneck, and the rest (0201–0270; 0200 = ALL)

---

## 3. Sources tested

### 3.1 taxrecords-nj.com "NJ Assessment Records Search": **USE**

- **Search page (GET):** `https://taxrecords-nj.com/pub/cgi/prc6.cgi?district=<d>&ms_user=ctb<CC>`. The form posts to `inf.cgi` with hidden `ms_user=ctb<CC>` and `passwd=` (empty).
- **Search (POST):** `https://taxrecords-nj.com/pub/cgi/inf.cgi`, using these form fields from the page:
  - `srch_type`: `1` = Current Owners/Assmt List, `2` = Deed/Sr1a List
  - `select_cc`, `district`: 4-digit district (sending the district in both fields works)
  - `adv`: `1` simple, `2` advanced
  - `out_type`: `1` single-line HTML, `2` multi-line HTML, `3` **Excel (ZIP containing a CSV)**
  - `ms_ln`: page size, one of 10/20/50/100/500/1000 (HTML only; the Excel output always contains every match)
  - Simple criteria: `p_loc` (location; matched as a contains-style string, so `1158 AMBOY` and `AMBOY` both work), `owner` (prefix match: `DREAM HOMES` found `DREAM HOMES 1076 LLC`), `block`, `lot`, `qual`. `block` and `lot` are exact: `lot=4` returned 0 rows where `4.01` exists.
  - Advanced criteria (`adv=2`, from the form): `street, city, class` (1, 2, 3A, 3B, 4A, 4B, 4C, 5A, 5B, 6A–6C, 15A–15F), `sr1a_f` (0/30/60/90/185/366 days), **`sale_from`, `sale_to` (yyyy-mm-dd; these filter on deed date)**, `cl_type` (class-4 use codes), `zone, book, page, built_f/built_t, sqft_f/sqft_t, land_f/land_t, impr_f/impr_t, net_f/net_t, sale_f/sale_t`
- **Excel flow:** the POST returns HTML containing `N Records Found for Download`, `Data as of MM/DD/YY` and `<a href=../download/<district><user><hhmmss>.zip>`. A GET on `https://taxrecords-nj.com/pub/download/<file>.zip` returns `application/zip` holding one latin-1 CSV. When there are 0 matches, the page still prints a ZIP link; the client skips it.
- **Detail page (GET permalink):** `https://taxrecords-nj.com/pub/cgi/m4.cgi?district=<d>&l02=<d><Join column with spaces replaced by _>`. Example: `m4.cgi?district=1213&l02=121300010____00004__01_____M` shows owner, mailing address, sq ft, year built, last sale and four years of tax-list history.
- **Auth / cost:** none. **Pagination / limits:** no cap observed on Excel output. New Brunswick returned 8,027 rows in 2.4 s (951 KB ZIP), Woodbridge 29,601, Jersey City 64,836 in 9.2 s (7.6 MB ZIP). HTML pages have a "Next" button that posts `pageno`.
- **Terms:** robots.txt is `Disallow: /`. There is a Daniel's Law statement and an "as is" disclaimer. No other ToS page exists; the homepage is only a list of county links.
- **Camden quirk:** the HTML list output for Camden (`ctb04`) shows an empty Owner column, but the CSV export has names. 46 of 46 rows in block 10 had a name, for example `COSBY, DONNA M`.

**Test 1: owner list, Union / Elizabeth, block 1.** `curl -X POST .../inf.cgi --data "ms_user=ctb20&passwd=&srch_type=1&select_cc=2004&district=2004&adv=1&out_type=1&ms_ln=50&p_loc=&owner=&block=1&lot=&qual="` returned `32 Records Found for District: 2004 ELIZABETH Data as of 10/23/26`. Rows: `1 | 1 | 2 | 1312-1322 MAGIE AVE | DURAND, ALEXIS - DURAND, MARIA`, `1 | 2 | 2 | 1308-1310 MAGIE AVE | TAE, JOHN W - TAE, RICHARD W`, `1 | 3 | 2 | 1304-1306 MAGIE AVE | LABAUT-ARAUJO, G - LABAUT, YUNIA`.

**Test 2: other target counties.** Each was a single POST:

| POST | Result |
|---|---|
| `ctb12`, `district=1213`, `block=10` | `8 Records ... 1213 NEW BRUNSWICK Data as of 10/23/26`, for example `10 | 4.01 | 4A | 369 GEORGE ST | GSNB, LLC` |
| `ctb12`, `district=1225`, `block=10` | `3 Records ... WOODBRIDGE Data as of 10/23/26` (cemeteries) |
| `ctb12`, `district=1205`, `p_loc=AMBOY` | `169 Records ... EDISON Data as of 10/23/26`, for example `692.01 | 1.01 | 4A | 1158 AMBOY AVE | HOGAN HOLDINGS CORP` and `... | 1096 AMBOY AVE | DREAM HOMES 1096 LLC` |
| `ctb09`, `district=0906`, `p_loc=MONTGOMERY` | `426 Records ... JERSEY CITY Data as of 10/23/26`, for example `11605 | 1 | 4A | 30 MONTGOMERY STREET | 30 MONTGOMERY OWNER,LLC % LAMAR CO.` |
| `ctb16`, `district=1608`, `p_loc=MARKET` | `334 Records ... PATERSON Data as of 10/23/26`, for example `4002 | 9 | 2 | 579 MARKET ST | SORIANO, AUSTRIA` |
| `ctb02`, `district=0223`, `block=10` | `1 Records ... HACKENSACK Data as of 10/21/26`, `10 | 1.01 | 4A | 500 SO RIVER ST | SUP II RIVERFRONT PLAZA LLC` |
| `ctb04`, `district=0408`, `block=10` | `46 Records ... CAMDEN CITY Data as of 10/21/26` |
| `ctb07`, `district=0714`, `block=10` / `p_loc=BROAD` | **`0 Records Found for District: 0714  Data as of //`** |

**Test 3: whole-town Excel export.** `--data "ms_user=ctb12&passwd=&srch_type=1&select_cc=1213&district=1213&adv=1&out_type=3&ms_ln=50&p_loc=&owner=&block=&lot=&qual="` returned `8027 Records Found for Download | Data as of 10/23/26` with link `../download/1213ctb12151442.zip`. The GET returned 200 `application/zip` (950,687 bytes) holding `1213ctb12151442.csv` (8,027 rows × 92 columns; 1,869 owners contain "LLC"). Header and the first two raw rows of the block-10 export (`1213ctb12151420.csv`):

```
"Municipality","Block","Lot","Qual","Property Location","Property Class","Owner's Name","Owner's Mailing Address","City/State/Zip","Sq. Ft.","Yr. Built","Building Class","Prior Block","Prior Lot","Prior Qual","Updated","Zone","Account","Mortgage Account","Bank Code","Sp Tax Cd","Sp Tax Cd","Sp Tax Cd","Sp Tax Cd","Map Page","Additional Lots","Land Desc","Building Desc","Class 4 Code","Acreage","EPL Own","EPL Use","EPL Desc","EPL Statute","EPL Init","EPL Further","EPL Facility Name","Taxes 1","Taxes 2","Taxes 3","Taxes 4","Sale Date","Deed Book","Deed Page","Sale Price","NU Code","Ratio","Type/Use","Year","Owner","Street","City/State/Zip","Land Assmnt","Building Assmnt","Exempt","Total Assmnt","Assessed","Year","Owner","Street","City/State/Zip","Land Assmnt","Building Assmnt","Exempt","Total Assmnt","Assessed","Year","Owner","Street","City/State/Zip","Land Assmnt","Building Assmnt","Exempt","Total Assmnt","Assessed","Year","Owner","Street","City/State/Zip","Land Assmnt","Building Assmnt","Exempt","Total Assmnt","Assessed","Latitude","Longitude","Neigh","VCS","StyDesc","Style","Join",
"1213","10","1.01","","170 NEILSON ST","15D","CHRIST CHURCH OF NEW BRUNSWICK","5 PATERSON ST","NEW BRUNSWICK, NJ  08901","0","1900","10","10","12","","2026-08-18","C-4","","","0","","","","","4900","","240X203","CHURCH BLDGS","0","1.1185","23","10","40","54:04-03.06","102870","110124","CHURCH CAMPUS","0","0","0","0","2010-11-05","6208","59","1","26","434500000","","2025","CHRIST CHURCH OF NEW BRUNSWICK","5 PATERSON ST","NEW BRUNSWICK, NJ  08901","2939400","1405600","0","4345000","4345000","2024","CHRIST CHURCH OF NEW BRUNSWICK","5 PATERSON ST","NEW BRUNSWICK, NJ  08901","2939400","1405600","0","4345000","4345000","2023","CHRIST CHURCH OF NEW BRUNSWICK","5 PATERSON ST","NEW BRUNSWICK, NJ  08901","2939400","1405600","0","4345000","4345000","2022","CHRIST CHURCH OF NEW BRUNSWICK","5 PATERSON ST","NEW BRUNSWICK, NJ  08901","2939400","1405600","0","4345000","4345000","0","0","","","","","00010    00001  01     M",
"1213","10","4.01","","369 GEORGE ST","4A","GSNB, LLC","373 LYONS RD","BASKING RIDGE, NJ  07920","11428","1900","49","10","4.1","","2026-03-06","C-4","00001400","","0","","","","","4900","","079X148","1S B / RETAIL","737","0.2684","0","0","0","","000000","000000","FIVE-TENANT RETAIL","87354.6","0","0","0","2025-12-22","19922","9","2900000","0","109.655172413793","","2025","KATZ, D & SONS, INC.","PO BOX 4777","HIGHLAND PARK, NJ  08904","2009900","1170100","0","3180000","3180000","2024","KATZ, D & SONS, INC.","PO BOX 4777","HIGHLAND PARK, NJ  08904","2009900","1170100","0","3180000","3180000","2023","KATZ, D & SONS, INC.","PO BOX 7356","MONROE TWP. NJ  08831","2009900","2754500","0","4764400","4764400","2022","KATZ, D & SONS, INC.","PO BOX 7356","MONROE TWP. NJ  08831","2009900","2754500","0","4764400","4764400","0","0","","","","","00010    00004  01     M",
```

A Jersey City row from the full `0906` export (`0906ctb09152901.csv`, 64,836 rows, 0 blank owner names, 0 blank mailing addresses):

```
"0906","702","3","","145 GRACE ST.","2","FRIAS, ALEXIS","145 GRACE ST., #2","JERSEY CITY, NJ  07307","3675","2005","18.4","892","45","","2026-10-15","","00134700","","660","","","","","7","","25X100","2S-BT-B+F-2U-G","0","0.0574","0","0","0","","000000","000000","","15957.39","18157.94","0","0","2026-09-16","10013","564","1","25","68340000","42","2026","FRIAS, ALEXIS & RAFAEL","145 GRACE ST., #2","JERSEY CITY, NJ  07307","190000","493400","0","683400","683400","2025",...
```

Notes on the CSV:
- The four "Year/Owner/Street/…/Total Assmnt" groups are the tax-list history, most recent first. The first group's year is 2025 for Middlesex and 2026 for Hudson and Camden. In both cases it holds the latest certified assessment. Cross-check: MOD-IV 2026 for 1213-10-4.01 shows land 2009900, improvement 1170100, net 3180000, which equals the "2025" group. The client maps that first group to `assessed_*` and adds `assessment_year`.
- Because the history groups carry the prior owner name, they let you infer the **seller** of the last sale. In row 2 above the current owner is `GSNB, LLC` with a sale on 2025-12-22 for $2.9M, and the 2025 tax-list owner is `KATZ, D & SONS, INC.`
- Sale data is fresh: Jersey City has sale dates through 2026-10 (12 rows), and Woodbridge's newest sale is 2026-10-09. Some sale dates are typos in the future, such as 2028-07-28 in New Brunswick.

**Test 4: deed (SR1A) export, Elizabeth.** Advanced search with a deed-date range:

```
curl -X POST https://taxrecords-nj.com/pub/cgi/inf.cgi --data "ms_user=ctb20&passwd=&srch_type=2&select_cc=2004&district=2004&adv=2&out_type=3&ms_ln=50&p_loc=&owner=&block=&lot=&qual=&street=&city=&class=+&sr1a_f=0&sale_from=2025-10-01&sale_to=2026-10-26&cl_type=+&zone=&book=&page=&built_f=0&built_t=0&sqft_f=0&sqft_t=0&land_f=0&land_t=0&impr_f=0&impr_t=0&net_f=0&net_t=0&sale_f=0&sale_t=0"
```

This returned `834 Records Found for Download | Data as of 09/24/26`, then the ZIP `2004ctb20151553.zip` (69,637 bytes, 834 rows × 30 columns). Deed dates ran 2025-10-01..2026-09-17 and recorded dates 2025-10-07..2026-09-24. 290 of the 834 grantees are LLC/INC/CORP. Header and two raw rows:

```
"Municipality","Grantor's Name (Seller)","Grantor's Mailing Address","Grantor's City/State/Zip","Grantee's Name (Buyer)","Grantee's Mailing Address","Grantee's City/State/Zip","Property Location","Block","Lot","Qual","Property Class","Land Assmnt","Building Assmnt","Total Assmnt","Recorded Date","Deed Date","Book","Page","Sale Price","NU Code","Sq. Ft.","Cl. 4 Use","SR1A #","Yr. Built","StyDesc","Style","Latitude","Longitude",
"2004","GREAT GATSBY REALTY LLC","1165 CHESTNUT ST","ELIZABETH, N J  07201","1165 CHESTNUT LLC","39 ELM ST","ELIZABETH, NJ  07208","1165 CHESTNUT ST","276","1","","2","138200","295300","433500","2026-07-20","2026-06-25","6625","2878","0","25","2080","","1111111","1890","","","0","0","","",
"2004","MARINO, KENNETH & KATHRYN","1134-1138 KIPLING RD","ELIZABETH, N J  07208","BONET, LUIS & LILIBETH CASTRO","1134-1138 KIPLING RD","ELIZABETH, N J  07208","1134-1138 KIPLING RD","1","9","","2","283500","323700","607200","2026-01-05","2025-12-23","6606","2400","860000","32","2200","","7584577","1952","COLONIAL","CL","0","0","","",
```

**Deed database freshness per county**, from simple `srch_type=2` POSTs:

| County | Result |
|---|---|
| Union / Elizabeth | 2,333 records for block 1, `Data as of 09/24/26` |
| Hudson / Jersey City | 110,925 records in the district, `09/11/26` |
| Passaic / Paterson | `10/14/26` |
| Bergen / Hackensack | `08/26/26` |
| Camden / Camden City | `08/31/26` (names present in the CSV) |
| **Middlesex (1213, 1225, 1205)** | **`Data as of 09/24/21`**. An advanced search with `sale_from=2025-10-01` returned 0 rows |

### 3.2 tax1.co.monmouth.nj.us (same software, Monmouth only): **FALLBACK, Monmouth only**

- `GET https://tax1.co.monmouth.nj.us/cgi-bin/prc6.cgi?menu=index&ms_user=monm&passwd=data&district=1301&mode=11`. The county dropdown offers only `<option value=1301>MONMOUTH`. The district dropdown follows whatever `district` you pass, which is why it lists Essex towns when you pass 0702.
- `POST .../inf.cgi` with `ms_user=monm&passwd=data&srch_type=1&select_cc=1301&district=1301&adv=1&out_type=1&ms_ln=50&block=1` returned `89 Records Found for District: 1301 ABERDEEN Data as of 10/25/26`. Example rows: `1 | 2 | 1 | 39-41 ATLANTIC AVE | MATAWAN PROPERTY GROUP, LLC` and `1 | 3 | 2 | 43 ATLANTIC AVE | DIAZ, ERICA PITT`. A Monmouth deed Excel export (`srch_type=2&adv=2&out_type=3&sale_from=2026-06-01`) returned `148 Records ... Data as of 10/07/26`.
- Other counties on this host with `monm`, or with `ms_user=ctb12` / `ctb07`, all returned 0 rows:

| District | Result |
|---|---|
| Newark 0714 | `Data as of 05/31/22` (deeds 06/30/20) |
| Woodbridge 1225 | `03/04/24` |
| Jersey City 0906 | `02/27/24` |

  **That is why the earlier Newark POSTs failed.**
- robots.txt: `Disallow: /`.

### 3.3 NJGIN Parcels_Composite_NJ_WM (ArcGIS): **FALLBACK for Essex** (already known)

- `GET https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/Parcels_Composite_NJ_WM/FeatureServer/0/query?where=PCL_MUN='0714' AND PCLBLOCK='991'&outFields=...&returnGeometry=false&f=json`
- Layer metadata: `maxRecordCount=2000`, pagination supported, `lastEditDate=1787045558134` (2026-08-18).
- Sample Newark rows: `991 | 26 | 384-386 CHESTNUT ST | OWNER_NAME='' | 382 386 CHESTNUT ST, NEWARK, NJ 07105 | DEED 12468/06025 131112 | 560000` and `991 | 22 | 394 CHESTNUT ST | OWNER_NAME='' | 103 OLIVER ST, NEWARK, NJ 07105 | 20240/29691 240521 | $1`.
- Fields: PCL_MUN, PCLBLOCK, PCLLOT, PCLQCODE, PROP_CLASS, PROP_LOC, OWNER_NAME (blank), ST_ADDRESS, CITY_STATE, ZIP_CODE, LAND_VAL, IMPRVT_VAL, NET_VALUE, BLDG_DESC, YR_CONSTR, DEED_BOOK, DEED_PAGE, DEED_DATE (YYMMDD), SALE_PRICE, SALES_CODE, and more.
- The client uses it for Essex `lookup_owner` and `search_owner_by_location` (`UPPER(PROP_LOC) LIKE '%...%'` works).

### 3.4 NJ Treasury statewide MOD-IV files: **FALLBACK (bulk, names redacted)**

- Listed on `https://www.nj.gov/treasury/taxation/lpt/statdata.shtml` (page "Last Updated: Thursday, 10/01/26"): `../pdf/lpt/modiv-2021.zip` … `modiv-2026.zip` plus `modivlayout.pdf`.
- `HEAD https://www.nj.gov/treasury/taxation/pdf/lpt/modiv-2026.zip` returned 200, `content-length: 268325509`, `last-modified: Mon, 03 Aug 2026`. **It supports `Range`** (`206`, `content-range: bytes 0-99/268325509`).
- The ZIP holds 21 members, one per county, for example `Essex 26 RE.txt` (133,445,988 bytes uncompressed, 15,968,444 compressed, dated 2026-05-21) and `Camden 26 RE.txt` (dated 2026-08-03). Each is fixed-width with 700-char records, sorted by district.
- **Owner name is redacted.** The layout labels bytes 176–210 `FILLER`, and all 190,094 Essex rows are blank there. Mailing address is present at 211–235 (street), 236–260 (city/state) and 261–269 (zip). Also present: class, location, building and land description, deed book/page, deed date (MMDDYY, 307–312), sale price (313–321), NU code (331–332), year built (416–419), and land, improvement and net value (421–447).
- Newark has 46,675 rows. 2,589 have deed dates on or after 2025-01-01. A raw row: `071400001    00024  01           20000509192961        15C8-12 CENTRE PL REAR ... 920 MAYOR GIBSON         NEWARK, NJ               07102 ...`
- The client reads only the county member over HTTP Range. Newark, which ends about 75% of the way into the Essex member, took about 30 s with 4 MB blocks.

### 3.5 NJ Treasury statewide SR1A sales files: **FALLBACK (deeds without names)**

- On the same statdata page: `statdata/Sales2020.zip` … `Sales2026.zip`, `statdata/YTDSR1A2026.zip`, and `../pdf/lpt/SR1Afilelayout.pdf`, which describes 663-char fixed-width records.

| File | Size | Last-modified | Contents |
|---|---|---|---|
| `Sales2026.zip` | 11,043,975 B | 2026-10-01 | 170,664 rows; recordings 2025-07..2026-06; deed dates mostly 2025-04..2026-06 |
| `Sales2025.zip` | 11,104,616 B | 2025-10-01 | 171,949 rows; recordings 2024-07..2025-06 |
| `YTDSR1A2026.zip` | 10,984,774 B | 2026-08-12 | 169,935 rows; an earlier cut of the 2026 file |

  `Sales2027.zip` returned 404.
- **Grantor and grantee name, street and city fields are blank in every row** (0 of 170,664 and 0 of 169,935 non-blank).
- Present: county, district, reported and verified price, U/N flag, SR NU code, deed book and page, deed date (YYMMDD), recorded date, block and lot (with suffix), qualifier, assessments, property class, year built and living space.
- Newark: 3,029 rows in Sales2026, 2,167 with deed date on or after 2025-09-01. Raw line: `0714000000141400JFF062926 000000 N15 000400000000400000000031500000109900000141400035350000000.00E7426712 … 1167-1169 BERGEN ST      260626202603926826061026061103691 …`, which decodes to 1167-1169 Bergen St, block 3691, NU 15, $400,000, deed 2026-06-10, recorded 2026-06-11, book 20260 page 39268.
- **Use:** flip and resale detection by block/lot and price anywhere in NJ, including Newark, with a lag of about 4 months. Names have to come from another source.

### 3.6 "Newark Parcels with Ownership" ArcGIS snapshot: **FALLBACK (name enrichment only, stale)**

- Found through `https://www.arcgis.com/sharing/rest/search?q=(Newark OR Essex) parcels owner type:"Feature Service"`. Layer: `https://services1.arcgis.com/WAUuvHqqP3le2PMh/arcgis/rest/services/Newark_Parcels_with_Ownership/FeatureServer/0`, owner `dikidjievai_newgin`.
- `lastEditDate` is 2021-11-23. A statistics query gives `max(MOD4_LASTUPDATE)` = 2020-07-31, `max(TENREBYR)` = 2019 and 48,672 rows. Fields include OWNERSNAME, OWNERADD1, OWNERADD2, DEEDBOOK, DEEDPAGE, SALESDATE and SALESPRICE.
- Sample: `991 | 6.42 | C42 | 404-406 CHESTNUT ST | AVILA, DIANA B. | 404 CHESTNUT ST NEWARK, NJ | 06133/811 11/30/2004 $250,000`.
- **How the client uses it:** it fills `owner_name` only when the current deed book and page (from NJGIN or MOD-IV) equal the snapshot's, meaning no recorded transfer since about 2020. On block 991, 20 of 30 parcels matched. Across the full Newark bulk pull, **25,085 of 46,675 parcels (54%) got a name**, including 17,472 of 31,000 class-2 residential parcels. `owner_name_source` records where each name came from. Recently traded parcels, which are exactly the flips, stay unnamed.

### 3.7 TaxDataHub (Essex's and Camden's "Record Search"): **BLOCKED (reCAPTCHA)**

- `https://www.essexcountynjtaxboard.org/info-forms/` links "Record Search" to `https://www.taxdatahub.com/6229fbf0ce4aef911f9de7bc/Essex%20County`. Camden's board page links to `https://www.taxdatahub.com/60d088c3d3501df3b0e45ddb/camden-county`; I did not fetch that page.
- The page (200, IIS/ASP.NET) loads `https://www.google.com/recaptcha/api.js?render=6Le-rx8gAAAAAPVm6T9P2acvLFeInkggLGmxLblZ`. Its `search-services.js` posts `{searchTerm, gRecaptchaToken}` to `search/<siteId>/SubmitSearchTerm` and `search/<siteId>/ExportExcel`. The page config includes `data.Towns` with 0714 Newark City, `DownloadLimit = 75000`, and filters for SaleDate, SalePrice and OwnerName.
- Every search and export requires a Google reCAPTCHA v3 token, so I did not automate it. **A person can export Newark to Excel from this site by hand in a browser** (up to 75,000 rows). That is the only free Newark owner-name source I found that might be current. Its Download policy repeats the Daniel's Law notice and points to Essex County OPRA for hard copies.

### 3.8 oprs.co.monmouth.nj.us (Open Public Records Search): **BLOCKED / unreachable**

- `curl https://oprs.co.monmouth.nj.us/`, `/oprs/` and `/Oprs/index.aspx`: TLS completes (DigiCert `*.co.monmouth.nj.us`), then the server returns **"Empty reply from server" (curl 52)**. Python `requests` got `RemoteDisconnected`. I did not try other User-Agents, because that would count as getting around a bot check.

### 3.9 njactb.org: **record search retired**

- `https://njactb.org/record-search/` says: *"Due to the enactment of Daniel's Law, effective January 1, 2023, the NJACTB website no longer provides access to the Tax List (MODIV) and Property Sales (SR1A) data. Questions can be directed to the 21 individual counties…"*
- `https://njactb.org/county-tax-boards/` lists the 21 board pages and administrators. Results for the target counties:

| County | Board page | Result |
|---|---|---|
| Bergen | `https://www.co.bergen.nj.us/...` | curl 60: certificate has expired |
| Bergen | `https://bergencountynj.gov/boards-commissions/about-board-of-taxation/` | 200; links to `taxrecords-nj.com/pub/cgi/prc6.cgi?district=0200&ms_user=ctb02` |
| Camden | `https://www.camdencounty.com/service/board-of-taxation/` | 200; links to TaxDataHub Camden |
| Essex | `https://www.essexcountynjtaxboard.org/` | 200; Record Search goes to TaxDataHub (3.7) |
| Hudson | `https://www.hcnj.us/finance/tax-assessments/board-of-taxation/` | 307 to a **Sucuri CloudProxy JavaScript challenge** ("Javascript is required…"): BLOCKED. Not needed, because ctb09 works |
| Middlesex | `https://www.middlesexcountynj.gov/.../board-of-taxation` | **403 Akamai "Access Denied"** |
| Passaic | `https://www.passaiccountynj.org/residents/board-of-taxation` | **403 Akamai "Access Denied"** |
| Union | `https://ucnj.org/taxation-board/` (to `/board-of-taxation/`) | HTTP 406 but full HTML; links to `taxrecords-nj.com/pub/cgi/prc6.cgi?district=2001&ms_user=ctb20` |

- `https://taxrecords-nj.com/` itself is a "Links" page pointing to each county board's search, for example "Middlesex 'click here' (...formerly available through NJACTB...)".

### 3.10 njpropertyrecords.com: **BLOCKED (Cloudflare)**

- `GET https://www.njpropertyrecords.com/` returned **403** with `cf-mitigated: challenge`, `server: cloudflare` and a "Just a moment..." challenge page.

### 3.11 Other things tried

- **Essex Register of Deeds (grantor/grantee index)**, linked from `https://essexregisternj.gov/`:

| URL | Result |
|---|---|
| `https://press.essexregisternj.gov/prodpress/index.aspx` | proxy **CONNECT rejected (502)** |
| `https://press.essexregister.com/prodpress/index.aspx` | HTTP/2 stream CANCEL, then "Empty reply from server" over HTTP/1.1 |

  The Register site also links to `http://www1.njcountyrecording.com/NJCR/Essex.aspx` and `https://www.landex.com/recordalert/essex`; I did not test either. The Register is the most promising free source for Newark grantee names. Recheck it from a normal network.
- **web.archive.org:** the availability API worked (`archive.org/wayback/available` returned a 2022-06-27 snapshot of njactb record-search), but the snapshot fetch and the CDX query reset mid-transfer at the proxy (`ws_closed_mid_exchange`). I did not use them.

---

## 4. Field availability summary

| Field | taxrecords-nj owner CSV | taxrecords-nj SR1A CSV | NJGIN | MOD-IV statewide | SR1A statewide |
|---|---|---|---|---|---|
| Owner name | yes | grantee and grantor, with addresses | blank | blank | blank |
| Owner mailing address | yes | yes | yes | yes | blank |
| Block / lot / qual / class / location | yes | yes | yes | yes | yes |
| Assessed land / improvement / total | yes (latest certified year, plus 3 prior years) | yes | yes | yes | yes |
| Year built / sq ft / building description | yes / yes / yes | yes / yes / style | yes / no / yes | yes / no / yes | yes / living space / no |
| Last sale date / price / book / page / NU | yes | deed date, recorded date, price, book, page, NU, SR1A # | yes (through 2024) | yes (through about 2025-12) | yes (recorded through 2026-06) |
| Prior owners | 4 tax-list years | not applicable | no | no | no |
| Lat / long | columns present (0 in samples) | columns present (0 in samples) | geometry | no | no |
| Freshness | 10/21–10/25/26 | 08/26–10/14/26 (Middlesex 2021) | about TY2025 | TY2026 (May 2026) | June 2026 recordings |

---

## 5. Client module: `nj_owner_records_client.py`

| Function | Six counties (02, 04, 09, 12, 16, 20) plus Monmouth 13 | Essex 07 (Newark 0714) |
|---|---|---|
| `lookup_owner(district, block, lot, qual="")` | taxrecords Excel export filtered to exact block/lot/qual (2 requests) | NJGIN query (1 request); Newark gets a 2020 snapshot name when the deed matches |
| `search_owner_by_location(district, location)` | taxrecords `p_loc` (contains-style match) | NJGIN `UPPER(PROP_LOC) LIKE '%loc%'` |
| `fetch_municipality_owners(district, max_rows=None)` | taxrecords whole-town Excel export (2 requests), cached for 1 h | MOD-IV statewide through HTTP Range, plus the Newark snapshot enrichment |
| `fetch_recent_deeds(district, since, max_rows=None)` | taxrecords Deed/Sr1a advanced export with `sale_from`/`sale_to`. **Middlesex:** owner-list last sales (grantee = current owner, grantor inferred from the prior tax-list owner, `grantor_inferred=True`) merged with unnamed statewide SR1A transfers | statewide SR1A (names blank) |
| `search_owner_by_name(district, prefix)` (extra) | taxrecords `owner` prefix match | `NotImplementedError` (names redacted) |
| `fetch_statewide_sr1a(district, since)` (extra) | statewide SR1A for any district | same |

- **Output keys** are exactly the ones requested. Some extras are added: `nu_code`, `assessment_year`, `assessment_history`, `data_as_of`, `owner_name_source` and `source` on owners; `grantor_address`, `prop_class`, `sr1a_number`, `usable`, `grantor_inferred` and `source` on deeds. `source_url` is the `m4.cgi` detail permalink, the exact NJGIN query URL, the downloaded ZIP URL, or the NJ Treasury file URL.
- **Politeness:** at least 1.1 s between requests per host (`NJ_RECORDS_MIN_INTERVAL`), a descriptive User-Agent (`NJ_RECORDS_UA`), and a 1-hour in-process cache of whole-town exports.
- **`NotImplementedError` cases:** Essex `search_owner_by_name`, and counties not in `_CGI_SOURCES`. The other counties are probably `ctb<CC>` too, but I only verified 02, 04, 09, 12, 16 and 20 on taxrecords-nj.com, plus Monmouth's `monm`/`data`.

### 5.1 Additional live runs of the client

Run with `python3 t_more.py`, `t_more2.py` and `t_flip.py`, which import the module:

```
Newark bulk: 46675 parcels, owner_name filled from 2020 snapshot (deed unchanged): 25085 (54%); class-2 named 17472/31000; max sale date 2026-01-24; 36s
0906 Jersey City (Hudson) location 'MONTGOMERY' 426 rows | first: 11604 2 MONTGOMERY ST. | CITY OF JERSEY CITY | 280 GROVE STREET JERSEY CITY, NJ 07302 | as of 2026-10-23
1608 Paterson (Passaic) location 'MARKET' 334 rows | first: 4002 9 579 MARKET ST | SORIANO, AUSTRIA | 579 MARKET ST PATERSON NJ 07513 | as of 2026-10-23
0223 Hackensack (Bergen) location 'MAIN' 374 rows | first: 36.03 1 232 SO MAIN ST | MT.ZION BAPTIST CHURCH OF LODI TNSP | 25 E WESLEY ST SOUTH HACKENSACK NJ 07606 | as of 2026-10-21
0408 Camden City (Camden) location 'ERIE' 183 rows | first: 1 1 75 ERIE ST | 75 ERIE ST, LLC | 1339 N FRONT STREET PHILADELPHIA, PA 19122 | as of 2026-10-21
1205 Edison (Middlesex) location '1158 AMBOY' 1 rows | first: 692.01 1.01 1158 AMBOY AVE | HOGAN HOLDINGS CORP | 297 CENTRAL AVE METUCHEN, NJ 08840 | as of 2026-10-23
1213 New Brunswick (Middlesex) location 'GEORGE ST' 111 rows | first: 8 4.01 335 GEORGE ST | RUTGERS, THE STATE UNIVERSITY | 33 KNIGHTSBRIDGE RD, FL3E PISCATAWAY, NJ 08854 | as of 2026-10-23

0906 Jersey City (Hudson) deeds since 2026-06-28 315 rows; corp grantees 87 | max deed 2026-09-09 max recorded 2026-09-11 | as of 2026-09-11
    e.g. 804 29 525 CENTRAL AVE. | WANG, YING -> 525 CENTRAL LLC | 61 MAPLE STREET, BOX 112, SUMMIT, NJ  07901 | 412621 2026-08-31 2026-09-04 NU 4 10009 516
0223 Hackensack (Bergen) deeds since 2026-06-28 43 rows; corp grantees 8 | max deed 2026-08-17 max recorded 2026-08-26 | as of 2026-08-26
    e.g. 423 15 202 BERRY ST | AAIA RMK LLC -> 202 BERRY STREET LLC | 202 BERRY ST, HACKENSACK, NJ  07601 | 450000 2026-07-30 2026-08-26 NU None 5883 1462
0408 Camden City (Camden) deeds since 2026-06-28 172 rows; corp grantees 76 | max deed 2026-08-25 max recorded 2026-08-31 | as of 2026-08-31
    e.g. 282 86 1453 SO 4TH ST | THE CITY OF CAMDEN -> HELPING OTHERS WITH MISSION CORP | 1451 S 4TH STREET, CAMDEN, NJ  08104 | 6000 2026-08-25 2026-08-31 NU 15 13176 749
1608 Paterson (Passaic) deeds since 2026-06-28 447 rows; corp grantees 219 | max deed 2026-10-13 max recorded 2026-10-14 | as of 2026-10-14
    e.g. 2102 5 250-254 6TH AVE | ROJAS PASCUAL -> 250 SIXTH AVE LLC | 250 254 6TH AVE, PATERSON NJ  07524 | 600000 2026-10-08 2026-10-14 NU None 5184 183
2004 Elizabeth (Union) deeds since 2026-06-28 109 rows; corp grantees 40 | max deed 2026-09-17 max recorded 2026-09-24 | as of 2026-09-24
    e.g. 430 9 440-442 ERICO AVE | BRITO-GOMEZ, AMAURY & DIAZ, MERCEDE -> SAINT MINA REALTY LLC | 440-442 ERICO AVE, ELIZABETH, N J  07202 | 1155000 2026-08-18 2026-08-28 NU None 6630 2703

Paterson deeds since 2024-10-26 3158 min deed 2024-10-26 max 2026-10-13
buy->resell within 365d with higher price: 59
  ('8202', '16', '') 31-35 E 36TH ST: 2026-02-25 THOMPSON DOROTHY B -> 15 LESLIE STREET LLC $450,000  ||  2026-10-01 15 LESLIE STREET LLC -> LEGACY COOP LLC $595,000
  ('2713', '6', '') 311-313 E 23RD ST: 2026-05-18 ADAMO THOMAS SHERIFF -> 2026 E23 LLC $360,000  ||  2026-09-30 2026 E23 LLC -> VELARDE LUIS M D ET ALS $670,000
  ('1213', '17', '') 230-232 SHERWOOD AVE: 2026-06-15 OTTILIO CATHERINE -> 5TH GEN HOLDINGS LLC $350,000  ||  2026-09-09 5TH GEN HOLDINGS LLC -> AHMED NASIR $460,000
```

A short deed window can come back empty when it starts after the county's "Data as of" date. For example, Jersey City, Hackensack and Camden returned 0 rows for `since=2026-09-11`. Use windows of 60 days or more.

---

## 6. Live demo output (`python3 docs/recon/nj_owner_records_client.py`, 2026-10-26, 33 s)

```
=== Woodbridge search_owner_by_location('AMBOY AVE'): 127 row(s) ===
  district_code='1225' | block='252' | lot='1.02' | qual='' | prop_class='4A' | property_location='135 AMBOY AVE' | owner_name='ACCESS WOODBRIDGE LLC' | owner_street='208 GATES RD' | owner_city_state='LITTLE FERRY NJ' | owner_zip='07643' | assessed_total=3025400 | year_built=None | sq_ft=None | last_sale_date='2019-10-08' | last_sale_price=1 | deed_book='17650' | deed_page='1080' | owner_name_source='county tax board MOD-IV (taxrecords-nj.com)' | source_url='https://taxrecords-nj.com/pub/cgi/m4.cgi?district=1225&l02=122500252____00001__02_____M'
  district_code='1225' | block='252' | lot='1.05' | qual='' | prop_class='2' | property_location='129 AMBOY AVE' | owner_name='129 AMBOY AVENUE LLC' | owner_street='129 AMBOY AVE' | owner_city_state='WOODBRIDGE NJ' | owner_zip='07095' | assessed_total=79100 | year_built=1925 | sq_ft=2471 | last_sale_date='2016-12-28' | last_sale_price=10 | deed_book='6925' | deed_page='582' | owner_name_source='county tax board MOD-IV (taxrecords-nj.com)' | source_url='https://taxrecords-nj.com/pub/cgi/m4.cgi?district=1225&l02=122500252____00001__05_____M'
  district_code='1225' | block='252' | lot='1.06' | qual='' | prop_class='4A' | property_location='145 AMBOY AVE' | owner_name='145 AMBOY AVENUE LLC' | owner_street='50 JONES ST' | owner_city_state='NEW HYDE PARK NY' | owner_zip='11040' | assessed_total=192600 | year_built=None | sq_ft=None | last_sale_date='2008-10-29' | last_sale_price=10 | deed_book='6001' | deed_page='643' | owner_name_source='county tax board MOD-IV (taxrecords-nj.com)' | source_url='https://taxrecords-nj.com/pub/cgi/m4.cgi?district=1225&l02=122500252____00001__06_____M'

=== Woodbridge lookup_owner(252, 1.05, ''): 1 row(s) ===
  district_code='1225' | block='252' | lot='1.05' | qual='' | prop_class='2' | property_location='129 AMBOY AVE' | owner_name='129 AMBOY AVENUE LLC' | owner_street='129 AMBOY AVE' | owner_city_state='WOODBRIDGE NJ' | owner_zip='07095' | assessed_total=79100 | year_built=1925 | sq_ft=2471 | last_sale_date='2016-12-28' | last_sale_price=10 | deed_book='6925' | deed_page='582' | owner_name_source='county tax board MOD-IV (taxrecords-nj.com)' | source_url='https://taxrecords-nj.com/pub/cgi/m4.cgi?district=1225&l02=122500252____00001__05_____M'

Woodbridge fetch_municipality_owners: 29601 parcels in one export; data_as_of=2026-10-23; corporate owners=2142

=== Woodbridge fetch_municipality_owners (first rows): 29601 row(s) ===
  district_code='1225' | block='1' | lot='1' | qual='' | prop_class='15E' | property_location='WEST POND RD' | owner_name='HOLY TRINITY CEMETERY' | owner_street='104 CENTER ST' | owner_city_state='PERTH AMBOY NJ' | owner_zip='08861' | assessed_total=72000 | year_built=None | sq_ft=None | last_sale_date=None | last_sale_price=0 | deed_book=None | deed_page=None | owner_name_source='county tax board MOD-IV (taxrecords-nj.com)' | source_url='https://taxrecords-nj.com/pub/cgi/m4.cgi?district=1225&l02=122500001____00001_________M'
  district_code='1225' | block='1' | lot='2.01' | qual='' | prop_class='4A' | property_location='119 WEST POND RD' | owner_name='JARAN PROP/SPEEDWAY LLC/E STENSENG' | owner_street='539 SOUTH MAIN ST' | owner_city_state='FINDLAY OH' | owner_zip='45840' | assessed_total=487200 | year_built=None | sq_ft=None | last_sale_date='2006-12-26' | last_sale_price=2300000 | deed_book='5772' | deed_page='78' | owner_name_source='county tax board MOD-IV (taxrecords-nj.com)' | source_url='https://taxrecords-nj.com/pub/cgi/m4.cgi?district=1225&l02=122500001____00002__01_____M'
  district_code='1225' | block='1' | lot='2.02' | qual='' | prop_class='1' | property_location='119 WEST POND RD' | owner_name='JARAN PROP LLC C/O JJ ELEK' | owner_street='117 MAIN ST' | owner_city_state='WOODBRIDGE NJ' | owner_zip='07095' | assessed_total=72800 | year_built=None | sq_ft=None | last_sale_date='2006-12-26' | last_sale_price=400000 | deed_book='5772' | deed_page='85' | owner_name_source='county tax board MOD-IV (taxrecords-nj.com)' | source_url='https://taxrecords-nj.com/pub/cgi/m4.cgi?district=1225&l02=122500001____00002__02_____M'

=== Woodbridge fetch_recent_deeds(since=2025-10-26): 1210 row(s) ===
  block='17.09' | lot='222' | qual='' | property_location='29 FAIRFIELD AVE' | grantor=None | grantee='AHMED, AZIZ' | grantee_address='29 FAIRFIELD AVE, FORDS, NJ 08863' | sale_price=1 | deed_date='2026-10-09' | recorded_date=None | nu_code='1' | deed_book='20154' | deed_page='485' | source='taxrecords-nj.com owner list last sale (grantor = prior tax-list owner)'
  block='263.01' | lot='16.01' | qual='' | property_location='240 BUNNS LN' | grantor='KUSNIR, MILAN & KROCKO,M&KROCKOVA,L' | grantee='KROCKO, MICHAL & KROCKOVA, LUDMILA' | grantee_address='240 BUNNS LN, WOODBRIDGE, NJ 07095' | sale_price=1 | deed_date='2026-10-09' | recorded_date=None | nu_code='1' | deed_book='20154' | deed_page='175' | source='taxrecords-nj.com owner list last sale (grantor = prior tax-list owner)'
  block='403.07' | lot='8' | qual='' | property_location='72 EDWARD ST' | grantor='CAPUTO, MICHAEL' | grantee='CAPUTO, MICHAEL&KISS-CAPUTO,MELISSA' | grantee_address='72 EDWARD ST, ISELIN, NJ 08830' | sale_price=1 | deed_date='2026-10-09' | recorded_date=None | nu_code='1' | deed_book='20153' | deed_page='1112' | source='taxrecords-nj.com owner list last sale (grantor = prior tax-list owner)'
  block='252' | lot='100' | qual='C0119' | property_location='119 CRICKET LN' | grantor='DAMANIA, VISHAL H & SANCHITA' | grantee='CASTILLO-INOA, LUXI Y ANGELES' | grantee_address='119 CRICKET LN, WOODBRIDGE, NJ 07095' | sale_price=270000 | deed_date='2026-10-08' | recorded_date=None | nu_code='0' | deed_book='20153' | deed_page='1654' | source='taxrecords-nj.com owner list last sale (grantor = prior tax-list owner)'
  by source: {'NJ Treasury statewide SR1A Sales2026.zip (names redacted)': 47, 'taxrecords-nj.com owner list last sale (grantor = prior tax-list owner)': 1163}

=== Elizabeth fetch_recent_deeds(since=2025-10-26, max_rows=4): 4 row(s) ===
  block='355' | lot='41' | qual='' | property_location='1019-1021 GROVE ST' | grantor='SOUSA, FERNANDO & TERESA COSTA' | grantee='SOUSA, FERNANDO & TERESA CONCEICAO' | grantee_address='1019-1021 GROVE ST, ELIZABETH, N J  07202' | sale_price=1 | deed_date='2026-09-17' | recorded_date='2026-09-23' | nu_code='25' | deed_book='6633' | deed_page='2338' | source='taxrecords-nj.com Deed/Sr1a list (Excel export)'
  block='263' | lot='16' | qual='' | property_location='1009 1/2 FLORA ST' | grantor='MAYNOR, EVELYN(ADM)' | grantee='GRANT,RICKY,LINDA & BRENT-MAYNOR,E' | grantee_address='914 UNION ST, LINDEN, N J  07036' | sale_price=1 | deed_date='2026-09-11' | recorded_date='2026-09-17' | nu_code='10' | deed_book='6632' | deed_page='198' | source='taxrecords-nj.com Deed/Sr1a list (Excel export)'
  block='104' | lot='8' | qual='' | property_location='614-618 UNION AVE' | grantor='JABER, WASEELAH A' | grantee='JABER, ATA' | grantee_address='612 UNION AVE, ELIZABETH, N J  07208' | sale_price=1 | deed_date='2026-09-10' | recorded_date='2026-09-24' | nu_code='14' | deed_book='6633' | deed_page='289' | source='taxrecords-nj.com Deed/Sr1a list (Excel export)'
  block='170' | lot='37' | qual='' | property_location='117 SAYRE ST' | grantor='GUERRERO, ROBERTH O' | grantee='SCOOBY DOO PAPA LLC' | grantee_address='117 SAYRE ST, ELIZABETH, N J  07208' | sale_price=10 | deed_date='2026-09-08' | recorded_date='2026-09-14' | nu_code='14' | deed_book='6632' | deed_page='2072' | source='taxrecords-nj.com Deed/Sr1a list (Excel export)'

=== Newark lookup_owner('991', '26'): 1 row(s) ===
  district_code='0714' | block='991' | lot='26' | qual='' | prop_class='4A' | property_location='384-386 CHESTNUT ST' | owner_name='MARTINS, RENATO & ANA' | owner_street='382 386 CHESTNUT ST' | owner_city_state='NEWARK, NJ' | owner_zip='07105' | assessed_total=467300 | year_built=None | sq_ft=None | last_sale_date='2013-11-12' | last_sale_price=560000 | deed_book='12468' | deed_page='06025' | owner_name_source="Newark 2020 parcel snapshot (ArcGIS 'Newark Parcels with Ownership'); deed book/page unchanged since then" | source_url='https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/Parcels_Composite_NJ_WM/FeatureServer/0/query?where=PCL_MUN%3D%270714%27+AND+PCLBLOCK%3D%27991%27+AND+PCLLOT%3D%2726%27&outFields=%2A&returnGeometry=false&f=json'

=== Newark search_owner_by_location('CHESTNUT ST'): 193 row(s) ===
  district_code='0714' | block='935' | lot='48' | qual='' | prop_class='4A' | property_location='158 CHESTNUT ST' | owner_name=None | owner_street='158 CHESTNUT ST' | owner_city_state='NEWARK, NJ' | owner_zip='07105' | assessed_total=262600 | year_built=None | sq_ft=None | last_sale_date='2022-01-19' | last_sale_price=1 | deed_book='20220' | deed_page='11980' | owner_name_source=None | source_url='https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/Parcels_Composite_NJ_WM/FeatureServer/0/query?where=PCL_MUN%3D%270714%27+AND+UPPER%28PROP_LOC%29+LIKE+%27%25CHESTNUT+ST%25%27&outFields=%2A&returnGeometry=false&f=json'
  district_code='0714' | block='919' | lot='43.29' | qual='' | prop_class='2' | property_location='116 CHESTNUT ST' | owner_name='COSTA, GERALDO M. & LINA V. COSTA' | owner_street='116 CHESTNUT ST' | owner_city_state='NEWARK, NJ' | owner_zip='07105-1103' | assessed_total=327400 | year_built=1999 | sq_ft=None | last_sale_date='2001-04-30' | last_sale_price=355000 | deed_book='05765' | deed_page='00635' | owner_name_source="Newark 2020 parcel snapshot (ArcGIS 'Newark Parcels with Ownership'); deed book/page unchanged since then" | source_url='https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/Parcels_Composite_NJ_WM/FeatureServer/0/query?where=PCL_MUN%3D%270714%27+AND+UPPER%28PROP_LOC%29+LIKE+%27%25CHESTNUT+ST%25%27&outFields=%2A&returnGeometry=false&f=json'
  district_code='0714' | block='919' | lot='43.28' | qual='' | prop_class='2' | property_location='118 CHESTNUT ST' | owner_name=None | owner_street='67 ELMORA AVE' | owner_city_state='ELIZABETH, NJ' | owner_zip='07202' | assessed_total=324000 | year_built=1999 | sq_ft=None | last_sale_date='2024-09-23' | last_sale_price=533100 | deed_book='20240' | deed_page='74320' | owner_name_source=None | source_url='https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/Parcels_Composite_NJ_WM/FeatureServer/0/query?where=PCL_MUN%3D%270714%27+AND+UPPER%28PROP_LOC%29+LIKE+%27%25CHESTNUT+ST%25%27&outFields=%2A&returnGeometry=false&f=json'

=== Newark fetch_municipality_owners(max_rows=5) [MOD-IV via HTTP Range]: 5 row(s) ===
  district_code='0714' | block='1' | lot='24' | qual='' | prop_class='15C' | property_location='8-12 CENTRE PL' | owner_name=None | owner_street='500 MAYOR GIBSON' | owner_city_state='NEWARK, NJ' | owner_zip='07102' | assessed_total=326900 | year_built=None | sq_ft=None | last_sale_date=None | last_sale_price=0 | deed_book=None | deed_page=None | owner_name_source=None | source_url='https://www.nj.gov/treasury/taxation/pdf/lpt/modiv-2026.zip#Essex 26 RE.txt'
  district_code='0714' | block='1' | lot='24.01' | qual='' | prop_class='15C' | property_location='8-12 CENTRE PL REAR' | owner_name='CITY OF NEWARK' | owner_street='920 MAYOR GIBSON' | owner_city_state='NEWARK, NJ' | owner_zip='07102' | assessed_total=757100 | year_built=None | sq_ft=None | last_sale_date='2003-01-02' | last_sale_price=0 | deed_book='05939' | deed_page='00870' | owner_name_source="Newark 2020 parcel snapshot (ArcGIS 'Newark Parcels with Ownership'); deed book/page unchanged since then" | source_url='https://www.nj.gov/treasury/taxation/pdf/lpt/modiv-2026.zip#Essex 26 RE.txt'
  district_code='0714' | block='1' | lot='30.01' | qual='' | prop_class='15C' | property_location='10-18 PASSAIC PL REAR' | owner_name='CITY OF NEWARK' | owner_street='920 MAYOR GIBSON' | owner_city_state='NEWARK, NJ' | owner_zip='07102' | assessed_total=261000 | year_built=None | sq_ft=None | last_sale_date='2003-01-02' | last_sale_price=1 | deed_book='05939' | deed_page='00870' | owner_name_source="Newark 2020 parcel snapshot (ArcGIS 'Newark Parcels with Ownership'); deed book/page unchanged since then" | source_url='https://www.nj.gov/treasury/taxation/pdf/lpt/modiv-2026.zip#Essex 26 RE.txt'

=== Newark fetch_recent_deeds(since=2025-10-26) [statewide SR1A, names redacted]: 1741 row(s) ===
  block='1954' | lot='4' | qual='' | property_location='292-294 2ND AVE W' | grantor=None | grantee=None | grantee_address=None | sale_price=10 | deed_date='2026-06-26' | recorded_date='2026-06-29' | nu_code='01' | deed_book='20260' | deed_page='42484' | source='NJ Treasury statewide SR1A Sales2026.zip (names redacted)'
  block='229' | lot='1' | qual='C0613' | property_location='65 SOUTH ORANGE AVE' | grantor=None | grantee=None | grantee_address=None | sale_price=1 | deed_date='2026-06-26' | recorded_date='2026-06-30' | nu_code='01' | deed_book='20260' | deed_page='42966' | source='NJ Treasury statewide SR1A Sales2026.zip (names redacted)'
  block='756' | lot='1' | qual='' | property_location='282-284 GRAFTON AVE' | grantor=None | grantee=None | grantee_address=None | sale_price=470000 | deed_date='2026-06-26' | recorded_date='2026-06-30' | nu_code=None | deed_book='20260' | deed_page='43131' | source='NJ Treasury statewide SR1A Sales2026.zip (names redacted)'

Newark search_owner_by_name -> NotImplementedError: Essex owner names are redacted in every free source tested

Done in 33s
```

---

## 7. Recommendations

1. **Primary pipeline (six counties):** once a week, call `fetch_municipality_owners(d)` and `fetch_recent_deeds(d, today-730)` for each target district. That is about four requests per town. Store the results and run flip and LLC analytics locally. Do not crawl per parcel.
2. **Middlesex deeds:** use the owner-list-derived deeds, which carry names and are current to 10/23/26, together with the statewide SR1A, which has all transfers without names. Separately, ask the Middlesex board (732-745-3350, ryan.riccio@co.middlesex.nj.us per NJACTB) why the ctb12 SR1A database stops at 09/24/21.
3. **Newark:**
   - Owner names: automate MOD-IV/NJGIN mailing addresses plus the 2020 snapshot match.
   - Current names: get them from a person exporting TaxDataHub Excel by hand (reCAPTCHA, 75k limit), from an OPRA request to the Essex County Board of Taxation for the MOD-IV list with names, or from the Essex Register index once it is reachable from a normal network.
   - Flips: the statewide SR1A files (block/lot, price, dates) work today.
4. **Compliance:** decide on the robots.txt position, and build a Daniel's Law nondisclosure suppression list before mailing.
