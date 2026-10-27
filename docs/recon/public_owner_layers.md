# Public GIS layers with owner names, and NJ entity lookup (live recon)

**Date of tests:** 2026-10-27. Every count, date and sample row below comes from a request made that day
from the sandbox (Python `requests`/`curl` through the agent proxy). User-Agent:
`NJWholesaleResearch/0.1 (public-records research; contact-via-repo)`. No browser User-Agent, no logins,
no tokens, no CAPTCHA handling, no click-through terms. Requests were paced at >= 1.1 s per host.

**Robots/terms checked:** `www.arcgis.com/robots.txt` is `User-agent: *` with no `Disallow` lines. The
`services*.arcgis.com` hosts have no robots file (`/robots.txt` returns 403 "Invalid URL").
`www.njportal.com` has no robots file (a 302 to a 404 page). The pages carry `<meta name="robots" content="index, nofollow">`.
OpenCorporates and data.jerseycitynj.gov are covered in their own sections below.

**How layers were found:** I ran the ArcGIS Online search API
(`https://www.arcgis.com/sharing/rest/search?q=...&f=json`) with 59 queries covering county names, town names
and owner/MOD-IV keywords. That gave about 1,150 unique items. Searching the item lists of the Newark, Jersey City, Camden County,
Middlesex County, Hudson County and Paterson accounts added more. I filtered the results to about 300 parcel-like
NJ items and fetched `?f=json` on each layer to list its fields. Every layer with an owner-like field was then checked
for: total count (`returnCountOnly`), how many rows have the owner field populated (`<owner> IS NOT NULL AND <owner> <> ''`),
a `groupByFieldsForStatistics` count per municipality code, `max` of every date field, and 2–3 sample rows.
Sample rows were requested with an explicit field list that excluded any column named like SSN, phone or e-mail.

**Join key:** NJGIN `PAMS_PIN` is `{PCL_MUN}_{PCLBLOCK}_{PCLLOT}[_{PCLQCODE}]`, for example `0906_101_1.01_HM`.
I queried 12 sample PINs from the layers below against the NJGIN layer
(`services2.arcgis.com/XVOqAjTOJ5P6ngMu/.../Parcels_Composite_NJ_WM/FeatureServer/0`). **All 12 matched exactly**, and every
NJGIN row had `OWNER_NAME = ''`. Our `parcel_id` is
`make_parcel_id(county, PCL_MUN, PCLBLOCK, PCLLOT, PCLQCODE)`, for example `HUDSON-0906-101-1.01-HM`. Strip whitespace before
comparing qualifier codes: several sources, NJGIN included, store a missing qualifier as a single space `' '`.

---

## 1. Bottom line

| Focus town (code) | Best owner-name layer found | Owner rows | Data vintage | Verdict |
|---|---|---|---|---|
| **Jersey City (0906)** | City of Jersey City `EnerGov_AddressPoints_and_Parcels/1` | 59,641 | MOD-IV updates through **Dec 2025** (layer edited 2026-03-06) | **USE** |
| **New Brunswick (1213)** | `Tax_Parcels` (New Brunswick utility org) | 8,038 | `ModIVDate` **2025-09-18** | **USE** |
| **Plainfield (2012)** | SpatialDataLogic `Plainfield_NJ_Parcels_081825` (field `Owner`) | 10,576 | MOD-IV through **Sep 2025** | **USE** (third-party copy) |
| Passaic (1607) | Neglia Engineering `Passaic_City_Parcels_20231228 view` | 7,986 | about Dec 2023, with no date field; name only | FALLBACK |
| Woodbridge (1225), Edison (1205), Perth Amboy (1216) | Middlesex County `Middlesex_County_NJ_Parcel_data` | 29,358 / 28,379 / 14,024 | MOD-IV of **Aug 2022** | FALLBACK |
| Newark (0714) | Existing 2020 snapshot (still the newest); Newark Rent Control Records 2021 (landlords only) | 48,125 / 14,127 | Jul 2020 / Jan 2021 | FALLBACK |
| Irvington (0709), East Orange (0705), Paterson (1608) | `nj_county_tax_parcels` (Essex and Passaic layers) | 9,622 / 9,946 / 24,160 | about early 2017 | FALLBACK (historical) |
| Elizabeth, Linden, Bayonne, West New York, Hackensack, Garfield, Camden, Pennsauken, Lindenwold, Gloucester City | **None found.** Several layers exist, but `OWNER_NAME` is blank or the literal `REDACTED` | 0 | n/a | NO. Keep the taxrecords-nj.com export or OPRA route |

Owner names now exist only in layers published **by a municipality, a utility, or that municipality's engineer/vendor**.
Every county or state layer refreshed since about 2023 has the name redacted: NJGIN, Camden County 2026, Hudson County
April 2026, and Middlesex `Parcels_Public`. They still carry the **mailing address and sale data**, which helps
absentee and freshness checks (section 4).

---

## 2. Verified layers that have owner names

### 2.1 Jersey City: `EnerGov_AddressPoints_and_Parcels`, layer 1 "Parcels" (**USE**)

- **Service:** `https://services2.arcgis.com/UXbywc7dSkfgdPp4/arcgis/rest/services/EnerGov_AddressPoints_and_Parcels/FeatureServer/1`
- **Owner:** ArcGIS org **"City of Jersey City"** (`UXbywc7dSkfgdPp4`), item `196af785d95443ec86b47b3f0ac6a09b` by `CDAUG_JerseyCity`,
  access `public`, item modified 2026-03-06. Snippet: "EnerGov address points and parcels". The name and the `TylerID`/`EnerGov_*` fields
  suggest it feeds the city's Tyler EnerGov permitting system.
- **Fields:** `M4_Owner_Name`; mailing in `M4_Owner_Street` and `M4_Owner_Csz` (city, state and ZIP in one padded string); `M4_Location`;
  `M4_Class`; `M4_Land_Value`, `M4_Impr_Value`, `M4_Net_Value`; `M4_Year_Built`; `M4_Facility_Name`; `M4_Update_Date` (string `MM/DD/YY`).
  Keys: `Parcel_PAMS_PIN`, `M4_PAMSPIN`, `M4_LEADLOT` (`0906-101-1.01-HM`), `Parcel_BLOCK`, `Parcel_LOT`, `Parcel_QUAL`, `M4_Block`, `M4_Lot`, `M4_Qual`.
  `Parcel_MUN` holds the text `"Jersey City"`, not `0906`. **No deed or sale fields.** Take those from `Parcel_2024` below.
- **Count:** 59,688 rows, 59,641 with an owner name. For comparison, the taxrecords-nj.com owner list has 64,836 rows. maxRecordCount is 2000.
- **Vintage:** `M4_Update_Date` falls in 2022 for 5,593 rows, 2023 for 15,899, 2024 for 6,683, 2025 for 10,125 (874 of them in Dec 2025), and 2026 for 0. Layer `dataLastEditDate` is 2026-03-06.
- **Example query:**
  `.../FeatureServer/1/query?where=Parcel_PAMS_PIN+IN+('0906_101_1.01_HM','0906_101_7_HM')&outFields=Parcel_PAMS_PIN,M4_Block,M4_Lot,M4_Qual,M4_Location,M4_Owner_Name,M4_Owner_Street,M4_Owner_Csz,M4_Class,M4_Net_Value,M4_Update_Date&returnGeometry=false&f=json`
- **Sample rows:**

  | Parcel_PAMS_PIN | M4_Location | M4_Owner_Name | M4_Owner_Street | M4_Owner_Csz | Class | M4_Update_Date |
  |---|---|---|---|---|---|---|
  | 0906_101_1.01_HM | 1075 SECAUCUS RD. | 1075 SECAUCUS ROAD LLC | 125 CHUBB AVENUE | LYNDHURST , NJ 07071 | 4B | 12/27/24 |
  | 0906_101_7_HM | SECAUCUS RD. | CITY OF JERSEY CITY | 280 GROVE ST | JERSEY CITY N J 07302 | 15C | 08/03/22 |
- **Terms/notes:** The item has no license or `accessInformation` text. This is the city's own public layer. The data is MOD-IV with names,
  which is a published tax-list record, so Daniel's Law suppression still applies downstream. The city could restrict the layer at any time;
  if it starts refusing requests, record that in `agent_runs` and fall back.
- **Companion layer, for sale data:** `https://services2.arcgis.com/UXbywc7dSkfgdPp4/arcgis/rest/services/Parcel_2024/FeatureServer/0`
  (item `5b360165ae724eafb364b32811114430`, same org). It has 58,311 rows, 58,280 with an owner. Data edited 2024-09-20, with updates through 2024.
  It adds `M4_Sale_Date`, `M4_Sale_Book`, `M4_Sale_Page`, `M4_Sale_Price`, `M4_Sale_Nu`, `M4_Number_Of_Owners`, `M4_Epl_Owner`. Example:
  `0906_101_1.01_HM`: sale 04/10/23, book 9745, page 521, price 1. An older copy, `.../Parcels/FeatureServer/0` (data 2022-11-14), adds nothing.

### 2.2 New Brunswick: `Tax_Parcels` (**USE**)

- **Service:** `https://services8.arcgis.com/6wX7jDKCT8cRPUHm/arcgis/rest/services/Tax_Parcels/FeatureServer/0`
- **Owner:** item `a311be549aab4e338aeb16adaffd0729` by `CalenDaugherty`, public, modified 2025-09-25. The org (`6wX7jDKCT8cRPUHm`) is private, so its name
  cannot be read anonymously. It is the same org as `NBWater_Manager` (New Brunswick Water Utility layers: hydrants, service lines,
  meter changes).
- **Fields:** `OwnersName`, `OwnerAddr1`, `OwnerAddr2`, `ZipCode`; `PropLoc`, `PropClass`, `BuildDesc`, `NoUnits`, `YearBuilt`, `SFLA`, `NetValue`;
  `SalesDate` (date), `DeedBook`, `DeedPage`, `SalePrice`, `LastUpdate_ModIV`, `ModIVDate`. `DeedLink` holds a direct URL to the Middlesex
  County Clerk image search for the deed. Keys: `PAMS_PIN`, `MUN`, `BLOCK`, `LOT`, `QCODE`, `LEADLOT`, `UNIQUEID`.
- **Count:** 8,043 rows, 8,038 with an owner name, all in `MUN='1213'`.
- **Vintage:** `ModIVDate` max is **2025-09-18**, `SalesDate` max 2025-06-24, `LastUpdate_ModIV` max 2025-01-06.
- **Example query:**
  `.../Tax_Parcels/FeatureServer/0/query?where=PAMS_PIN+IN+('1213_89_16','1213_89_40')&outFields=PAMS_PIN,MUN,BLOCK,LOT,QCODE,PropLoc,PropClass,OwnersName,OwnerAddr1,OwnerAddr2,ZipCode,SalesDate,DeedBook,DeedPage,SalePrice,ModIVDate&returnGeometry=false&f=json`
- **Sample rows** (individual owners' names and home addresses redacted before commit — Daniel's Law / no PII in git):

  | PAMS_PIN | PropLoc | OwnersName | OwnerAddr1 | OwnerAddr2 | Zip | SalesDate | Deed bk/pg | Price |
  |---|---|---|---|---|---|---|---|---|
  | 1213_89_16 | 75 RICHARDSON ST | [individual — redacted] | [redacted] | HILLSBOROUGH, NJ | [redacted] | 2018-10-10 | [redacted] | 210,000 |
  | 1213_89_40 | 76 SICARD ST | [individual — redacted] | [redacted] | COLLINGSWOOD, NJ | [redacted] | 2013-10-29 | [redacted] | 1 |
- **Terms/notes:** The item has no license text. Both samples are absentee owners. The utility org also publishes `Parcels_080524`
  (FALLBACK, 2.6 below) and a vacant-property registration layer (section 5).

### 2.3 Plainfield: `Plainfield_NJ_Parcels_081825` (**USE**, with a third-party caveat)

- **Service:** `https://services3.arcgis.com/DvTqoyLKkslnGFR5/arcgis/rest/services/Plainfield_NJ_Parcels_081825/FeatureServer/0`
- **Owner:** item `01b2f688e0d74d219610d21a53514ff9` by `jkennedy_SpatialDataLogic`, a GIS vendor that also publishes Mahwah, Franklin Lakes,
  Harrison Twp and other towns. The org is private. The layer's copyright text reads "Union County Bureau of GIS, NJ Office of Information Technology...".
- **Fields:** **Use `Owner`, not `OWNER_NAME`**: `OWNER_NAME` is blank on all 10,683 rows because it came from NJGIN. The owner block is
  `Owner`, `StreetAddr`, `CityState`, `ZipCode` (integer, so leading zeros are lost: `7062` means `07062`), `OwnerCity`, `OwnerState`, `NumberOfOw`.
  Deed and sale data: `DeedBookNu`, `DeedBookPa`, `DeedDate` (string `DD-Mon-YY`), `SalesPri_1`, `SR1A_Code`. Also `DateOfLast` (last MOD-IV change),
  `YearConstr`, `LandValue`, `NetTaxable`, `TaxDelinqu`. Keys: `PAMS_PIN`, `MUN`, `BLOCK`, `LOT`, `QCODE` (stored as `' '` when empty).
- **Count:** 10,683 rows, 10,576 with `Owner` populated, all in `MUN='2012'`.
- **Vintage:** `DateOfLast` falls in 2025 for 939 rows (Jun 110, Jul 184, Aug 155, Sep 86); `DeedDate` in 2025 for 342 rows (latest Sep-25).
  The layer was edited 2025-09-23. The service name says the extract was taken 2025-08-18.
- **Example query:**
  `.../FeatureServer/0/query?where=PAMS_PIN+IN+('2012_618_21','2012_328_23')&outFields=PAMS_PIN,MUN,BLOCK,LOT,QCODE,Location,Owner,StreetAddr,CityState,ZipCode,DeedDate,SalesPri_1,DateOfLast&returnGeometry=false&f=json`
- **Sample rows** (individual owners redacted before commit):

  | PAMS_PIN | Location | Owner | StreetAddr | CityState | DeedDate | SalesPri_1 |
  |---|---|---|---|---|---|---|
  | 2012_618_21 | [redacted] | [individual — redacted] | [redacted] | PLAINFIELD, NJ | 27-Oct-23 | 575,000 |
  | 2012_328_23 | [redacted] | [individuals — redacted] | [redacted] | PLAINFIELD, NJ | 09-Oct-23 | 487,000 |
- **Terms/notes, read before integrating:** this layer carries the **raw MOD-IV columns `SocialSecu` and `SpouseSoci`**. I ran
  count-only checks and never retrieved any value. `SpouseSoci` is `000…` placeholders on all 10,575 non-empty rows; `SocialSecu`
  (integer) is non-zero on **1 row**. **Never put these columns in `outFields`:** always send an explicit field list, never `*`.
  The publisher is a private vendor with no stated license, so the layer could disappear without notice. Re-check its count on each run.

### 2.4 Passaic City: `Passaic_City_Parcels_20231228 view` (FALLBACK)

- **Service:** `https://services7.arcgis.com/MS7rf0okSaLNf69i/arcgis/rest/services/Passaic_City_Parcels_20231228_view/FeatureServer/17`
- **Owner:** item `36023b89a1e94cfb813528a1d123c203` by `gis4neglia1` (Neglia Engineering, which publishes the Passaic City, Lyndhurst and South Hackensack tax
  maps). Public, modified 2025-08-07. The City of Passaic's own org publishes `City_Of_Passaic_Parcels_2025_10_17view`, which has **no owner field**.
- **Fields:** `OWNER_NAME`, `PROP_LOC`, `PROP_CLASS`, `ADD_LOTS1`, `Zone`, `VCS`, `Neigh`, `TaxSheet`. Keys: `PAMS_PIN`, `PCL_MUN`, `PCLBLOCK`, `PCLLOT`,
  `PCLQCODE`. **No mailing address, no deed or sale fields, no date field.**
- **Count:** 8,185 rows, 8,023 with an owner name (7,986 with `PCL_MUN='1607'`, 37 with null `PCL_MUN`).
- **Vintage:** No date field exists. The source service is named `Passaic_City_Parcels_20231228`, which points to a Dec 2023 tax list.
- **Example query:** `.../FeatureServer/17/query?where=PAMS_PIN+IN+('1607_2166_20','1607_2175.02_80')&outFields=PAMS_PIN,PCL_MUN,PCLBLOCK,PCLLOT,PCLQCODE,PROP_LOC,OWNER_NAME,PROP_CLASS&returnGeometry=false&f=json`
- **Sample rows** (individuals redacted before commit): `1607_2166_20 | [address] | [individual] | class 2` and `1607_2175.02_80 | [address] | [individual] | class 2`.
- **Verdict reason:** This is the only owner-name source for Passaic City newer than 2017. Treat a name as current only when NJGIN `DEED_DATE` is before 2023-12-28.

### 2.5 Middlesex County: `Middlesex_County_NJ_Parcel_data` (FALLBACK, Aug 2022)

- **Service:** `https://services.arcgis.com/BnY3izA2Kwu6jVHq/arcgis/rest/services/Middlesex_County_NJ_Parcel_data/FeatureServer/0`
- **Owner:** org **"Middlesex County, NJ"** (`BnY3izA2Kwu6jVHq`), item `53254de92aa345d4a2ac18a3bca486ef` by `middlesexgis`, public. Credits:
  "Civil Solutions; Middlesex County Office of Information Technology; NJOGIS".
- **Fields:** `OwnersName`, `OwnerAddr1`, `OwnerAddr2`, `ZipCode`, `PropLoc`, `PropClass`, `LandDesc`, `YearBuilt`, `SFLA`, `NetValue`, `SalesDate`
  (string `M/D/YYYY`), `DeedBook`, `DeedPage`, `SalePrice`, `LastUpdate_M4`, `MODIVDate`. Keys: `PAMS_PIN`, `MUN`, `BLOCK`, `LOT`, `QCODE`, `LeadLot`, `UNIQUEID`.
- **Count:** 275,590 rows (whole county), 274,477 with an owner name. Woodbridge 29,358, Edison 28,379, Perth Amboy 14,024, New Brunswick 7,782.
- **Vintage:** `MODIVDate` max is 2022-08-12. `SalesDate` falls in 2022 for 8,009 rows and in 2023 for 0. Rows were created 2022-08-19. The layer was touched on 2025-08-19,
  but the content is the 2022 tax list.
- **Example query:** `.../FeatureServer/0/query?where=PAMS_PIN+IN+('1205_753_1.02','1225_410.01_335')&outFields=PAMS_PIN,MUN,BLOCK,LOT,QCODE,PropLoc,OwnersName,OwnerAddr1,OwnerAddr2,ZipCode,SalesDate,DeedBook,DeedPage,SalePrice,MODIVDate&returnGeometry=false&f=json`
- **Sample rows** (individuals redacted before commit):
  `1205_753_1.02 | [address] | [individual — redacted] | [mailing redacted] | sale 2/8/2016 | 140,000` and
  `1225_410.01_335 | [address] | [individuals — redacted] | [mailing redacted] | sale 12/1/2019 | 340,000`.
- **Terms/notes:** The county's **current** public service,
  `https://mcgisportal.co.middlesex.nj.us/server/rest/services/GIS/Parcels_Public/MapServer/2`, has **no owner-name field**. It keeps
  `OwnerAddr1/2`, `SalesDate`, `DeedBook`, `MODIVDate` and others. So the county has deliberately stopped publishing names, and this
  2022 item could be withdrawn. Use it only as a historical name, kept when the deed has not changed (section 7).
  **Worked example of why the deed check matters:** the 2022 layer lists `1225_410.01_335` (a Woodbridge 1-family) under
  its 2019 buyers (names redacted here), deed of 12/1/2019. The county's current public record (`MODIVDate` 2026-09-29) shows a
  newer deed with `SalesDate` 3/31/2025. The 2022 name is therefore stale and must be dropped.
  **Do not use** `.../server/rest/services/GIS/Parcels/MapServer` on the same host. Its layer metadata lists `OwnersName`, but it is not the
  service the county labels "Public", so it looks like an unintended exposure. I read only its field list and requested **no records**.

### 2.6 Other owner-name layers (FALLBACK or historical)

| Layer (service URL) | Publisher | Rows with owner / total | Target coverage | Vintage | Verdict |
|---|---|---|---|---|---|
| `https://services6.arcgis.com/drkDXByy6E3bYW3a/arcgis/rest/services/nj_county_tax_parcels/FeatureServer/0` (EssexTaxMap) | `wharcgisdeveloper` (item `1b51022e174c421cb513c27b954e432a`; org private) | 176,771 / 186,202 | Newark 42,919; East Orange 9,946; Irvington 9,622 | `PCLLASTUPD` 2016-04-28; `DEED_DATE` year 2016 = 11,320 rows, 2017 = 53, so about an early-2017 tax list | FALLBACK (historical) |
| same service, `/6` (PassaicTaxMap) | same | 116,919 / 128,901 | Paterson 24,160; Passaic 5,329 | `PCLLASTUPD` 2016-05-02 | FALLBACK (historical) |
| same service, `/3` (MiddlesexTaxMap) | same | 240,196 / 282,325 | Woodbridge 29,112; Edison 28,424; New Brunswick 7,757; Perth Amboy 9,434 | `PCLLASTUPD` 2017-05-03 | NO (2.5 is newer) |
| `https://services1.arcgis.com/WAUuvHqqP3le2PMh/arcgis/rest/services/Newark_Rent_Control_Records/FeatureServer/0` | Newark (`dikidjievai_newgin`, item `eeca86eca94d4936bef0407a0c3576dc`) | 14,127 / 14,132 | Newark rental buildings | "pulled from quickbase on January 8th, 2021" | FALLBACK (landlord flag) |
| `https://services8.arcgis.com/6wX7jDKCT8cRPUHm/arcgis/rest/services/Parcels_080524/FeatureServer/0` (`Owner_Name_1`) | New Brunswick utility org (`NBWater_Manager`) | 6,278 / 8,157 | New Brunswick | created 2024-08-05; `EditDate` 2025-02-25 | FALLBACK (2.2 is newer and more complete) |
| `https://services7.arcgis.com/NbZxijYF6JgLi4dp/arcgis/rest/services/parcels_Newark_APE/FeatureServer/0` (`Owner`) | AKRF (consultant) | 42,744 / 46,522 | Newark | edited 2020-06-10 | NO (older than our 2020-07-31 snapshot) |
| `https://services1.arcgis.com/ze0XBzU1FXj94DJq/arcgis/rest/services/New_Brunswick_Parcels/FeatureServer/0` (and an identical copy on `services8.../cbDaIA5xFnHBUlC1/...`) | Rutgers students | 7,805 / 7,888 | New Brunswick | `LASTUPDATE` 2017-09-06 | NO |
| `https://services9.arcgis.com/QHXEWAb0pE2rvfbb/arcgis/rest/services/NJ_MODIV_with_names_MCL/FeatureServer/11` | Montclair State University | 36,574 / 39,681 | Clifton 22,018, Montclair 10,862, Little Falls 3,531 (only 1 Passaic City parcel) | 2016 | NO (not our towns) |
| `https://services.arcgis.com/rD2ylXRs80UroD90/arcgis/rest/services/NBHCE_Parcels/FeatureServer/0` (`Owner_s_Name_1`) | HNTB GeoShare | 729 / 1,114 | Jersey City 540, Bayonne 161, Newark 28 (a transit corridor) | about 2019–2021 | NO (too small) |

Sample rows (explicit field lists, verified working):
- Essex `/0`, `where=PCL_MUN='0709' AND PROP_CLASS='2'`: `0709_204_8 | [address] | [individuals — redacted] | [mailing: Newark]`
  and `0709_10_21 | [address] | [individual — redacted] | [mailing redacted] | DEED_DATE 130625 | 130,000`.
- Passaic `/6`, `where=PCL_MUN='1608' AND PROP_CLASS='2'`: `1608_1011_11 | [address] | [individuals — redacted] | [mailing: Totowa] | 050214 | 275,000`
  and `1608_121_5 | 10 WATSON ST | 1472 PROPERTIES LLC | [mailing: Paterson] | 160526 | 0`.
- Newark Rent Control: `0714_4187_9 (GIS_PIN) | [address] | [individual — redacted] | [mailing: Newark]` and
  `0714_696_6 | [address] | [individuals — redacted] | [mailing: Newark]`.
  `GIS_PIN` matches NJGIN `PAMS_PIN`.

---

## 3. Newark org (`services1.arcgis.com/WAUuvHqqP3le2PMh`): no newer ownership layer

I listed all 149 services in the org (`.../rest/services?f=json`) and checked the fields of every parcel-like one:

| Service | Owner fields? | Vintage | Note |
|---|---|---|---|
| `Newark_Parcels_with_Ownership/0` (what we use now) | `OWNERSNAME`, `OWNERADD1/2`, `DEEDBOOK/PAGE`, `SALESDATE` | `MOD4_LASTUPDATE` 2020-07-31 | 48,672 rows, 48,125 with a name. Still the newest named layer |
| `Newark_Parcels/0`, `Join_Features_to_Newark_Parcels/0` | yes | same 2020-07-31 extract | duplicates |
| `Parcels/0`, `registered_rent_properties2`, `Enriched_registered_rent_properties2`, `lsl_lots`, `addrsspointwblklotzoning`, `Doremus_Study_Area_Parcels` | yes | 2017–2020 | older |
| `newark_parcels_2023/0` (edited 2025-03-05), `parcelpt_8262025_/0` (2025-08-26), `NZLUR_2023` | **no owner fields** (zoning, redevelopment, `SALESDATE`/`SALESPRICE` only in NZLUR) | 2023–2025 | geometry and zoning only |
| `Newark_Rent_Control_Records/0` | `OWNER_NAME`, `ST_ADDRESS`, `CITY_STATE`, `ZIP_CODE`, `Owner_Occ`, `Units_Res` | Jan 2021 | FALLBACK (2.6) |
| `Newark_Rent_Control_Data/0` (Quickbase) | `OwnersName` on only 425 of 14,833 rows, plus **owner phone and e-mail columns** | Feb 2021 | **NO**: contact data, and TCPA/Daniel's Law risk. Do not ingest |
| `IZO_props_prelim` | `TAOWNER` (affordable-housing developers) | 2023 | not relevant |

---

## 4. Checked and rejected

| Layer | Why |
|---|---|
| NJGIN `Parcels_Composite_NJ_WM/FeatureServer/0` (NJOGIS) and `maps.nj.gov/arcgis/rest/services/Framework/Cadastral/MapServer/0` | 3,481,240 parcels, **0** with `OWNER_NAME`. `PCLLASTUPD` 2026-03-25, `PCL_PBDATE` 2026-06-04. The ~10 re-shares of it (NAIOPNJ, tdebaun_EMUA, lindseyhancock1, BLipman_trc, kyle_cruz, NJT_GIS, AAdekanmbi, ryesmin, knecovska, Maplink) are also 0 |
| Camden County `2026_Redacted_County_Parcel_Data/FeatureServer/2` (org "Camden County", item `b5ff5f57a1ee4fcdb75b4c45f3bb8f36`) | 200,217 rows. `M4_Owner`, `M4_Street`, `M4_CityState`, `M4_Zip` = literal **`REDACTED`** on every row (0 exceptions). It does carry deed book/page and `M4_SalesDate`: 7,406 sales in '25 and 1 in '26. Coverage: Camden 32,560, Pennsauken 13,228, Lindenwold 4,900, Gloucester City 4,757. It also carries SSN-named columns: `M4_SSNum` is `000000000` or blank except **2 rows** with a value beginning 1–9 (count-only check, values never retrieved), and `M4_SSSNum` is all `0000…`. **Never request those columns.** Useful only as a FALLBACK for sale and deed freshness |
| Camden County `Camden_City_Parcels_Ownership_Redacted`, `Camden_City_Tax_Lien_Parcels_Ownership_Redacted` | the owner columns were removed. The tax-lien layer (4,116 parcels, `parcel_status` up to 2024-08-21) is a possible distress signal but has no names |
| Hudson County `Hudson_County_Parcels_April_2026/FeatureServer/0` (org "Hudson County Interactive Mapping Initiative", item `154ac2cc72ec4ef5b43fbb3b0f7d8726`) | 148,282 rows, **no owner-name field**. It has `Owner_Address`, `City_State`, `Zip_Code`, `Sales_Date` (1,042 sales in '26), `Deed_Book/Page`, `Sales_Price`. **FALLBACK** for fresh mailing addresses and sales county-wide, joined on `PAMS_PIN` |
| Middlesex `GIS/Parcels_Public/MapServer/2` | no `OwnersName` (see 2.5). `GIS/Parcels` is internal: do not use |
| Elizabeth: `City_of_Elizabeth_Parcels/0` (HARBORuser_1), `Elizabeth_Parcels_May_2022/0`, `Elizabeth_Midtown_SID_Parcels` | 19,572 rows. `OWNER_NAME` blank (it came from NJGIN, `PCLLASTUPD` 2021-11-01) |
| Union: `Union_County_Parcels_Dec_2021/0` (no owner field), Rutgers `RU_Cnty_PM4_gdb/3` (149,402 rows, owner 0), `wharcgisdeveloper union_county_nj_parcels` (geometry only), `asgengnj Union_County_NJ_Parcels` (**token required**: not attempted further) | no names |
| `services1.arcgis.com/oX0kbxItOpLtURFa` "Union County GIS" `UnionParcels02102026` (20,403 rows with `OwnName`) | **False positive: this is Union County, Pennsylvania**. It has PA "Clean and Green" fields and spatial reference 2271 (PA North) |
| Hackensack `Hackensack_Parcels/4` | 13,009 rows, `OWNER_NAME` blank (NJGIN 2021 copy) |
| Camden City: Hopeworks `Camden_Housing__Population_and_Parcel_Data_WFL1/0`, `Camden_City_Vacant_Parcels/7` | `OWNER_NAME` blank |
| Rutgers `Camden_Parcels` (52 rows), `South_Jersey_Port_Corporation_Properties` (259 rows) | too small, institutional owners only |
| NJDOT `parcelfullmerge5` "Parcels within the Transit Villages" | 34,201 rows, owner 0 |
| South Jersey Gas `SJGParcels/0` | 650,159 rows (NJGIN 2025 copy). The owner-count query timed out and was not retried |
| Colliers `Parcels_WithPopUpAndSearchInfo/9` | Ocean County, owner 0 |
| Plainfield `PlainfieldParcels2022_07` (jsprague), `VOPlainfieldGIS` | Plainfield, Indiana and Plainfield, Illinois (not NJ) |
| `Linden Parcels_4`, `Linden_Parcel_Data` (ohiostate users) | Linden, Ohio |
| `GarfieldParcels`/`OpenGov_Parcels` (RFGS16), all "Garfield County" layers | Garfield County, Colorado/Utah |
| `FerdinandRol Hosted_Parcels_Test_WebMer_20201016`, `basicwitch Digital_Property_Maps`, `asgengnj Parcels_Gloucester_County` | `499 Token Required`: not pursued |
| Bergen: `bcgisweb.co.bergen.nj.us/arcgis/rest/services` | HTTP 404 (service listed in an AGOL item no longer exists). No public Bergen layer with names found |
| Hudson "Property Lookup" `http://gis.civilsolutions.biz/hudsonco_lupn/` | HTTP 404 |
| Jersey City open-data portal `data.jerseycitynj.gov` | robots.txt: `User-agent: *` → `Disallow: /api/` and `/explore/download`. **NO.** One catalog-API request went out in the same command as the robots.txt fetch, before I had read the file. It failed (connection reset), returned no data, and was not repeated |
| Regrid nationwide tile service | tiles only, no attributes |

---

## 5. Bonus: distress-signal layers found along the way (no owner names, or names to handle carefully)

| Layer | Content | Rows / vintage | Join |
|---|---|---|---|
| `https://services7.arcgis.com/24MTNeTGsw9FVZm6/arcgis/rest/services/City_of_Paterson__Abandoned_Properties_by_year_WFL1/FeatureServer/0` (org of `PatcityNJ`, City of Paterson) | Paterson abandoned/vacant property registry: `Location_Address`, `Block`, `Lot`, `Qualifier`, `Abandoned_Year` (2017–2025), `Application_Date`, `Date_as_Foreclosure`, `Date_as_Abandoned`, `Registry_Total_Balance`, `Agent_Name`, `Is_Active` | 522 rows (385 `Is_Active='TRUE'`, 137 null); edited 2025-07-07. The `Abandoned_Year=2025` rows I sampled had address and year only | `1608_{Block}_{Lot}[_{Qualifier}]` → `PAMS_PIN` |
| `https://services8.arcgis.com/6wX7jDKCT8cRPUHm/arcgis/rest/services/Vacant_Properties_Form/FeatureServer/0` (New Brunswick utility org) | New Brunswick vacant-property registrations (`STATUS`, `VPR_VacantPropertyType`, `VPR_PropertyAddress`, `VPR_PropertyOwnerName`, board-up cost and more) | 59 rows, edited 2026-07-15 | by address. **It also exposes owner and agent phone/e-mail columns. Ingest only address, status and type as a vacancy signal, never the contact fields** |
| Camden County `Camden_City_Tax_Lien_Parcels_Ownership_Redacted/0` | Camden City tax-lien parcels (owner removed) | 4,116 rows | `parcel_pams_pin` |

---

## 6. Task 2: NJ business entity and registered-agent lookup

### 6.1 NJ Division of Revenue and Enterprise Services (DORES), official

**Business Name Search** at `https://www.njportal.com/DOR/BusinessNameSearch/`: **free, no login, no CAPTCHA**.
- Search types: Business Name (`/Search/BusinessName`; case-insensitive, with a wildcard automatically added at the end), Keywords (`/Search/Keywords`, up to 5),
  and Entity Id (`/Search/EntityId`, exact 10 digits). Each is a **POST** form with an ASP.NET anti-forgery token (`__RequestVerificationToken`
  plus a cookie). There is no documented GET deep link, so generated links can only open the search page with the name shown alongside for the operator to paste.
- **Returns only:** Business Name, Entity Id, City, Type, Incorporated Date. I tested this with 2 POSTs: "PRUDENTIAL INSURANCE COMPANY OF AMERICA" returned
  `THE PRUDENTIAL INSURANCE COMPANY OF AMERICA | 0900050867 | Newark | DP | 4/3/1873`. **It does not return the registered agent or principal address.**
- No robots.txt. Page meta is `index, nofollow`. The state disclaimer (`https://www.njportal.com/errorpages/disclaimer.aspx`) says, in Section F, "anyone may
  view, copy or distribute State information found here without obligation". Section E says all systems "are subject to monitoring … to prevent unauthorized use".
  I found no explicit rule for or against automated use.

**Business Records Service** at `https://www.njportal.com/DOR/BusinessRecords/`: searching is free; documents are paid.
- The "Business Entity Status Report" wizard (`EntityDocs/BusinessStatCopies.aspx`, ASP.NET WebForms with ViewState and no CAPTCHA) can search by
  **Business Name, Entity ID, Principal Name (Officer/Director, Manager, Managing Member), Registered Agent, or Associated Name**.
  The **result list is free** and shows name, entity ID, city, type and original filing date. I walked it once for entity 0900050867 up to the
  "Select Documents" step and did not order anything. A free Principal-Name search can list every entity a person is a principal of, but it does not show the
  registered agent or addresses.
- Fee schedule (`https://www.nj.gov/treasury/revenue/fees.shtml`, fetched today): **"Each status report (all business entities) $5.00" plus
  "Each status report online convenience fee $1.25"**. The fee schedule describes status-report content (under the bulk line item) as name, legal status,
  registered agent/office, annual-report dates, business addresses and associated names. I did not buy a report to confirm the single-report layout.
  Standing certificates: corporation/LP $25, LLC/LLP short form $50, long form $100.
  Photocopies $0.10/page; certified copies $25/document ($50 for an LLC, per `copiesentities.shtml`).
- **Bulk option:** "Bulk Access Status Reports (Line item 01000000), **$0.0185 per record**". Content per record: "Business Entity Name, Legal Status,
  Registered Agent/Office, Annual Report Filing Dates, Business Addresses, and Associated Names". Selection by filing date or entity type, delivered by
  FTP or e-mail. This is the clean, licensed way to get registered agents for every NJ LLC, for example only entities formed in the last N years.
  It is ordered through DORES Records (PO Box 450, Trenton), not over the web.
- Also on the service: "Business Entity Lists" (`EntityDocs/BusinessList.aspx`, search by filing-date range, name, principal, agent or associated name). The price of a list
  was not shown before the cart and is unverified.

**Recommendation:** Generate a manual lookup card per LLC owner: (1) a link to the BNS page with the exact name, which gives the entity ID for free;
(2) a link to `BusinessStatCopies.aspx` for the $6.25 status report when a deal is live. For volume, price a bulk status-report extract
($0.0185/record). Do not script the WebForms wizard. It is not CAPTCHA-protected, but nothing on the site invites automated use.

### 6.2 OpenCorporates

- **API:** an API key is required. Unauthenticated calls return `401 {"error":{"message":"Invalid Api Token…"}}`; I tested
  `https://api.opencorporates.com/v0.4/companies/us_nj/0900050867` and `/v0.4/companies/search?q=prudential&jurisdiction_code=us_nj`.
  Documentation (`https://api.opencorporates.com/documentation/API-Reference`, v0.4.8) gives default limits of "up to 200 requests per month, and 50 requests per
  day". Free keys are only for open-data projects whose output is "also … released under an open licence (specifically share-alike
  attribution" (ODbL). "Paid-for API Accounts … remove the OpenCorporates share-alike restrictions".
- **Terms of use** (`https://opencorporates.com/terms-of-use-2/`):
  - Free website access is "for personal use only and not for the benefit of a corporation or company".
  - Free data products go only to "journalists, NGOs and academics who are conducting public benefit research"; "corporations … are not Permitted Users".
  - Prohibited: "Using any automated system (e.g. scraping) to extract data for any purpose other than search engine indexation".
  - "we do not use the robots exclusion protocol". The robots.txt that exists does disallow `/search`, `/officers`, `/companies?page=` paging and more.
  - A non-permitted user without a subscription "do[es] not have the right or permission to access or use OpenCorporates".
- **Verdict: NO** for automation, and do not generate OpenCorporates links for the operator either. A for-profit wholesaling business is not a Permitted User.
  NJ DORES is the primary source anyway; OpenCorporates mirrors it.

---

## 7. Integrate next (ranked)

Rule for every snapshot older than the current NJGIN pull: **keep a name only if the deed has not changed since the snapshot.**
NJGIN still publishes `DEED_BOOK`, `DEED_PAGE` and `DEED_DATE` (our `njgis_client.FIELDS` already pulls them). Accept the name when NJGIN
`DEED_BOOK/DEED_PAGE` equals the snapshot's deed book/page, or, when the snapshot has no deed fields, when NJGIN `DEED_DATE` (YYMMDD) is before the snapshot
date. Store `owner_name_source` and `owner_name_asof` so the briefing can show how old the name is. Always send explicit `outFields`, never `*`.
Page with `resultOffset`/`resultRecordCount=2000` and `orderByFields=<objectid>`, or split by municipality.

1. **Jersey City, `EnerGov_AddressPoints_and_Parcels/FeatureServer/1`.** Join: `Parcel_PAMS_PIN` == NJGIN `PAMS_PIN` (equivalently
   `parcel_id = "HUDSON-" + M4_LEADLOT` after `normalize_block_lot`). Take `M4_Owner_Name`, `M4_Owner_Street`, `M4_Owner_Csz`. Pull sale book, page and date from
   `Parcel_2024/FeatureServer/0` on the same `Parcel_PAMS_PIN`. About 60k rows, 30 pages. Vintage Dec 2025. It refreshes when the city republishes.
2. **New Brunswick, `Tax_Parcels/FeatureServer/0`.** Join: `PAMS_PIN` == NJGIN `PAMS_PIN` (`MUN` = `PCL_MUN`, `BLOCK` = `PCLBLOCK`, `LOT` = `PCLLOT`,
   `QCODE` = `PCLQCODE`). Take `OwnersName`, `OwnerAddr1/2`, `ZipCode`, `SalesDate`, `DeedBook/Page`, `DeedLink`. Vintage Sep 2025. 5 pages.
3. **Plainfield, `Plainfield_NJ_Parcels_081825/FeatureServer/0`.** Join: `PAMS_PIN` == NJGIN `PAMS_PIN` (strip `QCODE`, which is `' '` when empty). Take `Owner`,
   `StreetAddr`, `CityState`, `ZipCode` (zero-pad to 5), `DeedBookNu/DeedBookPa/DeedDate`. **Exclude `SocialSecu`/`SpouseSoci`.** Vintage Sep 2025.
4. **Woodbridge, Edison, Perth Amboy: Middlesex `Middlesex_County_NJ_Parcel_data/FeatureServer/0`, `where=MUN IN ('1225','1205','1216')`.**
   Join: `PAMS_PIN`. Keep a name only if `DeedBook/DeedPage` == NJGIN `DEED_BOOK/DEED_PAGE` (Aug 2022 snapshot). About 72k rows.
5. **Passaic City, `Passaic_City_Parcels_20231228_view/FeatureServer/17`.** Join: `PAMS_PIN`, or `PCL_MUN/PCLBLOCK/PCLLOT/PCLQCODE`. Name only; keep it
   if NJGIN `DEED_DATE` < 231228.
6. **Irvington, East Orange, Paterson (and a Newark cross-check): `nj_county_tax_parcels/FeatureServer/0` (Essex) and `/6` (Passaic).** Join: `PAMS_PIN`
   (or `PCL_MUN/PCLBLOCK/PCLLOT/PCLQCODE`). Keep only when `DEED_BOOK/DEED_PAGE` equals NJGIN's. This catches long-tenure owners, who are prime
   equity/estate leads. About 2017 vintage.
7. **Newark landlord flag: `Newark_Rent_Control_Records/FeatureServer/0`.** Join: `GIS_PIN` == NJGIN `PAMS_PIN`. Adds `OWNER_NAME`, owner mailing
   address and `Units_Res` (Jan 2021). Use it as a "registered rental, possible tired landlord" signal and as a name source when the deed is unchanged since 2021.
8. **Hudson-wide mailing and sale freshness (no names): `Hudson_County_Parcels_April_2026/FeatureServer/0`.** Join: `PAMS_PIN`. Use it to refresh
   absentee status and the last sale for Bayonne, West New York and Jersey City from an April 2026 county extract.
9. **Camden County sale freshness (no names): `2026_Redacted_County_Parcel_Data/FeatureServer/2`.** Join: `Parcel_PAMS_PIN`. Take only the
   `M4_DeedBkNum`, `M4_DeedBkPg`, `M4_SalesDate`, `M4_SalesPrice` and `M4_PropertyClass` fields.

Bonus distress feeds: the Paterson abandoned registry (`1608_{Block}_{Lot}` → `PAMS_PIN`) and Camden City tax-lien parcels
(`parcel_pams_pin`). No free public layer with owner names exists for Elizabeth, Linden, Bayonne, West New York, Hackensack, Garfield or the
Camden County towns. For those, keep the taxrecords-nj.com exports (see `assessment_owner_deeds.md`) or file OPRA requests with the tax assessor.

## 8. Compliance notes

- These layers republish MOD-IV owner names that the State now redacts in NJGIN. Every name ingested from them must pass the
  Daniel's Law suppression list. Record the source layer and its as-of date on the row.
- None of the ArcGIS items above states a license (`licenseInfo` is empty). The NJOGIS-derived ones carry only the standard "not survey data"
  disclaimer. Treat them as public-record republications that can change or disappear, and re-check count and vintage on every run.
- Several layers expose more than we need: SSN-named columns (Plainfield, Camden County 2026) and owner phone/e-mail (Newark Quickbase rent
  data, New Brunswick vacant-property form). Request explicit field lists only, and never store contact or ID fields from these sources. Phone outreach follows
  the TCPA/DNC rules in CLAUDE.md regardless of where a number came from.
