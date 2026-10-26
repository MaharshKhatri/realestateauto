"""Sheriff Sale agent — every upcoming NJ foreclosure auction on CivilView SalesWeb.

Source: https://salesweb.civilview.com/Sales/SalesSearch?countyId=N (verified live 2026-10-26).
List pages give sheriff #, sale date, plaintiff, defendant, address (+ status in Middlesex).
Detail pages (need the list page's session cookie) give the court docket, upset/judgment amount,
block/lot, occupancy and the status history (adjournments).

Each listing is matched to its NJGIN MOD-IV parcel (municipality + block/lot, falling back to
street address) so the lead carries assessed value, taxes, year built, last sale and absentee
status. Run standalone:  python -m agents.scrapers.sheriff_sale_agent [--county Essex] [--limit 50]
"""
from __future__ import annotations

import argparse
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup
from sqlalchemy import select

from agents.base import AgentContext, BaseAgent
from agents.scrapers.njgis_client import NJGISClient, feature_to_property
from core.dates import parse_us_date
from core.http import FetchError, PoliteSession
from core.text import clean, normalize_street, parse_one_line_address, strip_et_al
from db.models import DistressSignal, SignalType, utcnow
from db.repository import ensure_lead, upsert_property, upsert_signal

log = logging.getLogger(__name__)

BASE = "https://salesweb.civilview.com"
SOURCE = "civilview"

# CivilView county ids for NJ (verified from the SalesWeb index page).
NJ_COUNTY_IDS: dict[str, int] = {
    "Middlesex": 73, "Union": 15, "Essex": 2, "Hudson": 10, "Passaic": 17, "Bergen": 7,
    "Camden": 1, "Atlantic": 25, "Burlington": 3, "Cape May": 52, "Cumberland": 6,
    "Gloucester": 19, "Hunterdon": 32, "Monmouth": 8, "Morris": 9, "Ocean": 85, "Salem": 20,
}

CLOSED_STATUS = re.compile(
    r"PURCHASED|SOLD|CANCEL|REDEEMED|BIDDER PAID|SATISFIED|WITHDRAW|VACATED|DISMISSED BY PLAINTIFF|"
    r"PAID IN FULL|REINSTATED|DEED", re.I)
ON_HOLD_STATUS = re.compile(r"BANKRUPTCY(?! DISMISSED)|STAY", re.I)

LOT_BLOCK_PATTERNS = [
    re.compile(r"LOT(?:\(S\))?S?\s*:?\s*(?P<lot>[\dA-Z][\dA-Z.\-]*)\s*(?:FKA\s*[\dA-Z.\-]+\s*)?,?\s*(?:IN\s+)?"
               r"(?:TAX\s+)?BLOCK\s*:?\s*(?P<block>[\dA-Z][\dA-Z.\-]*)", re.I),
    re.compile(r"BLOCK\s*:?\s*(?P<block>[\dA-Z][\dA-Z.\-]*)\s*(?:FKA\s*[\dA-Z.\-]+\s*)?,?\s*(?:AND\s+)?"
               r"(?:TAX\s+)?LOT(?:\(S\))?S?\s*:?\s*(?P<lot>[\dA-Z][\dA-Z.\-]*)", re.I),
]
UPSET_IN_TEXT = re.compile(
    r"UPSET[^$]{0,90}\$\s*(?P<amt>\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?)", re.I)
DUE_IN_TEXT = re.compile(r"AMOUNT DUE[^$]{0,60}\$\s*(?P<amt>\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?)", re.I)
MUNI_IN_TEXT = re.compile(
    r"(?:LYING AND BEING IN|SITUATE (?:IN|AT))\s+(?:THE\s+)?(?:(?:TOWNSHIP|CITY|BOROUGH|TOWN|VILLAGE)\s+)?(?:OF\s+)?"
    r"(?P<muni>[A-Z][A-Z .'\-]+?)\s*,\s*(?:IN THE\s+)?COUNTY", re.I)


@dataclass
class SaleListing:
    county: str
    sheriff_number: str
    sale_date: date | None
    plaintiff: str
    defendant: str
    address: str
    status: str | None
    property_id: str | None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def detail_url(self) -> str | None:
        return f"{BASE}/Sales/SaleDetails?PropertyId={self.property_id}" if self.property_id else None


# -- parsing (pure functions; tested against saved real pages) -------------------------------------
def parse_list_page(html: str, county: str) -> list[SaleListing]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return []
    table = tables[-1]
    rows = table.find_all("tr")
    if not rows:
        return []
    header = [clean(c.get_text(" ")).lower() for c in rows[0].find_all(["th", "td"])]

    def col(name: str) -> int | None:
        for i, h in enumerate(header):
            if h.startswith(name):
                return i
        return None

    idx = {k: col(k) for k in ("sheriff", "status", "sales date", "plaintiff", "defendant", "address")}
    out: list[SaleListing] = []
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        def cell(key: str) -> str:
            i = idx.get(key)
            return clean(cells[i].get_text(" ")) if i is not None and i < len(cells) else ""

        link = row.find("a", href=re.compile(r"SaleDetails\?PropertyId=", re.I))
        pid = re.search(r"PropertyId=(\d+)", link["href"]).group(1) if link else None
        out.append(SaleListing(
            county=county, sheriff_number=cell("sheriff"), sale_date=parse_us_date(cell("sales date")),
            plaintiff=cell("plaintiff"), defendant=cell("defendant"), address=cell("address"),
            status=cell("status") or None, property_id=pid))
    return [s for s in out if s.sheriff_number]


def parse_money(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\$?\s*(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)", text)
    if not m:
        return None
    try:
        return float(re.sub(r"[,\s]", "", m.group(1)))
    except ValueError:
        return None


def extract_block_lot(*texts: str | None) -> tuple[str, str] | None:
    for text in texts:
        if not text:
            continue
        flat = clean(text.replace("\n", " "))
        for pat in LOT_BLOCK_PATTERNS:
            m = pat.search(flat)
            if m:
                return m.group("block").rstrip(".,-"), m.group("lot").rstrip(".,-")
    return None


def parse_detail_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    for item in soup.select(".sale-detail-item"):
        label = item.select_one(".sale-detail-label")
        value = item.select_one(".sale-detail-value")
        if label is None:
            continue
        key = clean(label.get_text()).rstrip(":").rstrip("*").strip().lower()
        fields[key] = value.get_text("\n", strip=True) if value else ""
    if not fields:
        return {}

    history: list[tuple[str, str]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows or "status" not in clean(rows[0].get_text(" ")).lower():
            continue
        for r in rows[1:]:
            cells = [clean(c.get_text(" ")) for c in r.find_all(["td", "th"])]
            if len(cells) >= 2 and cells[0]:
                history.append((cells[0], cells[1]))

    description = fields.get("description", "")
    note = fields.get("property note", "")
    all_text = "\n".join([description, note, fields.get("parcel #", "")])

    upset = None
    for key in ("approx. upset", "good faith upset", "upset", "minimum bid"):
        upset = upset or parse_money(fields.get(key))
    if upset is None:
        m = UPSET_IN_TEXT.search(all_text.replace("\n", " "))
        if m:
            upset = parse_money(m.group("amt"))
    judgment = parse_money(fields.get("judgment")) or parse_money(fields.get("approx. judgment"))
    if judgment is None:
        m = DUE_IN_TEXT.search(all_text.replace("\n", " "))
        if m:
            judgment = parse_money(m.group("amt"))

    occ_text = all_text.upper()
    occupancy = None
    m = re.search(r"OCCUP(?:ANCY|IED)[^A-Z]{0,12}(?:STATUS)?\s*:?\s*(?:BY\s+)?(OCCUPIED|VACANT|UNKNOWN|UNKOWN|TENANT|OWNER)", occ_text)
    if m:
        occupancy = {"UNKOWN": "UNKNOWN"}.get(m.group(1), m.group(1))
    elif "VACANT" in occ_text:
        occupancy = "VACANT"

    muni = None
    m = MUNI_IN_TEXT.search(clean(description.replace("\n", " ")))
    if m:
        muni = clean(m.group("muni")).upper()

    adjournments = [h for h in history if re.search(r"ADJ", h[0], re.I)]
    return {
        "fields": fields,
        "court_case": fields.get("court case #"),
        "sale_date": parse_us_date(fields.get("sales date") or fields.get("sale date") or ""),
        "address": clean((fields.get("address") or fields.get("property address") or "").replace("\n", " ")),
        "upset_amount": upset,
        "judgment_amount": judgment,
        "block_lot": extract_block_lot(fields.get("parcel #"), description, note),
        "occupancy": occupancy,
        "municipality_hint": muni,
        "attorney": fields.get("attorney"),
        "attorney_phone": fields.get("attorney phone"),
        "status_history": history,
        "current_status": history[-1][0] if history else None,
        "adjournments_total": len(adjournments),
        "defendant_adjournments": len([h for h in adjournments if re.search(r"DEF", h[0], re.I)]),
        "plaintiff_adjournments": len([h for h in adjournments if re.search(r"PLAINT|PLTF", h[0], re.I)]),
    }


ESTATE_DEFENDANT = re.compile(
    r"\b(HEIRS|DEVISEES|ESTATE OF|EXECUTOR|EXECUTRIX|ADMINISTRAT(?:OR|RIX)|DECEASED|PERSONAL REPRESENTATIVE)\b", re.I)


def is_estate_defendant(defendant: str | None) -> bool:
    return bool(defendant and ESTATE_DEFENDANT.search(defendant))


def first_defendant(defendant: str) -> str:
    """'JULMALI HABIBUL; ET. AL.' -> 'JULMALI HABIBUL'; 'CLARK REINHARD A/K/A ...; NANCY' -> 'CLARK REINHARD';
    'DOROTHEA BRADLEY, HER HEIRS, BENEFICIARIES' -> 'DOROTHEA BRADLEY'."""
    s = re.sub(r"^(?:THE\s+)?(?:UNKNOWN\s+)?(?:HEIRS|DEVISEES)[^,;]*?\bOF\s+", "", defendant.strip(), flags=re.I)
    s = re.sub(r"^ESTATE OF\s+", "", s, flags=re.I)
    s = re.split(r",\s*(?:HIS|HER|THEIR)\s+HEIRS\b|,\s*(?:DECEASED|DEC'D)\b|,\s*(?:EXECUTOR|EXECUTRIX|ADMINISTRAT)", s, flags=re.I)[0]
    s = re.split(r";|\bA/K/A\b|\bAKA\b|\bF/K/A\b|\bAND\b|&", s, flags=re.I)[0]
    # Comma-separated party lists ("ROGER NELSON, CAVALRY SPV I LLC, ..."): keep the first party. A lone
    # surname before the first comma means "LAST, FIRST" order, so keep two segments.
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) > 1:
        first_is_full_name = len(parts[0].split()) >= 2
        s = parts[0] if first_is_full_name else f"{parts[0]}, {parts[1]}"
    s = re.sub(r",?\s*(ET\.?\s*ALS?\.?|EL\.?\s*AL\.?|ET UX|ETC\.?)$", "", s, flags=re.I)
    s = re.sub(r",?\s*(JR|SR|II|III|IV)\.?$", r" \1", s, flags=re.I)
    return clean(s.strip(" ,."))


def severity_for(sale_date: date | None, today: date | None = None) -> float:
    today = today or date.today()
    if sale_date is None:
        return 0.6
    days = (sale_date - today).days
    if days < 0:
        return 0.5
    if days <= 21:
        return 1.0
    if days <= 45:
        return 0.9
    if days <= 90:
        return 0.75
    return 0.6


# -- the agent ---------------------------------------------------------------------------------------
class SheriffSaleAgent(BaseAgent):
    name = "sheriff_sales"
    description = "Scrape every upcoming NJ sheriff sale on CivilView, match to MOD-IV parcels, create leads."

    def run(self, ctx: AgentContext) -> None:
        counties: list[str] = self.options.get("counties") or ctx.options.get("counties") or [
            "Middlesex", "Union", "Essex", "Hudson", "Passaic", "Bergen", "Camden"]
        limit = self.options.get("limit")
        horizon_days = int(self.options.get("horizon_days", 180))
        workers = int(self.options.get("workers", 4))
        run_started = utcnow()
        # CivilView sessions are county-scoped (the server remembers the last county list opened), so
        # each county gets its own cookie jar; the per-host throttle is shared, so politeness holds.
        sessions: dict[str, PoliteSession] = {}
        gis = NJGISClient()
        today = date.today()

        candidates: list[SaleListing] = []
        for county in counties:
            cid = NJ_COUNTY_IDS.get(county)
            if cid is None:
                ctx.error(f"No CivilView id for county {county}")
                continue
            civil_http = sessions[county] = PoliteSession(min_interval=1.0, cache_ttl_hours=10)
            try:
                resp = civil_http.get(f"{BASE}/Sales/SalesSearch", params={"countyId": cid}, cache=False)
            except FetchError as exc:
                ctx.error(f"{county}: list page failed: {exc}")
                continue
            listings = parse_list_page(resp.text, county)
            ctx.bump(f"listed_{county.lower()}", len(listings))
            closed = [l for l in listings if l.status and CLOSED_STATUS.search(l.status)]
            self._close_signals(ctx, closed)
            for l in listings:
                if l.status and CLOSED_STATUS.search(l.status):
                    continue
                if l.sale_date and (l.sale_date < today - timedelta(days=2) or
                                    l.sale_date > today + timedelta(days=horizon_days)):
                    continue
                candidates.append(l)
        candidates.sort(key=lambda l: l.sale_date or date.max)
        if limit:
            candidates = candidates[: int(limit)]
        ctx.note(f"{len(candidates)} open sheriff sales within {horizon_days} days across {len(counties)} counties")

        # Network work (detail page + parcel match) runs in a small thread pool; the polite
        # session serializes requests per host, so CivilView and ArcGIS calls overlap. DB writes
        # stay on this thread.
        def fetch(listing: SaleListing) -> tuple[SaleListing, dict | None, str | None]:
            try:
                detail = self._fetch_detail(sessions[listing.county], listing)
                listing.detail = detail
                feature = self._match_parcel(gis, listing)
                return listing, feature, None
            except Exception as exc:  # noqa: BLE001
                return listing, None, f"{listing.county} {listing.sheriff_number}: {type(exc).__name__}: {exc}"

        processed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch, l) for l in candidates]
            for fut in as_completed(futures):
                listing, feature, err = fut.result()
                if err:
                    ctx.error(err)
                    continue
                self._store(ctx, listing, feature)
                ctx.checkpoint()  # short transactions: other agents write to the same DB in parallel
                processed += 1
                if processed % 25 == 0:
                    log.info("sheriff_sales: stored %d/%d", processed, len(candidates))

        # Anything from these counties we didn't see this run and whose sale date passed is stale.
        stale = ctx.session.scalars(select(DistressSignal).where(
            DistressSignal.source == SOURCE, DistressSignal.active.is_(True),
            DistressSignal.last_seen_at < run_started,
            DistressSignal.scheduled_auction_date < today)).all()
        for sig in stale:
            if sig.raw_metadata_json.get("county") in counties:
                sig.active = False
                sig.status = sig.status or "no longer listed"
        ctx.metrics["stale_deactivated"] = len(stale)
        ctx.metrics["civilview_http"] = {c: h.stats_summary() for c, h in sessions.items()}
        ctx.metrics["details_missing"] = sum(1 for l in candidates if not l.detail)
        ctx.metrics["njgis_http"] = gis.http.stats_summary()

    # -- helpers --------------------------------------------------------------------------------
    def _fetch_detail(self, http: PoliteSession, listing: SaleListing) -> dict:
        if not listing.detail_url:
            return {}
        detail = parse_detail_page(http.get(listing.detail_url).text)
        if not detail:   # a cached redirect page, or the session lost its county: fetch fresh
            detail = parse_detail_page(http.get(listing.detail_url, cache=False).text)
        if not detail:   # session cookie expired: re-open the county list page, then retry once
            http.get(f"{BASE}/Sales/SalesSearch", params={"countyId": NJ_COUNTY_IDS[listing.county]}, cache=False)
            detail = parse_detail_page(http.get(listing.detail_url, cache=False).text)
        return detail

    def _match_parcel(self, gis: NJGISClient, listing: SaleListing) -> dict | None:
        d = listing.detail or {}
        address = d.get("address") or listing.address
        munis = list(gis.municipalities(listing.county).keys())
        city_names = sorted({w for m in munis for w in [m, re.sub(r"\s+(TWP|CITY|BORO|TOWN|VILLAGE)$", "", m)]},
                            key=len, reverse=True)
        parsed = parse_one_line_address(address, known_cities=city_names)
        mun = None
        for hint in (d.get("municipality_hint"), parsed.city if parsed else None):
            if hint:
                mun = gis.resolve_municipality(listing.county, hint)
                if mun:
                    break
        block_lot = d.get("block_lot")
        if mun and block_lot:
            feature = gis.find_parcel(mun[1], block_lot[0], block_lot[1])
            if feature:
                return feature
        street = parsed.street if parsed else address
        if street:
            feature = gis.find_by_address(listing.county, street, mun[1] if mun else None)
            if feature:
                return feature
        return None

    def _close_signals(self, ctx: AgentContext, closed: list[SaleListing]) -> None:
        for l in closed:
            sig = ctx.session.scalar(select(DistressSignal).where(
                DistressSignal.signal_type == SignalType.SHERIFF_SALE, DistressSignal.source == SOURCE,
                DistressSignal.source_ref == f"{l.county}:{l.sheriff_number}"))
            if sig and sig.active:
                sig.active = False
                sig.status = l.status
                ctx.records_updated += 1

    def _store(self, ctx: AgentContext, listing: SaleListing, feature: dict | None) -> None:
        d = listing.detail or {}
        address = d.get("address") or listing.address
        parsed = parse_one_line_address(address)
        if feature:
            data = feature_to_property(feature)
        else:
            street = parsed.street if parsed else address
            data = {
                "parcel_id": f"{listing.county.upper()}-UNMATCHED-{re.sub(r'[^A-Z0-9]+', '-', normalize_street(street))}",
                "county": listing.county,
                "street_address": street,
            }
            ctx.bump("parcel_unmatched")
        data["county"] = listing.county
        if parsed:
            data["city"] = parsed.city.title()
            data["zip_code"] = parsed.zip
        if not data.get("street_address") and parsed:
            data["street_address"] = parsed.street
        owner = first_defendant(listing.defendant)
        prop, created = upsert_property(ctx.session, data, source=SOURCE)
        if owner and (not prop.owner_name or prop.owner_name_source == "sheriff_defendant"):
            prop.owner_name = owner
            prop.owner_name_source = "sheriff_defendant"
        ctx.records_processed += 1
        if created:
            ctx.bump("properties_created")

        sale_date = d.get("sale_date") or listing.sale_date
        status = d.get("current_status") or listing.status or "Scheduled"
        on_hold = bool(ON_HOLD_STATUS.search(status or ""))
        meta = {
            "county": listing.county, "sheriff_number": listing.sheriff_number, "address": address,
            "upset_amount": d.get("upset_amount"), "judgment_amount": d.get("judgment_amount"),
            "occupancy": d.get("occupancy"), "status_history": d.get("status_history"),
            "adjournments_total": d.get("adjournments_total", 0),
            "defendant_adjournments": d.get("defendant_adjournments", 0),
            "plaintiff_adjournments": d.get("plaintiff_adjournments", 0),
            "attorney": d.get("attorney"), "attorney_phone": d.get("attorney_phone"),
            "block_lot": d.get("block_lot"), "on_hold": on_hold,
            "parcel_matched": feature is not None,
        }
        _, sig_created = upsert_signal(
            ctx.session, prop, signal_type=SignalType.SHERIFF_SALE, source=SOURCE,
            source_ref=f"{listing.county}:{listing.sheriff_number}",
            severity_weight=severity_for(sale_date), scheduled_auction_date=sale_date,
            amount_owed=d.get("upset_amount") or d.get("judgment_amount"),
            plaintiff_or_lienholder=listing.plaintiff[:300], defendant=listing.defendant[:300],
            case_number=d.get("court_case"), status=status, source_url=listing.detail_url,
            raw_metadata_json=meta, active=True)
        if sig_created:
            ctx.bump("signals_created")
        if d.get("occupancy") == "VACANT":
            upsert_signal(ctx.session, prop, signal_type=SignalType.VACANT, source=SOURCE,
                          source_ref=f"{listing.county}:{listing.sheriff_number}:vacant",
                          severity_weight=0.7, source_url=listing.detail_url,
                          raw_metadata_json={"evidence": "sheriff sale occupancy: VACANT"}, active=True)
        if is_estate_defendant(listing.defendant):
            prop.is_estate_owned = True
            upsert_signal(ctx.session, prop, signal_type=SignalType.ESTATE_OWNER, source=SOURCE,
                          source_ref=f"{listing.county}:{listing.sheriff_number}:estate",
                          severity_weight=0.8, source_url=listing.detail_url, defendant=listing.defendant[:300],
                          raw_metadata_json={"evidence": f"sheriff sale defendant: {listing.defendant}"},
                          active=True)
            ctx.bump("estate_foreclosures")
        _, lead_created = ensure_lead(ctx.session, prop, primary_source="sheriff_sale", contact_name=owner)
        if lead_created:
            ctx.new_leads_found += 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--county", action="append", help="County name (repeatable). Default: the 7 priority counties.")
    parser.add_argument("--limit", type=int, help="Max listings to process (nearest sale dates first)")
    parser.add_argument("--horizon-days", type=int, default=180)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = SheriffSaleAgent(counties=args.county, limit=args.limit, horizon_days=args.horizon_days).execute()
    print(f"{run.agent_name}: {run.status} processed={run.records_processed} new_leads={run.new_leads_found} "
          f"errors={run.error_count} metrics={ {k: v for k, v in run.metrics_json.items() if 'http' not in k} }")
    for e in (run.errors or [])[:10]:
        print("  error:", e)


if __name__ == "__main__":
    main()
