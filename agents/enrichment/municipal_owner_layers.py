"""Owner names from municipal ArcGIS parcel layers that still publish the MOD-IV owner block.

NJGIN redacts owner names statewide, but a few towns' own public layers carry them (recon with live
queries: docs/recon/public_owner_layers.md):

  Jersey City   City of Jersey City EnerGov parcels   ~59.6k names, updates through Dec 2025
  New Brunswick New Brunswick Tax_Parcels             ~8.0k names, MOD-IV dated 2025-09-18
  Plainfield    SpatialDataLogic vendor copy          ~10.6k names, extract 2025-08-18 (could disappear)

Every layer joins on PAMS_PIN exactly as NJGIN publishes it. We query only the PINs of our own leads
(batches of 80, explicit outFields, the shared per-host throttle and disk cache) — never `*`: the Plainfield
layer carries raw MOD-IV SSN columns that must never be requested. A name is only applied when the layer's
record is at least as new as the last deed NJGIN knows about, so a sold property never gets the seller's
name. The same owner-apply rules as the county tax-list import are used (servicer mailings defer to the
foreclosure defendant; entity/estate flags; estate signal).

Run: python -m agents.enrichment.municipal_owner_layers
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select

from agents.base import AgentContext, BaseAgent
from agents.scrapers.owner_records_agent import OwnerEnrichmentAgent
from core.text import clean, is_entity, mailing_city, mailing_state
from db.models import ContactType, Lead, Property
from db.repository import upsert_contact

log = logging.getLogger(__name__)
BATCH = 80


def pin_key(pin: str | None) -> str:
    """'2012_618_21_ ' / '2012_618_21' → '2012_618_21' (empty qualifiers are stored as a space by some layers)."""
    return re.sub(r"[_\s]+$", "", (pin or "").strip()).upper()


def _epoch_date(v: Any) -> date | None:
    if isinstance(v, (int, float)) and v > 0:
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date()
    return None


def _parse_date(v: Any, fmts: tuple[str, ...]) -> date | None:
    if v in (None, "", " "):
        return None
    if isinstance(v, (int, float)):
        return _epoch_date(v)
    for fmt in fmts:
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _split_csz(csz: str | None) -> tuple[str, str]:
    """'JERSEY CITY N J 07302' / 'LYNDHURST , NJ 07071' → ('JERSEY CITY N J', '07302')."""
    s = clean(csz)
    m = re.search(r"(\d{5})(?:-\d{4})?\s*$", s)
    zip_code = m.group(1) if m else ""
    return re.sub(r"\s*,\s*", ", ", s[:m.start()] if m else s).strip(" ,"), zip_code


@dataclass
class Layer:
    key: str                      # owner_name_source / raw_json key
    label: str
    url: str
    mun_codes: tuple[str, ...]
    pin_field: str
    fields: tuple[str, ...]       # explicit outFields — never '*'
    normalize: Callable[[dict[str, Any]], dict[str, Any]]
    vintage: date                 # layer extract date when a record carries none


def _jersey_city(a: dict[str, Any]) -> dict[str, Any]:
    city_state, zip_code = _split_csz(a.get("M4_Owner_Csz"))
    return {"pin": a.get("Parcel_PAMS_PIN"), "owner_name": clean(a.get("M4_Owner_Name")) or None,
            "owner_street": clean(a.get("M4_Owner_Street")) or None, "owner_city_state": city_state,
            "owner_zip": zip_code, "asof": _parse_date(a.get("M4_Update_Date"), ("%m/%d/%y", "%m/%d/%Y")),
            "deed_date": None}


def _new_brunswick(a: dict[str, Any]) -> dict[str, Any]:
    return {"pin": a.get("PAMS_PIN"), "owner_name": clean(a.get("OwnersName")) or None,
            "owner_street": clean(a.get("OwnerAddr1")) or None, "owner_city_state": clean(a.get("OwnerAddr2")),
            "owner_zip": str(a.get("ZipCode") or "").strip()[:5], "asof": _epoch_date(a.get("ModIVDate")),
            "deed_date": _epoch_date(a.get("SalesDate"))}


def _plainfield(a: dict[str, Any]) -> dict[str, Any]:
    z = a.get("ZipCode")
    return {"pin": a.get("PAMS_PIN"), "owner_name": clean(a.get("Owner")) or None,
            "owner_street": clean(a.get("StreetAddr")) or None, "owner_city_state": clean(a.get("CityState")),
            "owner_zip": f"{int(z):05d}" if isinstance(z, (int, float)) and z else str(z or "").strip()[:5],
            "asof": _parse_date(a.get("DateOfLast"), ("%d-%b-%y", "%m/%d/%Y", "%Y-%m-%d")),
            "deed_date": _parse_date(a.get("DeedDate"), ("%d-%b-%y", "%m/%d/%Y", "%Y-%m-%d"))}


LAYERS = [
    Layer("jersey_city_gis", "Jersey City EnerGov parcels",
          "https://services2.arcgis.com/UXbywc7dSkfgdPp4/arcgis/rest/services/EnerGov_AddressPoints_and_Parcels/FeatureServer/1",
          ("0906",), "Parcel_PAMS_PIN",
          ("Parcel_PAMS_PIN", "M4_Owner_Name", "M4_Owner_Street", "M4_Owner_Csz", "M4_Update_Date"),
          _jersey_city, date(2025, 12, 31)),
    Layer("new_brunswick_gis", "New Brunswick Tax_Parcels",
          "https://services8.arcgis.com/6wX7jDKCT8cRPUHm/arcgis/rest/services/Tax_Parcels/FeatureServer/0",
          ("1213",), "PAMS_PIN",
          ("PAMS_PIN", "OwnersName", "OwnerAddr1", "OwnerAddr2", "ZipCode", "SalesDate", "ModIVDate"),
          _new_brunswick, date(2025, 9, 18)),
    Layer("plainfield_gis", "Plainfield parcels (SpatialDataLogic)",
          "https://services3.arcgis.com/DvTqoyLKkslnGFR5/arcgis/rest/services/Plainfield_NJ_Parcels_081825/FeatureServer/0",
          ("2012",), "PAMS_PIN",
          ("PAMS_PIN", "Owner", "StreetAddr", "CityState", "ZipCode", "DeedDate", "DateOfLast"),
          _plainfield, date(2025, 8, 18)),
]
FORBIDDEN_FIELDS = {"SocialSecu", "SpouseSoci"}


def record_is_current(rec: dict[str, Any], last_sale: date | None, vintage: date) -> bool:
    """The layer's owner is only trustworthy if it postdates NJGIN's latest deed for the parcel."""
    if last_sale is None:
        return True
    if rec.get("deed_date") and rec["deed_date"] >= last_sale - timedelta(days=45):
        return True
    asof = rec.get("asof") or vintage
    return asof >= last_sale


class MunicipalOwnerLayersAgent(BaseAgent):
    name = "municipal_owners"
    description = "Owner names + mailing from Jersey City / New Brunswick / Plainfield parcel layers (PAMS_PIN join)."

    def run(self, ctx: AgentContext) -> None:
        for layer in LAYERS:
            assert not FORBIDDEN_FIELDS & set(layer.fields), "never request SSN columns"
            props = ctx.session.scalars(select(Property).join(Lead).where(
                Property.mun_code.in_(layer.mun_codes), Property.pams_pin.is_not(None))).all()
            if not props:
                continue
            by_pin = {pin_key(p.pams_pin): p for p in props}
            pins = sorted({p.pams_pin.strip() for p in props})
            stats = {"leads": len(props), "returned": 0, "named": 0, "stale": 0, "no_name": 0}
            for i in range(0, len(pins), BATCH):
                chunk = pins[i:i + BATCH]
                where = f"{layer.pin_field} IN ({','.join(_sql_str(x) for x in chunk)})"
                try:
                    data = ctx.http.get_json(f"{layer.url}/query", params={
                        "where": where, "outFields": ",".join(layer.fields), "returnGeometry": "false", "f": "json"})
                except Exception as exc:  # noqa: BLE001 - one layer failing must not stop the others
                    ctx.error(f"{layer.label}: {type(exc).__name__}: {exc}")
                    break
                if "error" in data:
                    ctx.error(f"{layer.label}: {data['error']}")
                    break
                for feat in data.get("features") or []:
                    rec = layer.normalize(feat.get("attributes") or {})
                    p = by_pin.get(pin_key(rec["pin"]))
                    if p is None:
                        continue
                    stats["returned"] += 1
                    if not rec["owner_name"]:
                        stats["no_name"] += 1
                        continue
                    if not record_is_current(rec, p.last_sale_date, layer.vintage):
                        stats["stale"] += 1
                        continue
                    self._apply(ctx, p, layer, rec)
                    stats["named"] += 1
                ctx.checkpoint()
            ctx.records_processed += stats["returned"]
            ctx.metrics[layer.key] = stats
            ctx.note(f"{layer.label}: {stats['named']} of {stats['leads']} leads named "
                     f"({stats['stale']} skipped: sold after the layer's record)")

    @staticmethod
    def _apply(ctx: AgentContext, p: Property, layer: Layer, rec: dict[str, Any]) -> None:
        asof = (rec.get("asof") or layer.vintage).isoformat()
        OwnerEnrichmentAgent._apply_owner(ctx, p, {**rec, "sqft": None, "year_built": None},
                                          source=layer.key, asof=asof)
        if p.owner_name_source != layer.key or not p.lead or is_entity(clean(p.owner_name).upper()):
            return
        mailing = ", ".join(x for x in (rec["owner_street"], rec["owner_city_state"], rec["owner_zip"]) if x)
        upsert_contact(ctx.session, lead=p.lead, contact_type=ContactType.OWNER, source=layer.key,
                       name=clean(p.owner_name).title(), mailing_address=mailing or None, confidence_score=0.8,
                       notes=f"Owner of record per {layer.label} (as of {asof})",
                       raw_json={"mailing_city": mailing_city(rec["owner_city_state"] or ""),
                                 "mailing_state": mailing_state(rec["owner_city_state"] or "")})


def _sql_str(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = MunicipalOwnerLayersAgent().execute()
    print(f"{run.agent_name}: {run.status} processed={run.records_processed} updated={run.records_updated} "
          f"{run.metrics_json}")


if __name__ == "__main__":
    main()
