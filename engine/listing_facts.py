"""Living area and rents from the listing's own words (2026-11-14, the operator: "several warm deals stall because the
tax record has no square footage; when an agent says 'send your offer' we should already have it").

The firm number needs the living area on record (`offer_math.certainty_issues`). Realtor.com's structured field
already fills `Property.sqft` at harvest (`stale_listing_distress_agent`, source "listing") and the subject lookup
("realtor_record"); this reads what the listing agent wrote when that field is empty: "approx. 2,400 sq ft",
"1,850 SF of living space", "each unit is 1,100 square feet" (x the units). Only a figure the listing states is used:
never a lot, yard, garage, basement or commercial area, never an estimate from the assessment (a guessed size is what
the certainty check exists to stop). Rents ("$2,600/mo", "total rent $4,700", "gross income $56,400/yr") are kept on
the listing row for the call scripts, the clone score and the two-option tenant answer.

`enrich(session)` saves the stated area as `Property.sqft` (`sqft_source` "listing_text") on listed leads that have
none, flags the valuation for a live re-value (`revalue`, which `offer_math.trickle` works through fixers first), and stores `{"sqft", "unit_sqft", "rents", "rent_total"}` in `Listing.raw_json["parsed"]`.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Listing, Property

_NUM = r"(\d{1,2},\d{3}|\d{3,5})"
_UNIT = r"(?:sq\.?\s*f(?:ee)?t\.?|sq\.?\s*ft\.?|sqft|square\s+f(?:ee|oo)t|s\.?f\.?)(?![a-z])"
AREA = re.compile(rf"(?i)(?:approx(?:imately|\.)?\s*|about\s+|over\s+|\+/-\s*|~\s*)?{_NUM}\s*\+?/?-?\s*{_UNIT}")
# what a nearby word says the number measures: anything but the house's living area is skipped
NOT_LIVING = re.compile(r"(?i)\b(lot|land|yard|acre|acres|garage|basement|attic|deck|patio|warehouse|retail|office|"
                        r"commercial|store|storage|shed|barn|parking|driveway|pool|building\s+lot|plus|additional)\b")
EACH_UNIT = re.compile(r"(?i)\b(each|per)\s+(unit|apartment|apt|floor|side)\b")
LIVING = re.compile(r"(?i)\b(living|interior|home|house|total|finished|heated|gla|above\s+grade)\b")

RENT = re.compile(r"(?i)\$\s?(\d{1,2},\d{3}|\d{3,4})(?:\.\d{2})?\s*(?:/|per\s+|a\s+)?\s*(?:mo\b|mos\b|month\b|monthly\b)")
RENT_AFTER = re.compile(r"(?i)\brents?\s+(?:for|of|at|is|are)?\s*\$\s?(\d{1,2},\d{3}|\d{3,4})\b(?!\s*(?:/|per\s+)?\s*(?:yr|year|annual))")
RENT_TOTAL = re.compile(r"(?i)\btotal\s+(?:monthly\s+)?(?:rent|rents|rental\s+income|income)\s*(?:of|is|:)?\s*\$\s?"
                        r"(\d{1,2},\d{3}|\d{3,4})\b")
GROSS_YEAR = re.compile(r"(?i)\b(?:gross|annual|yearly)\s+(?:annual\s+)?(?:rent|rents|rental\s+income|income)\s*"
                        r"(?:of|is|:)?\s*\$\s?(\d{2,3},\d{3}|\d{5,6})\b")


def _n(s: str) -> int:
    return int(s.replace(",", ""))


def living_area(text: str | None, units: int = 1) -> tuple[int | None, int | None]:
    """(total living area, per-unit area) the listing states, or (None, None). A per-unit figure ("each unit is 1,100
    sq ft") is multiplied by the units; several plain figures keep the one a living word sits next to, else the
    largest that passes (a bedroom's size is never larger than the house's)."""
    if not text:
        return None, None
    units = max(int(units or 1), 1)
    best: tuple[int, int] | None = None                         # (rank, sqft)
    per_unit: int | None = None
    for m in AREA.finditer(text):
        val = _n(m.group(1))
        # the words around the number, inside its own sentence ("5,000 sq ft lot" is a lot; "... sq ft. Lot 50x100"
        # is a new sentence)
        before = re.split(r"[.;!]\s", text[max(0, m.start() - 40):m.start()])[-1]
        after = re.split(r"[.;!]\s|,", text[m.end():m.end() + 25])[0]
        near = before[-30:] + " " + after
        if NOT_LIVING.search(near):
            continue
        if EACH_UNIT.search(before + " " + after):
            if 350 <= val <= 3000 and units > 1:
                per_unit = val
            continue
        if not 450 <= val <= 9000:
            continue
        rank = 2 if LIVING.search(near) else 1
        if best is None or (rank, val) > best:
            best = (rank, val)
    if per_unit and (best is None or best[1] < per_unit * units * 0.8):
        return per_unit * units, per_unit
    return (best[1] if best else None), per_unit


def rents(text: str | None) -> tuple[list[int], int | None]:
    """(monthly rents the listing states, the stated or summed monthly total)."""
    if not text:
        return [], None
    found: list[int] = []
    for m in list(RENT.finditer(text)) + list(RENT_AFTER.finditer(text)):
        v = _n(m.group(1))
        if 400 <= v <= 9000 and v not in found:
            found.append(v)
    total = None
    m = RENT_TOTAL.search(text)
    if m and 400 <= _n(m.group(1)) <= 30000:
        total = _n(m.group(1))
    m = GROSS_YEAR.search(text)
    if total is None and m and 6000 <= _n(m.group(1)) <= 400000:
        total = round(_n(m.group(1)) / 12)
    if total is None and len(found) >= 2:
        total = sum(found)
    elif total is None and len(found) == 1:
        total = found[0]
    found = [v for v in found if v != total or len(found) == 1]
    return found, total


def parsed(listing: Listing | None) -> dict[str, Any]:
    return dict((listing.raw_json or {}).get("parsed") or {}) if listing is not None else {}


def enrich(session: Session, *, limit: int = 400) -> dict[str, int]:
    """Read the stated area and rents of live listings; save the area on properties that have none. Counts only."""
    out = {"listings_read": 0, "sqft_found": 0, "rents_found": 0}
    rows = session.scalars(select(Listing).where(Listing.status.in_(("for_sale", "active", None)),
                                                 Listing.description.is_not(None), Listing.property_id.is_not(None))
                           .order_by(Listing.last_seen_at.desc()).limit(limit)).all()
    for lst in rows:
        prop = session.get(Property, lst.property_id)
        if prop is None:
            continue
        units = int(prop.dwellings or 1)
        area, unit_area = living_area(lst.description, units)
        rr, total = rents(lst.description)
        data = parsed(lst)
        new = {"sqft": area, "unit_sqft": unit_area, "rents": rr, "rent_total": total}
        out["listings_read"] += 1
        if new != {k: data.get(k) for k in new}:
            lst.raw_json = {**(lst.raw_json or {}), "parsed": new}
        if total:
            out["rents_found"] += 1
        if area and not prop.sqft and not lst.sqft:
            prop.sqft, prop.sqft_source = int(area), "listing_text"
            out["sqft_found"] += 1
            v = prop.current_valuation
            if v is not None:
                # re-value live with the stated area (offer_math.trickle works through these, fixers first); not the
                # comps flag, which would hold the house in pre-flight until the re-value ran
                v.assumptions_json = {**(v.assumptions_json or {}), "revalue": "stated living area"}
    session.flush()
    return out
