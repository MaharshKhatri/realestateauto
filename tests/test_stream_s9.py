"""S9 cost-seg / 1031 trigger feed: entity filters, unit / class rules, cross-source dedupe, long-hold listings, the
nominal-sale guard, suppression, the one-page feed (no owner names, no tax advice) and the flat-fee pitch."""
from __future__ import annotations

import re
from datetime import date, datetime
from itertools import count

import pytest

from core.text import postal_title
from db.models import CashBuyer, DeedTransfer, FirmOutbox, FirmTarget, Listing, SignalType
from db.repository import upsert_property, upsert_signal
from engine.streams import s9_cpa_1031_feed as s9
from engine.streams.base import StreamContext, sample_path

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
    # pypdf reads reportlab's WinAnsi bullet (the sample's mask) back as \x7f
    return len(r.pages), " ".join(" ".join(p.extract_text().split()) for p in r.pages).replace("\x7f", "\u2022")


def sentences(body: str) -> list[str]:
    """The sentences above the sign-off: the greeting line and the operator's first name are not sentences; a lone
    initial ("N.") or a decimal ("1.2") does not end one."""
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    lines = [ln for ln in lines if ln not in ("Hello,", OP["first"]) and not re.fullmatch(r"Hi [\w.'-]+,", ln)]
    out, cur = [], []
    for tok in " ".join(lines).split():
        cur.append(tok)
        if re.search(r"[.?!][\")]?$", tok) and not re.fullmatch(r"[A-Z]\.", tok):
            out.append(" ".join(cur))
            cur = []
    return out + ([" ".join(cur)] if cur else [])


def touch_words(body: str) -> int:
    from engine import human_copy as hc
    lines = body.rstrip().splitlines()
    return hc.word_count("\n".join(lines[:-1] if lines and lines[-1].strip() == OP["first"] else lines))


def _sent_pitch(db, t, body: str, subject: str = "Entity purchases", days_ago: int = 8):
    """An earlier cold touch the outbox sent (the target then counts one touch)."""
    from datetime import timedelta
    row = FirmOutbox(key=f"s9:pitch:{t.id}:2026-W45", stream="s9", priority=3, kind="pitch", target_id=t.id,
                     to_email=t.email, subject=subject, body=body, status="sent", message_id=f"<p{t.id}@desk.example>",
                     sent_at=NOW - timedelta(days=days_ago), attachments_json=[])
    db.add(row)
    t.touches, t.stage, t.last_contact_at = 1, "CONTACTED", NOW - timedelta(days=days_ago)
    db.flush()
    return row


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
    # the masked sample (Touch 2): one page, rows 1-2 in full, the property and the entity / brokerage hidden after
    spages, stext = _pdf_text(sample_path(pdf))
    assert spages == 1 and s9.ELIGIBILITY_LINE in stext and "Sample: 2 of 24 rows in full" in stext
    assert "BLOCK 13 HOLDINGS LLC" in stext and "BLOCK 12 HOLDINGS LLC" in stext
    assert "BLOCK 11 HOLDINGS LLC" not in stext and "B•••• 1• H••••••• LLC" in stext
    assert "Long Hold Boulevard" not in stext and "Very Long Brokerage" not in stext
    assert "Jersey City (Hudson)" in stext and "Apartments (4C)" in stext   # town, kind and prices stay visible


def test_pitches_by_kind_two_sentences_no_attachment_stable_keys_and_dry_run(db, tmp_path):
    from engine import human_copy as hc
    deeds = [_deed(db, f"BLOCK {i} HOLDINGS LLC", price=1_000_000 + 100_000 * i) for i in range(3)]
    top = postal_title(deeds[-1].property_location)                         # the largest purchase
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
    feed = tmp_path / "out" / "s9_feed_2026-W46.pdf"
    assert res.products == 1                                               # the Union feed is the statewide one
    for o in res.outbound:
        assert o.kind == "pitch" and o.priority == 3 and o.in_reply_to is None
        assert o.attachments == [] and o.sample_attachments == [sample_path(feed)] and sample_path(feed).exists()
        assert o.subject == f"{top} + 2 more entity purchases in Union County"
        assert hc.lint(o.body) == [] and o.body.rstrip().endswith("Alex") and o.body.startswith("Hello,\n\n")
        assert len(sentences(o.body)) == 2 and touch_words(o.body) <= 60
        assert "$" not in o.body and top in o.body and "in Elizabeth" in o.body
        assert ("We pulled 3 recent multi-family and commercial purchases by business entities in Union County from "
                f"public deed and tax records, including {top} in Elizabeth (bought for 1.2 million dollars in March "
                "2026), that may fit cost-segregation or 1031 work.") in o.body
        assert sentences(o.body)[-1] == "Want the property and buyer-entity list?"
        assert "HOLDINGS" not in o.body.upper()                            # no buyer named in Touch 1
        assert "$500 a month flat, no other charge" in o.sample_body and "the client's CPA decides" in o.sample_body
        assert "no commission or per-deal fee" in o.sample_body and hc.lint(o.sample_body) == []
        assert "the first 2 rows in full, the rest with the property and buyer entity hidden" in o.sample_body
        assert o.sample_body.endswith('Reply "start" and I\'ll set it up.')
        for text in (o.body, o.sample_body, o.subject):
            for banned in ("referral", "clients", "guarantee", "opportunit", "—", "will qualify", "free", "!"):
                assert banned not in text.lower(), banned
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


# -- the 2-step funnel (2026-11-10) -------------------------------------------------------------------------------
def test_spoken_price_scope_and_singular_copy():
    assert s9.spoken_price(1_200_000) == "1.2 million dollars"
    assert s9.spoken_price(3_125_000) == "3.12 million dollars" and s9.spoken_price(2_000_000) == "2 million dollars"
    assert s9.spoken_price(650_000) == "650,000 dollars"
    row = s9.CostSegRow(entity="ELM LLC", town="Linden", county="Union", price=800_000, deed_date=date(2026, 5, 1),
                        kind="2 units (class 2)", source="deed export", keys=(), street="9 PINE ST, LINDEN")
    one = s9.pitch_body("cs", "Union County", 1, row, 8, "Alex")
    assert ("We pulled a recent multi-family or commercial purchase by a business entity in Union County from public "
            "deed and tax records, 9 Pine St in Linden (bought for 800,000 dollars in May 2026)") in one
    assert len(sentences(one)) == 2 and "$" not in one and "ELM" not in one
    assert s9.pitch_subject("cs", "Union County", "9 Pine St", 1) == "9 Pine St: an entity purchase in Union County"
    assert s9.pitch_subject("ex", "New Jersey", "9 Pine St", 4) == "9 Pine St + 3 more long-held listings in New Jersey"
    assert s9.short_kind("commercial / mixed use (class 4A)") == "Commercial (4A)"
    assert s9.short_kind("3 units (class 2)") == "3 units" and s9.short_kind("multi-family (listing)") == "Multi-family"
    full = s9.sample_body("Union County", 2, 0, False, 500)
    assert "every row in full" in full and "hidden" not in full


def test_a_1031_intermediary_hears_about_long_held_listings_in_its_county_first(db, tmp_path):
    from engine import human_copy as hc
    for i in range(4):
        _deed(db, f"BLOCK {i} HOLDINGS LLC", price=1_500_000 + i)              # Union purchases (Table A)
    near = _parcel(db, street_address="77 DIRECT ST", last_sale_date=date(1999, 3, 1), last_sale_price=150_000)
    _listing(db, near, list_price=900_000)                                     # the largest gain: named
    for i in range(3):
        _listing(db, _parcel(db, street_address=f"{10 + i} HARBOR AVE"), linked=True)
    qi = _target(db, "qualified_intermediary_1031")
    qi.county = "Hudson"
    far = _target(db, "qualified_intermediary_1031")
    far.county = "Ocean"                                                       # no rows there: New Jersey
    db.flush()
    res = s9.STREAM.run(db, _ctx(tmp_path))
    by = {o.target_id: o for o in res.outbound}
    o = by[qi.id]
    assert o.subject == "77 Direct St + 3 more long-held listings in Hudson County"
    assert ("We pulled 4 multi-family and commercial properties listed for sale in Hudson County whose last recorded "
            "sale was 8 or more years ago, from public listings and tax records, including 77 Direct St in Jersey City "
            "(last sold in 1999), that may fit 1031 work.") in o.body
    assert sentences(o.body)[-1] == "Want the property and listing-brokerage list?"
    assert len(sentences(o.body)) == 2 and touch_words(o.body) <= 60 and "$" not in o.body and hc.lint(o.body) == []
    assert "Quinn" not in o.body and "QUINN" not in o.body                     # listings never name an owner
    # its sample: the Hudson feed, Table B first, the named listing in full, the rest masked
    (sample,) = o.sample_attachments
    assert sample.exists() and sample.parent.name == "samples" and "hudson_ex" in sample.name
    pages, text = _pdf_text(sample)
    assert pages == 1 and "77 Direct St" in text and "10 Harbor Ave" in text   # rows 1-2 in full
    assert "11 Harbor Ave" not in text and "12 Harbor Ave" not in text and "1• H••••• Ave" in text
    assert "Harbor Realty Group" in text                                       # row 2's brokerage, in full
    assert text.count("Harbor Realty Group") == 1                              # row 4's (masked) is hidden
    assert "BLOCK" not in text                                                 # a Hudson feed: no Union purchases
    assert "property and listing brokerage hidden" in o.sample_body and "$500 a month" in o.sample_body
    w = by[far.id]
    assert w.subject.endswith("in New Jersey") and "in New Jersey" in w.body and "77 Direct St" in w.body
    _, wtext = _pdf_text(w.sample_attachments[0])
    assert "BLOCK 3 HOLDINGS LLC" not in wtext                                 # Table A rows sit after B: masked
    assert res.products == 3                                                   # statewide + Hudson + New Jersey (B first)


def test_second_touch_is_one_sentence_in_the_first_ones_thread(db, tmp_path):
    deeds = [_deed(db, f"BLOCK {i} HOLDINGS LLC", price=1_000_000 + 100_000 * i) for i in range(4)]
    named = postal_title(deeds[1].property_location)                          # named last week, not this week's top
    t = _target(db, "cost_segregation_firm")
    earlier = _sent_pitch(db, t, f"Hello,\n\nWe pulled 4 ... including {named} in Elizabeth (...).\n\nAlex",
                          subject=f"{named} + 3 more entity purchases in Union County")
    res = s9.STREAM.run(db, _ctx(tmp_path))
    (o,) = res.outbound
    assert o.key == f"s9:pitch:{t.id}:2026-W46" and o.kind == "pitch" and o.attachments == []
    assert o.in_reply_to == earlier.message_id and o.subject == f"Re: {earlier.subject}"
    assert sentences(o.body) == ["Should I send the Union County list over?"] and "$" not in o.body
    # the sample keeps last week's named purchase in row 1, in full
    _, text = _pdf_text(o.sample_attachments[0])
    assert text.index(named) < text.index(postal_title(deeds[3].property_location))
    assert "BLOCK 1 HOLDINGS LLC" in text


def test_targets_the_outbox_would_skip_never_take_a_pitch_slot(db, tmp_path):
    from datetime import timedelta
    for i in range(3):
        _deed(db, f"BLOCK {i} HOLDINGS LLC", price=1_000_000 + i)
    used = _target(db, "cost_segregation_firm")
    used.touches = 2
    recent = _target(db, "cost_segregation_firm")
    recent.touches, recent.last_contact_at = 1, NOW - timedelta(days=3)
    week = _target(db, "cost_segregation_firm")
    db.add(FirmOutbox(key=f"s9:pitch:{week.id}:2026-W46", stream="s9", kind="pitch", target_id=week.id,
                      to_email=week.email, subject="x", body="x", status="skipped"))
    fresh = _target(db, "cost_segregation_firm")
    db.flush()
    res = s9.STREAM.run(db, _ctx(tmp_path, pitches_per_run=1))
    assert [o.target_id for o in res.outbound] == [fresh.id]


def test_the_outbox_sends_touch1_bare_and_holds_the_masked_sample(db, tmp_path):
    from engine import firm_outbox as fo
    for i in range(3):
        _deed(db, f"BLOCK {i} HOLDINGS LLC", price=1_000_000 + i)
    t = _target(db, "cost_segregation_firm")
    (ob,) = s9.STREAM.run(db, _ctx(tmp_path)).outbound
    row = fo.enqueue(db, ob, NOW)
    assert row.status == "queued" and row.attachments_json == []
    held = db.query(FirmOutbox).filter_by(key=f"{ob.key}:sample").one()
    assert held.status == "held" and held.target_id == t.id and held.body == ob.sample_body
    assert [a["name"] for a in held.attachments_json] == ["s9_feed_2026-W46.sample.pdf"]
