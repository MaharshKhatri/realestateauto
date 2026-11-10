"""Stale-listing distress agent: active NJ listings that aren't selling.

Source: homeharvest (Realtor.com listing data; see docs/recon/comps_listings.md — verified live
2026-10-26, terms-of-use caveat applies: low volume, cached, internal use only).

Flags active (not pending/contingent) residential listings in the harvest ZIPs that show:
  * `stale_listings.min_days_on_market` (60) days on market or more   → STALE_LISTING
  * a price reduction (where the MLS feed exposes original list price / reduced flag)
  * distress language in the description or tags: as-is, cash only, investor special, needs TLC,
    estate sale, handyman special, short sale, tenant occupied, fixer-upper, bank-owned
Each flagged listing is matched to its NJGIN parcel (for assessment/tenure data) and becomes a
lead whose contact is the listing agent — the play is a below-list cash/as-is offer.

Harvest ZIPs: `stale_listings.zips` in config/settings.yaml (DEFAULT_ZIPS below is only the fallback), plus the
ZIPs our own parcels show for each town the self-improvement agent added to `swarm_state.auto_expansion`.

Listings that leave the market leave outreach: a pending / contingent / sold status retires the STALE_LISTING
signal at once, and a listing not seen in the last `deactivate_after_missed_harvests` (3) successful harvests of
its ZIP is marked off_market (its signal retired, the stacker stops matching it). A refusal from the provider
(ProviderBlocked) is honored: the run stops asking, keeps every ZIP already harvested, notes it on the agent run,
and no listing request goes out until `blocked_cooldown_hours` have passed.

Run standalone: python -m agents.scrapers.stale_listing_distress_agent [--zip 07111 ...]
"""
from __future__ import annotations

import argparse
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from agents.base import AgentContext, BaseAgent
from agents.scrapers.njgis_client import NJGISClient, feature_to_property
from core.config import settings
from core.text import is_fax_label, normalize_phone, normalize_street
from db.models import DistressSignal, Listing, Property, SignalType, SwarmState, utcnow
from db.repository import ensure_lead, upsert_listing, upsert_property, upsert_signal

log = logging.getLogger(__name__)
SOURCE = "homeharvest"
OFF_MARKET = {"pending", "contingent", "sold", "off_market"}   # listing statuses that end outreach on it
HARVESTS_KEY = "stale_listing_harvests"      # swarm_state: per ZIP, the start times of its recent successful harvests
BLOCK_KEY = "listing_provider_blocked"       # swarm_state: the provider's last refusal and when we may ask again
ZIP_RE = re.compile(r"^\d{5}$")

# Fallback only: the harvest reads `stale_listings.zips` from config/settings.yaml.
DEFAULT_ZIPS = [
    # Middlesex: Woodbridge twp sections, Edison, New Brunswick, Perth Amboy
    "07095", "07001", "07067", "08830", "08863", "07064", "08817", "08820", "08837", "08901", "08861",
    # Union: Elizabeth, Plainfield, Linden
    "07201", "07202", "07206", "07208", "07060", "07062", "07063", "07036",
    # Essex: Newark, Irvington, East Orange
    "07102", "07103", "07104", "07105", "07106", "07107", "07108", "07112", "07114", "07111", "07017", "07018",
    # Hudson: Jersey City, Bayonne
    "07302", "07304", "07305", "07306", "07307", "07002",
    # Passaic: Paterson, Passaic
    "07501", "07502", "07503", "07504", "07505", "07513", "07514", "07522", "07524", "07055",
    # Bergen: Hackensack, Garfield
    "07601", "07026",
    # Camden: Camden, Pennsauken
    "08102", "08103", "08104", "08105", "08109", "08110",
    # Mercer: Trenton, Hamilton, Ewing
    "08608", "08609", "08610", "08611", "08618", "08619", "08620", "08628", "08629", "08638", "08690", "08691",
    # Monmouth: Asbury Park, Neptune, Long Branch, Freehold
    "07712", "07753", "07740", "07728",
]

RESIDENTIAL = {"single_family", "multi_family", "duplex_triplex", "townhomes", "condo_townhome",
               "condo_townhome_rowhome_coop", "condos", "condo"}

# Keyword -> (regex, weight). Context guards remove common false positives.
KEYWORDS: dict[str, tuple[str, float]] = {
    "as-is": (r"\b(?:sold|being sold|sell(?:ing)?|offered|purchase[d]?)\s+(?:in\s+)?(?:its\s+)?['\"]?as[\s-]is\b|\bas[\s-]is\s+(?:condition|sale|basis)\b|\bas[\s-]is\b", 1.0),
    "cash only": (r"\bcash\s+(?:only|buyers?\s+only|offers?\s+only)\b", 1.5),
    "investor special": (r"\binvestors?\s+(?:special|alert|opportunity|delight|dream)\b|\bcalling all investors\b|\bbring(?:ing)? (?:your|all) investors\b", 1.2),
    "needs tlc": (r"\bneeds?\s+(?:some\s+|a\s+little\s+|lots? of\s+)?(?:tlc|work|updating|renovation|rehab|repairs?)\b|\btlc\b", 1.0),
    "estate sale": (r"\bestate\s+sale\b|\bsold\s+by\s+(?:the\s+)?estate\b|\bexecutor\b", 1.2),
    "handyman special": (r"\bhandy\s?man(?:'s)?\s+(?:special|dream|delight)\b", 1.2),
    "short sale": (r"\bshort\s+sale\b", 1.3),
    "tenant occupied": (r"\btenant[\s-]+occupied\b|\btenants?\s+in\s+place\b", 0.6),
    "fixer upper": (r"\bfixer[\s-]?upper\b|\bgut\s+(?:rehab|renovation)\b|\btotal\s+rehab\b", 1.2),
    "bank owned": (r"\bbank[\s-]owned\b|\breo\b|\bforeclosure\b", 1.0),
    "motivated seller": (r"\bmotivated\s+sellers?\b|\bbring\s+(?:all\s+)?offers\b|\bmust\s+sell\b", 1.0),
}
_KW = {k: (re.compile(p, re.I), w) for k, (p, w) in KEYWORDS.items()}
NEGATIONS = re.compile(r"\b(?:no|not|isn'?t|without|never)\b[^.!?;]{0,20}$", re.I)


def find_keywords(text: str | None, tags: list[str] | None = None) -> list[str]:
    hits: list[str] = []
    if text:
        for name, (rx, _) in _KW.items():
            for m in rx.finditer(text):
                prefix = text[max(0, m.start() - 25):m.start()]
                if NEGATIONS.search(prefix):
                    continue  # "no need for TLC", "not a short sale"
                hits.append(name)
                break
    for tag in tags or []:
        if tag in ("fixer_upper",) and "fixer upper" not in hits:
            hits.append("fixer upper")
    return hits


def distress_score(dom: int | None, reduced: bool | None, keywords: list[str]) -> float:
    """0..1 severity for a listing."""
    s = 0.0
    if dom:
        s += min(dom / 365, 1.0) * 0.45 if dom >= 90 else 0.0
    if reduced:
        s += 0.15
    s += min(sum(_KW[k][1] for k in keywords if k in _KW) * 0.12, 0.45)
    return round(min(s, 1.0), 3)


def qualifies(row: dict, min_dom: int, max_price: float) -> tuple[bool, list[str]]:
    if (row.get("status") or "").lower() in OFF_MARKET:
        return False, []
    if (row.get("property_type") or "").lower() not in RESIDENTIAL:
        return False, []
    price = row.get("list_price") or 0
    if not price or price > max_price:
        return False, []
    kws = find_keywords(row.get("description"), (row.get("raw") or {}).get("tags"))
    stale = (row.get("days_on_market") or 0) >= min_dom
    return (stale or bool(kws) or bool(row.get("price_reduced") and (row.get("days_on_market") or 0) >= 45)), kws


# -- harvest plan -------------------------------------------------------------------------------------
def stale_cfg() -> dict[str, Any]:
    return dict(settings().get("stale_listings", {}) or {})


def _zip(value: Any) -> str | None:
    z = str(value or "").strip()[:5]
    return z if ZIP_RE.match(z) else None


def auto_expansion_zips(session: Session, cfg: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """ZIPs for each town in swarm_state.auto_expansion, from the parcels we already hold there (property ZIPs come
    from the Census ZCTA lookup). A ZIP counts when it holds `auto_expansion_min_parcels` (5) of the town's parcels
    and `auto_expansion_min_share` (10%) of them; at most `auto_expansion_max_zips` (4) per town. A town we hold no
    parcels for yet maps to [] (its ZIPs aren't known until the absentee harvest has reached it)."""
    cfg = cfg if cfg is not None else stale_cfg()
    min_n = int(cfg.get("auto_expansion_min_parcels", 5))
    min_share = float(cfg.get("auto_expansion_min_share", 0.10))
    most = int(cfg.get("auto_expansion_max_zips", 4))
    state = session.get(SwarmState, "auto_expansion")
    out: dict[str, list[str]] = {}
    for town in ((state.value_json or {}).get("municipalities", []) if state else []):
        county, name = str(town.get("county") or ""), str(town.get("name") or "")
        if not (county and name):
            continue
        rows = session.execute(select(Property.zip_code, func.count(Property.id)).where(
            func.upper(Property.county) == county.upper(), func.upper(Property.municipality) == name.upper(),
            Property.zip_code.is_not(None)).group_by(Property.zip_code)).all()
        total = sum(n for _, n in rows)
        ranked = sorted(((_zip(z), n) for z, n in rows if _zip(z)), key=lambda r: (-r[1], r[0]))
        out[f"{name} ({county.title()})"] = [z for z, n in ranked if n >= min_n and n >= min_share * total][:most]
    return out


def harvest_zips(session: Session, cfg: dict[str, Any] | None = None,
                 override: list[str] | None = None) -> tuple[list[str], list[str]]:
    """(ZIPs to harvest in order, notes): the configured list (else DEFAULT_ZIPS), then auto-expansion towns' ZIPs."""
    cfg = cfg if cfg is not None else stale_cfg()
    notes: list[str] = []
    if override:
        return list(dict.fromkeys(z for z in (_zip(v) for v in override) if z)), notes
    base = cfg.get("zips") or DEFAULT_ZIPS
    bad = [str(v) for v in base if not _zip(v)]
    if bad:
        notes.append(f"stale_listings.zips: ignored {len(bad)} entries that aren't 5-digit ZIPs "
                     f"({', '.join(bad[:5])}); quote every ZIP in settings.yaml")
    zips = list(dict.fromkeys(z for z in (_zip(v) for v in base) if z))
    if cfg.get("include_auto_expansion", True):
        added: list[str] = []
        for town, town_zips in auto_expansion_zips(session, cfg).items():
            new = [z for z in town_zips if z not in zips]
            zips += new
            added += new
            if not town_zips:
                notes.append(f"auto-expansion {town}: no parcels of ours there yet, so its ZIPs aren't known")
        if added:
            notes.append(f"auto-expansion towns added {len(added)} ZIPs: {', '.join(added)}")
    return zips, notes


# -- provider refusals --------------------------------------------------------------------------------
def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def active_block(session: Session, now: datetime | None = None) -> dict[str, Any] | None:
    """The provider's last refusal while its pause lasts (UTC ISO times), else None."""
    st = session.get(SwarmState, BLOCK_KEY)
    v = dict(st.value_json or {}) if st else {}
    try:
        until = datetime.fromisoformat(v["until"]) if v.get("until") else None
    except ValueError:
        until = None
    return v if until and until > (now or utcnow()) else None


def honor_block(session: Session) -> dict[str, Any] | None:
    """Carry a refusal recorded by another process into this one, so nothing here asks the provider either."""
    from engine import homeharvest_provider as hh
    b = active_block(session)
    if b:
        until = datetime.fromisoformat(b["until"])
        hh.mark_blocked(b.get("message") or "refused earlier", until=(until - datetime(1970, 1, 1)).total_seconds())
    return b


def record_block(session: Session, *, zip_code: str, message: str, hours: float, harvested: int,
                 not_asked: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    info = {"at": _iso(now), "until": _iso(now + timedelta(hours=hours)), "zip": zip_code,
            "message": str(message)[:300], "zips_harvested": harvested, "zips_not_asked": not_asked}
    st = session.get(SwarmState, BLOCK_KEY)
    if st is None:
        st = SwarmState(key=BLOCK_KEY, value_json={})
        session.add(st)
    st.value_json = info
    flag_modified(st, "value_json")
    st.updated_at = now
    return info


# -- harvest history (for "not seen in the last N harvests") -----------------------------------------
def _history(session: Session) -> SwarmState:
    st = session.get(SwarmState, HARVESTS_KEY)
    if st is None:
        st = SwarmState(key=HARVESTS_KEY, value_json={"zips": {}})
        session.add(st)
    return st


def record_harvest(state: SwarmState, zip_code: str, started: datetime, keep: int = 6) -> list[str]:
    zips = dict((state.value_json or {}).get("zips") or {})
    stamps = [*zips.get(zip_code, []), started.isoformat()][-keep:]
    zips[zip_code] = stamps
    state.value_json = {"zips": zips}
    flag_modified(state, "value_json")
    state.updated_at = utcnow()
    return stamps


def missed_cutoff(state: SwarmState, zip_code: str, harvests: int) -> datetime | None:
    """Start of the Nth most recent successful harvest of the ZIP: a listing last seen before it was missing from
    all N. None until the ZIP has N harvests on record."""
    stamps = ((state.value_json or {}).get("zips") or {}).get(zip_code) or []
    if harvests < 1 or len(stamps) < harvests:
        return None
    return datetime.fromisoformat(stamps[-harvests])


class StaleListingDistressAgent(BaseAgent):
    name = "stale_listings"
    description = "Active listings with 60+ DOM, price cuts, or distress language in the harvest ZIPs."

    def run(self, ctx: AgentContext) -> None:
        from engine import homeharvest_provider as hh  # optional dependency

        cfg = stale_cfg()
        zips, notes = harvest_zips(ctx.session, cfg, self.options.get("zips"))
        for n in notes:
            ctx.note(n)
        min_dom = int(cfg.get("min_days_on_market", 60))
        max_price = float(cfg.get("max_list_price", 900000))
        missed = int(cfg.get("deactivate_after_missed_harvests", 3))
        pause_hours = hh.pause_hours()
        ctx.metrics["zips_planned"] = len(zips)
        block = honor_block(ctx.session)
        if block:
            ctx.note(f"Listing provider refused us at {block.get('at')} UTC (ZIP {block.get('zip')}): no listing "
                     f"requests until {block.get('until')} UTC, so this run asked nothing. Stored listings and "
                     "leads stay as they are.")
            ctx.metrics["provider_blocked"] = {**block, "skipped_run": True}
            return
        gis = NJGISClient()
        harvested: list[str] = []
        for i, zip_code in enumerate(zips):
            started = utcnow()
            try:
                rows = hh.active_listings(zip_code)
            except hh.ProviderBlocked as exc:
                info = record_block(ctx.session, zip_code=zip_code, message=str(exc), hours=pause_hours,
                                    harvested=len(harvested), not_asked=len(zips) - i)
                hh.mark_blocked(str(exc), hours=pause_hours)
                ctx.metrics["provider_blocked"] = info
                ctx.note(f"Listing provider refused the request at ZIP {zip_code} ({exc}). Stopped politely: "
                         f"{len(harvested)} ZIPs harvested this run are kept, {len(zips) - i} were not asked, and "
                         f"nothing goes to the provider until {info['until']} UTC.")
                ctx.error(f"listing provider blocked at {zip_code}: {exc}; stopped asking until {info['until']} UTC")
                ctx.checkpoint()
                break
            except Exception as exc:  # noqa: BLE001
                ctx.error(f"{zip_code}: {type(exc).__name__}: {exc}")
                continue
            ctx.bump("listings_seen", len(rows))
            flagged = 0
            ctx.checkpoint()
            for row in rows:
                ok, kws = qualifies(row, min_dom, max_price)
                self._store_listing(ctx, row, kws)
                status = (row.get("status") or "").lower()
                if status in OFF_MARKET:
                    self._retire(ctx, _ref(row), f"listing is {status}", status)
                if not ok:
                    continue
                flagged += 1
                try:
                    self._store_lead(ctx, gis, row, kws, min_dom)
                    ctx.checkpoint()
                except Exception as exc:  # noqa: BLE001
                    ctx.session.rollback()
                    ctx.error(f"{row.get('address')} {zip_code}: {type(exc).__name__}: {exc}")
            if rows:
                # An empty answer is no proof that every listing left the market: only a harvest that returned
                # listings counts toward "not seen in the last N harvests".
                history = _history(ctx.session)
                record_harvest(history, zip_code, started)
                harvested.append(zip_code)
                self._sweep(ctx, zip_code, missed_cutoff(history, zip_code, missed), missed)
            ctx.metrics.setdefault("flagged_by_zip", {})[zip_code] = flagged
            ctx.checkpoint()
        ctx.metrics["zips_harvested"] = len(harvested)
        ctx.metrics["njgis_http"] = gis.http.stats_summary()

    def _sweep(self, ctx: AgentContext, zip_code: str, cutoff: datetime | None, missed: int) -> None:
        """Listings of this ZIP missing from its last `missed` successful harvests are off the market; a signal still
        active on a listing already stored as pending / contingent / sold (an older run) is retired too."""
        live_refs = select(DistressSignal.source_ref).where(
            DistressSignal.signal_type == SignalType.STALE_LISTING, DistressSignal.source == SOURCE,
            DistressSignal.active.is_(True))
        for ref, status in ctx.session.execute(select(Listing.source_ref, Listing.status).where(
                Listing.source == SOURCE, Listing.zip_code == zip_code, Listing.status.in_(sorted(OFF_MARKET)),
                Listing.source_ref.in_(live_refs))).all():
            self._retire(ctx, ref, f"listing is {status}", status)
        if cutoff is None:
            return
        gone = ctx.session.scalars(select(Listing).where(
            Listing.source == SOURCE, Listing.zip_code == zip_code, Listing.last_seen_at < cutoff,
            or_(Listing.status.is_(None), Listing.status.notin_(sorted(OFF_MARKET))))).all()
        for lst in gone:
            lst.raw_json = {**(lst.raw_json or {}), "status_before_off_market": lst.status,
                            "off_market_since": _iso(utcnow())}
            lst.status = "off_market"
            ctx.bump("listings_off_market")
            self._retire(ctx, lst.source_ref, f"not seen in the last {missed} harvests of {zip_code}", "off_market")

    def _retire(self, ctx: AgentContext, source_ref: str, reason: str, status: str) -> None:
        """The listing left the market: its STALE_LISTING signal stops counting (outreach, scoring, READY_TO_CALL)."""
        sig = ctx.session.scalar(select(DistressSignal).where(
            DistressSignal.signal_type == SignalType.STALE_LISTING, DistressSignal.source == SOURCE,
            DistressSignal.source_ref == source_ref, DistressSignal.active.is_(True)))
        if sig is None:
            return
        sig.active, sig.status = False, status
        sig.raw_metadata_json = {**(sig.raw_metadata_json or {}), "status": status,
                                 "deactivated": {"reason": reason, "at": _iso(utcnow())}}
        ctx.bump("stale_signals_deactivated")
        lead = sig.property.lead if sig.property else None
        if lead is not None:
            from engine.scoring import ready_to_call
            lead.ready_to_call = ready_to_call(lead)[0]

    def _store_listing(self, ctx: AgentContext, row: dict, kws: list[str]) -> None:
        upsert_listing(
            ctx.session, source=SOURCE, source_ref=_ref(row),
            url=row.get("url"), status=row.get("status"), address=row.get("address"), city=row.get("city"),
            zip_code=row.get("zip"), property_type=row.get("property_type"), beds=row.get("beds"),
            baths=row.get("baths"), sqft=row.get("sqft"), year_built=row.get("year_built"),
            list_price=row.get("list_price"), list_date=_d(row.get("list_date")),
            days_on_market=row.get("days_on_market"), price_reduced=row.get("price_reduced"),
            distress_keywords=kws, description=row.get("description"), lat=row.get("lat"), lon=row.get("lon"),
            raw_json={"original_list_price": row.get("original_list_price"), "mls": row.get("mls"),
                      "estimated_value": row.get("estimated_value"),
                      "lot_sqft": row.get("lot_sqft")})        # the listing's lot size (s13 teardown sheets)
        ctx.records_processed += 1

    def _store_lead(self, ctx: AgentContext, gis: NJGISClient, row: dict, kws: list[str], min_dom: int) -> None:
        raw = row.get("raw") or {}
        county = (((raw.get("location") or {}).get("county") or {}).get("name") or "").title()
        street = row.get("address") or ""
        feature = None
        if county and street:
            mun = gis.resolve_municipality(county, row.get("city") or "")
            feature = gis.find_by_address(county, street, mun[1] if mun else None)
            if feature is None and mun:
                feature = gis.find_by_address(county, street, None)
        if feature:
            data = feature_to_property(feature)
            far = _miles_apart(data, row)
            if far is not None and far > MAX_PARCEL_MILES:
                # Same street name in another town of the county ("22 Grand Ave", Iselin vs South River): a valuation
                # of that parcel would price a different house. Keep the listing, not the wrong parcel.
                ctx.bump("parcel_rejected_distance")
                ctx.note(f"{street}, {row.get('city')}: nearest NJGIN match is {far:.1f} mi away "
                         f"({data.get('municipality')}); not linked")
                feature = None
        if not feature:
            data = {"parcel_id": f"{(county or 'NJ').upper()}-UNMATCHED-{re.sub(r'[^A-Z0-9]+', '-', normalize_street(street))}-{row.get('zip')}",
                    "county": county or "Unknown", "street_address": street.upper()}
            if row.get("lat") is not None and row.get("lon") is not None:
                # The listing's own coordinates: without a point the house could never be valued (comps need one).
                data.update(lat=row["lat"], lon=row["lon"])
            ctx.bump("parcel_unmatched")
        data.update({"city": row.get("city"), "zip_code": row.get("zip")})
        for key in ("beds", "baths", "year_built"):
            if row.get(key) and not data.get(key):
                data[key] = row[key]
        prop, created = upsert_property(ctx.session, data, source=SOURCE)
        if row.get("sqft") and not prop.sqft:
            prop.sqft, prop.sqft_source = int(row["sqft"]), "listing"
        if created:
            ctx.bump("properties_created")
        agent = next(iter(raw.get("advertisers") or []), {}) or {}
        listed = [p for p in (agent.get("phones") or []) if p and p.get("number")]
        phones = [p for p in listed if not is_fax_label(p.get("type"))]       # a fax line is never the number to call
        fax = sorted({n for n in (normalize_phone(p["number"]) for p in listed if is_fax_label(p.get("type"))) if n})
        dom = row.get("days_on_market")
        meta: dict[str, Any] = {
            "url": row.get("url"), "list_price": row.get("list_price"), "list_date": row.get("list_date"),
            "days_on_market": dom, "price_reduced": row.get("price_reduced"),
            "original_list_price": row.get("original_list_price"), "keywords": kws,
            "listing_agent": agent.get("name"), "listing_broker": (agent.get("broker") or {}).get("name"),
            "listing_agent_phone": phones[0]["number"] if phones else None,
            "listing_agent_phone_type": phones[0].get("type") if phones else None,
            "listing_agent_email": agent.get("email"), "mls": row.get("mls"), "status": row.get("status"),
        }
        if fax:
            meta["listing_agent_fax"] = fax
        _, created_sig = upsert_signal(
            ctx.session, prop, signal_type=SignalType.STALE_LISTING, source=SOURCE, source_ref=_ref(row),
            severity_weight=distress_score(dom, row.get("price_reduced"), kws), status=row.get("status"),
            amount_owed=None, source_url=row.get("url"), raw_metadata_json=meta, active=True)
        if created_sig:
            ctx.bump("signals_created")
        _, lead_created = ensure_lead(ctx.session, prop, primary_source="stale_listing",
                                      contact_name=f"{agent.get('name')} (listing agent)" if agent.get("name") else None)
        if lead_created:
            ctx.new_leads_found += 1


MAX_PARCEL_MILES = 1.0            # a listing and its parcel farther apart than this are two different houses


def _miles_apart(parcel: dict, row: dict) -> float | None:
    from core.geo import haversine_miles
    try:
        if None in (parcel.get("lat"), parcel.get("lon"), row.get("lat"), row.get("lon")):
            return None
        return haversine_miles(float(parcel["lat"]), float(parcel["lon"]), float(row["lat"]), float(row["lon"]))
    except (TypeError, ValueError):
        return None


def _ref(row: dict) -> str:
    """The natural key a listing and its STALE_LISTING signal share."""
    return str(row.get("property_id") or row.get("url"))


def _d(value: str | None):
    from datetime import date
    return date.fromisoformat(value) if value else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Stale/distressed active listings")
    parser.add_argument("--zip", action="append",
                        help="ZIP code (repeatable). Default: stale_listings.zips + auto-expansion towns.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = StaleListingDistressAgent(zips=args.zip).execute()
    print(f"{run.agent_name}: {run.status} processed={run.records_processed} new_leads={run.new_leads_found} "
          f"errors={run.error_count} flagged={run.metrics_json.get('flagged_by_zip')}")
    for e in (run.errors or [])[:10]:
        print("  error:", e)


if __name__ == "__main__":
    main()
