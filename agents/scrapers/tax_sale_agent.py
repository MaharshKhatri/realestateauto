"""Tax-sale agent — municipal tax lien sales on RealAuction (`<town>.newjerseytaxsale.com`).

Verified 2026-10-26 (docs/recon/tax_sale_clerk_opendata.md §1.1): 91 municipal sites in the 7 target
counties; N.J.A.C. 5:33-1.1(d)(1) requires these line items to be public and free; robots.txt allows
`/index.cfm` and `/popup/`.

Two lists per town:
  * Preview ("items for sale") — owner is delinquent NOW (taxes, water, sewer) and can still pay before
    the sale date → TAX_DELINQUENT with the sale date as the deadline. Rows carry the owner name.
  * Results ("sold") — a tax sale certificate now sits on title → TAX_SALE_CERTIFICATE. A third-party
    holder may file to foreclose 2 years after the sale; the owner can redeem until final judgment.

Rows are keyed by (host, tax year, cert id), grouped per parcel, matched to NJGIN parcels in batches
(one query per ~40 blocks), and only residential (class 2) parcels become leads.

ACCESS (2026-10-26): the sites' firewall returns 403 to every non-browser client (our identified
User-Agent, curl, python-requests). We do not impersonate a browser to get around that, so the
agent works in two modes:
  * live  — one honest request; if it is refused, live mode is switched off for the run;
  * import (default fallback) — pages the operator saved from a browser
    (`data/imports/tax_sales/<Town>/*.html`: the "Preview Items", "Results" and "Bidding Rules" pages)
    are parsed exactly like live pages. Also recommended: an OPRA request to each tax collector for
    the tax sale list as a spreadsheet (free; see docs/nj_data_sources.md §6).

Run: python -m agents.scrapers.tax_sale_agent [--town EastOrange] [--county Essex] [--discover]
"""
from __future__ import annotations

import argparse
import html
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.base import AgentContext, BaseAgent
from agents.scrapers.njgis_client import NJGISClient, feature_to_property
from core.config import CONFIG_DIR, DATA_DIR, settings
from core.dates import parse_us_date
from core.http import FetchError, PoliteSession
from db.models import Property, SignalType
from db.repository import ensure_lead, normalize_block_lot, upsert_property, upsert_signal

log = logging.getLogger(__name__)
SOURCE = "realauction"
SITES_FILE = CONFIG_DIR / "sources" / "realauction_nj.yaml"
IMPORT_DIR = DATA_DIR / "imports" / "tax_sales"
ROW_RE = re.compile(r'<tr class="(?:highlightRow|evenRow|oddRow)[^"]*" id="cert(\d+)">(.*?)</tr>', re.S)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
PAGES_RE = re.compile(r"of (\d+) \(Displaying \d+ of ([\d,]+) records\)")
SALE_DATE_RE = re.compile(r"held via the Internet\s*(?:on\s*)?(\d{1,2}/\d{1,2}/\d{4})", re.I)
CHARGE_TYPES = {"T": "taxes", "W": "water", "S": "sewer", "O": "other"}


@dataclass
class LienRow:
    cert_id: str
    adv: str
    tax_year: str
    block: str
    lot: str
    qual: str
    owner: str | None
    charge_type: str | None
    face: float
    status: str | None = None
    winning_bid: str | None = None
    bidder: str | None = None


@dataclass
class ParcelLiens:
    block: str
    lot: str
    qual: str
    rows: list[LienRow] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(sum(r.face for r in self.rows), 2)

    @property
    def owner(self) -> str | None:
        return next((r.owner for r in self.rows if r.owner), None)

    @property
    def charge_types(self) -> list[str]:
        return sorted({CHARGE_TYPES.get(c, c) for r in self.rows for c in (r.charge_type or "").split()})


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _money(s: str) -> float:
    try:
        return float(re.sub(r"[^\d.]", "", s) or 0)
    except ValueError:
        return 0.0


def parse_block_lot(text: str) -> tuple[str, str, str] | None:
    """'12 - 2.06' -> ('12','2.06',''); '8 - 52 - C001' -> ('8','52','C001')."""
    parts = [p.strip() for p in (text or "").split(" - ")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1], parts[2] if len(parts) > 2 else ""


def parse_rows(page_html: str, kind: str) -> list[LienRow]:
    out: list[LienRow] = []
    for cert_id, body in ROW_RE.findall(page_html):
        tds = [_clean(x) for x in TD_RE.findall(body)]
        if kind == "preview" and len(tds) >= 7:
            bl = parse_block_lot(tds[2])
            if bl:
                out.append(LienRow(cert_id, tds[0], tds[1], *bl, owner=tds[3] or None, charge_type=tds[4] or None,
                                   face=_money(tds[5]), status=tds[6] or None))
        elif kind == "results" and len(tds) >= 7:
            bl = parse_block_lot(tds[3])
            if bl:
                out.append(LienRow(cert_id, tds[0], tds[1], *bl, owner=None, charge_type=None,
                                   face=_money(tds[4]), status="Sold", winning_bid=tds[5] or None, bidder=tds[6] or None))
    return out


def page_count(page_html: str) -> int:
    m = PAGES_RE.search(page_html)
    return int(m.group(1)) if m else 1


def parse_sale_date(rules_html: str) -> date | None:
    m = SALE_DATE_RE.search(_clean(rules_html))
    return parse_us_date(m.group(1)) if m else None


def page_kind(page_html: str) -> str | None:
    """Classify a saved RealAuction page: preview list, results list, or bidding rules."""
    text = _clean(page_html[:200000])
    if ROW_RE.search(page_html):
        return "preview" if "Owner Name" in text else "results"
    if SALE_DATE_RE.search(text):
        return "rules"
    return None


def load_sites(counties: list[str] | None = None, towns: list[str] | None = None) -> list[dict]:
    sites = (yaml.safe_load(SITES_FILE.read_text()) or {}).get("sites", [])
    if counties:
        wanted = {c.lower() for c in counties}
        sites = [s for s in sites if s["county"].lower() in wanted]
    if towns:
        wanted_t = {t.lower().replace(" ", "") for t in towns}
        sites = [s for s in sites if s["town"].lower().replace(" ", "") in wanted_t
                 or s["host"].split(".")[0].lower() in wanted_t]
    return sites


def local_parcels(session: Session, mun_code: str, keys: list[tuple[str, str, str]]) -> dict[tuple[str, str, str], Property]:
    """Existing properties in this municipality keyed like group_by_parcel: (block, lot, qualifier)."""
    blocks = {k[0] for k in keys}
    out: dict[tuple[str, str, str], Property] = {}
    if not blocks:
        return out
    for prop in session.scalars(select(Property).where(Property.mun_code == mun_code)):
        key = (normalize_block_lot(prop.block), normalize_block_lot(prop.lot), (prop.qual or "").strip().upper())
        if key[0] in blocks:
            out.setdefault(key, prop)
            out.setdefault((key[0], key[1], ""), prop)
    return {k: v for k, v in out.items() if k in set(keys) or (k[0], k[1]) in {(a, b) for a, b, _ in keys}}


def group_by_parcel(rows: list[LienRow]) -> dict[tuple[str, str, str], ParcelLiens]:
    groups: dict[tuple[str, str, str], ParcelLiens] = {}
    for r in rows:
        key = (normalize_block_lot(r.block), normalize_block_lot(r.lot), r.qual.upper())
        groups.setdefault(key, ParcelLiens(*key)).rows.append(r)
    return groups


class TaxSaleAgent(BaseAgent):
    name = "tax_sales"
    description = "Municipal tax lien sale lists (RealAuction): delinquent owners and sold certificates."

    def run(self, ctx: AgentContext) -> None:
        counties = self.options.get("counties") or settings().markets.get("counties", [])
        sites = load_sites(counties, self.options.get("towns"))
        self.min_face = float(self.options.get("min_face", 250))
        self.results_max_age_days = int(self.options.get("results_max_age_days", 760))
        http = PoliteSession(min_interval=1.0, cache_ttl_hours=6)
        gis = NJGISClient()
        live = not self.options.get("import_only")
        imports = Path(self.options.get("imports") or IMPORT_DIR)
        for site in sites:
            pages: dict[str, list[str]] | None = None
            if live:
                pages = self._fetch_live(ctx, http, site)
                if pages is None:
                    live = False   # refused: never hammer a site that blocks us; fall back to imports
            if pages is None:
                pages = self._load_saved(site, imports)
            if not pages:
                continue
            self._process_site(ctx, gis, site, pages)
            ctx.checkpoint()
        ctx.metrics["realauction_http"] = http.stats_summary()
        ctx.metrics["live_access"] = live

    def _fetch_live(self, ctx: AgentContext, http: PoliteSession, site: dict) -> dict[str, list[str]] | None:
        host = site["host"].rstrip("/")
        try:
            rules = http.get(f"https://{host}/index.cfm",
                             params={"folder": "showDocument", "documentName": "biddingRules"}).text
        except FetchError as exc:
            if exc.status in (401, 403, 429):
                ctx.note(f"Live access refused by {host} (HTTP {exc.status}; the sites only serve browsers). "
                         f"Switched to importing operator-saved pages from {IMPORT_DIR} — see docs/nj_data_sources.md §6.")
                return None
            ctx.error(f"{host}: rules page failed: {exc}")
            return {}
        if "currently offline" in rules.lower():
            ctx.bump("sites_offline")
            return {}
        return {"rules": [rules], "preview": self._fetch_all(ctx, http, host, "previewItems"),
                "results": self._fetch_all(ctx, http, host, "auctionResults")}

    @staticmethod
    def _load_saved(site: dict, root: Path) -> dict[str, list[str]]:
        names = {site["host"].split(".")[0].lower(), site["town"].lower().replace(" ", "")}
        pages: dict[str, list[str]] = defaultdict(list)
        if not root.exists():
            return pages
        for folder in root.iterdir():
            if folder.is_dir() and folder.name.lower().replace(" ", "") in names:
                for f in sorted(folder.glob("*.htm*")):
                    text = f.read_text(encoding="utf-8", errors="replace")
                    kind = page_kind(text)
                    if kind:
                        pages[kind].append(text)
        return pages

    def _process_site(self, ctx: AgentContext, gis: NJGISClient, site: dict, pages: dict[str, list[str]]) -> None:
        today = date.today()
        sale_date = next((d for d in (parse_sale_date(t) for t in pages.get("rules", [])) if d), None)
        preview = [r for t in pages.get("preview", []) for r in parse_rows(t, "preview")]
        results = [r for t in pages.get("results", []) for r in parse_rows(t, "results")]
        if sale_date and (today - sale_date).days > self.results_max_age_days:
            results = []
        active = [r for r in preview if (r.status or "").lower() == "active" and r.face >= self.min_face]
        sold = [r for r in results if r.face >= self.min_face]
        ctx.metrics.setdefault("sites", {})[site["town"]] = {
            "sale_date": sale_date.isoformat() if sale_date else None,
            "preview_rows": len(preview), "active": len(active), "results_rows": len(results)}
        if not active and not sold:
            return
        mun = gis.resolve_municipality(site["county"], site["town"])
        if mun is None:
            mun = gis.resolve_municipality(site["county"], re.sub(r"(?<!^)(?=[A-Z])", " ", site["town"]))
        if mun is None:
            ctx.error(f"{site['town']} ({site['county']}): cannot resolve municipality in NJGIN")
            return
        self._store_site(ctx, gis, site, mun, sale_date, active, sold)

    def _fetch_all(self, ctx: AgentContext, http: PoliteSession, host: str, folder: str) -> list[str]:
        url = f"https://{host}/index.cfm?folder={folder}" + ("&mode=preview" if folder == "auctionResults" else "")
        texts: list[str] = []
        page, pages = 1, 1
        while page <= pages:
            try:
                text = http.post(url, data={"pageNum": page, "orderBy": "AdvNum", "orderDir": "asc"}).text
            except FetchError as exc:
                ctx.error(f"{host} {folder} p{page}: {exc}")
                break
            pages = page_count(text)
            texts.append(text)
            page += 1
        return texts

    def _store_site(self, ctx: AgentContext, gis: NJGISClient, site: dict, mun: tuple[str, str],
                    sale_date: date | None, active: list[LienRow], sold: list[LienRow]) -> None:
        mun_name, mun_code = mun
        delinquent = group_by_parcel(active)
        certs = group_by_parcel(sold)
        keys = list(delinquent) + list(certs)
        # Stack onto parcels we already track first (same municipality + block + lot + qualifier), so tax liens
        # land on existing sheriff-sale / absentee properties even when NJGIN is unreachable.
        local = local_parcels(ctx.session, mun_code, keys)
        ctx.bump("matched_existing_parcels", len(local))
        blocks = sorted({k[0] for k in keys if k not in local and (k[0], k[1], "") not in local})
        parcels: dict[tuple[str, str, str], dict] = {}
        for i in range(0, len(blocks), 40):
            chunk = ",".join("'" + b.replace("'", "''") + "'" for b in blocks[i:i + 40])
            for f in gis.query(f"PCL_MUN='{mun_code}' AND PCLBLOCK IN ({chunk})"):
                a = f["attributes"]
                key = (normalize_block_lot(a.get("PCLBLOCK")), normalize_block_lot(a.get("PCLLOT")),
                       (a.get("PCLQCODE") or "").strip().upper())
                parcels.setdefault(key, f)
        for kind, groups in (("delinquent", delinquent), ("certificate", certs)):
            for key, liens in groups.items():
                ctx.records_processed += 1
                prop = local.get(key) or local.get((key[0], key[1], ""))
                if prop is not None:
                    if prop.property_class not in (None, "2"):
                        ctx.bump("non_residential_skipped")
                        continue
                    self._store_parcel(ctx, site, None, kind, liens, sale_date, prop=prop)
                    continue
                feature = parcels.get(key) or parcels.get((key[0], key[1], ""))
                if feature is None:
                    ctx.bump("parcel_unmatched")
                    continue
                if feature["attributes"].get("PROP_CLASS") != "2":
                    ctx.bump("non_residential_skipped")
                    continue
                self._store_parcel(ctx, site, feature, kind, liens, sale_date)
        ctx.note(f"{site['town']}: {len(delinquent)} delinquent parcels (sale {sale_date}), {len(certs)} certificate parcels")

    def _store_parcel(self, ctx: AgentContext, site: dict, feature: dict | None, kind: str, liens: ParcelLiens,
                      sale_date: date | None, prop: Property | None = None) -> None:
        if prop is None:
            data = feature_to_property(feature)
            prop, created = upsert_property(ctx.session, data, source="njgin_modiv")
            if created:
                ctx.bump("properties_created")
        if liens.owner and not prop.owner_name:
            prop.owner_name, prop.owner_name_source = liens.owner, "tax_sale_list"
        today = date.today()
        first = liens.rows[0]
        meta: dict[str, Any] = {
            "town": site["town"], "host": site["host"], "sale_date": sale_date.isoformat() if sale_date else None,
            "total_face": liens.total, "charges": liens.charge_types, "tax_years": sorted({r.tax_year for r in liens.rows}),
            "cert_ids": [r.cert_id for r in liens.rows], "items": len(liens.rows),
        }
        if kind == "delinquent":
            upcoming = sale_date if sale_date and sale_date >= today else None
            meta["owner_on_list"] = liens.owner
            sev = min(0.5 + liens.total / 20000, 1.0) if "taxes" in liens.charge_types else min(0.3 + liens.total / 20000, 0.8)
            _, new = upsert_signal(
                ctx.session, prop, signal_type=SignalType.TAX_DELINQUENT, source=SOURCE,
                source_ref=f"{site['host']}:{first.tax_year}:{prop.parcel_id}", severity_weight=round(sev, 3),
                scheduled_auction_date=upcoming, amount_owed=liens.total, plaintiff_or_lienholder=prop.municipality,
                status="on tax sale list" + (f" — sale {upcoming:%m/%d/%Y}" if upcoming else ""),
                source_url=f"https://{site['host']}/index.cfm?folder=previewItems", raw_metadata_json=meta, active=True)
        else:
            meta.update({"winning_bids": [r.winning_bid for r in liens.rows], "bidders": [r.bidder for r in liens.rows],
                         "foreclosure_filing_earliest": (sale_date + timedelta(days=730)).isoformat() if sale_date else None})
            _, new = upsert_signal(
                ctx.session, prop, signal_type=SignalType.TAX_SALE_CERTIFICATE, source=SOURCE,
                source_ref=f"{site['host']}:{first.tax_year}:{prop.parcel_id}", severity_weight=min(0.6 + liens.total / 25000, 1.0),
                filing_date=sale_date, amount_owed=liens.total,
                plaintiff_or_lienholder=f"certificate holder (bidder {first.bidder})" if first.bidder else "certificate holder",
                status="certificate sold — redeemable until final judgment",
                source_url=f"https://{site['host']}/index.cfm?folder=auctionResults&mode=preview",
                raw_metadata_json=meta, active=True)
        if new:
            ctx.bump(f"signals_{kind}")
        _, lead_created = ensure_lead(ctx.session, prop, primary_source="tax_sale" if kind == "delinquent" else "tax_lien",
                                      contact_name=liens.owner)
        if lead_created:
            ctx.new_leads_found += 1


def discover_sites(counties: list[str]) -> list[dict]:
    """Refresh the NJ site directory from RealAuction's jump menu and map towns to counties via NJGIN."""
    http = PoliteSession(min_interval=1.0, cache_ttl_hours=0)
    text = http.post("https://manalapan.newjerseytaxsale.com/jumpMenu/flyoutmenu.cfm",
                     data={"state": "NJ", "auctionsToList": "all"}, cache=False).text
    found = re.findall(r'aria-label="([^"]+)"[^>]*?url="//([^"/]+)', text)
    gis = NJGISClient()
    sites = []
    for town, host in found:
        for county in counties:
            name = re.sub(r"\s+(Twp|Township|City|Borough|Boro|Town|Village)$", "", town, flags=re.I)
            if gis.resolve_municipality(county, name):
                sites.append({"county": county, "town": town, "host": host})
                break
    return sites


def main() -> None:
    parser = argparse.ArgumentParser(description="NJ tax lien sale lists (RealAuction)")
    parser.add_argument("--town", action="append", help="town or host prefix, e.g. EastOrange (repeatable)")
    parser.add_argument("--county", action="append")
    parser.add_argument("--discover", action="store_true", help="refresh config/sources/realauction_nj.yaml")
    parser.add_argument("--import-only", action="store_true", help="only parse saved pages in data/imports/tax_sales")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.discover:
        counties = args.county or settings().markets.get("counties", [])
        sites = discover_sites(counties)
        doc = yaml.safe_load(SITES_FILE.read_text())
        doc["sites"] = sorted(sites, key=lambda s: (s["county"], s["town"]))
        SITES_FILE.write_text(yaml.safe_dump(doc, sort_keys=False, width=120))
        print(f"{len(sites)} sites written to {SITES_FILE}")
        return
    run = TaxSaleAgent(towns=args.town, counties=args.county, import_only=args.import_only).execute()
    print(f"{run.agent_name}: {run.status} processed={run.records_processed} new_leads={run.new_leads_found} errors={run.error_count}")
    for n in (run.self_improvement_notes or "").splitlines():
        print("  ", n)
    for e in (run.errors or [])[:10]:
        print("  error:", e)


if __name__ == "__main__":
    main()
