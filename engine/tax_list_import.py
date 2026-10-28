"""Import a town's tax-delinquent / tax-sale list (the tax collector's spreadsheet, usually the reply to an OPRA
request from engine/opra_generator.py) → TAX_DELINQUENT signals on the matching parcels.

Collectors send whatever their billing system exports, so columns are found by name (see COLUMNS): block, lot,
qualifier, owner, property location, amount due, tax year. CSV and Excel (.xlsx) both work; the header row is the
first row that names both Block and Lot.

Parcels match exactly (municipality + block + lot + qualifier): first against parcels we already track (so the
lien stacks onto an existing sheriff-sale / absentee lead), then against NJGIN MOD-IV. Only class-2 residential
parcels become leads. Re-importing the same list updates the same signals (natural key: town + list year + parcel).
The uploaded file is kept under data/imports/tax_lists/<town>/ (git-ignored: it names owners).
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import DATA_DIR
from core.text import clean, muni_base
from db.models import Property, SignalType
from db.repository import ensure_lead, normalize_block_lot, upsert_property, upsert_signal

IMPORT_DIR = DATA_DIR / "imports" / "tax_lists"
SOURCE = "opra_tax_list"
COLUMNS = {
    "block": ("block", "blk", "block no", "block #"),
    "lot": ("lot", "lot no", "lot #"),
    "qual": ("qual", "qualifier", "qcode", "qual code"),
    "owner": ("owner", "owner name", "owners name", "name", "taxpayer", "assessed owner", "property owner"),
    "location": ("location", "property location", "prop location", "address", "property address", "street"),
    "amount": ("total due", "amount due", "total", "balance", "balance due", "delinquent amount", "amount",
               "taxes due", "total amount", "principal", "lien amount"),
    "year": ("tax year", "year", "delinquent year", "years", "fiscal year"),
    "kind": ("type", "charge", "charge type", "description", "lien type"),
}


@dataclass
class TaxRow:
    block: str
    lot: str
    qual: str = ""
    owner: str | None = None
    location: str | None = None
    amount: float = 0.0
    year: str | None = None
    kind: str | None = None


@dataclass
class ImportResult:
    town: str
    county: str
    mun_code: str | None
    rows: int = 0
    parcels: int = 0
    matched_local: int = 0
    matched_njgin: int = 0
    unmatched: int = 0
    non_residential: int = 0
    signals_new: int = 0
    new_leads: int = 0
    saved_as: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _money(text: Any) -> float:
    if isinstance(text, (int, float)):
        return float(text)
    s = re.sub(r"[^\d.\-()]", "", str(text or ""))
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()") or 0)
    except ValueError:
        return 0.0
    return -v if neg else v


def read_table(data: bytes, filename: str) -> list[list[str]]:
    """Rows of cells from CSV / TSV / XLSX bytes."""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [["" if v is None else (v.isoformat() if isinstance(v, (date, datetime)) else str(v)) for v in row]
                for row in ws.iter_rows(values_only=True)]
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [r for r in csv.reader(io.StringIO(text), dialect)]


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_:.]", " ", str(h or "").strip().lower())).strip()


def parse_rows(table: list[list[str]]) -> list[TaxRow]:
    header_i = next((i for i, r in enumerate(table[:30])
                     if {"block", "lot"} <= {_norm(c).split(" ")[0] for c in r if c}), None)
    if header_i is None:
        raise ValueError("no header row with Block and Lot columns found")
    header = [_norm(c) for c in table[header_i]]
    idx: dict[str, int] = {}
    for key, aliases in COLUMNS.items():
        for alias in aliases:
            if alias in header and header.index(alias) not in idx.values():
                idx[key] = header.index(alias)
                break
        if key not in idx and key in ("block", "lot"):
            idx[key] = next(i for i, h in enumerate(header) if h.split(" ")[0] == key)
    out = []
    for r in table[header_i + 1:]:
        def get(k: str) -> str:
            i = idx.get(k)
            return clean(r[i]) if i is not None and i < len(r) and r[i] is not None else ""
        block, lot = get("block"), get("lot")
        if not block or not lot or not re.search(r"\d", block):
            continue
        out.append(TaxRow(block=block, lot=lot, qual=get("qual").upper(), owner=get("owner") or None,
                          location=get("location") or None, amount=_money(get("amount")), year=get("year") or None,
                          kind=get("kind") or None))
    return out


def resolve_town(session: Session, county: str, town: str, gis: Any = None) -> tuple[str, str] | None:
    """(MUN_NAME, mun_code) — from parcels we hold first, else NJGIN."""
    base = muni_base(town)
    for name, code in session.execute(select(Property.municipality, Property.mun_code).where(
            Property.county == county, Property.mun_code.is_not(None)).distinct()):
        if muni_base(name) == base:
            return name, code
    if gis is not None:
        return gis.resolve_municipality(county, town)
    return None


def import_tax_list(session: Session, data: bytes, filename: str, *, county: str, town: str, gis: Any = None,
                    save: bool = True, list_year: str | None = None) -> ImportResult:
    from agents.scrapers.njgis_client import feature_to_property
    from agents.scrapers.tax_sale_agent import local_parcels
    county = county.strip().title()
    mun = resolve_town(session, county, town, gis)
    result = ImportResult(town=town, county=county, mun_code=mun[1] if mun else None)
    if mun is None:
        raise ValueError(f"can't find the municipality {town!r} in {county} County")
    if save:
        folder = IMPORT_DIR / re.sub(r"[^A-Za-z0-9]+", "_", muni_base(town).title()).strip("_")
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{date.today():%Y-%m-%d}_{Path(filename).name}"
        dest.write_bytes(data)
        result.saved_as = str(dest)
    rows = parse_rows(read_table(data, filename))
    result.rows = len(rows)
    groups: dict[tuple[str, str, str], list[TaxRow]] = defaultdict(list)
    for r in rows:
        groups[(normalize_block_lot(r.block), normalize_block_lot(r.lot), r.qual)].append(r)
    result.parcels = len(groups)
    local = local_parcels(session, mun[1], list(groups))
    features: dict[tuple[str, str, str], dict] = {}
    if gis is not None:
        blocks = sorted({k[0] for k in groups if k not in local and (k[0], k[1], "") not in local})
        for i in range(0, len(blocks), 40):
            chunk = ",".join("'" + b.replace("'", "''") + "'" for b in blocks[i:i + 40])
            for f in gis.query(f"PCL_MUN='{mun[1]}' AND PCLBLOCK IN ({chunk})"):
                a = f["attributes"]
                features.setdefault((normalize_block_lot(a.get("PCLBLOCK")), normalize_block_lot(a.get("PCLLOT")),
                                     (a.get("PCLQCODE") or "").strip().upper()), f)
    year = list_year or str(date.today().year)
    for key, items in groups.items():
        prop = local.get(key) or local.get((key[0], key[1], ""))
        if prop is not None:
            result.matched_local += 1
        else:
            feature = features.get(key) or features.get((key[0], key[1], ""))
            if feature is None:
                result.unmatched += 1
                continue
            if feature["attributes"].get("PROP_CLASS") != "2":
                result.non_residential += 1
                continue
            prop, _ = upsert_property(session, feature_to_property(feature), source="njgin_modiv")
            result.matched_njgin += 1
        if prop.property_class not in (None, "2"):
            result.non_residential += 1
            continue
        owner = next((r.owner for r in items if r.owner), None)
        if owner and not prop.owner_name:
            prop.owner_name, prop.owner_name_source = owner, "tax_collector_list"
        total = round(sum(r.amount for r in items), 2)
        meta = {"town": town, "file": Path(filename).name, "owner_on_list": owner, "items": len(items),
                "tax_years": sorted({r.year for r in items if r.year}), "charges": sorted({r.kind for r in items
                                                                                          if r.kind}),
                "location_on_list": next((r.location for r in items if r.location), None)}
        _, new = upsert_signal(
            session, prop, signal_type=SignalType.TAX_DELINQUENT, source=SOURCE,
            source_ref=f"{mun[1]}:{year}:{prop.parcel_id}", severity_weight=round(min(0.5 + total / 20000, 1.0), 3),
            amount_owed=total or None, plaintiff_or_lienholder=mun[0],
            status="on the tax collector's delinquent list", raw_metadata_json=meta, active=True)
        result.signals_new += int(new)
        _, lead_new = ensure_lead(session, prop, primary_source="tax_list", contact_name=owner)
        result.new_leads += int(lead_new)
    session.flush()
    if result.unmatched:
        result.notes.append(f"{result.unmatched} block/lot pairs didn't match a parcel (typos, merged lots or a "
                            "different qualifier format); they were skipped.")
    return result
