"""The clone score (2026-11-14): listings shaped like the deals that said yes come first; a cold opener goes only
where the desk's offer lands at 78%+ of the ask, and never to a retail listing. Prices are untouched."""
from __future__ import annotations

from engine import lead_scoring as ls
from tests.factories import make_lead


def _listed(db, *, ask, buyer_max, units=1, text="Sold as-is, needs work.", county="Monmouth", city="Asbury Park"):
    ld = make_lead(db, sheriff_days=None, stale_listing=True, list_price=ask, dwellings=units, listing_text=text,
                   county=county, city=city, assumptions={"quant": {"buyer_max": buyer_max}})
    db.commit()
    return ld


def test_the_asbury_profile_scores_high_and_lowballs_are_out(db):
    clone = _listed(db, ask=535000, buyer_max=485900, units=2,
                    text="Estate sale. Tenant occupied 2-family, rents $2,600/mo. Sold as-is.")
    low = _listed(db, ask=600000, buyer_max=400000, text="Needs work.")
    retail = _listed(db, ask=500000, buyer_max=470000, text="Fully renovated, move-in ready, new kitchen.")
    a, b, c = ls.score(db, clone), ls.score(db, low), ls.score(db, retail)
    from engine.offer_math import fees
    assert a.eligible and abs(a.ratio - (485900 - fees()[0]) / 535000) < 0.001 and a.score >= 70
    assert {"2 units", "tenants / rents", "estate / probate", "fixer words", "investor town (Asbury Park)"} <= set(a.parts)
    assert not b.eligible and "under 78%" in b.why_not
    assert not c.eligible and c.why_not == "retail / renovated listing"
    assert [s.lead_id for s in ls.rank(db, 5)] == [clone.id]
