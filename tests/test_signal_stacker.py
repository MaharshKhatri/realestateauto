"""Signal stacker: listing cross-match (incl. condo ambiguity), portfolio owners, tax-sale local parcel match."""
from datetime import datetime

from agents.enrichment.signal_stacker import (ListingIndex, SignalStackerAgent, house_number, owner_keys,
                                               portfolio_groups, zip5)
from agents.scrapers.tax_sale_agent import local_parcels
from db.models import DistressSignal, Listing, Property, SignalType
from tests.factories import make_lead


def listing(ref, address, zip_code="07106", lat=None, lon=None, **kw):
    return Listing(source="homeharvest", source_ref=ref, address=address, zip_code=zip_code, lat=lat, lon=lon,
                   status="for_sale", list_price=kw.get("list_price", 300000), days_on_market=kw.get("dom", 60),
                   url=f"https://listing.test/{ref}", last_seen_at=datetime(2026, 10, 26))


def test_helpers():
    assert zip5("07106-1234") == "07106" and zip5(None) == ""
    assert house_number("12-14 Main St") == "12" and house_number("Main St") == ""


def test_listing_index_by_address_and_by_distance():
    a = listing("A", "12 Test Street", "07106")
    b = listing("B", "40 Elm Ave", "07106", lat=40.7300, lon=-74.2000)
    idx = ListingIndex.build([a, b])
    by_addr = Property(street_address="12 TEST ST", zip_code="07106")
    assert idx.match(by_addr) is a
    near = Property(street_address="40 ELM AVENUE REAR", zip_code="07107", lat=40.7301, lon=-74.2001)
    assert idx.match(near) is b                               # same house number, ~15 m away
    far = Property(street_address="40 ELM AVE", zip_code="07999", lat=40.80, lon=-74.20)
    assert idx.match(far) is None


def test_owner_keys_skip_city_hall_care_of_and_people():
    llc = Property(owner_name="BRICK CITY HOLDINGS LLC", owner_mailing_address="PO BOX 77",
                   owner_city_state_zip="NEWARK, NJ 07101", street_address="5 A ST", city="Newark")
    assert "name:BRICK CITY HOLDINGS LLC" in owner_keys(llc) and any(k.startswith("mail:") for k in owner_keys(llc))
    city = Property(owner_name="CITY OF NEWARK", owner_mailing_address="920 BROAD ST",
                    owner_city_state_zip="NEWARK, NJ 07102", street_address="9 B ST", city="Newark")
    assert owner_keys(city) == []
    care_of = Property(owner_name="JANE ROE", owner_mailing_address="C/O ACME MGMT 1 MAIN ST",
                       owner_city_state_zip="NEWARK, NJ 07102", street_address="9 C ST", city="Newark")
    assert owner_keys(care_of) == []                            # a person's name alone never groups


def test_portfolio_groups_union_find():
    def p(i, name, mail):
        return Property(id=i, owner_name=name, owner_mailing_address=mail, owner_city_state_zip="MIAMI, FL 33101",
                        street_address=f"{i} X ST", city="Newark")
    props = [p(1, "ALPHA LLC", "1 OCEAN DR"), p(2, "ALPHA LLC", "9 OTHER RD"), p(3, "BETA LLC", "9 OTHER RD"),
             p(4, "GAMMA LLC", "5 LONE WAY")]
    groups = portfolio_groups(props)
    assert len(groups) == 1 and sorted(x.id for x in groups[0]) == [1, 2, 3]   # name links 1-2, mailing links 2-3


def test_stacker_agent_listed_foreclosure_condo_ambiguity_and_portfolios(db):
    fore = make_lead(db, street_address="12 TEST ST", zip_code="07106", owner_name="ALPHA HOLDINGS LLC",
                     owner_mailing_address="1 OCEAN DR", owner_city_state_zip="MIAMI, FL 33101", is_absentee=True)
    second = make_lead(db, owner_name="ALPHA HOLDINGS LLC", owner_mailing_address="1 OCEAN DR",
                       owner_city_state_zip="MIAMI, FL 33101", is_absentee=True)
    unit1 = make_lead(db, street_address="300 TOWER RD UNIT 1", zip_code="07106", lat=None, lon=None)
    unit2 = make_lead(db, street_address="300 TOWER RD UNIT 2", zip_code="07106", lat=None, lon=None)
    db.add_all([listing("L-12", "12 Test St", "07106", dom=45),
                listing("L-300", "300 Tower Rd Unit 1", "07106"), listing("L-300B", "300 Tower Rd Unit 1", "07106")])
    db.commit()
    run = SignalStackerAgent(no_agents=True).execute()
    assert run.status == "success"
    db.expire_all()
    sig = db.query(DistressSignal).filter_by(property_id=fore.property_id, signal_type=SignalType.ACTIVE_LISTING).one()
    assert sig.raw_metadata_json["days_on_market"] == 45 and "SHERIFF_SALE" in sig.raw_metadata_json["stacked_with"]
    u1 = {s.signal_type for s in db.get(Property, unit1.property_id).active_signals}
    u2 = {s.signal_type for s in db.get(Property, unit2.property_id).active_signals}
    assert SignalType.ACTIVE_LISTING in u1 and SignalType.ACTIVE_LISTING not in u2   # exact unit wins
    port = db.query(DistressSignal).filter_by(signal_type=SignalType.PORTFOLIO_OWNER).all()
    assert {s.property_id for s in port} == {fore.property_id, second.property_id}
    assert all(s.raw_metadata_json["properties"] == 2 for s in port)
    # idempotent: a second run doesn't duplicate, and a listing that disappears deactivates its signal
    db.query(Listing).filter_by(source_ref="L-12").delete()
    db.commit()
    SignalStackerAgent(no_agents=True).execute()
    db.expire_all()
    assert not db.get(DistressSignal, sig.id).active
    assert db.query(DistressSignal).filter_by(signal_type=SignalType.PORTFOLIO_OWNER).count() == 2


def test_tax_sale_matches_local_parcels_first(db):
    lead = make_lead(db, block="120", lot="7", mun_code="0714")
    hits = local_parcels(db, "0714", [("120", "7", ""), ("999", "1", "")])
    assert hits[("120", "7", "")].id == lead.property_id and ("999", "1", "") not in hits
