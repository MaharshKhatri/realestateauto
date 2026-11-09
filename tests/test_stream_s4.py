"""Stream 4: rehab trigger reports for contractors (engine/streams/s4_contractor_leads.py)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from engine.streams import s4_contractor_leads as s4
from engine.streams.base import StreamContext
from tests.factories import add_contact, make_lead

OP = {"name": "Sam Operator", "first": "Sam", "phone": "(973) 555-0199", "email": "sam@example.com",
      "postal": "1 Main St, Newark, NJ 07102"}


def _ctx(tmp_path, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=datetime.now().replace(microsecond=0), out_dir=tmp_path / "out", operator=OP, send=send,
                         limits=limits)


def _text(path) -> str:
    from pypdf import PdfReader
    return "\n".join(p.extract_text() for p in PdfReader(str(path)).pages)


def _pages(path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(path)).pages)


# -- the classifier -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    # oil tanks
    ("Underground oil tank on site.", {s4.OIL_TANK}),
    ("UST on property, sold as is.", {s4.OIL_TANK}),
    ("Tank sweep needed.", {s4.OIL_TANK}),
    ("Buyer responsible for tank sweep.", {s4.OIL_TANK}),
    ("Oil tank on property.", {s4.OIL_TANK}),
    ("The home is older and includes an oil tank.", {s4.OIL_TANK}),
    ("An underground oil tank filled with sand is located beneath the driveway.", {s4.OIL_TANK}),
    ("No oil tank.", set()),
    ("Oil tank removed in 2015 with NJDEP closure.", set()),
    ("Tank removed.", set()),
    ("Underground oil tank, removed in 2010.", set()),
    ("The underground oil tank has been properly decommissioned.", set()),
    ("Oil tank sweep done, clean.", set()),
    ("Tank sweep clean.", set()),
    ("Gas heat.", set()),
    ("New tankless hot water heater and two hot water tanks.", set()),
    ("Roth double-wall oil tank offers a space-saving heating solution.", set()),
    ("Lead-Free Certification and UST Oil Tank Scan documentation available.", set()),
    # roofs
    ("Needs a new roof.", {s4.ROOF}),
    ("Roof leaks in the rear bedroom.", {s4.ROOF}),
    ("New roof needed.", {s4.ROOF}),
    ("Roof at end of life.", {s4.ROOF}),
    ("No HOA fees and needs a new roof.", {s4.ROOF}),
    ("New roof.", set()),
    ("Roof 3 years old.", set()),
    ("Updated roof and siding.", set()),
    ("No roof leaks.", set()),
    ("Roof leak was repaired in 2022.", set()),
    ("The house does not need a new roof.", set()),
    ("Comes with a home warranty with roof leak protection.", set()),
    ("Private rooftop deck with city views.", set()),
    # heating / cooling
    ("No heat.", {s4.HVAC}),
    ("Boiler needs replacement.", {s4.HVAC}),
    ("Furnace not working.", {s4.HVAC}),
    ("Needs HVAC.", {s4.HVAC}),
    ("AC not working.", {s4.HVAC}),
    ("New roof 2021, needs HVAC.", {s4.HVAC}),
    ("New boiler.", set()),
    ("Updated HVAC.", set()),
    ("No heat or AC issues.", set()),
    ("Central air and gas forced-air heat keep things comfortable.", set()),
    # structure and damage
    ("Foundation issues.", {s4.STRUCTURAL}),
    ("Structural damage to the party wall.", {s4.STRUCTURAL}),
    ("Sagging floors upstairs.", {s4.STRUCTURAL}),
    ("Water damage in basement.", {s4.STRUCTURAL}),
    ("This fire-damaged property is sold as is.", {s4.STRUCTURAL}),
    ("Solid structural bones and a strong foundation.", set()),
    ("Numerous mechanical and structural updates.", set()),
    ("Foundation repaired!", set()),
    ("Recently completed foundation repairs.", set()),
    ("No water damage.", set()),
    # full rehab
    ("Gut rehab.", {s4.FULL_GUT}),
    ("Needs everything.", {s4.FULL_GUT}),
    ("Tear down.", {s4.FULL_GUT}),
    ("Contractor special!", {s4.FULL_GUT}),
    ("Investor special.", {s4.FULL_GUT}),
    ("Sold as-is, needs work.", {s4.FULL_GUT}),
    ("Property requires a TOTAL REHAB.", {s4.FULL_GUT}),
    ("A beautifully reimagined, gut-renovated single-family home.", set()),
    ("This home underwent gut renovation in 2018.", set()),
    ("Not a tear down.", set()),
    ("Sold as-is.", set()),
])
def test_classifier_triggers_and_negations(text, expected):
    assert set(s4.find_triggers(text)) == expected


def test_classifier_several_triggers_and_short_quoted_phrases():
    text = ("Sold as-is. Needs a new roof and the boiler needs replacement. Underground oil tank in the front yard. "
            "Being sold AS IS and the house needs work throughout the entire first and second floors plus the basement "
            "and garage. Call Mary at 973-555-1212 or mary@example.com for showings.")
    found = s4.find_triggers(text)
    assert list(found) == [s4.OIL_TANK, s4.ROOF, s4.HVAC, s4.FULL_GUT]       # several per listing, fixed order
    for phrase in found.values():
        assert 0 < s4.phrase_words(phrase) <= 12
        assert "Mary" not in phrase and "973" not in phrase and "@" not in phrase
    assert "needs work" in found[s4.FULL_GUT] and found[s4.FULL_GUT].endswith("...")   # cut mid-sentence: marked
    assert found[s4.OIL_TANK] == "Underground oil tank in the front yard"


def test_small_helpers():
    assert s4.deed_street("210 12 HILLSIDE RD") == "210-12 Hillside Rd"
    assert s4.deed_street("1025 27 GALLOPING HILL RD") == "1025-27 Galloping Hill Rd"
    assert s4.deed_street("12 3RD ST") == "12 3rd St"
    assert s4.county_key("ESSEX county ") == "Essex"
    assert s4.brokerage_label("RE/MAX PROFESSIONALS I -  Broker") == "RE/MAX PROFESSIONALS I"
    assert s4.brokerage_label("Fathom ") == "Fathom"
    assert s4.brokerage_label("Christian Sample") == ""                       # could be a person: left out
    excerpt = s4.excerpt("Call Pat Agent now, the furnace not working since May", 30, 49)
    assert "Pat" not in excerpt
    found = s4.find_triggers("Needs a new roof and a 100 amp panel, 973 555 1212 anytime.")
    assert found[s4.ROOF].startswith("Needs a new roof and a 100 amp panel") and "555" not in found[s4.ROOF]


def test_fresh_purchases_entities_only_window_and_package_deeds():
    today = date(2026, 11, 9)

    def buy(addr, days, price=300_000, cls="2"):
        return {"address": addr, "deed_date": (today - timedelta(days=days)).isoformat(), "price": price, "class": cls,
                "county": "Essex", "municipality": "NEWARK CITY"}

    fams = {
        "a": [SimpleNamespace(entity_name="ACME FLIP HOMES LLC", recent_purchases_json=[
            buy("1 NEW ST", 10), buy("1 NEW ST", 10), buy("2 OLD ST", 200), buy("3 SHOP ST", 20, cls="4A"),
            buy("4 PKG ST", 5, price=900_000), buy("5 PKG ST", 5, price=900_000)])],
        "b": [SimpleNamespace(entity_name="QUINN Q SAMPLE", recent_purchases_json=[buy("6 PERSON ST", 10)])],
        "c": [SimpleNamespace(entity_name="Owner @ 9 ELM ST, NEWARK, NJ", recent_purchases_json=[buy("7 CLUSTER ST", 10)])],
    }
    got = s4.fresh_purchases(fams, today, 120)
    assert [x["address"] for x in got] == ["1 NEW ST"]
    assert got[0]["entity"] == "ACME FLIP HOMES LLC"


def test_select_rows_trade_rows_lead_the_sample():
    def row(street, trig, rank, source="listing"):
        return s4.Row(county="Essex", town="Newark", street=street, triggers=[trig], words="x", price=1, days=1,
                      brokerage="", source=source, rank=rank)
    rows = [row("gut", s4.FULL_GUT, 4.0), row("tank", s4.OIL_TANK, 2.6), row("roof", s4.ROOF, 2.1),
            row("flip", s4.FRESH_FLIP, 2.5, "deed")]
    assert [r.street for r in s4.select_rows(rows)] == ["gut", "tank", "roof", "flip"]
    assert [r.street for r in s4.select_rows(rows, lead=(s4.OIL_TANK,))][0] == "tank"
    assert [r.street for r in s4.select_rows(rows, lead=(s4.FULL_GUT, s4.FRESH_FLIP))][:2] == ["gut", "flip"]
    assert len(s4.select_rows(rows, limit=2)) == 2


def test_pitch_body_is_plain_short_and_truthful():
    from engine import human_copy as hc
    for kind in s4.TRADE_TRIGGERS:
        body = s4.pitch_body("Middlesex", 24, 12, kind, 500, "Sam")
        assert hc.lint(body) == [], (kind, hc.lint(body))
        assert hc.word_count(body) <= 100                                 # margin under the 110-word limit
        assert body.splitlines()[-1] == "Sam" and "$500/month" in body
        low = body.lower()
        assert "referral" not in low and "client" not in low and "—" not in body and "guarantee" not in low


# -- the stream on a database -------------------------------------------------------------------------------------
def _listing(db, text, **kw):
    lead = make_lead(db, sheriff_days=kw.pop("sheriff_days", None), stale_listing=True, listing_text=text, **kw)
    return lead


def _desk(db):
    """Three Essex listings with triggers, one without, a fresh entity flip, a person's and an old purchase."""
    from db.models import CashBuyer
    tank = _listing(db, "Charming colonial. Underground oil tank in the front yard, buyer responsible for removal.",
                    sheriff_days=20)                                      # also a sheriff sale with a defendant name
    roof = _listing(db, "Needs a new roof and many repairs. Priced accordingly.")
    gut = _listing(db, "Contractor special! Needs everything, bring your contractor.")
    plain = _listing(db, "Move-in ready with a new roof and a new boiler. No oil tank.")
    add_contact(db, gut, "LISTING_AGENT", name="Pat Agent", phone="9735550100", company="Sample Realty LLC",
                email="pat@samplerealty.example")
    today = date.today()

    def buy(addr, days):
        return {"address": addr, "municipality": "NEWARK CITY", "county": "Essex", "price": 210000, "class": "2",
                "deed_date": (today - timedelta(days=days)).isoformat(), "units": 1, "block": "900", "lot": addr[:2],
                "mun_code": "0714"}
    db.add_all([
        CashBuyer(entity_key="ACMEFLIP", entity_name="ACME FLIP HOMES LLC", mailing_address="1 OFFICE PARK, NEWARK, NJ 07102",
                  recent_purchases_json=[buy("77 FLIP ST", 30), buy("66 OLD ST", 200)]),
        CashBuyer(entity_key="PERSON", entity_name="JOHN Q PUBLIC", mailing_address="5 HOME LN, NEWARK, NJ 07102",
                  recent_purchases_json=[buy("88 PRIVATE ST", 20)]),
        CashBuyer(entity_key="CLUSTER", entity_name="Owner @ 9 ELM ST, NEWARK, NJ", mailing_address="9 ELM ST, NEWARK, NJ 07102",
                  recent_purchases_json=[buy("99 CLUSTER ST", 20)]),
    ])
    db.flush()
    return tank, roof, gut, plain


def test_report_rows_privacy_and_one_page(db, tmp_path):
    from sqlalchemy import select

    from db.models import Listing
    tank, roof, gut, plain = _desk(db)
    db.execute(Listing.__table__.update().where(Listing.property_id == gut.property_id).values(
        description="Contractor special! Needs everything, bring your contractor. Call Pat Agent 973-555-0100."))
    res = s4.STREAM.run(db, _ctx(tmp_path))
    assert res.products == 1 and res.found == 4
    assert res.metrics["listings_triggered"] == 3 and res.metrics["fresh_flips"] == 1
    assert res.metrics["trig_oil_tank"] == 1 and res.metrics["trig_roof"] == 1 and res.metrics["trig_full_gut"] == 1
    pdf = next((tmp_path / "out").glob("s4_rehab_triggers_essex_*.pdf"))
    assert _pages(pdf) == 1
    text = _text(pdf)
    assert "NJ Rehab Trigger Report: Essex County" in text
    for lead in (tank, roof, gut):
        assert lead.property.street_address.title() in text
    assert plain.property.street_address.title() not in text            # "new roof", "no oil tank": no trigger
    assert "77 Flip St" in text and "ACME FLIP HOMES LLC" in text and "Sample Realty LLC" in text
    for secret in ("QUINN", "Pat Agent", "JOHN Q PUBLIC", "88 Private", "99 Cluster", "66 Old", "555-0100",
                   "samplerealty", "Owner @"):
        assert secret not in text, secret
    assert "data, not an appraisal" in text and "no more" in text and "Sam Operator" in text
    assert "referral fee" not in text.lower().replace("no referral fees", "")
    blob = repr(res.metrics) + repr(res.notes)                           # the public run log: counts only
    assert "Test St" not in blob and "ACME" not in blob and "@" not in blob
    assert len(db.scalars(select(Listing)).all()) == 4


def test_suppressed_address_is_left_out(db, tmp_path):
    from engine.compliance import suppress_address
    tank, roof, gut, _ = _desk(db)
    p = roof.property
    suppress_address(db, f"{p.street_address}, {p.city} NJ {p.zip_code}", "daniels_law")
    suppress_address(db, "77 FLIP ST, Newark NJ", "stop_mail")
    res = s4.STREAM.run(db, _ctx(tmp_path, min_rows=1))
    text = _text(next((tmp_path / "out").glob("s4_rehab_triggers_essex_*.pdf")))
    assert p.street_address.title() not in text and "77 Flip St" not in text
    assert tank.property.street_address.title() in text
    assert res.metrics["suppressed"] == 2


def test_wrong_parcel_and_pending_listing_skipped(db, tmp_path):
    from db.models import Listing
    tank, roof, gut, _ = _desk(db)
    db.execute(Listing.__table__.update().where(Listing.property_id == roof.property_id).values(status="pending"))
    db.execute(Listing.__table__.update().where(Listing.property_id == gut.property_id).values(lat=40.0, lon=-74.9))
    res = s4.STREAM.run(db, _ctx(tmp_path, min_rows=1))
    assert res.metrics["wrong_parcel"] == 1
    assert res.metrics["listings_triggered"] == 1                        # only the tank listing is live and right


def test_pitch_matches_trade_county_and_caps(db, tmp_path):
    from db.models import FirmTarget
    tank, roof, gut, _ = _desk(db)

    def target(kind, county="Essex", stage="NEW", score=0.0):
        t = FirmTarget(kind=kind, stream="s4", org=f"{kind} co", email=f"{kind}.{county}.{stage}@example.com".lower(),
                       county=county, stage=stage, score=score)
        db.add(t)
        return t
    tk = target("oil_tank_contractor", score=9)
    target("roofer", score=8)
    target("hvac_contractor", score=7)                                   # no heating rows in Essex: no sample to send
    target("rehab_general_contractor", score=6)
    target("oil_tank_contractor", county="Bergen", score=10)             # no Bergen report
    target("oil_tank_contractor", stage="LOST", score=11)
    target("cpa", score=12)                                              # not a kind this stream sells to
    db.flush()
    wide = s4.STREAM.run(db, _ctx(tmp_path, pitches_per_run=10))
    assert sorted(o.attachments[0].name.split("_essex_")[1].rsplit("_", 1)[0] for o in wide.outbound) == [
        "oil_tank_contractor", "rehab_general_contractor", "roofer"]
    res = s4.STREAM.run(db, _ctx(tmp_path, pitches_per_run=2))
    assert len(res.outbound) == 2 and res.metrics["pitches"] == 2
    ob = res.outbound[0]
    y, w, _ = datetime.now().isocalendar()
    assert ob.target_id == tk.id and ob.key == f"s4:pitch:{tk.id}:{y}-W{w}" and ob.stream == "s4"
    assert ob.kind == "pitch" and ob.priority == 3 and ob.to == tk.email
    assert ob.subject.startswith("Essex County rehab trigger report") and not ob.subject.lower().startswith("re:")
    sample = ob.attachments[0]
    assert sample.exists() and _pages(sample) == 1 and "oil_tank_contractor" in sample.name
    text = _text(sample)
    # the tank row leads the tank contractor's sample; the general report leads with the heavier gut job
    assert text.index(tank.property.street_address.title()) < text.index(gut.property.street_address.title())
    general = _text(next(p for p in (tmp_path / "out").glob("s4_rehab_triggers_essex_*.pdf")
                         if "contractor" not in p.name and "roofer" not in p.name))
    assert general.index(gut.property.street_address.title()) < general.index(tank.property.street_address.title())
    assert "1 pointing to an oil tank" in ob.body
    assert res.outbound[1].subject.startswith("Essex County") and "roofer" in res.outbound[1].attachments[0].name


def test_keys_stable_no_resend_and_dry_run(db, tmp_path):
    from db.models import FirmTarget
    from engine import firm_outbox as fo
    _desk(db)
    t = FirmTarget(kind="roofer", stream="s4", org="Roof Co", email="office@roofco.example", county="Essex", stage="NEW")
    db.add(t)
    db.flush()
    dry = s4.STREAM.run(db, _ctx(tmp_path, send=False))
    assert dry.outbound == [] and dry.products == 1                     # a dry run still writes the report
    first = s4.STREAM.run(db, _ctx(tmp_path))
    again = s4.STREAM.run(db, _ctx(tmp_path))
    assert [o.key for o in first.outbound] == [o.key for o in again.outbound] == [f"s4:pitch:{t.id}:"
                                                                                   f"{datetime.now().isocalendar()[0]}-W"
                                                                                   f"{datetime.now().isocalendar()[1]}"]
    assert fo.enqueue(db, first.outbound[0]) is not None
    assert s4.STREAM.run(db, _ctx(tmp_path)).outbound == []              # this week's key is used: nothing again


def test_report_fits_one_page_with_many_rows(db, tmp_path):
    for i in range(34):
        _listing(db, f"Estate sale, sold strictly as-is and needs extensive repairs throughout, unit {i} has fire "
                     "damage in the kitchen and the roof leaks badly in two bedrooms upstairs.")
    res = s4.STREAM.run(db, _ctx(tmp_path, report_rows=34))
    pdf = next((tmp_path / "out").glob("s4_rehab_triggers_essex_*.pdf"))
    assert _pages(pdf) == 1 and 3 <= res.found < 34                       # trimmed to fit, never a second page


def test_empty_database(db, tmp_path):
    res = s4.STREAM.run(db, _ctx(tmp_path))
    assert (res.processed, res.found, res.products, res.outbound) == (0, 0, 0, [])
    assert res.metrics["counties_reported"] == 0
