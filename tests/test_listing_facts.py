"""Living area and rents from the listing's own words (2026-11-14): the firm number needs the living area on record,
and warm deals stalled on "what's the square footage?"; only a stated figure counts, never a lot, garage or basement."""
from __future__ import annotations

import pytest

from db.models import Listing
from engine import listing_facts as lf
from tests.factories import make_lead


@pytest.mark.parametrize("text,units,want", [
    ("Approx. 2,400 sq ft colonial on a 5,000 sq ft lot", 1, (2400, None)),
    ("Each unit is 1,100 square feet. Lot 50x100", 2, (2200, 1100)),
    ("3 bed 1 bath, 1,350 SF of living space, detached garage 400 sq ft", 1, (1350, None)),
    ("Huge 8,500 sq ft lot, home needs TLC", 1, (None, None)),
    ("Lovely home, 1,800 sq. ft., new roof", 1, (1800, None)),
    ("Investor special. Sold as-is.", 1, (None, None)),
])
def test_living_area_is_only_what_the_listing_states_for_the_house(text, units, want):
    assert lf.living_area(text, units) == want


@pytest.mark.parametrize("text,want", [
    ("Unit 1 rents for $1,800/mo and unit 2 $2,100 per month", ([1800, 2100], 3900)),
    ("Total monthly rent $4,700. Tenants in place", ([4700], 4700)),
    ("Gross annual income $56,400", ([], 4700)),
    ("Tenant on a lease through June 2027 at $2,600/mo", ([2600], 2600)),
])
def test_rents(text, want):
    assert lf.rents(text) == want


def test_enrich_saves_a_stated_area_and_queues_a_live_revalue(db):
    ld = make_lead(db, sheriff_days=None, stale_listing=True, list_price=420000, dwellings=2, sqft=None,
                   listing_text="Two family, each unit is 1,050 sq ft. Rents $1,900/mo and $2,000/mo. As-is.")
    db.commit()
    out = lf.enrich(db)
    db.commit()
    p = ld.property
    assert out["sqft_found"] == 1 and (p.sqft, p.sqft_source) == (2100, "listing_text")
    assert p.current_valuation.assumptions_json.get("revalue") == "stated living area"
    lst = db.query(Listing).filter_by(property_id=p.id).one()
    assert lf.parsed(lst) == {"sqft": 2100, "unit_sqft": 1050, "rents": [1900, 2000], "rent_total": 3900}
    assert lf.enrich(db)["sqft_found"] == 0                       # once: the property has its area now
