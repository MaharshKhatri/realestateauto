"""Small builders for Phase 2 database tests (a property + lead + signals + current valuation)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from db.models import Lead, SignalType, Valuation
from db.repository import ensure_lead, upsert_contact, upsert_property, upsert_signal

_seq = iter(range(1, 10_000))


def make_lead(db, *, sheriff_days: int | None = 20, upset: float | None = 150000, defendant: str = "JOHN Q SMITH",
              absentee: bool | None = None, vacant: bool = False, verdict: str | None = "STRONG",
              assumptions: dict[str, Any] | None = None, stale_listing: bool = False, **prop_kw) -> Lead:
    n = next(_seq)
    data = {"parcel_id": f"ESSEX-0714-{n}-1", "county": "Essex", "municipality": "NEWARK CITY", "mun_code": "0714",
            "street_address": f"{n} TEST ST", "city": "Newark", "zip_code": "07106", "property_class": "2",
            "dwellings": 1, "year_built": 1925, "assessed_total": 200000, "annual_taxes": 9000,
            "last_sale_date": date(1995, 1, 1), "last_sale_price": 90000, "is_absentee": absentee,
            "lat": 40.73, "lon": -74.2, "estimated_value": 400000}
    data.update(prop_kw)
    prop, _ = upsert_property(db, data, source="test")
    if sheriff_days is not None:
        upsert_signal(db, prop, signal_type=SignalType.SHERIFF_SALE, source="civilview", source_ref=f"Essex:F-{n}",
                      scheduled_auction_date=date.today() + timedelta(days=sheriff_days), amount_owed=upset,
                      defendant=defendant, plaintiff_or_lienholder="BIG BANK NA", case_number=f"F-00{n}-24",
                      raw_metadata_json={"adjournments_total": 1, "defendant_adjournments": 0})
    if vacant:
        upsert_signal(db, prop, signal_type=SignalType.VACANT, source="civilview", source_ref=f"vacant:{n}")
    if stale_listing:
        upsert_signal(db, prop, signal_type=SignalType.STALE_LISTING, source="homeharvest", source_ref=f"L{n}",
                      raw_metadata_json={"days_on_market": 140, "list_price": 450000, "keywords": ["as-is"],
                                         "listing_agent": "Pat Agent", "listing_agent_phone": "9735550100"})
    lead, _ = ensure_lead(db, prop, primary_source="sheriff_sale" if sheriff_days is not None else "stale_listing")
    if verdict:
        a = {"arv_before_tax_drag": 500000, "frfpa_applies": False, "valued_on": date.today().isoformat()}
        a.update(assumptions or {})
        db.add(Valuation(property=prop, arv_estimate=500000, as_is_value=400000, est_repair_cost=60000,
                         rehab_tier="MEDIUM", target_assignment_fee=30000, mao_conservative=260000,
                         mao_aggressive=285000, anchor_offer=228800, confidence_score=80, verdict=verdict,
                         debt_estimate=upset, deal_spread=110000, is_current=True, assumptions_json=a,
                         comps_json=[{"address": "9 Comp St, Newark 07106", "sold_price": 480000,
                                      "sold_date": "2026-08-01", "sqft": 1400, "distance_mi": 0.2}]))
    db.flush()
    db.refresh(prop)
    return lead


def add_contact(db, lead: Lead, contact_type: str = "OWNER", name: str = "John Smith", phone: str | None = "9735551234",
                **kw):
    c, _ = upsert_contact(db, lead=lead, contact_type=contact_type, source="test", name=name, phone=phone, **kw)
    db.flush()
    db.refresh(lead)
    return c
