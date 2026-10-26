# RECON EVIDENCE ONLY — not used by the production pipeline.
# taxrecords-nj.com and tax1.co.monmouth.nj.us publish robots.txt "Disallow: /". This module documents
# the request format discovered during reconnaissance (2026-10-26). Production imports operator-downloaded
# exports instead: agents/scrapers/owner_records_agent.py (see docs/nj_data_sources.md §5).
"""
nj_owner_records_client.py -- free NJ owner / assessment / deed-transfer (SR1A) lookups.

Standalone (requests + bs4 + stdlib only).  Every source below was exercised live on
2026-10-26; see docs/recon/assessment_owner_deeds.md for the evidence and caveats.

Sources, in order of preference
-------------------------------
1. taxrecords-nj.com  "NJ Assessment Records Search" (county boards of taxation MOD-IV
   + SR1A databases).  POST https://taxrecords-nj.com/pub/cgi/inf.cgi with
   ms_user=ctb<CC>, passwd="" (CC = 2-digit county code).  out_type=3 returns a link
   to a ZIP holding a CSV of *every* matching record (no row cap observed: Jersey City
   = 64,836 rows in one request).  Owner names ARE present.
   Working counties tested: 02 Bergen, 04 Camden, 09 Hudson, 12 Middlesex, 16 Passaic,
   20 Union.  Monmouth (13) is served by the same software at
   https://tax1.co.monmouth.nj.us/cgi-bin/inf.cgi with ms_user=monm / passwd=data.
   NOTE: robots.txt on both hosts is "User-agent: * / Disallow: /".  Keep volume low
   (prefer one bulk export per municipality over per-parcel crawling).
2. Essex County (07xx, e.g. Newark 0714) has NO data on taxrecords-nj.com ("Data as
   of //"), and the Essex board's own search (TaxDataHub) requires Google reCAPTCHA.
   Fallbacks used here (owner names are redacted statewide under Daniel's Law):
     - NJGIN Parcels_Composite_NJ_WM ArcGIS layer (per-parcel / location queries)
     - NJ Treasury statewide MOD-IV file modiv-<year>.zip (bulk; read with HTTP Range
       requests so only the county member, ~16 MB compressed for Essex, is fetched)
     - NJ Treasury statewide SR1A file Sales<year>.zip (deeds; grantor/grantee blank)
     - Newark only: a 2020 "Newark Parcels with Ownership" ArcGIS snapshot is used to
       fill owner_name ONLY when the current deed book/page equals the 2020 one (i.e.
       no recorded transfer since).  owner_name_source says when this was done.
3. Middlesex (12xx): the taxrecords-nj.com Deed/Sr1a database is stale ("Data as of
   09/24/21"), so recent deeds are derived from the current owner list (last sale per
   parcel; grantee = current owner; grantor inferred from the prior tax-list owner)
   and supplemented with unnamed transfers from the statewide SR1A file.

Public API
----------
    lookup_owner(district_code, block, lot, qual="", session=None) -> list[dict]
    search_owner_by_location(district_code, location, session=None) -> list[dict]
    fetch_municipality_owners(district_code, session=None, max_rows=None) -> list[dict]
    fetch_recent_deeds(district_code, since, session=None, max_rows=None) -> list[dict]
    fetch_statewide_sr1a(district_code, since, session=None, max_rows=None) -> list[dict]
    search_owner_by_name(district_code, owner_prefix, session=None) -> list[dict]
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import os
import re
import threading
import time
import zipfile
from collections import OrderedDict
from typing import Iterator, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

__all__ = [
    "lookup_owner",
    "search_owner_by_location",
    "search_owner_by_name",
    "fetch_municipality_owners",
    "fetch_recent_deeds",
    "fetch_statewide_sr1a",
    "DISTRICTS",
]

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
USER_AGENT = os.environ.get(
    "NJ_RECORDS_UA",
    "nj-owner-records-client/0.1 (python-requests; NJ public-records research)",
)
MIN_INTERVAL_S = float(os.environ.get("NJ_RECORDS_MIN_INTERVAL", "1.1"))  # per host
TIMEOUT_S = 180

TAXRECORDS_CGI = "https://taxrecords-nj.com/pub/cgi/"
MONMOUTH_CGI = "https://tax1.co.monmouth.nj.us/cgi-bin/"
NJGIN_PARCELS_QUERY = (
    "https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/"
    "Parcels_Composite_NJ_WM/FeatureServer/0/query"
)
NEWARK_2020_QUERY = (
    "https://services1.arcgis.com/WAUuvHqqP3le2PMh/arcgis/rest/services/"
    "Newark_Parcels_with_Ownership/FeatureServer/0/query"
)
NJ_SR1A_URL = "https://www.nj.gov/treasury/taxation/lpt/statdata/Sales{year}.zip"
NJ_MODIV_URL = "https://www.nj.gov/treasury/taxation/pdf/lpt/modiv-{year}.zip"

COUNTY_NAMES = {
    "01": "Atlantic", "02": "Bergen", "03": "Burlington", "04": "Camden", "05": "Cape May",
    "06": "Cumberland", "07": "Essex", "08": "Gloucester", "09": "Hudson", "10": "Hunterdon",
    "11": "Mercer", "12": "Middlesex", "13": "Monmouth", "14": "Morris", "15": "Ocean",
    "16": "Passaic", "17": "Salem", "18": "Somerset", "19": "Sussex", "20": "Union",
    "21": "Warren",
}

# county code -> (cgi base, ms_user, passwd).  Only combinations verified live.
_CGI_SOURCES = {
    "02": (TAXRECORDS_CGI, "ctb02", ""),
    "04": (TAXRECORDS_CGI, "ctb04", ""),
    "09": (TAXRECORDS_CGI, "ctb09", ""),
    "12": (TAXRECORDS_CGI, "ctb12", ""),
    "16": (TAXRECORDS_CGI, "ctb16", ""),
    "20": (TAXRECORDS_CGI, "ctb20", ""),
    "13": (MONMOUTH_CGI, "monm", "data"),
}
_STALE_SR1A_DB = {"12"}  # Middlesex Deed/Sr1a db: "Data as of 09/24/21"
_ESSEX = "07"
_NEWARK = "0714"

# Target municipalities (district codes as used by every source above).
DISTRICTS = {
    "0714": "Newark (Essex)",
    "0906": "Jersey City (Hudson)",
    "1205": "Edison (Middlesex)",
    "1213": "New Brunswick (Middlesex)",
    "1225": "Woodbridge (Middlesex)",
    "1608": "Paterson (Passaic)",
    "2004": "Elizabeth (Union)",
    "0223": "Hackensack (Bergen)",
    "0408": "Camden City (Camden)",
}

# --------------------------------------------------------------------------------------
# HTTP plumbing (per-host throttle, shared session)
# --------------------------------------------------------------------------------------
_last_hit: dict = {}
_throttle_lock = threading.Lock()
_default_session: Optional[requests.Session] = None


def _session(session: Optional[requests.Session]) -> requests.Session:
    global _default_session
    if session is not None:
        return session
    if _default_session is None:
        _default_session = requests.Session()
        _default_session.headers["User-Agent"] = USER_AGENT
    return _default_session


def _throttle(url: str) -> None:
    host = urlparse(url).netloc.lower()
    with _throttle_lock:
        wait = _last_hit.get(host, 0.0) + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.monotonic()


def _request(session, method: str, url: str, ok_404: bool = False, **kw) -> requests.Response:
    s = _session(session)
    kw.setdefault("timeout", TIMEOUT_S)
    _throttle(url)
    r = s.request(method, url, **kw)
    if ok_404 and r.status_code == 404:
        return r
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------------------
def _norm_district(code: str) -> str:
    code = str(code).strip()
    if not re.fullmatch(r"\d{4}", code):
        raise ValueError(f"district_code must be 4 digits (county+municipality), got {code!r}")
    return code


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _nz(v) -> Optional[int]:
    """int, but 0/blank -> None (for year built, sq ft)."""
    i = _to_int(v)
    return i if i else None


def _iso_date(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s or s.startswith("0000"):
        return None
    try:
        return _dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def _yy(yy: int) -> int:
    return 2000 + yy if yy < 50 else 1900 + yy


def _date_yymmdd(s: str) -> Optional[str]:
    s = (s or "").strip()
    if len(s) != 6 or not s.isdigit() or s == "000000":
        return None
    try:
        return _dt.date(_yy(int(s[:2])), int(s[2:4]), int(s[4:])).isoformat()
    except ValueError:
        return None


def _date_mmddyy(s: str) -> Optional[str]:
    s = (s or "").strip()
    if len(s) != 6 or not s.isdigit() or s == "000000":
        return None
    try:
        return _dt.date(_yy(int(s[4:])), int(s[:2]), int(s[2:4])).isoformat()
    except ValueError:
        return None


def _split_csz(v: str):
    """'BASKING RIDGE, NJ  07920' -> ('BASKING RIDGE, NJ', '07920')."""
    v = re.sub(r"\s+", " ", (v or "")).strip()
    if not v:
        return None, None
    m = re.match(r"^(.*?)[\s,]+(\d{5}(?:-?\d{4})?)$", v)
    if m:
        return m.group(1).strip(" ,") or None, m.group(2)
    return v, None


def _fmt_zip(z) -> Optional[str]:
    z = re.sub(r"\D", "", str(z or ""))
    if not z or set(z) == {"0"}:
        return None
    if len(z) == 9:
        return f"{z[:5]}-{z[5:]}" if z[5:] != "0000" else z[:5]
    return z[:5]


def _bl_norm(x: str) -> str:
    """Normalise a block or lot ('00991', '6.42', '1.A') for matching across sources."""
    x = (x or "").strip().upper()
    if "." in x:
        a, b = x.split(".", 1)
    else:
        a, b = x, ""
    a = a.lstrip("0") or "0"
    b = b.strip()
    return f"{a}.{b}" if b else a


def _fixed_bl(base: str, suffix: str) -> str:
    """MOD-IV / SR1A fixed-width block or lot: base X(5) + suffix X(4) -> '52.02'."""
    base = (base or "").strip().lstrip("0") or "0"
    suffix = (suffix or "").strip()
    return f"{base}.{suffix}" if suffix else base


def _strip0(x) -> str:
    return str(x or "").strip().lstrip("0")


def _norm_name(n: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (n or "").upper())


def _sql(s: str) -> str:
    return str(s).replace("'", "''")


# --------------------------------------------------------------------------------------
# Source 1: taxrecords-nj.com / tax1.co.monmouth.nj.us  (county board MOD-IV + SR1A)
# --------------------------------------------------------------------------------------
_SIMPLE_FORM = {"adv": "1", "out_type": "3", "ms_ln": "50", "p_loc": "", "owner": "",
                "block": "", "lot": "", "qual": ""}
_ADV_FORM = {"street": "", "city": "", "class": " ", "sr1a_f": "0", "sale_from": "",
             "sale_to": "", "cl_type": " ", "zone": "", "book": "", "page": "",
             "built_f": "0", "built_t": "0", "sqft_f": "0", "sqft_t": "0", "land_f": "0",
             "land_t": "0", "impr_f": "0", "impr_t": "0", "net_f": "0", "net_t": "0",
             "sale_f": "0", "sale_t": "0"}


def _cgi_source(district: str):
    cc = district[:2]
    if cc == _ESSEX:
        return None
    if cc not in _CGI_SOURCES:
        raise NotImplementedError(
            f"county {cc} ({COUNTY_NAMES.get(cc, '?')}) not verified on taxrecords-nj.com; "
            "add it to _CGI_SOURCES after testing ms_user=ctb{cc}")
    return _CGI_SOURCES[cc]


def _cgi_export(district: str, srch_type: int, criteria: dict, session, advanced=False):
    """POST a search with out_type=3 (Excel), download the ZIP, return (header, rows, meta)."""
    base, user, pw = _cgi_source(district)
    data = {"ms_user": user, "passwd": pw, "srch_type": str(srch_type),
            "select_cc": district, "district": district, **_SIMPLE_FORM}
    if advanced:
        data.update(_ADV_FORM)
        data["adv"] = "2"
    data.update({k: ("" if v is None else str(v)) for k, v in criteria.items()})
    url = urljoin(base, "inf.cgi")
    r = _request(session, "POST", url, data=data)
    text = r.text
    m = re.search(r"(\d+)\s+Records Found for Download", text)
    if not m:
        raise RuntimeError(f"unexpected response from {url}: {BeautifulSoup(text, 'html.parser').get_text(' ', strip=True)[:300]}")
    asof = re.search(r"Data as of\s*(\d\d)/(\d\d)/(\d\d)", text)
    meta = {"count": int(m.group(1)),
            "data_as_of": (_dt.date(_yy(int(asof.group(3))), int(asof.group(1)), int(asof.group(2))).isoformat()
                           if asof else None),
            "cgi_base": base, "zip_url": None}
    if meta["count"] == 0:
        return [], [], meta
    a = BeautifulSoup(text, "html.parser").find("a", href=re.compile(r"\.zip$", re.I))
    if not a:
        raise RuntimeError(f"no ZIP link in Excel response from {url}")
    zip_url = urljoin(url, a["href"])
    meta["zip_url"] = zip_url
    zr = _request(session, "GET", zip_url)
    zf = zipfile.ZipFile(io.BytesIO(zr.content))
    raw = zf.read(zf.namelist()[0]).decode("latin-1")
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return [], [], meta
    return rows[0], [r for r in rows[1:] if any(c.strip() for c in r)], meta


def _hdr_index(header):
    idx = {}
    for i, h in enumerate(header):
        idx.setdefault(h.strip(), i)
    return idx


def _owner_from_cgi_row(header, idx, row, district, meta) -> dict:
    def g(name):
        i = idx.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    history = []
    for i, h in enumerate(header):
        if h.strip() == "Year" and i + 8 < len(row) and row[i].strip():
            history.append({
                "year": row[i].strip(), "owner": row[i + 1].strip() or None,
                "street": row[i + 2].strip() or None, "city_state_zip": row[i + 3].strip() or None,
                "land": _to_int(row[i + 4]), "improvement": _to_int(row[i + 5]),
                "exempt": _to_int(row[i + 6]), "total": _to_int(row[i + 7]),
            })
    cur = history[0] if history else {}
    city_state, zipc = _split_csz(g("City/State/Zip"))
    join = g("Join")
    if join:
        source_url = f"{meta['cgi_base']}m4.cgi?district={district}&l02={district}{join.replace(' ', '_')}"
    else:
        source_url = meta["zip_url"]
    return {
        "district_code": g("Municipality") or district,
        "block": g("Block"), "lot": g("Lot"), "qual": g("Qual"),
        "prop_class": g("Property Class") or None,
        "property_location": g("Property Location") or None,
        "owner_name": g("Owner's Name") or None,
        "owner_street": g("Owner's Mailing Address") or None,
        "owner_city_state": city_state,
        "owner_zip": zipc,
        "assessed_land": cur.get("land"),
        "assessed_improvement": cur.get("improvement"),
        "assessed_total": cur.get("total"),
        "year_built": _nz(g("Yr. Built")),
        "building_desc": g("Building Desc") or None,
        "sq_ft": _nz(g("Sq. Ft.")),
        "last_sale_date": _iso_date(g("Sale Date")),
        "last_sale_price": _to_int(g("Sale Price")),
        "deed_book": g("Deed Book") or None,
        "deed_page": g("Deed Page") or None,
        "source_url": source_url,
        # extras
        "nu_code": g("NU Code") or None,
        "assessment_year": cur.get("year"),
        "assessment_history": history,
        "data_as_of": meta["data_as_of"],
        "owner_name_source": "county tax board MOD-IV (taxrecords-nj.com)" if g("Owner's Name") else None,
        "source": "taxrecords-nj.com owner list (Excel export)",
    }


def _deed_from_cgi_row(idx, row, district, meta) -> dict:
    def g(name):
        i = idx.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    ge_addr = ", ".join(x for x in (g("Grantee's Mailing Address"), g("Grantee's City/State/Zip")) if x) or None
    gr_addr = ", ".join(x for x in (g("Grantor's Mailing Address"), g("Grantor's City/State/Zip")) if x) or None
    return {
        "district_code": g("Municipality") or district,
        "block": g("Block"), "lot": g("Lot"), "qual": g("Qual"),
        "property_location": g("Property Location") or None,
        "grantor": g("Grantor's Name (Seller)") or None,
        "grantee": g("Grantee's Name (Buyer)") or None,
        "grantee_address": ge_addr,
        "sale_price": _to_int(g("Sale Price")),
        "deed_date": _iso_date(g("Deed Date")),
        "recorded_date": _iso_date(g("Recorded Date")),
        "nu_code": g("NU Code") or None,
        "deed_book": g("Book") or None,
        "deed_page": g("Page") or None,
        "source_url": meta["zip_url"],
        # extras
        "grantor_address": gr_addr,
        "prop_class": g("Property Class") or None,
        "sr1a_number": g("SR1A #") or None,
        "year_built": _nz(g("Yr. Built")),
        "sq_ft": _nz(g("Sq. Ft.")),
        "data_as_of": meta["data_as_of"],
        "source": "taxrecords-nj.com Deed/Sr1a list (Excel export)",
    }


_BULK_CACHE: dict = {}  # district -> (monotonic time, rows); avoids re-exporting a whole town
BULK_CACHE_TTL_S = 3600


def _cgi_owners(district, criteria, session, max_rows=None) -> list:
    bulk = not any(str(v).strip() for v in criteria.values())
    hit = _BULK_CACHE.get(district) if bulk else None
    if hit and time.monotonic() - hit[0] < BULK_CACHE_TTL_S:
        out = hit[1]
    else:
        header, rows, meta = _cgi_export(district, 1, criteria, session)
        idx = _hdr_index(header) if header else {}
        out = [_owner_from_cgi_row(header, idx, r, district, meta) for r in rows]
        if bulk:
            _BULK_CACHE[district] = (time.monotonic(), out)
    return out[:max_rows] if max_rows else out


# --------------------------------------------------------------------------------------
# Source 2: NJGIN statewide parcels (ArcGIS) -- owner names redacted
# --------------------------------------------------------------------------------------
_NJGIN_FIELDS = ("PCL_MUN,PCLBLOCK,PCLLOT,PCLQCODE,PROP_CLASS,PROP_LOC,OWNER_NAME,ST_ADDRESS,"
                 "CITY_STATE,ZIP_CODE,LAND_VAL,IMPRVT_VAL,NET_VALUE,BLDG_DESC,YR_CONSTR,"
                 "DEED_BOOK,DEED_PAGE,DEED_DATE,SALE_PRICE,SALES_CODE,PCLLASTUPD")


def _arcgis_query(url, where, out_fields, session, max_rows=None, page=2000) -> list:
    out, offset = [], 0
    while True:
        params = {"where": where, "outFields": out_fields, "returnGeometry": "false",
                  "f": "json", "resultOffset": offset, "resultRecordCount": page,
                  "orderByFields": "OBJECTID"}
        d = _request(session, "GET", url, params=params).json()
        if "error" in d:
            raise RuntimeError(f"ArcGIS error from {url}: {d['error']}")
        feats = d.get("features") or []
        out.extend(f["attributes"] for f in feats)
        if max_rows and len(out) >= max_rows:
            return out[:max_rows]
        if not feats or not d.get("exceededTransferLimit"):
            return out
        offset += len(feats)


def _njgin_where_url(where: str) -> str:
    return requests.Request("GET", NJGIN_PARCELS_QUERY, params={
        "where": where, "outFields": "*", "returnGeometry": "false", "f": "json"}).prepare().url


def _owner_from_njgin(a: dict, where: str) -> dict:
    return {
        "district_code": a.get("PCL_MUN"),
        "block": (a.get("PCLBLOCK") or "").strip(), "lot": (a.get("PCLLOT") or "").strip(),
        "qual": (a.get("PCLQCODE") or "").strip(),
        "prop_class": (a.get("PROP_CLASS") or "").strip() or None,
        "property_location": (a.get("PROP_LOC") or "").strip() or None,
        "owner_name": (a.get("OWNER_NAME") or "").strip() or None,
        "owner_street": (a.get("ST_ADDRESS") or "").strip() or None,
        "owner_city_state": (a.get("CITY_STATE") or "").strip() or None,
        "owner_zip": _fmt_zip(a.get("ZIP_CODE")),
        "assessed_land": a.get("LAND_VAL"),
        "assessed_improvement": a.get("IMPRVT_VAL"),
        "assessed_total": a.get("NET_VALUE"),
        "year_built": _nz(a.get("YR_CONSTR")),
        "building_desc": (a.get("BLDG_DESC") or "").strip() or None,
        "sq_ft": None,
        "last_sale_date": _date_yymmdd(a.get("DEED_DATE")),
        "last_sale_price": a.get("SALE_PRICE"),
        "deed_book": (a.get("DEED_BOOK") or "").strip() or None,
        "deed_page": (a.get("DEED_PAGE") or "").strip() or None,
        "source_url": _njgin_where_url(where),
        "nu_code": (a.get("SALES_CODE") or "").strip() or None,
        "owner_name_source": None,
        "source": "NJGIN Parcels_Composite_NJ_WM (owner name redacted)",
    }


def _njgin_owners(where: str, session, max_rows=None) -> list:
    rows = _arcgis_query(NJGIN_PARCELS_QUERY, where, _NJGIN_FIELDS, session, max_rows=max_rows)
    return [_owner_from_njgin(a, where) for a in rows]


# --------------------------------------------------------------------------------------
# Source 3: NJ Treasury statewide MOD-IV (owner names redacted) via HTTP Range
# --------------------------------------------------------------------------------------
class _HttpRangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP Range requests (lets zipfile read one member)."""

    def __init__(self, url, session, block_size=4 << 20, max_blocks=4):
        super().__init__()
        self.url, self.session, self.bs, self.max_blocks = url, session, block_size, max_blocks
        h = _request(session, "HEAD", url)
        self.size = int(h.headers["Content-Length"])
        self.pos = 0
        self._blocks: "OrderedDict[int, bytes]" = OrderedDict()

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def _block(self, n):
        if n in self._blocks:
            self._blocks.move_to_end(n)
            return self._blocks[n]
        start = n * self.bs
        end = min(self.size, start + self.bs) - 1
        r = _request(self.session, "GET", self.url, headers={"Range": f"bytes={start}-{end}"})
        if r.status_code != 206:
            raise RuntimeError(f"server ignored Range for {self.url} (HTTP {r.status_code})")
        self._blocks[n] = r.content
        while len(self._blocks) > self.max_blocks:
            self._blocks.popitem(last=False)
        return r.content

    def readinto(self, b):
        if self.pos >= self.size:
            return 0
        want = min(len(b), self.size - self.pos)
        got = 0
        while got < want:
            n, off = divmod(self.pos, self.bs)
            chunk = self._block(n)[off: off + (want - got)]
            b[got: got + len(chunk)] = chunk
            got += len(chunk)
            self.pos += len(chunk)
        return got


def _modiv_url(session) -> str:
    year = _dt.date.today().year
    for y in (year + 1, year, year - 1):
        url = NJ_MODIV_URL.format(year=y)
        if _request(session, "HEAD", url, ok_404=True).status_code == 200:
            return url
    raise RuntimeError("no modiv-<year>.zip found on nj.gov")


def _modiv_owners(district: str, session, max_rows=None) -> list:
    cc = district[:2]
    url = _modiv_url(session)
    zf = zipfile.ZipFile(_HttpRangeFile(url, session))
    member = next((n for n in zf.namelist() if n.lower().startswith(COUNTY_NAMES[cc].lower())), None)
    if member is None:
        raise RuntimeError(f"no {COUNTY_NAMES[cc]} member in {url}")
    out, seen, key = [], False, district.encode()

    def f(l, a, b):
        return l[a - 1:b].strip()

    with zf.open(member) as fh:
        for raw in fh:
            if raw[:4] != key:
                if seen:
                    break  # file is sorted by district
                continue
            seen = True
            l = raw.decode("latin-1").rstrip("\r\n")
            out.append({
                "district_code": district,
                "block": _fixed_bl(l[4:9], l[9:13]),
                "lot": _fixed_bl(l[13:18], l[18:22]),
                "qual": f(l, 23, 33),
                "prop_class": f(l, 56, 58) or None,
                "property_location": f(l, 59, 83) or None,
                "owner_name": f(l, 176, 210) or None,  # FILLER in the public file (redacted)
                "owner_street": f(l, 211, 235) or None,
                "owner_city_state": f(l, 236, 260) or None,
                "owner_zip": _fmt_zip(f(l, 261, 269)),
                "assessed_land": _to_int(f(l, 421, 429)),
                "assessed_improvement": _to_int(f(l, 430, 438)),
                "assessed_total": _to_int(f(l, 439, 447)),
                "year_built": _nz(f(l, 416, 419)),
                "building_desc": f(l, 84, 98) or None,
                "sq_ft": None,
                "last_sale_date": _date_mmddyy(f(l, 307, 312)),
                "last_sale_price": _to_int(f(l, 313, 321)),
                "deed_book": f(l, 296, 300) or None,
                "deed_page": f(l, 301, 305) or None,
                "source_url": url + f"#{member}",
                "nu_code": f(l, 331, 332) or None,
                "owner_name_source": None,
                "source": f"NJ Treasury statewide MOD-IV {url.rsplit('/', 1)[-1]} (owner name redacted)",
            })
            if max_rows and len(out) >= max_rows:
                break
    return out


# --------------------------------------------------------------------------------------
# Newark-only name enrichment from the 2020 snapshot (only when deed is unchanged)
# --------------------------------------------------------------------------------------
def _enrich_newark(owners: list, session) -> None:
    need = [o for o in owners if o.get("district_code") == _NEWARK and not o.get("owner_name")]
    if not need:
        return
    blocks = sorted({o["block"] for o in need if o.get("block")})
    fields = "BLOCK,LOT,QCODE,OWNERSNAME,DEEDBOOK,DEEDPAGE,SALESDATE"
    snap = []
    if len(blocks) > 200:
        snap = _arcgis_query(NEWARK_2020_QUERY, "1=1", fields, session)
    else:
        for i in range(0, len(blocks), 40):
            chunk = ",".join(f"'{_sql(b)}'" for b in blocks[i:i + 40])
            snap += _arcgis_query(NEWARK_2020_QUERY, f"BLOCK IN ({chunk})", fields, session)
    idx = {}
    for a in snap:
        idx[(_bl_norm(a.get("BLOCK")), _bl_norm(a.get("LOT")), (a.get("QCODE") or "").strip().upper())] = a
    for o in need:
        a = idx.get((_bl_norm(o["block"]), _bl_norm(o["lot"]), (o.get("qual") or "").strip().upper()))
        if not a or not (a.get("OWNERSNAME") or "").strip():
            continue
        if (_strip0(a.get("DEEDBOOK")) and _strip0(a.get("DEEDBOOK")) == _strip0(o.get("deed_book"))
                and _strip0(a.get("DEEDPAGE")) == _strip0(o.get("deed_page"))):
            o["owner_name"] = a["OWNERSNAME"].strip()
            o["owner_name_source"] = ("Newark 2020 parcel snapshot (ArcGIS 'Newark Parcels with "
                                      "Ownership'); deed book/page unchanged since then")


# --------------------------------------------------------------------------------------
# Source 4: NJ Treasury statewide SR1A sales files (grantor/grantee redacted)
# --------------------------------------------------------------------------------------
_SR1A_ZIP_CACHE: dict = {}


def _sr1a_zip(year: int, session):
    if year not in _SR1A_ZIP_CACHE:
        url = NJ_SR1A_URL.format(year=year)
        r = _request(session, "GET", url, ok_404=True, timeout=900)
        _SR1A_ZIP_CACHE[year] = None if r.status_code == 404 else (url, r.content)
    return _SR1A_ZIP_CACHE[year]


def _sr1a_years(since: _dt.date, until: _dt.date):
    # Sales<Y>.zip holds SR1As recorded July (Y-1) .. June Y (verified for 2025 & 2026).
    first = since.year + (1 if since.month >= 7 else 0)
    last = until.year + (1 if until.month >= 7 else 0)
    return range(first, last + 1)


def fetch_statewide_sr1a(district_code: str, since: "_dt.date", session=None,
                         max_rows: Optional[int] = None) -> list:
    """All SR1A transfers for a district from NJ Treasury Sales<year>.zip (names redacted).

    Lags ~3-4 months (Sales2026.zip, published 2026-10-01, ends with June 2026 recordings).
    """
    district = _norm_district(district_code)
    today = _dt.date.today()
    out, seen = [], set()
    key = district.encode()

    def f(l, a, b):
        return l[a - 1:b].strip()

    for y in _sr1a_years(since, today):
        got = _sr1a_zip(y, session)
        if not got:
            continue
        url, blob = got
        zf = zipfile.ZipFile(io.BytesIO(blob))
        with zf.open(zf.namelist()[0]) as fh:
            for raw in fh:
                if raw[:4] != key:
                    continue
                l = raw.decode("latin-1").rstrip("\r\n")
                dd = _date_yymmdd(f(l, 339, 344))
                if not dd or dd < since.isoformat() or dd > today.isoformat():
                    continue
                sig = (f(l, 99, 105), f(l, 329, 333), f(l, 334, 338), f(l, 351, 368))
                if sig in seen:  # same SR1A can appear in consecutive yearly files
                    continue
                seen.add(sig)
                ge = " ".join(x for x in (f(l, 239, 263), f(l, 264, 288), _fmt_zip(f(l, 289, 297)) or "") if x)
                out.append({
                    "district_code": district,
                    "block": _fixed_bl(f(l, 351, 355), f(l, 356, 359)),
                    "lot": _fixed_bl(f(l, 360, 364), f(l, 365, 368)),
                    "qual": f(l, 620, 624),
                    "property_location": f(l, 298, 322) or None,
                    "grantor": f(l, 110, 144) or None,   # blank in the public file
                    "grantee": f(l, 204, 238) or None,   # blank in the public file
                    "grantee_address": ge or None,
                    "sale_price": _to_int(f(l, 38, 46)),
                    "deed_date": dd,
                    "recorded_date": _date_yymmdd(f(l, 345, 350)),
                    "nu_code": f(l, 35, 37) or None,
                    "deed_book": f(l, 329, 333) or None,
                    "deed_page": f(l, 334, 338) or None,
                    "source_url": url,
                    "prop_class": f(l, 627, 629) or None,
                    "usable": f(l, 34, 34) == "U",
                    "year_built": _nz(f(l, 653, 656)),
                    "sq_ft": _nz(f(l, 657, 663)),
                    "source": f"NJ Treasury statewide SR1A {url.rsplit('/', 1)[-1]} (names redacted)",
                })
    out.sort(key=lambda d: d["deed_date"] or "", reverse=True)
    return out[:max_rows] if max_rows else out


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------
def lookup_owner(district_code: str, block: str, lot: str, qual: str = "", session=None) -> list:
    """Current owner + mailing address for one parcel (block/lot[/qual])."""
    district = _norm_district(district_code)
    if district[:2] == _ESSEX:
        where = (f"PCL_MUN='{district}' AND PCLBLOCK='{_sql(block)}' AND PCLLOT='{_sql(lot)}'"
                 + (f" AND PCLQCODE='{_sql(qual)}'" if qual else ""))
        rows = _njgin_owners(where, session)
        if district == _NEWARK:
            _enrich_newark(rows, session)
        return rows
    rows = _cgi_owners(district, {"block": block, "lot": lot, "qual": qual}, session)
    exact = [r for r in rows if _bl_norm(r["block"]) == _bl_norm(block)
             and _bl_norm(r["lot"]) == _bl_norm(lot)
             and (not qual or r["qual"].strip().upper() == qual.strip().upper())]
    return exact or rows


def search_owner_by_location(district_code: str, location: str, session=None) -> list:
    """Parcels whose property location matches e.g. '1158 AMBOY' or 'MONTGOMERY'."""
    district = _norm_district(district_code)
    loc = (location or "").strip().upper()
    if not loc:
        raise ValueError("location is required")
    if district[:2] == _ESSEX:
        rows = _njgin_owners(f"PCL_MUN='{district}' AND UPPER(PROP_LOC) LIKE '%{_sql(loc)}%'", session)
        if district == _NEWARK:
            _enrich_newark(rows, session)
        return rows
    return _cgi_owners(district, {"p_loc": loc}, session)


def search_owner_by_name(district_code: str, owner_prefix: str, session=None) -> list:
    """Parcels whose owner name starts with owner_prefix (e.g. an LLC) -- not for Essex."""
    district = _norm_district(district_code)
    if district[:2] == _ESSEX:
        raise NotImplementedError("Essex owner names are redacted in every free source tested")
    return _cgi_owners(district, {"owner": owner_prefix.strip().upper()}, session)


def fetch_municipality_owners(district_code: str, session=None, max_rows: Optional[int] = None) -> list:
    """Whole-municipality owner/assessment list in one bulk export."""
    district = _norm_district(district_code)
    if district[:2] == _ESSEX:
        rows = _modiv_owners(district, session, max_rows=max_rows)
        if district == _NEWARK:
            _enrich_newark(rows, session)
        return rows
    return _cgi_owners(district, {}, session, max_rows=max_rows)


def _infer_grantor(owner: dict) -> Optional[str]:
    cur = _norm_name(owner.get("owner_name"))
    sale_year = int(owner["last_sale_date"][:4])
    for h in owner.get("assessment_history") or []:
        if h.get("owner") and _norm_name(h["owner"]) != cur and (_to_int(h.get("year")) or 0) <= sale_year:
            return h["owner"]
    return None


def _derived_deeds_from_owner_list(district: str, since: _dt.date, session) -> list:
    today = _dt.date.today().isoformat()
    out = []
    for o in _cgi_owners(district, {}, session):
        d = o.get("last_sale_date")
        if not d or d < since.isoformat() or d > today:
            continue
        addr = ", ".join(x for x in (o.get("owner_street"), " ".join(
            y for y in (o.get("owner_city_state"), o.get("owner_zip")) if y)) if x) or None
        out.append({
            "district_code": o["district_code"], "block": o["block"], "lot": o["lot"], "qual": o["qual"],
            "property_location": o["property_location"],
            "grantor": _infer_grantor(o),
            "grantee": o["owner_name"],
            "grantee_address": addr,
            "sale_price": o["last_sale_price"],
            "deed_date": d,
            "recorded_date": None,
            "nu_code": o.get("nu_code"),
            "deed_book": o["deed_book"], "deed_page": o["deed_page"],
            "source_url": o["source_url"],
            "prop_class": o["prop_class"],
            "grantor_inferred": True,
            "data_as_of": o.get("data_as_of"),
            "source": "taxrecords-nj.com owner list last sale (grantor = prior tax-list owner)",
        })
    return out


def fetch_recent_deeds(district_code: str, since: "_dt.date", session=None,
                       max_rows: Optional[int] = None) -> list:
    """Deed transfers (SR1A) with deed_date >= since, newest first."""
    district = _norm_district(district_code)
    if isinstance(since, _dt.datetime):
        since = since.date()
    cc = district[:2]
    if cc == _ESSEX:
        return fetch_statewide_sr1a(district, since, session=session, max_rows=max_rows)
    if cc in _STALE_SR1A_DB:
        named = _derived_deeds_from_owner_list(district, since, session)
        have = {(_bl_norm(d["block"]).split(".")[0], _strip0(d["deed_book"]), _strip0(d["deed_page"]))
                for d in named}
        extra = [d for d in fetch_statewide_sr1a(district, since, session=session)
                 if (_bl_norm(d["block"]).split(".")[0], _strip0(d["deed_book"]), _strip0(d["deed_page"]))
                 not in have]
        out = sorted(named + extra, key=lambda d: d["deed_date"] or "", reverse=True)
        return out[:max_rows] if max_rows else out
    today = _dt.date.today()
    header, rows, meta = _cgi_export(
        district, 2, {"sale_from": since.isoformat(), "sale_to": today.isoformat()}, session, advanced=True)
    idx = _hdr_index(header) if header else {}
    out = [_deed_from_cgi_row(idx, r, district, meta) for r in rows]
    out.sort(key=lambda d: d["deed_date"] or "", reverse=True)
    return out[:max_rows] if max_rows else out


# --------------------------------------------------------------------------------------
# Live demo
# --------------------------------------------------------------------------------------
def _show(title, rows, keys, n=3):
    print(f"\n=== {title}: {len(rows)} row(s) ===")
    for r in rows[:n]:
        print("  " + " | ".join(f"{k}={r.get(k)!r}" for k in keys))


if __name__ == "__main__":
    OK = ["district_code", "block", "lot", "qual", "prop_class", "property_location", "owner_name",
          "owner_street", "owner_city_state", "owner_zip", "assessed_total", "year_built", "sq_ft",
          "last_sale_date", "last_sale_price", "deed_book", "deed_page", "owner_name_source", "source_url"]
    DK = ["block", "lot", "qual", "property_location", "grantor", "grantee", "grantee_address",
          "sale_price", "deed_date", "recorded_date", "nu_code", "deed_book", "deed_page", "source"]
    since = _dt.date.today() - _dt.timedelta(days=365)
    t0 = time.time()

    # ---------------- Woodbridge (Middlesex 1225): county-board data, names present -------
    loc = search_owner_by_location("1225", "AMBOY AVE")
    _show("Woodbridge search_owner_by_location('AMBOY AVE')", loc, OK)
    res = next((r for r in loc if r["prop_class"] == "2"), loc[0] if loc else None)
    if res:
        _show(f"Woodbridge lookup_owner({res['block']}, {res['lot']}, {res['qual']!r})",
              lookup_owner("1225", res["block"], res["lot"], res["qual"]), OK, n=1)
    bulk = fetch_municipality_owners("1225")
    corp_re = re.compile(r"\bL\.?L\.?C\b|\bINC\b|\bCORP")
    n_corp = sum(1 for r in bulk if corp_re.search(r["owner_name"] or ""))
    print(f"\nWoodbridge fetch_municipality_owners: {len(bulk)} parcels in one export; "
          f"data_as_of={bulk[0]['data_as_of'] if bulk else None}; corporate owners={n_corp}")
    _show("Woodbridge fetch_municipality_owners (first rows)", bulk, OK)
    deeds = fetch_recent_deeds("1225", since)
    _show(f"Woodbridge fetch_recent_deeds(since={since})", deeds, DK, n=4)
    print("  by source:", {s: sum(1 for d in deeds if d['source'] == s) for s in {d['source'] for d in deeds}})

    # ---------------- Elizabeth (Union 2004): real SR1A db with grantor/grantee -----------
    eliz = fetch_recent_deeds("2004", since, max_rows=4)
    _show(f"Elizabeth fetch_recent_deeds(since={since}, max_rows=4)", eliz, DK, n=4)

    # ---------------- Newark (Essex 0714): fallbacks, names redacted ----------------------
    _show("Newark lookup_owner('991', '26')", lookup_owner("0714", "991", "26"), OK, n=1)
    _show("Newark search_owner_by_location('CHESTNUT ST')",
          search_owner_by_location("0714", "CHESTNUT ST"), OK)
    nb = fetch_municipality_owners("0714", max_rows=5)
    _show("Newark fetch_municipality_owners(max_rows=5) [MOD-IV via HTTP Range]", nb, OK)
    nd = fetch_recent_deeds("0714", since)
    _show(f"Newark fetch_recent_deeds(since={since}) [statewide SR1A, names redacted]", nd, DK)
    try:
        search_owner_by_name("0714", "ABC LLC")
    except NotImplementedError as e:
        print(f"\nNewark search_owner_by_name -> NotImplementedError: {e}")
    print(f"\nDone in {time.time() - t0:.0f}s")
