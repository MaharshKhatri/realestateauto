"""S7's own listing pass: for-sale listings priced around New Jersey's graduated transfer-fee lines, with their agents.

Why it exists (operator, 2026-11-10): s7 found 0 listings because the stale-listing harvest stops at $900K
(`stale_listings.max_list_price`) and only makes leads of stale / distressed listings. The graduated percent fee
(P.L.2025, c.69) starts at $1M, so s7 needs its own pass over the price bands that hold the dead zones.

What it does, once per Eastern calendar day (swarm_state `s7_highvalue_harvest`):
- builds one price band per dead zone from s7's YAML-tier solver (`s7.dead_zones()`), from min($20K, 1% of the line)
  under the line to $50K past the zone's edge on a $10K grid (today: $990K-$1.07M, $1.98M-$2.08M, $2.48M-$2.57M,
  $2.98M-$3.07M, $3.48M-$3.57M), and merges bands less than $500K apart into one query range (the four upper bands
  become one $1.98M-$3.57M query), so each town costs two queries;
- asks homeharvest (`listing_type="for_sale"`, `price_min` / `price_max`) town by town (`TOWNS`, high-value towns in our
  counties plus the Morris / Somerset ones), the $1M band in every town first, through the provider's own throttled
  call (`homeharvest_provider._scrape`: one request at a time, `MIN_INTERVAL_S` apart, a refusal recorded and honored);
  answers are cached on disk for `harvest_cache_hours` (20) so a re-run the same day asks nothing again;
- stops at the first refusal (`ProviderBlocked`): the refusal is written where the stale-listing harvest reads it
  (swarm_state `listing_provider_blocked`), nothing is retried around it, and listings already fetched are kept; stops
  too at `harvest_max_minutes` (12) or `harvest_max_queries` (100), and the next day starts where this one stopped;
- stores, with the stale-listing agent's own helpers (`_store_listing`, `_store_lead`, `_retire`), only the listings
  that sit in a dead zone (residential, not pending / contingent): the listing row, the parcel (NJGIN match or the
  listing's own point), an active listing signal and the lead, plus the listing agent's published business contacts
  the way the listing-agent enricher stores them, so s7's dead-zone logic sees them in the same run. A listing it
  stored that later goes pending / contingent / sold, leaves the dead zone, or is missing from its query's last 3
  answered harvests leaves outreach again (signal retired; off_market when nobody has seen it since).

It runs only where the daily listing harvest runs (a successful stale-listing harvest on this database in the last
`harvest_live_days`, 3): the cloud desk. A laptop that handed the desk over, a fresh clone and the test suite never send
Realtor.com a request from here. Counts only in the returned metrics (the cloud run log is public).
"""
from __future__ import annotations

import logging
import math
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from db.models import DistressSignal, Lead, Listing, SignalType, SwarmState, utcnow

log = logging.getLogger(__name__)

STATE_KEY = "s7_highvalue_harvest"          # swarm_state: day, cursor, last counts, the listings this pass tracks
HISTORY_KEY = "s7_highvalue_harvests"       # swarm_state: per query, the start times of its recent answered harvests
TOWNS: tuple[str, ...] = (
    # Essex
    "Montclair, NJ", "Short Hills, NJ", "Millburn, NJ", "Livingston, NJ", "Glen Ridge, NJ", "Essex Fells, NJ",
    # Union
    "Summit, NJ", "Westfield, NJ", "Mountainside, NJ", "New Providence, NJ",
    # Bergen
    "Ridgewood, NJ", "Alpine, NJ", "Saddle River, NJ", "Englewood Cliffs, NJ", "Englewood, NJ", "Tenafly, NJ",
    "Franklin Lakes, NJ", "Upper Saddle River, NJ", "Glen Rock, NJ", "Cresskill, NJ", "Haworth, NJ", "Wyckoff, NJ",
    "Ho-Ho-Kus, NJ",
    # Hudson
    "Hoboken, NJ", "Jersey City, NJ", "Weehawken, NJ",
    # Monmouth
    "Rumson, NJ", "Holmdel, NJ", "Colts Neck, NJ", "Spring Lake, NJ", "Sea Girt, NJ", "Long Branch, NJ",
    "Fair Haven, NJ",
    # Mercer, Camden, Middlesex, Passaic
    "Princeton, NJ", "West Windsor, NJ", "Haddonfield, NJ", "Metuchen, NJ", "Wayne, NJ",
    # Morris, Somerset
    "Chatham, NJ", "Madison, NJ", "Mendham, NJ", "Morristown, NJ", "Bernardsville, NJ", "Basking Ridge, NJ",
    "Far Hills, NJ",
)
GRID = 10_000                     # band edges on a $10K grid
BELOW_MAX, BELOW_PCT = 20_000, 0.01   # a band starts min($20K, 1% of the line) under it
ABOVE = 50_000                    # ... and ends $50K past the dead zone's edge
MERGE_GAP = 500_000               # bands closer than this share one query (one request instead of several)
MAX_MINUTES = 12.0
MAX_QUERIES = 100
MAX_NEW_LEADS = 40                # NJGIN lookups per pass
MAX_ERRORS_IN_ROW = 3             # the network is down: stop instead of asking 90 times
PAGE_LIMIT = 400                  # rows per query (a $1M band in one town is a page or two)
CACHE_HOURS = 20.0
LIVE_DAYS = 3.0
MISSED_HARVESTS = 3
HARVEST_TAG = "s7_highvalue"


@dataclass(frozen=True)
class Query:
    location: str
    price_min: int
    price_max: int

    @property
    def key(self) -> str:
        return f"{self.location}|{self.price_min}-{self.price_max}"


# -- the plan (pure) ----------------------------------------------------------------------------------------------
def bands(zones: Iterable[tuple[float, float]] | None = None) -> list[tuple[int, int]]:
    """One (price_min, price_max) per dead zone, each containing the zone with a margin on both sides."""
    from engine.streams import s7_mansion_tax_cliff as s7
    out = []
    for t, edge in (s7.dead_zones() if zones is None else zones):
        lo = math.floor((t - min(BELOW_MAX, BELOW_PCT * t)) / GRID) * GRID
        hi = math.ceil((edge + ABOVE) / GRID) * GRID
        out.append((int(lo), int(hi)))
    return sorted(out)


def query_ranges(price_bands: list[tuple[int, int]], merge_gap: float = MERGE_GAP) -> list[tuple[int, int]]:
    """Bands less than `merge_gap` apart become one range (a query costs requests; a few extra rows cost nothing)."""
    out: list[list[int]] = []
    for lo, hi in sorted(price_bands):
        if out and lo - out[-1][1] <= merge_gap:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def plan(towns: Iterable[str], ranges: list[tuple[int, int]]) -> list[Query]:
    """Range-major: the $1M band in every town before any upper band (the $1M line is where most listings are)."""
    towns = list(dict.fromkeys(towns))
    return [Query(t, lo, hi) for lo, hi in ranges for t in towns]


# -- the provider -------------------------------------------------------------------------------------------------
def _slim(row: dict[str, Any]) -> dict[str, Any]:
    """A normalized listing with only the raw parts the stores read (agents, county, tags): small enough to cache."""
    raw = row.get("raw") or {}
    county = ((raw.get("location") or {}).get("county")) or {}
    return {**{k: v for k, v in row.items() if k != "raw"},
            "raw": {"advertisers": raw.get("advertisers") or [], "location": {"county": county},
                    "tags": raw.get("tags") or []}}


def fetch_band(location: str, price_min: int, price_max: int, *, limit: int = PAGE_LIMIT,
               cache_hours: float = CACHE_HOURS) -> list[dict[str, Any]]:
    """For-sale listings in one town and price range, normalized and de-duplicated like `active_listings` (one row
    per property, the richest feed kept). Goes through the provider's throttled call, so a refusal raises
    `ProviderBlocked` and is remembered there. `extra_property_data=False`: the agents and the county are in the
    search answer itself, so the extra per-page detail request is never made."""
    from engine import homeharvest_provider as hh
    from engine.listing_comps import _DiskCache
    cache = _DiskCache(cache_hours) if cache_hours and cache_hours > 0 else None
    key = f"hv_for_sale|{location}|{int(price_min)}|{int(price_max)}|{int(limit)}"
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit
    rows = [hh._normalize(r, "for_sale") for r in hh._scrape(
        location=location, listing_type="for_sale", price_min=int(price_min), price_max=int(price_max),
        extra_property_data=False, limit=int(limit))]
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = str(r.get("property_id") or r.get("url"))
        if k not in best or hh._richness(r) > hh._richness(best[k]):
            best[k] = r
    out = [_slim(r) for r in best.values()]
    if cache is not None:
        cache.set(key, out)
    return out


# -- state ---------------------------------------------------------------------------------------------------------
def _row(session: Session, key: str, default: dict[str, Any]) -> SwarmState:
    st = session.get(SwarmState, key)
    if st is None:
        st = SwarmState(key=key, value_json=default)
        session.add(st)
    return st


def state(session: Session) -> dict[str, Any]:
    st = session.get(SwarmState, STATE_KEY)
    return dict(st.value_json or {}) if st else {}


def listing_harvest_live(session: Session, days: float = LIVE_DAYS) -> bool:
    """True when the stale-listing harvest answered on this database in the last `days`: this is the machine that
    harvests listings (the cloud desk), so this pass may ask the provider too."""
    from agents.scrapers.stale_listing_distress_agent import HARVESTS_KEY
    st = session.get(SwarmState, HARVESTS_KEY)
    stamps = [s for v in (((st.value_json or {}).get("zips") or {}) if st else {}).values() for s in (v or [])]
    latest = None
    for s in stamps:
        try:
            d = datetime.fromisoformat(str(s))
        except ValueError:
            continue
        latest = d if latest is None or d > latest else latest
    return latest is not None and latest >= utcnow() - timedelta(days=float(days))


# -- the pass ------------------------------------------------------------------------------------------------------
def harvest(session: Session, *, now: datetime, limits: dict[str, Any] | None = None,
            fetch: Callable[[str, int, int], list[dict[str, Any]]] | None = None, gis: Any = None,
            clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Run the day's pass if it is due here. Returns {"status": ..., counts}: status is done_today / not_live /
    blocked_before (nothing asked), or ran / blocked / errors (the day is marked done)."""
    from agents.scrapers import stale_listing_distress_agent as sl
    from engine import homeharvest_provider as hh
    lim = limits or {}
    st = state(session)
    today = now.date().isoformat()
    if st.get("day") == today:
        return {"status": "done_today"}
    if not lim.get("harvest_anywhere") and not listing_harvest_live(session, float(lim.get("harvest_live_days",
                                                                                           LIVE_DAYS))):
        return {"status": "not_live"}
    if sl.honor_block(session) or hh.blocked():
        return {"status": "blocked_before"}       # a refusal is being honored: nothing is asked, nothing extended

    towns = tuple(lim.get("harvest_towns") or TOWNS)
    queries = plan(towns, query_ranges(bands(), float(lim.get("harvest_merge_gap", MERGE_GAP))))
    if not queries:
        return {"status": "not_live"}
    max_q = int(lim.get("harvest_max_queries", MAX_QUERIES))
    budget = float(lim.get("harvest_max_minutes", MAX_MINUTES)) * 60
    if fetch is None:
        limit, hours = int(lim.get("harvest_page_limit", PAGE_LIMIT)), float(lim.get("harvest_cache_hours",
                                                                                     CACHE_HOURS))

        def fetch(loc: str, lo: int, hi: int) -> list[dict[str, Any]]:
            return fetch_band(loc, lo, hi, limit=limit, cache_hours=hours)
    start = int(st.get("cursor") or 0) % len(queries)
    order = queries[start:] + queries[:start]
    t0 = clock()
    m: Counter[str] = Counter()
    rows: dict[str, tuple[Query, dict[str, Any]]] = {}
    answered: list[tuple[Query, datetime]] = []
    status, asked, in_row = "ran", 0, 0
    for q in order:
        if asked >= max_q or clock() - t0 >= budget:
            m["cut_off"] = 1
            break
        asked += 1
        started = utcnow()
        try:
            found = fetch(q.location, q.price_min, q.price_max)
        except hh.ProviderBlocked as exc:
            hours = hh.pause_hours()
            sl.record_block(session, zip_code=q.location, message=str(exc), hours=hours, harvested=len(answered),
                            not_asked=len(order) - asked + 1)
            hh.mark_blocked(str(exc), hours=hours)
            status, m["blocked"] = "blocked", 1
            asked -= 1                             # tomorrow starts with the query that was refused
            break
        except Exception as exc:  # noqa: BLE001 - one failed town never stops the pass
            m["errors"] += 1
            in_row += 1
            log.warning("s7 high-value harvest: %s %s-%s failed: %s", q.location, q.price_min, q.price_max,
                        type(exc).__name__)
            if in_row >= MAX_ERRORS_IN_ROW:
                status = "errors"
                asked -= in_row                    # tomorrow starts with the first query of the failed streak
                break
            continue
        in_row = 0
        answered.append((q, started))
        m["queries"] += 1
        m["rows"] += len(found)
        for r in found:
            ref = sl._ref(r)
            if ref not in rows or hh._richness(r) > hh._richness(rows[ref][1]):
                rows[ref] = (rows[ref][0] if ref in rows else q, r)
    m["listings"] = len(rows)
    refs = _store(session, rows, answered, dict(st.get("refs") or {}), lim, gis, m, {q.key for q in queries})
    m["seconds"] = int(round(clock() - t0))
    counts = {k: int(v) for k, v in m.items()}
    st.update(day=today, last_run=utcnow().isoformat(timespec="seconds"), status=status,
              cursor=(start + asked) % len(queries), queries_planned=len(queries), counts=counts, refs=refs)
    row = _row(session, STATE_KEY, {})
    row.value_json = st
    flag_modified(row, "value_json")
    row.updated_at = utcnow()
    session.flush()
    return {"status": status, **counts}


def _store(session: Session, rows: dict[str, tuple[Query, dict[str, Any]]], answered: list[tuple[Query, datetime]],
           refs: dict[str, dict[str, Any]], lim: dict[str, Any], gis: Any, m: Counter[str],
           planned: set[str]) -> dict[str, Any]:
    """Dead-zone listings -> listing + parcel + signal + lead + agent contacts (the stale-listing agent's helpers);
    tracked listings that left the zone or the market leave outreach. Returns the refs this pass tracks."""
    from agents.base import AgentContext
    from agents.scrapers import stale_listing_distress_agent as sl
    from engine.streams import s7_mansion_tax_cliff as s7
    agent = sl.StaleListingDistressAgent()
    actx = AgentContext(session=session, http=None)  # type: ignore[arg-type] - the stores never use it
    min_dom = int(sl.stale_cfg().get("min_days_on_market", 60))
    cap = int(lim.get("harvest_max_new_leads", MAX_NEW_LEADS))
    for ref, (q, row) in rows.items():
        status = (row.get("status") or "").lower()
        ptype = (row.get("property_type") or "").lower()
        in_zone = s7.cliff(row.get("list_price")) is not None
        m["in_zone"] += int(in_zone)
        live = status not in sl.OFF_MARKET and ptype in sl.RESIDENTIAL
        tracked = ref in refs
        if not (tracked or (in_zone and live)):
            continue                               # out of zone and never ours: nothing stored, nothing touched
        if in_zone and live and not tracked and m["new_leads"] >= cap:
            m["over_cap"] += 1                     # the next day's pass picks it up
            continue
        kws = sl.find_keywords(row.get("description"), (row.get("raw") or {}).get("tags"))
        agent._store_listing(actx, row, kws)       # flushes: the savepoints below sit inside a real transaction
        if status in sl.OFF_MARKET:
            agent._retire(actx, ref, f"listing is {status}", status)
            continue
        if not (in_zone and live):
            if refs[ref].get("own"):
                agent._retire(actx, ref, "price left the transfer-fee dead zone", "out_of_zone")
            continue
        had = _signal(session, ref) is not None
        try:
            with session.begin_nested():
                if gis is None:
                    from agents.scrapers.njgis_client import NJGISClient
                    from core.http import PoliteSession
                    gis = NJGISClient(PoliteSession(min_interval=0.5))
                agent._store_lead(actx, gis, row, kws, min_dom)
                sig = _signal(session, ref)
                lead = sig.property.lead if sig is not None and sig.property is not None else None
                if sig is not None and (refs.get(ref, {}).get("own") or not had):
                    sig.raw_metadata_json = {**(sig.raw_metadata_json or {}), "harvest": HARVEST_TAG}
                if lead is not None:
                    m["contacts"] += save_agents(session, lead, row)
        except Exception as exc:  # noqa: BLE001 - one bad listing (a parcel lookup, odd data) never stops the rest
            m["store_errors"] += 1
            log.warning("s7 high-value harvest: a listing in %s was not stored: %s", q.location, type(exc).__name__)
            continue
        if not tracked:
            m["new_leads"] += int(not had)
        refs[ref] = {"q": q.key, "own": bool(refs.get(ref, {}).get("own") or not had)}
    m["retired"] += int(actx.metrics.get("stale_signals_deactivated", 0))
    _sweep(session, agent, actx, answered, refs, int(lim.get("harvest_missed", MISSED_HARVESTS)), m, planned)
    return refs


def _signal(session: Session, ref: str) -> DistressSignal | None:
    from agents.scrapers.stale_listing_distress_agent import SOURCE
    return session.scalar(select(DistressSignal).where(
        DistressSignal.signal_type == SignalType.STALE_LISTING, DistressSignal.source == SOURCE,
        DistressSignal.source_ref == ref))


def save_agents(session: Session, lead: Lead, row: dict[str, Any]) -> int:
    """The listing's advertisers (agent name, brokerage, phones that aren't fax lines, e-mail) as LISTING_AGENT
    contacts, stored exactly as the listing-agent enricher stores them (so its next run updates, never duplicates).
    Returns the contacts created."""
    from agents.enrichment.listing_agent_enricher import SOURCE, advertiser_contacts
    from db.models import ContactType
    from db.repository import upsert_contact
    from engine.compliance import classify_number
    contacts = advertiser_contacts(row.get("raw") or {})
    street = (lead.property.street_address if lead.property else None) or row.get("address") or ""
    n = 0
    for c in contacts:
        if not (c.get("phone") or c.get("email")):
            continue
        _, created = upsert_contact(
            session, lead=lead, contact_type=ContactType.LISTING_AGENT, source=SOURCE, name=c.get("name"),
            phone=c.get("phone"), email=c.get("email"), company=c.get("company"), phone_type=c.get("phone_type"),
            confidence_score=0.9, dnc_status=classify_number(session, c.get("phone"), b2b=True),
            notes=f"Listing agent for {street} (list ${row.get('list_price') or 0:,.0f})",
            raw_json={"listing_url": row.get("url"), "role": c.get("role")})
        n += int(created)
    if lead.contact_name is None and contacts and contacts[0].get("name"):
        lead.contact_name = f"{contacts[0]['name']} (listing agent)"
    return n


def _sweep(session: Session, agent: Any, actx: Any, answered: list[tuple[Query, datetime]],
           refs: dict[str, dict[str, Any]], missed: int, m: Counter[str], planned: set[str]) -> None:
    """A tracked listing missing from its query's last `missed` answered harvests, and seen by no other harvest since,
    is off the market: listing off_market, signal retired, no longer tracked. A narrow price band can legitimately come
    back empty, so every answered query counts (a listing whose price moved out of the band leaves s7 either way). A
    listing whose query is no longer planned (the town list changed) goes once nobody has seen it for `missed` days."""
    from agents.scrapers import stale_listing_distress_agent as sl
    hist = _row(session, HISTORY_KEY, {"zips": {}})
    for q, started in answered:
        sl.record_harvest(hist, q.key, started)
    before = int(actx.metrics.get("stale_signals_deactivated", 0))
    for ref, info in list(refs.items()):
        qkey = str(info.get("q") or "")
        cutoff = (sl.missed_cutoff(hist, qkey, missed) if qkey in planned
                  else utcnow() - timedelta(days=missed))
        if cutoff is None:
            continue
        lst = session.scalar(select(Listing).where(Listing.source == sl.SOURCE, Listing.source_ref == ref))
        if lst is not None and lst.last_seen_at and lst.last_seen_at >= cutoff:
            continue
        if lst is not None and (lst.status or "").lower() not in sl.OFF_MARKET:
            lst.raw_json = {**(lst.raw_json or {}), "status_before_off_market": lst.status,
                            "off_market_since": utcnow().isoformat(timespec="seconds")}
            lst.status = "off_market"
            m["swept"] += 1
        agent._retire(actx, ref, f"not seen in the last {missed} high-value harvests", "off_market")
        refs.pop(ref, None)
    m["retired"] += int(actx.metrics.get("stale_signals_deactivated", 0)) - before
