"""Owner-records enrichment: owner names, living area, and named cash buyers / flippers.

NJGIN redacts owner names. Three sources fill them in (docs/nj_data_sources.md §5):

1. **County tax board exports (manual download → automatic import).** The county boards' public
   record search (taxrecords-nj.com, linked from the board websites) exports a whole municipality
   as a ZIP/CSV: owner name, mailing address, sq ft, year built (Bergen, Camden, Hudson, Middlesex,
   Passaic, Union; data current to within days). Its robots.txt disallows automated crawling, so
   the operator downloads each town by hand (one form submission, "Excel" output) into
       data/imports/owner_lists/   (Current Owners/Assmt List export)
       data/imports/deeds/         (Deed/Sr1a List export, with a sale-date range)
   and this agent imports every file found there. Files are matched to our parcels by
   district + block + lot + qualifier.
2. **Newark 2020 ownership snapshot** (City of Newark ArcGIS layer, public API): Essex has no
   current free source; the snapshot names a parcel only when its deed book/page is unchanged
   since 2020, so the name is still the current owner.
3. **Deed exports → cash buyers.** Grantees that are entities or repeat buyers become named cash
   buyers; GIS-clustered buyers (known only by mailing address) get their names; parcels bought
   and resold within 12 months mark the first grantee as a flipper.

Run: python -m agents.scrapers.owner_records_agent [--imports data/imports]
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select

from agents.base import AgentContext, BaseAgent
from agents.scrapers.nj_tax_assessment_agent import address_mailing_key, mailing_key
from core.config import DATA_DIR
from core.text import clean, is_entity, is_estate, is_government, is_lender, is_servicer_mailing, mailing_state
from db.models import CashBuyer, Property, SignalType
from db.repository import make_parcel_id, normalize_block_lot, upsert_cash_buyer, upsert_deed, upsert_signal

log = logging.getLogger(__name__)

COUNTY_BY_CODE = {
    "01": "Atlantic", "02": "Bergen", "03": "Burlington", "04": "Camden", "05": "Cape May", "06": "Cumberland",
    "07": "Essex", "08": "Gloucester", "09": "Hudson", "10": "Hunterdon", "11": "Mercer", "12": "Middlesex",
    "13": "Monmouth", "14": "Morris", "15": "Ocean", "16": "Passaic", "17": "Salem", "18": "Somerset",
    "19": "Sussex", "20": "Union", "21": "Warren",
}
NEWARK_2020 = ("https://services1.arcgis.com/WAUuvHqqP3le2PMh/arcgis/rest/services/"
               "Newark_Parcels_with_Ownership/FeatureServer/0/query")
SRC_TAXLIST = "county_tax_list"
SRC_NEWARK = "newark_2020_snapshot"


# -- file reading -------------------------------------------------------------------------------------
def read_export(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read a taxrecords-nj.com export (.zip containing one CSV, or the .csv itself)."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            raw = zf.read(name).decode("latin-1")
    else:
        raw = path.read_text(encoding="latin-1")
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return [], []
    return [h.strip() for h in rows[0]], [r for r in rows[1:] if any(c.strip() for c in r)]


def _getter(header: list[str], row: list[str]):
    idx: dict[str, int] = {}
    for i, h in enumerate(header):
        idx.setdefault(h, i)

    def g(name: str) -> str:
        i = idx.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""
    return g


def _int(v: str) -> int | None:
    v = (v or "").replace(",", "").replace("$", "").strip()
    try:
        return int(float(v)) if v else None
    except ValueError:
        return None


def _date(v: str) -> date | None:
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            from datetime import datetime
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def split_city_state_zip(v: str) -> tuple[str, str | None]:
    """'ELIZABETH, N J  07201' -> ('ELIZABETH, NJ', '07201')."""
    v = re.sub(r"\s+", " ", v or "").strip()
    v = re.sub(r"\bN\.?\s?J\b\.?", "NJ", v)
    v = re.sub(r"\s+,", ",", v)
    m = re.match(r"^(.*?)[\s,]+(\d{5})(?:-?\d{4})?$", v)
    return (m.group(1).strip(" ,"), m.group(2)) if m else (v, None)


def parcel_id_for(district: str, block: str, lot: str, qual: str) -> str | None:
    county = COUNTY_BY_CODE.get((district or "")[:2])
    if not county or not block or not lot:
        return None
    return make_parcel_id(county, district, block, lot, qual)


def owner_rows(path: Path) -> Iterator[dict[str, Any]]:
    header, rows = read_export(path)
    if "Owner's Name" not in header:
        raise ValueError(f"{path.name}: not an owner-list export (no \"Owner's Name\" column)")
    for row in rows:
        g = _getter(header, row)
        city_state, zip_ = split_city_state_zip(g("City/State/Zip"))
        yield {
            "parcel_id": parcel_id_for(g("Municipality"), g("Block"), g("Lot"), g("Qual")),
            "owner_name": clean(g("Owner's Name")) or None,
            "owner_street": clean(g("Owner's Mailing Address")) or None,
            "owner_city_state": city_state or None, "owner_zip": zip_,
            "sqft": _int(g("Sq. Ft.")) or None, "year_built": _int(g("Yr. Built")) or None,
            "building_desc": clean(g("Building Desc")) or None, "location": clean(g("Property Location")),
        }


def deed_rows(path: Path) -> Iterator[dict[str, Any]]:
    header, rows = read_export(path)
    if "Grantee's Name (Buyer)" not in header:
        raise ValueError(f"{path.name}: not a Deed/Sr1a export (no \"Grantee's Name (Buyer)\" column)")
    for row in rows:
        g = _getter(header, row)
        district = g("Municipality")
        ge_cs, ge_zip = split_city_state_zip(g("Grantee's City/State/Zip"))
        yield {
            "parcel_id": parcel_id_for(district, g("Block"), g("Lot"), g("Qual")),
            "district": district, "county": COUNTY_BY_CODE.get(district[:2], ""),
            "location": clean(g("Property Location")),
            "grantor": clean(g("Grantor's Name (Seller)")) or None,
            "grantee": clean(g("Grantee's Name (Buyer)")) or None,
            "grantee_street": clean(g("Grantee's Mailing Address")) or None,
            "grantee_city_state": ge_cs or None, "grantee_zip": ge_zip,
            "price": _int(g("Sale Price")), "deed_date": _date(g("Deed Date")),
            "recorded_date": _date(g("Recorded Date")), "nu_code": g("NU Code") or None,
            "prop_class": g("Property Class") or None, "sqft": _int(g("Sq. Ft.")) or None,
            "year_built": _int(g("Yr. Built")) or None,
        }


def is_arms_length(d: dict) -> bool:
    """SR1A non-usable code blank/0 and a real price."""
    return (d.get("price") or 0) >= 30000 and (d.get("nu_code") in (None, "", "0", "00"))


def find_flips(deeds: list[dict], months: int = 12, min_gain: float = 0.10) -> list[dict]:
    """Same parcel bought then resold within `months` at a gain → the first grantee flipped it."""
    by_parcel: dict[str, list[dict]] = defaultdict(list)
    for d in deeds:
        if d.get("parcel_id") and d.get("deed_date") and (d.get("price") or 0) >= 30000:
            by_parcel[d["parcel_id"]].append(d)
    flips = []
    for pid, ds in by_parcel.items():
        ds.sort(key=lambda d: d["deed_date"])
        for buy, sell in zip(ds, ds[1:]):
            held = (sell["deed_date"] - buy["deed_date"]).days
            if 30 <= held <= months * 31 and sell["price"] >= buy["price"] * (1 + min_gain):
                flips.append({"parcel_id": pid, "flipper": buy["grantee"], "bought": buy["price"],
                              "sold": sell["price"], "buy_date": buy["deed_date"].isoformat(),
                              "sell_date": sell["deed_date"].isoformat(), "days_held": held,
                              "end_buyer": sell["grantee"], "location": sell["location"]})
    return flips


# -- the agent ----------------------------------------------------------------------------------------
class OwnerEnrichmentAgent(BaseAgent):
    name = "owner_enrichment"
    description = "Attach owner names / sqft from county exports and the Newark snapshot; name cash buyers."

    def run(self, ctx: AgentContext) -> None:
        root = Path(self.options.get("imports") or DATA_DIR / "imports")
        self._import_owner_lists(ctx, root / "owner_lists")
        self._import_deeds(ctx, root / "deeds")
        if not self.options.get("skip_newark"):
            self._newark_snapshot(ctx)

    # 1. owner lists
    def _import_owner_lists(self, ctx: AgentContext, folder: Path) -> None:
        files = sorted(folder.glob("*.zip")) + sorted(folder.glob("*.csv")) if folder.exists() else []
        if not files:
            ctx.note(f"No owner-list exports in {folder} — download them from the county record search "
                     "(see docs/nj_data_sources.md §5) to fill owner names for absentee leads.")
            return
        props = {p.parcel_id: p for p in ctx.session.scalars(select(Property))}
        buyers_by_key = {address_mailing_key(b.mailing_address): b for b in ctx.session.scalars(select(CashBuyer))
                         if b.mailing_address}
        for path in files:
            matched = named_buyers = 0
            try:
                rows = list(owner_rows(path))
            except Exception as exc:  # noqa: BLE001
                ctx.error(f"{path.name}: {exc}")
                continue
            for r in rows:
                ctx.records_processed += 1
                if r["owner_street"] and r["owner_name"] and buyers_by_key:
                    b = buyers_by_key.get(mailing_key(r["owner_street"], r["owner_city_state"]))
                    if b and b.entity_name.startswith("Owner @") and not is_government(r["owner_name"]):
                        b.entity_name = r["owner_name"]
                        b.contact_name = b.contact_name or r["owner_name"]
                        named_buyers += 1
                p = props.get(r["parcel_id"] or "")
                if p is None:
                    continue
                matched += 1
                self._apply_owner(ctx, p, r)
            ctx.checkpoint()
            ctx.metrics.setdefault("owner_lists", {})[path.name] = {"rows": len(rows), "matched_leads": matched,
                                                                    "buyers_named": named_buyers}
            ctx.note(f"{path.name}: {len(rows):,} parcels, {matched} matched our properties, {named_buyers} cash buyers named")

    @staticmethod
    def _apply_owner(ctx: AgentContext, p: Property, r: dict) -> None:
        from agents.scrapers.sheriff_sale_agent import first_defendant
        sheriff = next((s for s in p.active_signals if s.signal_type == SignalType.SHERIFF_SALE and s.defendant), None)
        servicer = is_servicer_mailing(r["owner_street"], r["owner_city_state"])
        p.raw_json = {**(p.raw_json or {}), SRC_TAXLIST: {"owner_name": r["owner_name"], "mailing": r["owner_street"],
                                                          "city_state_zip": f"{r['owner_city_state']} {r['owner_zip']}"}}
        if sheriff and (is_lender(r["owner_name"]) or servicer or not r["owner_name"]):
            # Tax bills of mortgaged homes often go to the servicer; the foreclosure defendant is the owner.
            p.owner_name, p.owner_name_source = first_defendant(sheriff.defendant), "sheriff_defendant"
        elif r["owner_name"]:
            p.owner_name = r["owner_name"]
            p.owner_name_source = SRC_TAXLIST
            p.is_corporate_owned = is_entity(r["owner_name"])
            p.is_estate_owned = is_estate(r["owner_name"]) or bool(p.is_estate_owned)
        if servicer:
            p.is_absentee = p.is_out_of_state = None
            r = {**r, "owner_street": None}
        if r["owner_street"]:
            p.owner_mailing_address = r["owner_street"]
            p.owner_city_state_zip = f"{r['owner_city_state'] or ''} {r['owner_zip'] or ''}".strip()
            p.owner_state = mailing_state(r["owner_city_state"]) or p.owner_state
        if r["sqft"] and r["sqft"] >= 300:
            p.sqft, p.sqft_source = r["sqft"], SRC_TAXLIST
        if r["year_built"] and not p.year_built and 1700 < r["year_built"] <= date.today().year:
            p.year_built = r["year_built"]
        if p.is_estate_owned:
            upsert_signal(ctx.session, p, signal_type=SignalType.ESTATE_OWNER, source=SRC_TAXLIST,
                          source_ref=p.parcel_id, severity_weight=0.8, active=True,
                          raw_metadata_json={"evidence": f"tax list owner: {r['owner_name']}"})
        if p.lead and not p.lead.contact_name:
            p.lead.contact_name = r["owner_name"]
        ctx.records_updated += 1

    # 2. deeds → named buyers + flips
    def _import_deeds(self, ctx: AgentContext, folder: Path) -> None:
        files = sorted(folder.glob("*.zip")) + sorted(folder.glob("*.csv")) if folder.exists() else []
        if not files:
            ctx.note(f"No deed exports in {folder}.")
            return
        deeds: list[dict] = []
        for path in files:
            try:
                rows = list(deed_rows(path))
            except Exception as exc:  # noqa: BLE001
                ctx.error(f"{path.name}: {exc}")
                continue
            for d in rows:
                if not d["parcel_id"]:
                    continue
                deeds.append(d)
                upsert_deed(ctx.session, parcel_id=d["parcel_id"], deed_date=d["deed_date"], grantee=d["grantee"],
                            source=SRC_TAXLIST, county=d["county"], property_location=d["location"],
                            grantor=d["grantor"],
                            grantee_address=", ".join(x for x in (d["grantee_street"], d["grantee_city_state"]) if x) or None,
                            sale_price=d["price"], nu_code=d["nu_code"], property_class=d["prop_class"],
                            raw_json={"recorded_date": d["recorded_date"].isoformat() if d["recorded_date"] else None,
                                      "sqft": d["sqft"], "year_built": d["year_built"]})
            ctx.records_processed += len(rows)
            ctx.checkpoint()
            ctx.note(f"{path.name}: {len(rows):,} deeds imported")

        flips = find_flips(deeds)
        flips_by_grantee: dict[str, list[dict]] = defaultdict(list)
        for f in flips:
            if f["flipper"]:
                flips_by_grantee[f["flipper"]].append(f)

        # Investor purchases: entity grantees or anyone with 2+ arm's-length buys, plus every flipper.
        by_grantee: dict[str, list[dict]] = defaultdict(list)
        for d in deeds:
            if d["grantee"] and is_arms_length(d) and not is_government(d["grantee"]):
                by_grantee[d["grantee"]].append(d)
        buyers_by_key = {address_mailing_key(b.mailing_address): b for b in ctx.session.scalars(select(CashBuyer))
                         if b.mailing_address}
        created = 0
        for grantee, ds in by_grantee.items():
            if not (is_entity(grantee) or len(ds) >= 2 or grantee in flips_by_grantee):
                continue
            ds.sort(key=lambda d: d["deed_date"] or date.min, reverse=True)
            street, city_state = ds[0]["grantee_street"], ds[0]["grantee_city_state"]
            key = mailing_key(street, city_state) if street else None
            existing = buyers_by_key.get(key) if key else None
            purchases = [{"address": d["location"], "county": d["county"], "price": d["price"],
                          "deed_date": d["deed_date"].isoformat() if d["deed_date"] else None,
                          "grantor": d["grantor"], "year_built": d["year_built"], "sqft": d["sqft"],
                          "class": d["prop_class"]} for d in ds]
            flips_here = flips_by_grantee.get(grantee, [])
            if existing:   # GIS-clustered buyer: attach the real name, merge purchases
                if existing.entity_name.startswith("Owner @"):
                    existing.entity_name = grantee
                existing.flips_count = max(existing.flips_count or 0, len(flips_here))
                if flips_here:
                    existing.buyer_type = "flipper"
                continue
            prices = [d["price"] for d in ds]
            _, was_created = upsert_cash_buyer(
                ctx.session, grantee,
                mailing_address=", ".join(x for x in (street, city_state) if x) or None,
                mailing_key=key, buyer_type="flipper" if flips_here else ("investor" if is_entity(grantee) else "repeat_buyer"),
                target_counties=sorted({d["county"] for d in ds if d["county"]}),
                target_municipalities=sorted({d["district"] for d in ds}),
                min_arv=float(min(prices)), max_arv=float(max(prices)),
                cash_purchases_count=len(ds), flips_count=len(flips_here), total_volume=float(sum(prices)),
                avg_purchase_price=round(sum(prices) / len(prices), -2),
                first_purchase_date=min(d["deed_date"] for d in ds if d["deed_date"]),
                last_purchase_date=max(d["deed_date"] for d in ds if d["deed_date"]),
                recent_purchases_json=purchases[:25] + [{"flip": f} for f in flips_here[:10]],
                reliability_score=min(100.0, 20 + 10 * len(ds) + 15 * len(flips_here)),
                source="county_deed_export")
            created += was_created
        ctx.checkpoint()
        ctx.metrics["deeds"] = len(deeds)
        ctx.metrics["flips_found"] = len(flips)
        ctx.metrics["deed_buyers_created"] = created
        if deeds:
            ctx.note(f"deeds: {len(deeds):,} imported, {len(flips)} flips (bought & resold ≤12 months), "
                     f"{created} new named buyers")

    # 3. Newark 2020 snapshot
    def _newark_snapshot(self, ctx: AgentContext) -> None:
        props = ctx.session.scalars(select(Property).where(Property.mun_code == "0714",
                                                           Property.owner_name.is_(None))).all()
        if not props:
            return
        blocks = sorted({p.block for p in props if p.block})
        index: dict[tuple[str, str, str], dict] = {}
        for i in range(0, len(blocks), 40):
            chunk = ",".join("'" + b.replace("'", "''") + "'" for b in blocks[i:i + 40])
            params = {"where": f"BLOCK IN ({chunk})", "returnGeometry": "false", "f": "json",
                      "outFields": "BLOCK,LOT,QCODE,OWNERSNAME,DEEDBOOK,DEEDPAGE,YEARBUILT,NOUNITS",
                      "resultRecordCount": 2000}
            offset = 0
            while True:
                params["resultOffset"] = offset
                data = ctx.http.get_json(NEWARK_2020, params=params)
                feats = data.get("features", [])
                for f in feats:
                    a = f["attributes"]
                    index[(normalize_block_lot(a.get("BLOCK")), normalize_block_lot(a.get("LOT")),
                           (a.get("QCODE") or "").strip().upper())] = a
                if not data.get("exceededTransferLimit") or not feats:
                    break
                offset += len(feats)
        named = 0
        for p in props:
            a = index.get((normalize_block_lot(p.block), normalize_block_lot(p.lot), (p.qual or "").strip().upper()))
            raw = (p.raw_json or {}).get("njgin_modiv") or {}
            if not a or not (a.get("OWNERSNAME") or "").strip():
                continue
            same_deed = (str(a.get("DEEDBOOK") or "").lstrip("0") == str(raw.get("DEED_BOOK") or "").lstrip("0")
                         and str(a.get("DEEDPAGE") or "").lstrip("0") == str(raw.get("DEED_PAGE") or "").lstrip("0")
                         and str(a.get("DEEDBOOK") or "").strip("0"))
            if not same_deed:
                continue
            p.owner_name = clean(a["OWNERSNAME"])
            p.owner_name_source = SRC_NEWARK
            p.is_corporate_owned = is_entity(p.owner_name)
            p.is_estate_owned = is_estate(p.owner_name) or bool(p.is_estate_owned)
            if p.lead and not p.lead.contact_name:
                p.lead.contact_name = p.owner_name
            named += 1
        ctx.records_updated += named
        ctx.metrics["newark_named"] = named
        ctx.note(f"Newark 2020 snapshot: named {named} of {len(props)} unnamed Newark parcels (deed unchanged since 2020)")
        ctx.checkpoint()


def main() -> None:
    parser = argparse.ArgumentParser(description="Owner-records enrichment")
    parser.add_argument("--imports", help="folder containing owner_lists/ and deeds/ (default data/imports)")
    parser.add_argument("--skip-newark", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = OwnerEnrichmentAgent(imports=args.imports, skip_newark=args.skip_newark).execute()
    print(f"{run.agent_name}: {run.status} processed={run.records_processed} updated={run.records_updated} errors={run.error_count}")
    for n in (run.self_improvement_notes or "").splitlines():
        print("  ", n)
    for e in (run.errors or [])[:10]:
        print("  error:", e)


if __name__ == "__main__":
    main()
