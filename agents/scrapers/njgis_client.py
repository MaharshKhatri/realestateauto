"""Client for the NJGIN statewide parcel + MOD-IV composite (ArcGIS FeatureServer).

Verified live 2026-10-26: ~3.9M parcels, maxRecordCount 2000. OWNER_NAME is blank statewide
(NJ Daniel's Law redaction); mailing address, assessment, tax, last sale (through 2024) and year
built are populated. Geometry is returned as WGS84 centroids (returnCentroid=true).
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Iterator

from core.dates import parse_modiv_date
from core.http import PoliteSession
from core.text import clean, is_entity, is_estate, is_servicer_mailing, mailing_state, muni_base, normalize_street
from db.repository import make_parcel_id

log = logging.getLogger(__name__)

PARCELS_URL = ("https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/"
               "Parcels_Composite_NJ_WM/FeatureServer/0/query")
SOURCE = "njgin_modiv"

FIELDS = ["PAMS_PIN", "PCL_MUN", "PCLBLOCK", "PCLLOT", "PCLQCODE", "PROP_CLASS", "COUNTY", "MUN_NAME",
          "PROP_LOC", "ST_ADDRESS", "CITY_STATE", "ZIP_CODE", "LAND_VAL", "IMPRVT_VAL", "NET_VALUE",
          "LAST_YR_TX", "BLDG_DESC", "LAND_DESC", "CALC_ACRE", "BLDG_CLASS", "DEED_BOOK", "DEED_PAGE",
          "DEED_DATE", "YR_CONSTR", "SALES_CODE", "SALE_PRICE", "DWELL", "COMM_DWELL"]


class NJGISClient:
    def __init__(self, http: PoliteSession | None = None):
        # ArcGIS Online handles load well; 0.5s spacing is still polite.
        self.http = http or PoliteSession(min_interval=0.5)

    # -- low level ------------------------------------------------------------------------------
    def query(self, where: str, *, out_fields: list[str] | None = None, centroid: bool = True,
              geometry: tuple[float, float] | None = None, distance_miles: float | None = None,
              order_by: str | None = None, limit: int | None = None, page_size: int = 1000,
              cache: bool = True) -> Iterator[dict[str, Any]]:
        """Yield features as {'attributes': {...}, 'centroid': (lon, lat) | None}. Pages automatically."""
        offset = 0
        yielded = 0
        while True:
            params: dict[str, Any] = {
                "where": where,
                "outFields": ",".join(out_fields or FIELDS),
                "returnGeometry": "false",
                "returnCentroid": "true" if centroid else "false",
                "outSR": "4326",
                "resultOffset": offset,
                "resultRecordCount": min(page_size, (limit - yielded) if limit else page_size),
                "f": "json",
            }
            if order_by:
                params["orderByFields"] = order_by
            if geometry is not None:
                params.update({
                    "geometry": f"{geometry[0]},{geometry[1]}", "geometryType": "esriGeometryPoint",
                    "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
                })
                if distance_miles:
                    params.update({"distance": distance_miles, "units": "esriSRUnit_StatuteMile"})
            data = self.http.get_json(PARCELS_URL, params=params, cache=cache)
            if "error" in data:
                raise RuntimeError(f"NJGIN query error: {data['error']}")
            features = data.get("features", [])
            for f in features:
                c = f.get("centroid")
                yield {"attributes": f["attributes"], "centroid": (c["x"], c["y"]) if c else None}
                yielded += 1
                if limit and yielded >= limit:
                    return
            if not data.get("exceededTransferLimit") and len(features) < params["resultRecordCount"]:
                return
            if not features:
                return
            offset += len(features)

    def count(self, where: str) -> int:
        data = self.http.get_json(PARCELS_URL, params={"where": where, "returnCountOnly": "true", "f": "json"})
        return int(data.get("count", 0))

    # -- lookups --------------------------------------------------------------------------------
    @lru_cache(maxsize=64)
    def municipalities(self, county: str) -> dict[str, str]:
        """{MUN_NAME: PCL_MUN} for a county, e.g. {'WOODBRIDGE TWP': '1225', ...}."""
        params = {
            "where": f"COUNTY='{county.upper()}'",
            "outStatistics": '[{"statisticType":"count","onStatisticField":"OBJECTID","outStatisticFieldName":"n"}]',
            "groupByFieldsForStatistics": "MUN_NAME,PCL_MUN", "f": "json",
        }
        data = self.http.get_json(PARCELS_URL, params=params)
        out: dict[str, str] = {}
        for f in data.get("features", []):
            a = f["attributes"]
            if a.get("MUN_NAME") and a.get("PCL_MUN"):
                # Keep the dominant code if a name appears with stray codes.
                out.setdefault(a["MUN_NAME"], a["PCL_MUN"])
        return out

    def resolve_municipality(self, county: str, name: str) -> tuple[str, str] | None:
        """Match a free-text town ('BLOOMFIELD', 'Township of Edison') to (MUN_NAME, PCL_MUN)."""
        munis = self.municipalities(county)
        target = muni_base(name)
        if not target:
            return None
        for mun_name, code in munis.items():
            if muni_base(mun_name) == target:
                return mun_name, code
        for mun_name, code in munis.items():  # e.g. 'WEST ORANGE' vs 'WEST ORANGE TWP'
            if muni_base(mun_name).startswith(target) or target.startswith(muni_base(mun_name)):
                return mun_name, code
        return None

    def find_parcel(self, mun_code: str, block: str, lot: str, qual: str | None = None) -> dict | None:
        b = _sql(_strip_zeros(block))
        l = _sql(_strip_zeros(lot))
        where = f"PCL_MUN='{mun_code}' AND PCLBLOCK='{b}' AND PCLLOT='{l}'"
        rows = list(self.query(where, limit=10))
        if not rows:
            # Some towns store '335.00' style blocks/lots or leading zeros.
            where = (f"PCL_MUN='{mun_code}' AND (PCLBLOCK LIKE '{b}' OR PCLBLOCK LIKE '{b}.%' OR PCLBLOCK LIKE '0{b}') "
                     f"AND (PCLLOT LIKE '{l}' OR PCLLOT LIKE '{l}.%' OR PCLLOT LIKE '0{l}')")
            rows = list(self.query(where, limit=10))
        if not rows:
            return None
        if qual:
            q = re.sub(r"\s+", "", qual).upper()
            for r in rows:
                if re.sub(r"\s+", "", r["attributes"].get("PCLQCODE") or "").upper() == q:
                    return r
        rows.sort(key=lambda r: (bool(r["attributes"].get("PCLQCODE")), -(r["attributes"].get("NET_VALUE") or 0)))
        return rows[0]

    def find_by_address(self, county: str, street: str, mun_code: str | None = None) -> dict | None:
        """Match a street address like '186 NEWARK AVE' within a county (and municipality if known)."""
        norm = normalize_street(street)
        m = re.match(r"^(\d+)[A-Z]?(?:-\d+[A-Z]?)?\s+(.+)$", norm)
        if not m:
            return None
        number, rest = m.group(1), m.group(2)
        rest = re.split(r"\s(?:UNIT|APT|#|STE|FL)\b", rest)[0]
        words = [w for w in rest.split() if w not in {"N", "S", "E", "W"}]
        if not words:
            return None
        name_word = _sql(words[0])
        scope = f"PCL_MUN='{mun_code}'" if mun_code else f"COUNTY='{county.upper()}'"
        where = f"{scope} AND (PROP_LOC LIKE '{number} %{name_word}%' OR PROP_LOC LIKE '{number}-%{name_word}%')"
        rows = list(self.query(where, limit=25))
        if not rows:
            return None
        target = f"{number} {' '.join(words)}"
        def score(r: dict) -> tuple:
            loc = normalize_street(r["attributes"].get("PROP_LOC"))
            overlap = len(set(loc.split()) & set(target.split()))
            return (overlap, r["attributes"].get("PROP_CLASS") == "2", r["attributes"].get("NET_VALUE") or 0)
        rows.sort(key=score, reverse=True)
        return rows[0]

    def nearby_sales(self, lat: float, lon: float, radius_miles: float, *, months: int = 24,
                     prop_class: str = "2", min_price: int = 30000, limit: int = 400) -> list[dict]:
        """Recorded sales near a point (public-record comps). DEED_DATE is YYMMDD text, so the
        date filter enumerates YYMM prefixes."""
        prefixes = _month_prefixes(months)
        date_clause = " OR ".join(f"DEED_DATE LIKE '{p}%'" for p in prefixes)
        where = f"PROP_CLASS='{prop_class}' AND SALE_PRICE>{min_price} AND ({date_clause})"
        return list(self.query(where, geometry=(lon, lat), distance_miles=radius_miles, limit=limit))

    def latest_sale_month(self, county: str) -> date | None:
        """Most recent deed month present in the layer for a county (data vintage)."""
        today = date.today()
        for back in range(0, 36):
            y, m = (today.year * 12 + today.month - 1 - back) // 12, (today.year * 12 + today.month - 1 - back) % 12 + 1
            prefix = f"{y % 100:02d}{m:02d}"
            if self.count(f"COUNTY='{county.upper()}' AND PROP_CLASS='2' AND DEED_DATE LIKE '{prefix}%'") > 200:
                return date(y, m, 1)
        return None


# -- mapping to our schema -------------------------------------------------------------------------
def _strip_zeros(v: str | None) -> str:
    s = str(v or "").strip().upper()
    m = re.match(r"^0*(\d+)(\.0+)?$", s)
    return m.group(1) if m else s


def _sql(v: str) -> str:
    return v.replace("'", "''")


def _month_prefixes(months: int, anchor: date | None = None) -> list[str]:
    anchor = anchor or date.today()
    out = []
    for back in range(months + 1):
        idx = anchor.year * 12 + anchor.month - 1 - back
        out.append(f"{(idx // 12) % 100:02d}{idx % 12 + 1:02d}")
    return out


def same_street(prop_loc: str | None, mail_street: str | None) -> bool:
    """True if a mailing street plausibly equals the property location.

    Handles ranges and spelling variants: '718-720 N 8TH ST' vs '718 722 NO 8TH STREET'.
    """
    a, b = normalize_street(prop_loc), normalize_street(mail_street)
    if not a or not b:
        return False
    nums_a = set(re.findall(r"\b\d+[A-Z]?\b", a.split(" UNIT")[0]))
    nums_b = set(re.findall(r"\b\d+[A-Z]?\b", b.split(" UNIT")[0]))
    stop = {"ST", "AVE", "RD", "DR", "PL", "CT", "LN", "BLVD", "TER", "N", "S", "E", "W", "UNIT", "APT"}
    words_a = {w for w in re.findall(r"[A-Z0-9]+", a) if w not in stop and not w.isdigit()}
    words_b = {w for w in re.findall(r"[A-Z0-9]+", b) if w not in stop and not w.isdigit()}
    return bool(nums_a & nums_b) and bool(words_a & words_b)


def stories_from_desc(desc: str | None) -> float | None:
    m = re.match(r"^\s*(\d(?:\.\d)?)\s*-?\s*S", (desc or "").upper())
    if m:
        return float(m.group(1))
    m = re.match(r"^\s*(\d)\s*1/2\s*S", (desc or "").upper())
    return float(m.group(1)) + 0.5 if m else None


def estimate_sqft(desc: str | None, dwellings: int | None, default_per_unit: int = 1400) -> int:
    """Rough living-area estimate from MOD-IV building description when no listing data exists."""
    units = max(int(dwellings or 1), 1)
    stories = stories_from_desc(desc)
    d = (desc or "").upper()
    if "CONDO" in d or "TH" == d.strip():
        per_unit = 1100
    elif units >= 2:
        per_unit = 1050
    elif stories is None:
        per_unit = default_per_unit
    else:
        per_unit = {1.0: 1150, 1.5: 1450, 2.0: 1700, 2.5: 2000, 3.0: 2300}.get(stories, default_per_unit)
    return int(per_unit * units)


def feature_to_property(feature: dict[str, Any]) -> dict[str, Any]:
    """Map an NJGIN feature to a properties-table dict (owner_name intentionally absent)."""
    a = feature["attributes"]
    county = (a.get("COUNTY") or "").title()
    prop_loc = clean(a.get("PROP_LOC"))
    mail_street = clean(a.get("ST_ADDRESS"))
    city_state = clean(a.get("CITY_STATE"))
    zip_raw = re.sub(r"\D", "", a.get("ZIP_CODE") or "")
    owner_zip = zip_raw[:5] if zip_raw else ""
    state = mailing_state(city_state)
    servicer = is_servicer_mailing(mail_street, city_state)
    if mail_street and not servicer:
        absentee = (not same_street(prop_loc, mail_street)) or (state not in (None, "NJ"))
    else:
        absentee = None   # no mailing address, or tax bill goes to a mortgage servicer: occupancy unknown
    yr = a.get("YR_CONSTR") or None
    sale_price = a.get("SALE_PRICE")
    centroid = feature.get("centroid")
    return {
        "parcel_id": make_parcel_id(county, a.get("PCL_MUN") or "", a.get("PCLBLOCK") or "",
                                    a.get("PCLLOT") or "", a.get("PCLQCODE")),
        "pams_pin": a.get("PAMS_PIN"),
        "county": county,
        "municipality": a.get("MUN_NAME"),
        "mun_code": a.get("PCL_MUN"),
        "block": a.get("PCLBLOCK"),
        "lot": a.get("PCLLOT"),
        "qual": clean(a.get("PCLQCODE")) or None,
        "street_address": prop_loc or None,
        "property_class": a.get("PROP_CLASS"),
        "building_desc": clean(a.get("BLDG_DESC")) or None,
        "dwellings": a.get("DWELL") or None,
        "owner_mailing_address": mail_street or None,
        "owner_city_state_zip": f"{city_state} {owner_zip}".strip() or None,
        "owner_state": state,
        "is_absentee": absentee,
        "is_out_of_state": (state is not None and state != "NJ") if mail_street and not servicer else None,
        "year_built": yr if yr and 1700 < yr <= date.today().year else None,
        "lot_size": a.get("CALC_ACRE"),
        "assessed_land": a.get("LAND_VAL"),
        "assessed_improvement": a.get("IMPRVT_VAL"),
        "assessed_total": a.get("NET_VALUE"),
        "annual_taxes": a.get("LAST_YR_TX"),
        "last_sale_date": parse_modiv_date(a.get("DEED_DATE")),
        "last_sale_price": sale_price if sale_price and sale_price > 100 else None,
        "lon": centroid[0] if centroid else None,
        "lat": centroid[1] if centroid else None,
        "raw_json": a,
    }


def owner_flags(owner_name: str | None) -> dict[str, bool | None]:
    if not owner_name:
        return {}
    return {"is_corporate_owned": is_entity(owner_name), "is_estate_owned": is_estate(owner_name)}


def years_owned(sale_date: date | None) -> float | None:
    if not sale_date:
        return None
    return (date.today() - sale_date) / timedelta(days=365.25)
