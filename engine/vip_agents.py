"""VIP pocket-listing agents (operator 2026-11-10: "any address she emails us is immediately parsed, underwritten within
6 minutes and answered with a firm cash offer").

A listing agent who offered to send us her other as-is / estate listings is marked VIP with a sealed note
`{"vip_agent": "<e-mail>"}` (kept in swarm_state `vip_agents`, never in a committed file). When she e-mails a street
address we don't have, the inbox check that reads it (every desk run, ~6 minutes):

1. finds the parcel on NJGIN in one of the target counties (town from the e-mail, else our parcels by ZIP / town);
   only a residential (class 2) parcel, never one on a Daniel's Law / stop suppression, never an address in her
   signature block;
2. stores her price as the asking price of a pocket listing (a Listing row + an ACTIVE_LISTING signal, source
   `vip_agent`, her own words as the description, so condition words like "estate" / "as-is" count) and her as the
   listing agent;
3. underwrites it live (the normal Underwriter: NJGIN sales + listing comps, quant buyer max);
4. answers it like a "still available" on that house: the desk's one firm number when every certainty check passes
   (sent by itself under the Touch 2 rules), else the no-price "what would the seller need to net?" note.

Nothing here can price above the desk's own offer math (`engine/offer_math.py`), and a house the parcel lookup can't
find stays on the original thread for a person to read.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from db.models import SwarmState

log = logging.getLogger(__name__)
VIP_KEY = "vip_agents"
SOURCE = "vip_agent"
POCKET_TEXT = "It is still available."            # how a VIP's new address is read: Touch 2 on that house

_SUFFIX = (r"(?:Ave|Avenue|St|Street|Rd|Road|Dr|Drive|Ln|Lane|Ct|Court|Pl|Place|Blvd|Boulevard|Way|Ter|Terrace|Pkwy|"
           r"Parkway|Cir|Circle|Hwy|Highway)")
ADDRESS_RE = re.compile(
    r"\b(\d{1,5}[A-Za-z]?(?:-\d{1,5})?)\s+((?:[A-Z][A-Za-z'.-]*\s+){0,3}?" + _SUFFIX + r")\.?(?![A-Za-z])"
    r"(?:[ \t]*(?:,|\bin\b)[ \t]*([A-Z][A-Za-z.'-]+(?:[ \t]+[A-Z][A-Za-z.'-]+){0,2}?))?"
    r"(?:[ \t]*,?[ \t]*NJ\b[ \t]*(\d{5})?)?")
_NOT_TOWN = re.compile(r"^(NJ|New Jersey|Unit|Apt|Suite|Ste|Floor|Fl|It|The|Is|And|Asking|List|Listed|Price|Needs)$",
                       re.I)


# -- the VIP list ----------------------------------------------------------------------------------------------------
def vip_emails(session: Session) -> set[str]:
    row = session.get(SwarmState, VIP_KEY)
    return {str(e).strip().lower() for e in ((row.value_json or {}).get("emails") or [])} if row else set()


def set_vip(session: Session, email: str, *, remove: bool = False) -> set[str]:
    addr = (email or "").strip().lower()
    if "@" not in addr:
        raise ValueError("not an e-mail address")
    current = vip_emails(session)
    current = current - {addr} if remove else current | {addr}
    row = session.get(SwarmState, VIP_KEY)
    if row is None:
        row = SwarmState(key=VIP_KEY, value_json={})
        session.add(row)
    row.value_json = {"emails": sorted(current)}
    session.flush()
    return current


# -- reading the e-mail (pure) ---------------------------------------------------------------------------------------
def _body_without_signature(text: str, sender_name: str | None) -> str:
    """Drop everything from the sender's own name line on (her signature carries the office address)."""
    lines = (text or "").splitlines()
    first = (sender_name or "").strip().lower()
    for i, line in enumerate(lines):
        s = line.strip().lower()
        if first and s and (s == first or s.startswith(first.split()[0] + " ") and len(s.split()) <= 3) and i > 0:
            return "\n".join(lines[:i])
    return text or ""


def pocket_addresses(text: str, sender_name: str | None = None) -> list[dict[str, Any]]:
    """Street addresses an agent names in her own words: [{street, town, zip, start, end}]."""
    body = _body_without_signature(text, sender_name)
    out, seen = [], set()
    for m in ADDRESS_RE.finditer(body):
        number, street, town, zip5 = m.group(1), " ".join(m.group(2).split()), m.group(3), m.group(4)
        if town and _NOT_TOWN.match(town.split()[0]):
            town = None
        key = f"{number} {street}".upper()
        if key in seen:
            continue
        seen.add(key)
        out.append({"street": f"{number} {street}", "town": town, "zip": zip5, "start": m.start(), "end": m.end()})
    return out


def pocket_price(text: str, after: int = 0) -> float | None:
    """The price the agent gives for the house (the first amount after its address, within 160 characters)."""
    from engine import reply_negotiator as rn
    window = (text or "")[after:after + 160]
    for m in rn._MONEY.finditer(window):
        value = rn._amount_value(m.group(1), m.group(2), m.group(3), True)
        if value and 20_000 <= value <= 20_000_000:
            return float(value)
    return None


# -- opening the lead ------------------------------------------------------------------------------------------------
def _same_street(a: str | None, b: str | None) -> bool:
    from core.text import normalize_street
    return bool(a and b and normalize_street(a) == normalize_street(b))


def _county_for(session: Session, gis: Any, town: str | None, zip5: str | None) -> tuple[str, str | None] | None:
    from core.config import settings
    from engine.inbound import guess_county
    counties = [str(c) for c in (settings().markets.get("counties") or [])]
    guess = guess_county(session, zip5, town)
    order = ([guess] if guess in counties else []) + [c for c in counties if c != guess]
    for county in order:
        if not town:
            return (county, None) if county == guess else None
        try:
            mun = gis.resolve_municipality(county, town)
        except Exception:                               # noqa: BLE001 - NJGIN down: try the next county
            mun = None
        if mun:
            return county, mun[1]
    return None


def open_pocket_lead(session: Session, *, text: str, from_email: str, from_name: str | None, message_id: str,
                     current_lead: Any, agent: Any, gis: Any = None, underwriter: Any = None) -> Any:
    """The lead for the first new address a VIP agent's e-mail names (created, priced and underwritten), an existing
    lead when the address is already ours, or None when nothing usable is named."""
    from agents.scrapers.njgis_client import NJGISClient, feature_to_property
    from db.models import ContactType, Listing, SignalType
    from db.repository import ensure_lead, upsert_contact, upsert_property, upsert_signal
    from engine import gmail_autopilot as gm
    from engine.streams.base import address_ok
    here = getattr(getattr(current_lead, "property", None), "street_address", None)
    for addr in pocket_addresses(text, from_name):
        if _same_street(addr["street"], here):
            continue
        try:
            existing = gm.resolve_lead(session, addr["street"])
        except Exception:                               # noqa: BLE001 - ambiguous / unknown street
            existing = None
        if existing is not None:
            found = session.get(type(current_lead), existing) if isinstance(existing, int) else existing
            fp = getattr(found, "property", None)
            places = {(getattr(fp, "city", "") or "").lower(), (getattr(fp, "municipality", "") or "").lower()}
            if not addr["town"] or any(x.startswith(addr["town"].lower()) for x in places if x):
                return found                            # the same street in another town is a different house
        gis = gis or NJGISClient()
        place = _county_for(session, gis, addr["town"], addr["zip"])
        if place is None:
            continue
        county, mun = place
        try:
            feature = gis.find_by_address(county, addr["street"].upper(), mun)
        except Exception as exc:                        # noqa: BLE001
            log.warning("pocket listing lookup failed: %s", type(exc).__name__)
            continue
        if feature is None:
            continue
        pdata = feature_to_property(feature)
        if str(pdata.get("property_class") or "") != "2":
            continue                                    # offices, apartments, land: not a pocket listing we price
        prop, _ = upsert_property(session, pdata, source=SOURCE, overwrite=False)
        if not address_ok(session, prop):
            continue                                    # Daniel's Law / stop suppression always wins
        price = pocket_price(text, addr["end"])
        ref = f"vip:{(message_id or '').strip('<>')[:80]}:{re.sub(r'[^A-Z0-9]+', '-', addr['street'].upper())}"[:200]
        words = " ".join((text or "").split())[:1500]
        if not session.query(Listing).filter_by(source_ref=ref).first():
            session.add(Listing(property_id=prop.id, source=SOURCE, source_ref=ref, status="for_sale",
                                address=addr["street"], city=addr["town"] or prop.city, zip_code=prop.zip_code,
                                list_price=price, days_on_market=0, description=words,
                                raw_json={"pocket": True, "agent_email": from_email}))
        upsert_signal(session, prop, signal_type=SignalType.ACTIVE_LISTING, source=SOURCE, source_ref=ref,
                      severity_weight=1.0, active=True,
                      raw_metadata_json={"list_price": price, "pocket": True, "listing_agent": from_name or "",
                                         "agent_email": from_email})
        lead, _ = ensure_lead(session, prop, primary_source=SOURCE)
        upsert_contact(session, lead=lead, contact_type=ContactType.LISTING_AGENT, source=SOURCE,
                       name=(from_name or getattr(agent, "name", None) or None), email=from_email,
                       company=getattr(agent, "company", None), confidence_score=1.0,
                       notes="VIP agent: sent this pocket listing by e-mail")
        session.flush()
        session.refresh(prop)
        try:
            if underwriter is None:
                from engine.underwriter import Underwriter, _default_listing_provider
                underwriter = Underwriter(gis=gis, listing_provider=_default_listing_provider())
            underwriter.underwrite(session, prop)
        except Exception as exc:                        # noqa: BLE001 - kept; priced on the next pass
            log.warning("pocket listing underwrite failed: %s", type(exc).__name__)
        session.flush()
        session.refresh(lead)
        return lead
    return None
