"""Virtual desk inspection: everything the operator can learn about a property without going there.

For any lead it assembles a dossier:
  * 1-click links — Google Street View and satellite at the parcel's lat/lon (Google Maps URLs API), Bing
    aerial, a Google Maps address search, the Zillow address page, the MLS listing (when listed), and the
    FEMA flood-map address search;
  * condition flags read from the listing description and tags (roof, water/mold, electrical, oil tank —
    a classic NJ deal-breaker — foundation, fire, "as-is/cash only", occupancy hints) plus age-based NJ
    risks (pre-1978 lead paint, pre-1960 wiring, oil heat era);
  * an NJ remote rehab / due-diligence checklist tailored to those flags;
  * photo and 3-minute video-walkthrough request messages for the seller, the tenant or the listing agent,
    so condition is confirmed by phone camera instead of a drive.

Nothing here scrapes: the links open the public map / listing pages in the operator's own browser.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote, quote_plus

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Lead, Listing, SignalType

CONDITION_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("roof", re.compile(r"\b(roof|leak(s|ing)?|tarp)\b", re.I), "Roof mentioned — check satellite for tarps/patches; ask for roof photos and age."),
    ("water", re.compile(r"\b(water damage|mold|mould|flood(ed|ing)?|seepage|moisture|sump|wet basement)\b", re.I),
     "Water / mold language — ask for basement photos and whether there is a sump pump; budget remediation."),
    ("electrical", re.compile(r"\b(knob (and|&) tube|knob-and-tube|fuse box|100 ?amp|60 ?amp|electrical (work|issues))\b", re.I),
     "Old wiring — budget a panel/rewire; many insurers won't cover knob-and-tube."),
    ("oil_tank", re.compile(r"\b(oil tank|underground tank|oil heat|oil-fired|oil burner|\bUST\b|tank sweep)\b", re.I),
     "Oil heat / tank mentioned — order a tank sweep before closing (an underground tank leak is a NJ deal-breaker)."),
    ("foundation", re.compile(r"\b(foundation|structural|settling|sagging|joist)\b", re.I),
     "Structural language — ask for basement/foundation photos; consider an engineer's video review."),
    ("fire", re.compile(r"\b(fire damage|fire-damaged|burned|smoke damage)\b", re.I), "Fire damage — likely a gut rehab; confirm with interior photos."),
    ("as_is", re.compile(r"\b(as[- ]is|cash only|investor special|handyman|tlc|needs work|fixer|contractor special|gut|"
                          r"cannot be financed|no co\b|estate sale|203k|bring your contractor)\b", re.I),
     "As-is / cash-only language — the seller already expects an investor price."),
    ("tenant", re.compile(r"\b(tenant[- ]occupied|tenants in place|tenant in place|currently rented|month[- ]to[- ]month|lease)\b", re.I),
     "Tenants mentioned — ask for leases, rents and deposits; price as a tenant-occupied sale."),
    ("vacant", re.compile(r"\b(vacant|unoccupied|lockbox|easy to show)\b", re.I), "Listing suggests vacant — confirm and record occupancy."),
]
NJ_CHECKLIST = [
    "Oil tank sweep (~$300–500) if built before ~1980 or any sign of oil heat",
    "Sewer lateral camera scope for pre-1960 houses",
    "Municipal certificate of occupancy / resale inspection requirements (call the building department)",
    "Smoke / CO / fire-extinguisher certificate (N.J.A.C. 5:70-2.3) needed at transfer",
    "Flood zone and flood history (FEMA link below; NJ seller flood-risk disclosure)",
    "Open permits and code violations (building department)",
    "Title search: tax liens, water/sewer liens, judgments, tax sale certificates",
]


@dataclass
class Dossier:
    lead_id: int
    address: str
    links: dict[str, str]
    flags: list[dict[str, str]]
    occupancy_hint: str | None
    checklist: list[str]
    requests: dict[str, str]
    listing_excerpt: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def map_links(address: str, lat: float | None, lon: float | None, listing_url: str | None = None) -> dict[str, str]:
    links: dict[str, str] = {}
    if lat and lon:
        links["street_view"] = f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat:.6f},{lon:.6f}"
        links["satellite"] = (f"https://www.google.com/maps/@?api=1&map_action=map&center={lat:.6f},{lon:.6f}"
                              "&zoom=20&basemap=satellite")
        links["bing_aerial"] = f"https://www.bing.com/maps?cp={lat:.6f}~{lon:.6f}&lvl=20&style=a"
    links["google_maps"] = f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"
    links["zillow"] = f"https://www.zillow.com/homes/{quote(address.replace(',', '').replace(' ', '-'))}_rb/"
    if listing_url:
        links["listing_photos"] = listing_url
    links["fema_flood"] = f"https://msc.fema.gov/portal/search?AddressQuery={quote_plus(address)}"
    links["nj_flood_mapper"] = "https://www.njfloodmapper.org/"
    return links


def condition_flags(text: str | None, keywords: list[str] | None = None, year_built: int | None = None) -> list[dict[str, str]]:
    blob = " ".join([text or "", " ".join(keywords or [])])
    flags: list[dict[str, str]] = []
    for key, pat, advice in CONDITION_PATTERNS:
        m = pat.search(blob)
        if m:
            flags.append({"flag": key, "evidence": m.group(0), "advice": advice})
    if year_built:
        if year_built < 1978:
            flags.append({"flag": "lead_paint", "evidence": f"built {year_built}",
                          "advice": "Pre-1978: federal lead-paint disclosure applies; budget for lead-safe work."})
        if year_built < 1960 and not any(f["flag"] == "electrical" for f in flags):
            flags.append({"flag": "old_wiring_risk", "evidence": f"built {year_built}",
                          "advice": "Pre-1960: ask for an electrical panel photo (knob-and-tube / fuses)."})
        if year_built < 1980 and not any(f["flag"] == "oil_tank" for f in flags):
            flags.append({"flag": "oil_tank_risk", "evidence": f"built {year_built}",
                          "advice": "Oil-heat era house: ask how it's heated and order a tank sweep."})
    return flags


def occupancy_hint(flags: list[dict[str, str]]) -> str | None:
    kinds = {f["flag"] for f in flags}
    if "tenant" in kinds:
        return "TENANT_OCCUPIED (listing text — confirm)"
    if "vacant" in kinds:
        return "VACANT (listing text — confirm)"
    return None


def request_messages(address: str, who: str = "seller") -> dict[str, str]:
    """Photo / video-walkthrough requests (instead of a visit) to the seller, tenant or listing agent."""
    from engine import human_copy as hc
    op = hc.operator()
    me = " ".join(x for x in (op["first"] or op["name"], "with " + op["company"] if op["company"] else "") if x)
    st = hc.street(address)
    callback = f" My number is {op['phone']}." if op["phone"] else ""
    shots = ("the front, the back and roof line, the kitchen, each bathroom, the basement with the boiler or furnace "
             "and electric panel, and anything that needs work")
    sms = (f"Hi, this is {me} about {st}. Instead of a visit, could you text me 6 quick photos (front, back, kitchen, "
           "baths, basement with the boiler and panel)? Or a 3-minute FaceTime video works too."
           f"{callback} Reply STOP and I won't text again.")
    agent_sms = (f"Hi, it's {me}. I'm working on a cash offer for your listing at {st}. Could you send any extra "
                 "interior photos (basement, mechanicals, roof) or do a 3-minute video walk-through next time you're "
                 "there? I'll turn an offer around the same day.")
    email = hc.compose(
        f"photos for {st}", "Hi,",
        [f"So I can give you an accurate as-is number for {st} without a visit, could you send about six photos "
         f"({shots})? If it's easier, a short FaceTime, WhatsApp or Zoom call works too and takes about three "
         "minutes. If the house is rented, please don't disturb your tenants; outside photos plus whatever you have "
         "on file are fine."],
        hc.footer_lines(op, "Reply STOP and I won't contact you again."), summary_title="---", op=op)
    out = {"sms_owner": hc.plain(sms), "sms_agent": hc.plain(agent_sms),
           "email": f"Subject: {email.subject}\n\n{email.text}"}
    return out if who != "agent" else {"sms_agent": out["sms_agent"]}


def listing_text(session: Session, lead: Lead) -> tuple[str | None, list[str], str | None]:
    """(description, keywords, url) from the property's MLS listing, if it has one."""
    sig = next((s for s in lead.property.active_signals
                if s.signal_type in (SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)), None)
    if sig is None:
        return None, [], None
    meta = sig.raw_metadata_json or {}
    lst = session.scalar(select(Listing).where(Listing.source_ref == sig.source_ref))
    return (lst.description if lst else None), list(meta.get("keywords") or []), meta.get("url") or sig.source_url


def build_dossier(session: Session, lead: Lead) -> Dossier:
    p = lead.property
    address = f"{(p.street_address or '').title()}, {(p.city or p.municipality or '').title()}, NJ {p.zip_code or ''}".strip()
    desc, kws, url = listing_text(session, lead)
    flags = condition_flags(desc, kws, p.year_built)
    checklist = list(NJ_CHECKLIST)
    if any(f["flag"] in ("oil_tank", "oil_tank_risk") for f in flags):
        checklist.insert(0, "PRIORITY: tank sweep — oil heat / tank risk flagged")
    if any(f["flag"] == "water" for f in flags):
        checklist.insert(0, "PRIORITY: basement water — ask for photos and a sump/drainage answer")
    notes = []
    if not (p.lat and p.lon):
        notes.append("No coordinates on file — Street View/satellite links unavailable; use the address search.")
    if not desc:
        notes.append("No listing description on file — request photos or a video walk-through.")
    has_agent = any(c.contact_type == "LISTING_AGENT" for c in lead.contacts)
    return Dossier(lead_id=lead.id, address=address, links=map_links(address, p.lat, p.lon, url), flags=flags,
                   occupancy_hint=occupancy_hint(flags), checklist=checklist,
                   requests=request_messages(address, "agent" if has_agent and not lead.property.owner_name else "seller")
                   | request_messages(address, "agent"),
                   listing_excerpt=(desc[:600] + "…") if desc and len(desc) > 600 else desc, notes=notes)
