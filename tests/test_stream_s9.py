"""S9 cost-seg / 1031 trigger feed: entity filters, unit / class rules, cross-source dedupe, long-hold listings, the
nominal-sale guard, suppression, the one-page feed (no owner names, no tax advice) and the flat-fee pitch."""
from __future__ import annotations

from datetime import date, datetime
from itertools import count

import pytest

from db.models import CashBuyer, DeedTransfer, FirmTarget, Listing, SignalType
from db.repository import upsert_property, upsert_signal
from engine.streams import s9_cpa_1031_feed as s9
from engine.streams.base import StreamContext

NOW = datetime(2026, 11, 9, 10, 0)          # ISO week 2026-W46
TODAY = NOW.date()
OP = {"name": "Alex Operator", "first": "Alex", "company": "Desk Research", "phone": "(555) 555-0100",
      "email": "desk@example.com", "postal": "PO Box 1, Newark NJ 07101"}
_n = count(1)


def _ctx(tmp_path, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=NOW, out_dir=tmp_path / "out", operator=OP, send=send, limits=limits)


def _deed(db, grantee: str = "ELM STREET HOLDINGS LLC", *, cls: str = "4A", price: int = 1_200_000,
          when: date = date(2026, 3, 15), location: str | None = None, parcel: str | None = None) -> DeedTransfer:
    n = next(_n)
    d = DeedTransfer(parcel_id=parcel or f"UNION-2004-{500 + n}-{n}", county="Union", property_location=location
                     or f"{n} ELM ST", grantor="PRIOR OWNER LLC", grantee=grantee, sale_price=price, deed_date=when,
                     property_class=cls, source="county_tax_list", raw_json={})
    db.add(d)
    db.flush()
    return d


def _parcel(db, **kw):
    n = next(_n)
    data = {"parcel_id": f"HUDSON-0906-{700 + n}-{n}", "county": "Hudson", "municipality": "JERSEY CITY CITY",
            "mun_code": "0906", "street_address": f"{n} OAK AVE", "city": "Jersey City", "zip_code": "07306",
            "property_class": "2", "dwellings": 3, "owner_name": "QUINN Q SAMPLE",
            "last_sale_date": date(2005, 6, 1), "last_sale_price": 250_000, "lat": 40.73, "lon": -74.06}
    data.update(kw)
    prop, _ = upsert_property(db, data, source="test")
    return prop


def _listing(db, prop, *, list_price: float = 800_000, status: str = "for_sale", ptype: str = "multi_family",
             linked: bool = False, broker: str = "Harbor Realty Group", lat: float = 40.73, lon: float = -74.06):
    n = next(_n)
    ref = f"L{n}"
    db.add(Listing(property_id=None if linked else prop.id, source="homeharvest", source_ref=ref, status=status,
                   address=prop.street_address.title(), city=prop.city, zip_code=prop.zip_code, property_type=ptype,
                   list_price=list_price, days_on_market=90, lat=lat, lon=lon))
    if linked:
        upsert_signal(db, prop, signal_type=SignalType.STALE_LISTING, source="homeharvest", source_ref=ref,
                      raw_metadata_json={"listing_broker": broker, "listing_agent": "Pat Agent"})
    db.flush()


def _target(db, kind: str = "real_estate_cpa", email: str | None = None, stage: str = "NEW") -> FirmTarget:
    t = FirmTarget(kind=kind, stream="s9", org="Example CPA LLC", email=email or f"{kind}{next(_n)}@cpa-example.com",
                   county="Union", stage=stage, score=1.0)
    db.add(t)
    db.flush()
    return t


def _pdf_text(path) -> tuple[int, str]:
    from pypdf import PdfReader
    r = PdfReader(str(path))
    return len(r.pages), " ".join(" ".join(p.extract_text().split()) for p in r.pages)


# -- pure helpers -------------------------------------------------------------------------------------------------
def test_business_entity_excludes_people_trusts_lenders_and_tax_exempt_owners():
    assert s9.business_entity("ELM STREET HOLDINGS LLC")
    assert s9.business_entity("857 SILVA, LLC")
    assert s9.business_entity("GARDEN STATE REAL ESTATE LLC")             # "real estate" is not an estate
    assert not s9.business_entity("QUINN Q SAMPLE")
    assert not s9.business_entity("QUINN SAMPLE REVOCABLE TRUST")         # a personal trust names a person
    assert not s9.business_entity("SAMPLE QUINN TRUSTEE")
    assert not s9.business_entity("ESTATE OF QUINN SAMPLE")
    assert not s9.business_entity("U.S. BANK NATIONAL ASSOCIATION")       # an REO deed, not an investor
    assert not s9.business_entity("FIRST BAPTIST CHURCH INC")
    assert not s9.business_entity("HOUSING AUTHORITY OF THE CITY OF ELIZABETH")
    assert not s9.business_entity(None)


def test_units_label_growth_and_keys():
    assert s9.units_label("4C", None) == "apartment (class 4C)"
    assert s9.units_label("4A", 1).startswith("commercial")
    assert s9.units_label("2", 3) == "3 units (class 2)"
    assert s9.units_label("2", 1) is None and s9.units_label("2", None) is None
    assert s9.units_label("4B", None) is None and s9.units_label("15D", 5) is None
    assert s9.gain_multiple(800_000, 250_000) == pytest.approx(3.2)
    assert s9.gain_multiple(800_000, None) is None
    # 3.2x over 21 years is ~5.7%/yr (a real gain); 20x over 16 years is ~21%/yr (a nominal deed or a rebuild)
    assert s9.annual_growth(3.2, date(2005, 6, 1), TODAY) == pytest.approx(0.057, abs=0.002)
    assert s9.annual_growth(20.0, date(2010, 6, 1), TODAY) > 0.12
    a = s9.purchase_keys("ELM LLC", "12-14 ELM ST", 1_200_000, date(2026, 3, 15))
    b = s9.purchase_keys("ELM LLC", None, 1_200_000, date(2026, 3, 2))
    assert set(a) & set(b)                                                 # same buyer, price and month


# -- cost-seg triggers --------------------------------------------------------------------------------------------
def test_cost_seg_rows_from_deeds_buyers_and_parcels_with_dedupe(db):
    _deed(db, "ELM STREET HOLDINGS LLC", cls="4A", price=1_200_000, location="12-14 ELM ST")
    _deed(db, "QUINN Q SAMPLE", cls="4C", price=2_000_000)                 # a person: never shown
    _deed(db, "TINY LOT LLC", cls="4A", price=300_000)                     # under $500K
    _deed(db, "OLD DEAL LLC", cls="4C", price=900_000, when=date(2024, 12, 1))   # outside 18 months
    _deed(db, "HOUSE LLC", cls="2", price=700_000)                         # class 2, units unknown: not a trigger
    p = _parcel(db, parcel_id="UNION-2004-900-1", county="Union", municipality="ELIZABETH CITY", mun_code="2004",
                dwellings=3, last_sale_date=date(2010, 1, 1))
    _deed(db, "THREE FAMILY LLC", cls="2", price=650_000, parcel=p.parcel_id, location=p.street_address)
    # the same Elm St purchase on a cash-buyer record (no town on it) must not appear twice
    db.add(CashBuyer(entity_key="elm", entity_name="ELM STREET HOLDINGS LLC", last_purchase_date=date(2026, 3, 15),
                     recent_purchases_json=[{"address": "12-14 ELM ST", "county": "Union", "price": 1_200_000,
                                             "deed_date": "2026-03-15", "class": "4A"},
                                            {"address": "9 PINE ST", "county": "Union", "price": 800_000,
                                             "deed_date": "2026-05-01", "class": "2", "units": 2,
                                             "municipality": "LINDEN CITY", "mun_code": "2009"}]))
    _parcel(db, owner_name="RIVER APARTMENTS LLC", property_class="4C", dwellings=None,
            last_sale_date=date(2026, 1, 20), last_sale_price=3_100_000)
    db.flush()
    metrics = {"deeds_seen": 0, "purchases_seen": 0, "parcels_seen": 0, "suppressed": 0}
    rows = s9.cost_seg_rows(db, TODAY, dict(s9.DEFAULTS), metrics)
    names = [r.entity for r in rows]
    assert sorted(names) == sorted(["ELM STREET HOLDINGS LLC", "ELM STREET HOLDINGS LLC", "THREE FAMILY LLC",
                                    "RIVER APARTMENTS LLC"])
    assert names.count("ELM STREET HOLDINGS LLC") == 2                     # the Elm deed once + the Pine purchase
    assert names[0] == "RIVER APARTMENTS LLC"                              # largest first
    elm = next(r for r in rows if r.entity == "ELM STREET HOLDINGS LLC" and r.price == 1_200_000)
    assert elm.town == "Elizabeth" and elm.source == "deed export"         # town from the parcel's municipal code
    pine = next(r for r in rows if r.price == 800_000)
    assert pine.kind == "2 units (class 2)" and pine.town == "Linden"


# -- 1031 triggers ------------------------------------------------------------------------------------------------
def test_exchange_rows_long_hold_gain_status_type_nominal_and_wrong_parcel(db):
    good = _parcel(db)
    _listing(db, good, linked=True, broker="Harbor Realty Group")          # linked through its STALE_LISTING signal
    direct = _parcel(db, street_address="77 DIRECT ST", last_sale_date=date(1999, 3, 1), last_sale_price=150_000)
    _listing(db, direct, list_price=900_000)                               # direct property_id link, no broker
    _listing(db, _parcel(db), status="pending")
    _listing(db, _parcel(db, last_sale_date=date(2021, 5, 1)))             # held 5 years
    _listing(db, _parcel(db, last_sale_price=700_000))                     # ask only 1.14x the last sale
    _listing(db, _parcel(db, last_sale_date=date(2010, 6, 1), last_sale_price=40_000))   # ~19%/yr: nominal deed
    _listing(db, _parcel(db, dwellings=1), ptype="single_family")
    _listing(db, _parcel(db), lat=40.95, lon=-74.20)                       # listing 12+ miles from the parcel
    metrics = {"listings_seen": 0, "suppressed": 0, "wrong_parcel": 0, "nominal_sale": 0}
    rows = s9.exchange_rows(db, TODAY, dict(s9.DEFAULTS), metrics)
    assert [r.street for r in rows] == ["77 Direct St", good.street_address.title()]   # largest gain first
    assert rows[1].brokerage == "Harbor Realty Group" and rows[0].brokerage == ""
    assert rows[1].town == "Jersey City" and rows[1].multiple == pytest.approx(3.2)
    assert metrics["nominal_sale"] == 1 and metrics["wrong_parcel"] == 1


def test_suppressed_addresses_never_reach_the_feed(db):
    from engine.compliance import suppress_address
    p = _parcel(db)
    _listing(db, p)
    _deed(db, "ELM STREET HOLDINGS LLC", location="12 ELM ST", parcel="UNION-2004-901-1")
    suppress_address(db, f"{p.street_address.title()}, Jersey City, NJ 07306", source="daniels_law")
    suppress_address(db, "12 Elm St, Elizabeth, NJ", source="internal")   # town written as the postal name
    m = {"deeds_seen": 0, "purchases_seen": 0, "parcels_seen": 0, "listings_seen": 0, "suppressed": 0,
         "wrong_parcel": 0, "nominal_sale": 0}
    assert s9.exchange_rows(db, TODAY, dict(s9.DEFAULTS), m) == []
    assert s9.cost_seg_rows(db, TODAY, dict(s9.DEFAULTS), m) == []
    assert m["suppressed"] == 2


# -- the stream ---------------------------------------------------------------------------------------------------
def test_full_feed_is_one_page_with_no_owner_names_and_no_tax_advice(db, tmp_path):
    for i in range(14):
        _deed(db, f"BLOCK {i} HOLDINGS LLC", cls="4C" if i % 2 else "4A", price=1_000_000 + 50_000 * i)
    _deed(db, "QUINN Q SAMPLE", cls="4C", price=2_000_000)
    for i in range(14):
        _listing(db, _parcel(db, street_address=f"{100 + i} LONG HOLD BOULEVARD UNIT {i}"),
                 list_price=700_000 + 10_000 * i, linked=bool(i % 2),
                 broker="Very Long Brokerage Name Realty Associates of Hudson County LLC")
    res = s9.STREAM.run(db, _ctx(tmp_path))
    assert res.products == 1 and res.metrics["feed_rows"] == 24
    assert res.metrics["costseg_found"] == 14 and res.metrics["exchange_found"] == 14
    pdf = tmp_path / "out" / "s9_feed_2026-W46.pdf"
    pages, text = _pdf_text(pdf)
    assert pages == 1
    assert s9.ELIGIBILITY_LINE in text and "may qualify" in text
    assert "BLOCK 13 HOLDINGS LLC" in text and "Long Hold Boulevard" in text
    low = text.lower()
    for banned in ("quinn", "sample", "you should", "we recommend", "guarantee", "will qualify"):
        assert banned not in low, banned


def test_pitches_by_kind_flat_fee_stable_keys_and_dry_run(db, tmp_path):
    from engine import human_copy as hc
    for i in range(3):
        _deed(db, f"BLOCK {i} HOLDINGS LLC", price=1_000_000 + i)
    qi = _target(db, "qualified_intermediary_1031")                        # no 1031 rows this week: not pitched
    cs = _target(db, "cost_segregation_firm")
    cpa = _target(db, "real_estate_cpa")
    _target(db, "real_estate_cpa", stage="LOST")
    _target(db, "property_tax_appeal_attorney")
    assert s9.STREAM.run(db, _ctx(tmp_path, send=False)).outbound == []
    res = s9.STREAM.run(db, _ctx(tmp_path))
    by = {o.target_id: o for o in res.outbound}
    assert set(by) == {cs.id, cpa.id} and qi.id not in by
    assert by[cs.id].key == f"s9:pitch:{cs.id}:2026-W46"
    for o in res.outbound:
        assert hc.lint(o.body) == [] and hc.word_count(o.body) < 110
        assert o.body.rstrip().endswith("Alex") and "$500 a month" in o.body
        assert o.attachments == [tmp_path / "out" / "s9_feed_2026-W46.pdf"] and o.priority == 3
        for banned in ("referral", "clients", "guarantee", "opportunit", "—", "will qualify"):
            assert banned not in o.body.lower(), banned
    again = s9.STREAM.run(db, _ctx(tmp_path / "2"))
    assert sorted(o.key for o in again.outbound) == sorted(o.key for o in res.outbound)
    capped = s9.STREAM.run(db, _ctx(tmp_path / "3", pitches_per_run=1))
    assert len(capped.outbound) == 1


def test_empty_database(db, tmp_path):
    res = s9.STREAM.run(db, _ctx(tmp_path))
    assert (res.found, res.products, res.outbound) == (0, 0, [])
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.pdf"))
    assert s9.STREAM.key == "s9" and s9.STREAM.priority == 3 and s9.STREAM.cadence_hours == 24


def test_page_counter_and_the_one_page_guard(db, tmp_path, monkeypatch):
    from pypdf import PdfReader

    from engine.streams.base import render_table_pdf
    from engine.streams.s6_tax_appeals import pdf_pages
    long = render_table_pdf(tmp_path / "long.pdf", title="t", subtitle="s", head=["a", "b"],
                            rows=[[str(i), "x"] for i in range(90)])
    assert pdf_pages(long) == len(PdfReader(str(long)).pages) > 1
    # even when every row would wrap, the feed drops rows until it fits on one page
    for i in range(12):
        _deed(db, f"BLOCK {i} HOLDINGS LLC", price=1_000_000 + i)
        _listing(db, _parcel(db, street_address=f"{i} WRAP ST"), linked=True)
    monkeypatch.setattr(s9, "clip", lambda text, n: (" ".join(str(text or "").split()) + " ") * 6)
    res = s9.STREAM.run(db, _ctx(tmp_path, rows_costseg=12, rows_1031=12))
    pdf = tmp_path / "out" / "s9_feed_2026-W46.pdf"
    assert len(PdfReader(str(pdf)).pages) == 1 and 0 < res.metrics["feed_rows"] < 24
