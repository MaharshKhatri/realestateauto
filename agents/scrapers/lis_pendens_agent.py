"""Lis pendens / foreclosure-complaint filings from NJ county clerks → LIS_PENDENS signals (the earliest public
foreclosure step, months before a sheriff's sale is scheduled).

Automated (polite: one search per document type per run, identified User-Agent, per-host rate limit), verified
2026-10-28 (docs/recon/tax_sale_clerk_opendata.md Part 2; Monmouth checked 2026-10-28):
  * Union  — UCPA (clerk.ucnj.org/UCPA): GET DocIndex?s=date, POST DateResults dt=17 Foreclosure / dt=7 Lis Pendens.
             No robots.txt, no CAPTCHA. Gives party names, recorded date, book/page and instrument; no address.
  * Essex  — PRESS (press.essexregister.com): ASP.NET form, doc types 23 LP Foreclosure, 25 LP Foreclosure and
             Recovery, 21 LP In Rem (+ 22 LP Discharge, which closes the matching filing). Gives the town too.
  * Monmouth — OPRS, the same PRESS form (oprs.co.monmouth.nj.us): FORECLOSURE LISPENDENS / LIS PENDENS / LIS IN REM
             (+ DISCHARGE/LISPENDENS). Gives town AND block / lot, so filings match parcels exactly.

Drop-in import (browser-gated or robots-disallowed portals — Passaic PRESS, Hudson Acclaim, Bergen / Middlesex /
Camden BrowserView, Mercer): the operator runs the search in their own browser and saves the results
page (.html) or an export (.csv) into data/imports/lis_pendens/<county>/; this agent parses it on the next run.
A CSV may also carry block / lot or the street address (from the document image), which matches exactly.

Matching a filing to a parcel, most exact first:
  1. block + lot (+ qualifier) in the import → our parcel, else the NJGIN MOD-IV parcel;
  2. street address in the import → our parcel in that town, else NJGIN address search;
  3. owner-name match: the defendant's name tokens against owner names we hold (county owner lists, municipal
     layers, sheriff defendants, OWNER / heir contacts) in the same town (Essex, imports) or county (Union).
     Only a *unique* match counts; ambiguous names stay unmatched rather than tagging a stranger's house.
Unmatched filings stay in `clerk_filings` and re-try on every run (new owner lists make new matches), and the
dashboard lists them with the clerk link so the operator can read the address off the public document.
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.base import AgentContext, BaseAgent
from agents.scrapers.njgis_client import NJGISClient, feature_to_property
from core.config import DATA_DIR, settings
from core.http import FetchError, PoliteSession
from core.text import clean, is_entity, is_estate, muni_base
from db.models import ClerkFiling, Contact, ContactType, DistressSignal, Lead, Property, SignalType, utcnow
from db.repository import ensure_lead, normalize_block_lot, upsert_property, upsert_signal

IMPORT_DIR = DATA_DIR / "imports" / "lis_pendens"
UCPA = "https://clerk.ucnj.org/UCPA"
PRESS = "https://press.essexregister.com/ProdPRESS/clerk/ClerkHome.aspx?op=basic"
PRESS_PREFIX = "ctl00$ContentPlaceHolder1$"
UCPA_TYPES = {"17": "Foreclosure", "7": "Lis Pendens"}
PRESS_TYPES = {"23": "LIS PENDENS FORECLOSURE", "25": "LIS PENDENS FORECLOSURE AND RECOVERY",
               "21": "LIS PENDENS IN REM", "22": "LIS PENDENS DISCHARGE"}
# PRESS installs (same ASP.NET form). Monmouth OPRS, verified 2026-10-28: no robots.txt, no CAPTCHA, terms-of-use
# popup without an automation clause; its grid names the columns DEFENDANT / PLAINTIFF and carries block / lot.
PRESS_SITES = {
    "Essex": {"url": PRESS, "types": PRESS_TYPES, "total": "750"},
    "Monmouth": {"url": "https://oprs.co.monmouth.nj.us/oprs/clerk/ClerkHome.aspx?op=basic", "total": "1000",
                 "types": {"LISPENFO": "FORECLOSURE LISPENDENS", "LISPEN": "LIS PENDENS",
                           "LISINRM": "LIS IN REM COMPLAINT", "LISPENDI": "DISCHARGE/LISPENDENS"}},
}
PARTY_HEADERS = {"DIRECT PARTY": "DIRECT PARTY", "DEFENDANT": "DIRECT PARTY", "GRANTOR": "DIRECT PARTY",
                 "INDIRECT PARTY": "INDIRECT PARTY", "PLAINTIFF": "INDIRECT PARTY", "GRANTEE": "INDIRECT PARTY"}
AUTOMATED = {"Union": "ucpa", "Essex": "press", "Monmouth": "press"}
MANUAL = {
    "Passaic": "PRESS works in a browser, but robots.txt disallows automated access: search LP / Foreclosure LP by "
               "date at records.passaiccountynj.org/press and save the results page here.",
    "Hudson": "Acclaim (acclaim.hcnj.us) needs you to accept its terms first: search Doc Type + Record Date and save.",
    "Bergen": "BrowserView has a reCAPTCHA: search Document Type = Lis Pendens by date in your browser and export.",
    "Middlesex": "BrowserView has a reCAPTCHA: search Document Type = Lis Pendens by date in your browser and export.",
    "Camden": "Lis pendens are on the Premium ($25/month) tier only; free deeds/mortgages stay manual.",
    "Mercer": "records.mercercounty.org/publicsearch (document type NLP, Notice of Lis Pendens): search by date in your "
              "browser and export.",
}
TAX_LIEN_RE = re.compile(r"\b(TL|TAX\s*LIEN|TAX\s*CERT\w*|TOWER\s+DBW|FIG\s+\d+|BRIDGE\s+TAX|MTAG|US\s+BANK\s+CUST|"
                         r"CUSTODIAN\s+FOR|FWI|IRONWORKS|PRO\s+CAPITAL|LIEN\s+FUND)\b", re.I)
DISCHARGE_RE = re.compile(r"DISCHARGE|CANCEL|RELEASE", re.I)
# Co-defendants who are junior lienholders or the government, not the owner: never used for a name match.
JUNIOR_DEFENDANT_RE = re.compile(
    r"\b(STATE OF NEW JERSEY|UNITED STATES|USA\b|SECRETARY OF|DEPARTMENT OF|INTERNAL REVENUE|CITY OF|TOWNSHIP OF|"
    r"TOWN OF|BOROUGH OF|COUNTY OF|VILLAGE OF|BANK|MIDLAND|CAPITAL ONE|DISCOVER|AMERICAN EXPRESS|CAVALRY|LVNV|"
    r"PORTFOLIO RECOVERY|MORTGAGE ELECTRONIC|MERS\b|CONDOMINIUM|HOMEOWNERS|ASSOCIATION|CREDIT UNION|FUNDING|"
    r"ACQUISITIONS|COLLECTION|HOSPITAL|MEDICAL|BOARD OF)", re.I)
NAME_STOP = {"ESTATE", "OF", "THE", "AND", "ET", "ALS", "AL", "UX", "VIR", "JR", "SR", "II", "III", "IV", "MR", "MRS",
             "MS", "DECEASED", "DECD", "HEIRS", "HEIR", "UNKNOWN", "EXECUTOR", "EXECUTRIX", "ADMINISTRATOR",
             "ADMINISTRATRIX", "ADMR", "EXR", "TRUSTEE", "AKA", "FKA", "NKA", "HIS", "HER", "WIFE", "HUSBAND",
             "SPOUSE", "MRS", "INDIVIDUALLY", "AS", "BY", "FOR"}
ENTITY_STOP = {"LLC", "L", "C", "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LP", "LLP", "LTD", "THE", "OF",
               "AND", "PROPERTIES", "PROPERTY", "PROPERTYS"}


@dataclass
class Filing:
    county: str
    source: str
    instrument: str
    doc_type: str
    recorded: date | None
    defendants: list[str] = field(default_factory=list)
    plaintiffs: list[str] = field(default_factory=list)
    town: str | None = None
    block: str | None = None
    lot: str | None = None
    qual: str | None = None
    address: str | None = None
    docket: str | None = None
    url: str | None = None

    @property
    def discharge(self) -> bool:
        return bool(DISCHARGE_RE.search(self.doc_type))

    @property
    def estate(self) -> bool:
        return any(is_estate(d) or "(ESTATE OF)" in d.upper() for d in self.defendants)

    @property
    def tax_lien(self) -> bool:
        return "IN REM" in self.doc_type.upper() or any(TAX_LIEN_RE.search(p) for p in self.plaintiffs)


# -- parsing -----------------------------------------------------------------------------------------
def _date(text: str | None) -> date | None:
    text = clean(text)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_ucpa(page_html: str, county: str = "Union") -> list[Filing]:
    """UCPA DateResults: each filing is a 2-row table — [type, role, name, recorded] then [role, reverse name, book/page]."""
    soup = BeautifulSoup(page_html, "html.parser")
    out: dict[str, Filing] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr", recursive=False) or table.find_all("tr")
        if len(rows) != 2:
            continue
        link = rows[0].find("a", href=re.compile(r"NameDetails"))
        if link is None:
            continue
        a = [c.get_text(" ", strip=True) for c in rows[0].find_all("td")]
        b = [c.get_text(" ", strip=True) for c in rows[1].find_all("td")]
        if len(a) < 4 or len(b) < 3:
            continue
        m = re.search(r"im=(\d+)&is=([A-Z]*)", link["href"])
        instrument = f"{m.group(1)}-{m.group(2)}" if m else clean(b[-1])
        doc_type, role, name, recorded = a[0], a[1].lower(), clean(a[2]), _date(a[3])
        reverse = clean(b[1])
        f = out.setdefault(instrument, Filing(county=county, source="ucpa", instrument=instrument, doc_type=doc_type,
                                              recorded=recorded, url=f"{UCPA}/{link['href'].lstrip('/')}"
                                              if not link["href"].startswith("http") else link["href"]))
        defendant, plaintiff = (name, reverse) if role.startswith(("defendant", "grantor", "mortgagor")) else (reverse, name)
        if defendant and defendant not in f.defendants:
            f.defendants.append(defendant)
        if plaintiff and plaintiff not in f.plaintiffs:
            f.plaintiffs.append(plaintiff)
    return list(out.values())


def _own_rows(table: Any) -> list[Any]:
    """Rows of this table only (PRESS nests the grid inside layout tables)."""
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _cells(tr: Any) -> list[str]:
    return [clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"], recursive=False)]


def _press_grid(soup: BeautifulSoup) -> Any:
    """The results grid (its id differs between PRESS builds): the table whose own header cells name the parties."""
    for table in soup.find_all("table"):
        for tr in _own_rows(table)[:3]:
            cells = [c.upper() for c in _cells(tr)]
            if cells and (set(cells) & set(PARTY_HEADERS)) and any(c.startswith("INSTRUMENT") for c in cells):
                return table
    return None


def parse_press(page_html: str, county: str) -> tuple[list[dict[str, str]], list[str]]:
    """PRESS results grid → party rows (one per direct/indirect pair) + pager postback targets."""
    soup = BeautifulSoup(page_html, "html.parser")
    grid = _press_grid(soup)
    if grid is None:
        return [], []
    rows = _own_rows(grid)
    header_i = next(i for i, r in enumerate(rows) if set(c.upper() for c in _cells(r)) & set(PARTY_HEADERS))
    header = [PARTY_HEADERS.get(h.upper(), h.upper()) for h in _cells(rows[header_i])]
    idx = {k: next((i for i, h in enumerate(header) if h.startswith(k)), None) for k in
           ("TYPE", "DIRECT PARTY", "INDIRECT PARTY", "INSTRUMENT", "RECORDED", "TOWN", "BLOCK", "LOT")}
    out = []
    for r in rows[header_i + 1:]:
        cells = _cells(r)
        row = {k.lower().replace(" ", "_"): (cells[i] if i is not None and i < len(cells) else "")
               for k, i in idx.items()}
        if re.fullmatch(r"\d{6,}", row["instrument"]) and row["direct_party"]:
            out.append(row)
    pager = []
    for a in (rows[0].find_all("a") if rows and header_i > 0 else []):
        m = re.search(r"__doPostBack\('([^']+)'", a.get("href") or "")
        if m:
            pager.append(m.group(1))
    return out, pager


def group_press(rows: list[dict[str, str]], county: str, source: str = "press", url: str | None = None) -> list[Filing]:
    out: dict[str, Filing] = {}
    for r in rows:
        inst = r["instrument"]
        f = out.setdefault(inst, Filing(county=county, source=source, instrument=inst, doc_type=r.get("type") or "",
                                        recorded=_date(r.get("recorded")), town=r.get("town") or None, url=url))
        blk, lot = r.get("block") or "", r.get("lot") or ""
        if blk and blk.upper() != "N/A" and lot:
            f.block, f.lot = blk, lot
        for key, bucket in (("direct_party", f.defendants), ("indirect_party", f.plaintiffs)):
            v = clean(r.get(key))
            if v and v not in bucket:
                bucket.append(v)
    return list(out.values())


COLUMN_ALIASES = {
    "county": ("county",),
    "town": ("town", "town name", "municipality", "muni", "city"),
    "defendant": ("defendant", "defendants", "direct party", "grantor", "owner", "party", "name", "search name",
                  "mortgagor"),
    "plaintiff": ("plaintiff", "plaintiffs", "indirect party", "grantee", "complainant", "reverse party", "lender"),
    "recorded": ("recorded", "recorded date", "record date", "date recorded", "filing date", "date"),
    "instrument": ("instrument", "instrument #", "instrument number", "doc #", "document #", "document number",
                   "file #", "book/page", "cfn"),
    "doc_type": ("type", "doc type", "document type"),
    "block": ("block",), "lot": ("lot",), "qual": ("qual", "qualifier"),
    "address": ("address", "property address", "property location", "location", "street"),
    "docket": ("docket", "docket #", "docket number", "case", "case number"),
}


def _map_columns(header: list[str]) -> dict[str, int]:
    norm = [re.sub(r"\s+", " ", h.strip().lower().replace("_", " ")) for h in header]
    out: dict[str, int] = {}
    for key, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in norm and key not in out and norm.index(alias) not in out.values():
                out[key] = norm.index(alias)
                break
    return out


def parse_import_csv(text: str, county: str) -> list[Filing]:
    """Operator export (any column order; see COLUMN_ALIASES). One row per filing or per party."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return []
    cols = _map_columns(rows[0])
    if "defendant" not in cols and "address" not in cols and "block" not in cols:
        return []
    out: dict[str, Filing] = {}
    for n, r in enumerate(rows[1:], start=2):
        def get(k: str) -> str:
            i = cols.get(k)
            return clean(r[i]) if i is not None and i < len(r) else ""
        if not any(r):
            continue
        cty = (get("county") or county).title()
        inst = get("instrument") or f"row{n}:{get('docket')}:{get('defendant')}:{get('recorded')}"
        f = out.setdefault(f"{cty}|{inst}", Filing(
            county=cty, source="import", instrument=inst[:48], doc_type=get("doc_type") or "LIS PENDENS",
            recorded=_date(get("recorded")), town=get("town") or None, block=get("block") or None,
            lot=get("lot") or None, qual=get("qual") or None, address=get("address") or None,
            docket=get("docket") or None))
        for key, bucket in (("defendant", f.defendants), ("plaintiff", f.plaintiffs)):
            v = get(key)
            if v and v not in bucket:
                bucket.append(v)
    return list(out.values())


def parse_import_file(path: Path, county: str) -> list[Filing]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in (".csv", ".txt", ".tsv"):
        return parse_import_csv(text, county)
    if "NameDetails" in text:
        return [Filing(**{**f.__dict__, "source": "import"}) for f in parse_ucpa(text, county)]
    rows, _ = parse_press(text, county)
    return group_press(rows, county, source="import")


# -- name matching -----------------------------------------------------------------------------------
def name_tokens(name: str | None) -> frozenset[str]:
    s = re.sub(r"\(.*?ESTATE OF.*?\)", " ", (name or "").upper())
    toks = re.findall(r"[A-Z0-9]+", s)
    if is_entity(s):
        return frozenset(t for t in toks if t not in ENTITY_STOP)
    return frozenset(t for t in toks if len(t) > 1 and t not in NAME_STOP and not t.isdigit())


def names_match(a: frozenset[str], b: frozenset[str], entity: bool) -> bool:
    if not a or not b:
        return False
    if entity:
        return a == b
    return len(a & b) >= 2 and (a <= b or b <= a)


@dataclass
class OwnerIndex:
    """Owner names we hold, per county: property id, town base name, tokens, entity flag."""
    by_county: dict[str, list[tuple[int, str, frozenset[str], bool]]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def build(cls, session: Session, counties: set[str]) -> OwnerIndex:
        idx = cls()
        seen: set[tuple[int, frozenset[str]]] = set()

        def add(pid: int, county: str, town: str | None, name: str | None) -> None:
            toks = name_tokens(name)
            if len(toks) < (1 if is_entity((name or "").upper()) else 2) or (pid, toks) in seen:
                return
            seen.add((pid, toks))
            idx.by_county[county].append((pid, muni_base(town), toks, is_entity((name or "").upper())))
        props = session.execute(select(Property.id, Property.county, Property.municipality, Property.owner_name)
                                .where(Property.county.in_(counties))).all()
        towns = {pid: (cty, town) for pid, cty, town, _ in props}
        for pid, cty, town, owner in props:
            for part in re.split(r"\s*(?:&|;| AND )\s*", owner or ""):
                add(pid, cty, town, part)
        for pid, name in session.execute(
                select(Lead.property_id, Contact.name).join(Lead, Contact.lead_id == Lead.id).where(
                    Contact.contact_type.in_((ContactType.OWNER, ContactType.HEIR_EXECUTOR)))).all():
            if pid in towns:
                add(pid, towns[pid][0], towns[pid][1], name)
        for pid, defendant in session.execute(
                select(DistressSignal.property_id, DistressSignal.defendant).where(
                    DistressSignal.signal_type == SignalType.SHERIFF_SALE,
                    DistressSignal.property_id.in_(list(towns)))).all():
            if pid in towns:
                for part in re.split(r"\s*;\s*|\s*,\s*(?=[A-Z]+\s+[A-Z]+,)", defendant or ""):
                    add(pid, towns[pid][0], towns[pid][1], part)
        return idx

    def match(self, county: str, town: str | None, names: list[str]) -> tuple[int | None, str]:
        tb = muni_base(town) if town else None
        hits: set[int] = set()
        for name in names:
            if JUNIOR_DEFENDANT_RE.search(name):
                continue
            toks = name_tokens(name)
            entity = is_entity(name.upper())
            for pid, ptown, ptoks, pent in self.by_county.get(county, []):
                if tb and ptown and ptown != tb:
                    continue
                if entity == pent and names_match(toks, ptoks, entity):
                    hits.add(pid)
        if len(hits) == 1:
            return hits.pop(), "owner_name_town" if tb else "owner_name_county"
        return None, "ambiguous_name" if hits else "unmatched"


# -- storage -----------------------------------------------------------------------------------------
def store_filing(session: Session, f: Filing) -> ClerkFiling:
    row = session.scalar(select(ClerkFiling).where(ClerkFiling.county == f.county,
                                                   ClerkFiling.instrument == f.instrument))
    if row is None:
        row = ClerkFiling(county=f.county, instrument=f.instrument)
        session.add(row)
    row.source, row.doc_type, row.recorded_date = f.source, f.doc_type[:80], f.recorded
    row.defendants, row.plaintiffs = list(f.defendants), list(f.plaintiffs)
    for key in ("town", "block", "lot", "qual", "address", "docket", "url"):
        value = getattr(f, key)
        if value:
            setattr(row, key, value[:500] if key == "url" else value)
    row.is_discharge, row.estate, row.tax_lien = f.discharge, f.estate, f.tax_lien
    row.last_seen_at = utcnow()
    session.flush()
    return row


def _find_local(session: Session, county: str, town: str | None, block: str | None, lot: str | None,
                qual: str | None, address: str | None) -> Property | None:
    stmt = select(Property).where(Property.county == county)
    cands = [p for p in session.scalars(stmt) if not town or muni_base(p.municipality) == muni_base(town)
             or (p.city and muni_base(p.city) == muni_base(town))]
    if block and lot:
        b, lt, q = normalize_block_lot(block), normalize_block_lot(lot), (qual or "").strip().upper()
        exact = [p for p in cands if normalize_block_lot(p.block) == b and normalize_block_lot(p.lot) == lt
                 and (not q or (p.qual or "").strip().upper() == q)]
        if len(exact) == 1:
            return exact[0]
    if address:
        street = re.sub(r"[^A-Z0-9 ]", "", address.split(",")[0].upper())
        same = [p for p in cands if re.sub(r"[^A-Z0-9 ]", "", (p.street_address or "").upper()) == street]
        if len(same) == 1:
            return same[0]
    return None


def newark_lookup(http: PoliteSession, gis: NJGISClient, names: list[str]) -> tuple[dict | None, str | None]:
    """Defendant name → the one Newark parcel the City's ownership snapshot names, kept only if the deed book/page
    is unchanged in today's MOD-IV record (same rule as owner_records_agent's Newark snapshot)."""
    from agents.scrapers.owner_records_agent import NEWARK_2020
    hits: dict[tuple[str, str, str], dict] = {}
    for name in names:
        if JUNIOR_DEFENDANT_RE.search(name):
            continue
        toks = name_tokens(name)
        entity = is_entity(name.upper())
        if len(toks) < (1 if entity else 2):
            continue
        longest = max(toks, key=len)
        where = " AND ".join(f"OWNERSNAME LIKE '%{t.replace(chr(39), chr(39) * 2)}%'"
                             for t in sorted(toks, key=len, reverse=True)[:3])
        data = http.get_json(NEWARK_2020, params={"where": where, "returnGeometry": "false", "f": "json",
                                                  "outFields": "BLOCK,LOT,QCODE,OWNERSNAME,DEEDBOOK,DEEDPAGE",
                                                  "resultRecordCount": 25})
        for feat in data.get("features", []):
            a = feat["attributes"]
            if longest in (a.get("OWNERSNAME") or "").upper() and names_match(
                    toks, name_tokens(a.get("OWNERSNAME")), entity):
                hits[(normalize_block_lot(a.get("BLOCK")), normalize_block_lot(a.get("LOT")),
                      (a.get("QCODE") or "").strip().upper())] = a
    if len(hits) != 1:
        return None, "newark_ambiguous" if hits else None
    (block, lot, qual), a = next(iter(hits.items()))
    feature = gis.find_parcel("0714", block, lot, qual or None)
    if feature is None:
        return None, "newark_no_parcel"
    m = feature["attributes"]
    same_deed = (str(a.get("DEEDBOOK") or "").lstrip("0") == str(m.get("DEED_BOOK") or "").lstrip("0")
                 and str(a.get("DEEDPAGE") or "").lstrip("0") == str(m.get("DEED_PAGE") or "").lstrip("0")
                 and str(a.get("DEEDBOOK") or "").strip("0"))
    if not same_deed:
        return None, "newark_deed_changed"
    return {**feature, "owner_2020": clean(a.get("OWNERSNAME"))}, "newark_2020_owner"


def resolve_property(session: Session, f: Filing, owners: OwnerIndex, gis: NJGISClient | None,
                     http: PoliteSession | None = None) -> tuple[Property | None, str]:
    local = _find_local(session, f.county, f.town, f.block, f.lot, f.qual, f.address) \
        if (f.block and f.lot) or f.address else None
    if local is not None:
        return local, "block_lot" if f.block and f.lot else "address"
    if gis is not None and f.town and ((f.block and f.lot) or f.address):
        mun = gis.resolve_municipality(f.county, f.town)
        if mun is not None:
            feature = (gis.find_parcel(mun[1], f.block, f.lot, f.qual) if f.block and f.lot else
                       gis.find_by_address(f.county, f.address.split(",")[0], mun[1]))
            if feature is not None:
                prop, _ = upsert_property(session, feature_to_property(feature), source="njgin_modiv")
                return prop, "njgin_block_lot" if f.block and f.lot else "njgin_address"
    pid, how = owners.match(f.county, f.town, f.defendants)
    if pid is None and how != "ambiguous_name" and gis is not None and http is not None and f.county == "Essex" \
            and muni_base(f.town) == "NEWARK":
        feature, why = newark_lookup(http, gis, f.defendants)
        if feature is not None:
            prop, _ = upsert_property(session, feature_to_property(feature), source="njgin_modiv")
            if not prop.owner_name:
                prop.owner_name, prop.owner_name_source = feature["owner_2020"], "newark_2020_snapshot"
            return prop, why
        how = why or how
    return (session.get(Property, pid) if pid else None), how


def apply_filing(session: Session, row: ClerkFiling, prop: Property) -> tuple[bool, bool]:
    """LIS_PENDENS (+ ESTATE_OWNER for an estate defendant) on the property; returns (new signal, new lead)."""
    sev = 0.6 + (0.15 if row.tax_lien else 0) + (0.1 if row.estate else 0)
    meta = {"doc_type": row.doc_type, "instrument": row.instrument, "town": row.town, "estate": row.estate,
            "tax_lien_foreclosure": row.tax_lien, "match": row.match_method, "defendants": row.defendants[:6],
            "plaintiffs": row.plaintiffs[:4]}
    _, new = upsert_signal(
        session, prop, signal_type=SignalType.LIS_PENDENS, source=f"clerk_{row.county.lower()}",
        source_ref=f"{row.county}:{row.instrument}", severity_weight=round(min(sev, 1.0), 2),
        filing_date=row.recorded_date, defendant="; ".join(row.defendants)[:300] or None,
        plaintiff_or_lienholder="; ".join(row.plaintiffs)[:300] or None, case_number=row.docket,
        status=("tax-lien foreclosure filed" if row.tax_lien else "foreclosure complaint filed"),
        source_url=row.url, raw_metadata_json=meta, active=True)
    if row.estate:
        upsert_signal(session, prop, signal_type=SignalType.ESTATE_OWNER, source=f"clerk_{row.county.lower()}",
                      source_ref=f"{row.county}:{row.instrument}:estate", severity_weight=0.7,
                      defendant=next((d for d in row.defendants if is_estate(d) or "ESTATE" in d.upper()), None),
                      status="estate named as foreclosure defendant", source_url=row.url,
                      raw_metadata_json={"instrument": row.instrument}, active=True)
    owner = row.defendants[0] if row.defendants else None
    _, lead_new = ensure_lead(session, prop, primary_source="lis_pendens", contact_name=owner)
    return new, lead_new


def close_discharged(session: Session, row: ClerkFiling) -> int:
    """A discharge ends the lis pendens: deactivate the earlier filing's signal for the same parties."""
    keys = {name_tokens(d) for d in row.defendants}
    plaint = {name_tokens(p) for p in row.plaintiffs}
    closed = 0
    for prior in session.scalars(select(ClerkFiling).where(
            ClerkFiling.county == row.county, ClerkFiling.is_discharge.is_(False),
            ClerkFiling.property_id.is_not(None))):
        if not ({name_tokens(d) for d in prior.defendants} & keys) or (plaint and not
                                                                       ({name_tokens(p) for p in prior.plaintiffs} & plaint)):
            continue
        sig = session.scalar(select(DistressSignal).where(
            DistressSignal.signal_type == SignalType.LIS_PENDENS, DistressSignal.source == f"clerk_{row.county.lower()}",
            DistressSignal.source_ref == f"{row.county}:{prior.instrument}"))
        if sig is not None and sig.active:
            sig.active, sig.status = False, f"discharged {row.recorded_date or ''} ({row.instrument})".strip()
            closed += 1
    return closed


# -- fetching ----------------------------------------------------------------------------------------
def _form_fields(form: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in form.find_all("input"):
        name, kind = i.get("name"), (i.get("type") or "text").lower()
        if name and kind not in ("submit", "button", "image", "checkbox", "radio"):
            out[name] = i.get("value") or ""
    for sel in form.find_all("select"):
        opt = sel.find("option", selected=True) or sel.find("option")
        if sel.get("name") and opt is not None:
            out[sel["name"]] = opt.get("value") or ""
    return out


def fetch_ucpa(http: PoliteSession, start: date, end: date, doc_type: str) -> str:
    http.get(f"{UCPA}/DocIndex", params={"s": "date"}, cache=False)          # session cookie
    return http.post(f"{UCPA}/DateResults", data={"dt": doc_type, "from": start.strftime("%m/%d/%Y"),
                                                  "to": end.strftime("%m/%d/%Y"), "rpp": "500", "pOpt": "1"},
                     cache=False).text


def fetch_press(http: PoliteSession, start: date, end: date, doc_type: str, max_pages: int = 8,
                url: str = PRESS, total: str = "750") -> list[str]:
    home = BeautifulSoup(http.get(url, cache=False).text, "html.parser")
    data = _form_fields(home.find("form"))
    p = PRESS_PREFIX
    data.update({p + "ddlMunTab2": "COUNTY WIDE", p + "ddlDocTypeTab2": doc_type,
                 p + "txtFromTab2": start.strftime("%m/%d/%Y"), p + "txtToTab2": end.strftime("%m/%d/%Y"),
                 p + "ddlShowRecTab2": "100", p + "ddlTotalRecTab2": total, p + "btnSearchTab2": "Search",
                 p + "hidDiv": "basic"})
    page = http.post(url, data=data, cache=False).text
    pages = [page]
    _, pager = parse_press(page, "Essex")
    for target in pager[:max_pages - 1]:
        form = BeautifulSoup(pages[-1], "html.parser").find("form")
        d2 = _form_fields(form)
        d2.pop(p + "btnSearchTab2", None)
        d2.update({"__EVENTTARGET": target, "__EVENTARGUMENT": ""})
        pages.append(http.post(url, data=d2, cache=False).text)
    return pages


class LisPendensAgent(BaseAgent):
    name = "lis_pendens"
    description = ("County-clerk lis pendens / foreclosure filings (Union UCPA + Essex PRESS live; other counties from "
                   "saved pages / CSV in data/imports/lis_pendens/) → LIS_PENDENS signals by parcel or owner name.")

    def run(self, ctx: AgentContext) -> None:
        counties = set(self.options.get("counties") or settings().markets.get("counties", []))
        days = int(self.options.get("days", 14))
        end = date.today()
        start = end - timedelta(days=days)
        filings: list[Filing] = []
        if not self.options.get("import_only"):
            http = PoliteSession(min_interval=3.0, cache_ttl_hours=0)
            if "Union" in counties:
                for dt, label in UCPA_TYPES.items():
                    try:
                        got = parse_ucpa(fetch_ucpa(http, start, end, dt))
                        ctx.bump(f"union_{label.lower().replace(' ', '_')}", len(got))
                        filings += got
                    except FetchError as exc:
                        ctx.error(f"Union UCPA {label}: {exc}")
            for county, site in PRESS_SITES.items():
                if county not in counties:
                    continue
                for dt, label in site["types"].items():
                    try:
                        pages = fetch_press(http, start, end, dt, url=site["url"], total=site["total"])
                        rows = [r for page in pages for r in parse_press(page, county)[0]]
                        got = group_press(rows, county, url=site["url"])
                        ctx.bump(f"{county.lower()}_{dt.lower()}", len(got))
                        filings += got
                    except FetchError as exc:
                        ctx.error(f"{county} PRESS {label}: {exc}")
            ctx.metrics["clerk_http"] = http.stats_summary()
        filings += self._imports(ctx, Path(self.options.get("imports") or IMPORT_DIR))
        rows = [store_filing(ctx.session, f) for f in filings]
        ctx.records_processed += len(rows)
        self.match_all(ctx, counties | {r.county for r in rows})
        ctx.note("Manual counties (save results into data/imports/lis_pendens/<county>/): "
                 + "; ".join(f"{c}: {why}" for c, why in MANUAL.items() if c in counties))

    def _imports(self, ctx: AgentContext, root: Path) -> list[Filing]:
        out: list[Filing] = []
        if not root.exists():
            return out
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in (".csv", ".tsv", ".txt", ".html", ".htm"):
                continue
            county = (path.parent.name if path.parent != root else "").title() or "Unknown"
            got = parse_import_file(path, county)
            ctx.bump("imported_filings", len(got))
            out += got
        return out

    def match_all(self, ctx: AgentContext, counties: set[str]) -> None:
        gis = None if self.options.get("offline") else NJGISClient()
        http = None if self.options.get("offline") else PoliteSession(min_interval=1.5, cache_ttl_hours=24)
        owners = OwnerIndex.build(ctx.session, counties)
        for row in ctx.session.scalars(select(ClerkFiling).where(ClerkFiling.county.in_(counties))):
            if row.is_discharge:
                ctx.bump("discharges_closed", close_discharged(ctx.session, row))
                continue
            f = Filing(county=row.county, source=row.source, instrument=row.instrument, doc_type=row.doc_type,
                       recorded=row.recorded_date, defendants=list(row.defendants or []),
                       plaintiffs=list(row.plaintiffs or []), town=row.town, block=row.block, lot=row.lot,
                       qual=row.qual, address=row.address, docket=row.docket, url=row.url)
            prop = ctx.session.get(Property, row.property_id) if row.property_id else None
            if prop is None:
                try:
                    prop, how = resolve_property(ctx.session, f, owners, gis, http)
                except FetchError as exc:
                    ctx.error(f"NJGIN lookup for {row.county} {row.instrument}: {exc}")
                    prop, how = None, "lookup_failed"
                row.match_method = how
                if prop is None:
                    ctx.bump(f"filings_{how}")
                    continue
                row.property_id = prop.id
            if prop.property_class not in (None, "2"):
                ctx.bump("non_residential_skipped")
                continue
            new, lead_new = apply_filing(ctx.session, row, prop)
            ctx.bump("signals_new" if new else "signals_seen")
            if lead_new:
                ctx.new_leads_found += 1
        ctx.checkpoint()
