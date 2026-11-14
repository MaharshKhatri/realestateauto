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


def test_cold_candidates_are_clone_deals_only_best_first(db, monkeypatch):
    from core.config import settings
    from engine import agent_outreach as ao
    from tests.factories import add_contact
    monkeypatch.setitem(settings(), "clone", {**settings()["clone"], "enabled": True})
    good = _listed(db, ask=535000, buyer_max=485900, units=2, text="Estate sale, tenant occupied 2-family. As-is.")
    ok = _listed(db, ask=500000, buyer_max=420000, text="Needs work.")
    low = _listed(db, ask=600000, buyer_max=400000, text="Needs work.")
    for ld in (good, ok, low):
        add_contact(db, ld, contact_type="LISTING_AGENT", name="Pat Agent", phone=None, email=f"pat{ld.id}@b.example")
    db.commit()
    ids = [ld.id for ld in ao.candidates(db, 10)]
    assert ids[0] == good.id and low.id not in ids
