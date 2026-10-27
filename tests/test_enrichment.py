import json
from datetime import date

from agents.enrichment.listing_agent_enricher import advertiser_contacts
from agents.enrichment.municipal_owner_layers import LAYERS, pin_key, record_is_current
from agents.enrichment.public_owner_resolver import parse_defendants
from agents.enrichment.skiptrace_pipeline import import_text, parse_people_text, search_links
from agents.scrapers.nj_tax_assessment_agent import address_mailing_key, mailing_key
from core.text import format_phone, normalize_phone, split_person_name
from tests.conftest import FIXTURES
from tests.factories import make_lead


def test_phone_normalization():
    assert normalize_phone("+1 (973) 555-1234") == "9735551234"
    assert normalize_phone("973.555.1234 ext 2") == "9735551234"
    assert normalize_phone("555-1234") is None
    assert format_phone("9735551234") == "(973) 555-1234"


def test_split_person_name():
    assert split_person_name("SAMPLE, QUINN Q") == ("Quinn", "Sample")
    assert split_person_name("Maria L. Gonzalez Jr.") == ("Maria", "Gonzalez")
    assert split_person_name("ROBERT & LINDA JONES")[1] == "Jones"


def test_defendant_parsing_roles():
    parties = parse_defendants("INDIA TESTWELL, INDIVIDUALLY AND AS ADMINISTRATOR OF THE ESTATE OF JULIETT TESTWELL, ET ALS.")
    roles = {p.role for p in parties}
    assert "HEIR_EXECUTOR" in roles and "DECEASED" in roles
    plain = parse_defendants("KILO SAMPSON, ET AL")
    assert [(p.name, p.role) for p in plain] == [("Kilo Sampson", "OWNER")]
    junk = parse_defendants("UNITED STATES OF AMERICA; STATE OF NEW JERSEY")
    assert not [p for p in junk if p.role in ("OWNER", "HEIR_EXECUTOR")]


def test_mailing_keys_ignore_state_and_zip():
    a = mailing_key("123 MAIN ST", "JERSEY CITY, NJ")
    b = mailing_key("123 MAIN STREET", "JERSEY CITY, N.J. 07306")
    assert a == b
    assert address_mailing_key("123 Main St, Jersey City, NJ 07306") == a


def test_listing_advertisers_flatten_agent_and_office_phones():
    raw = {"advertisers": [{"type": "seller", "name": "Pat Agent", "email": "PAT@EXAMPLE.COM",
                            "phones": [{"number": "973-555-0100", "type": "Mobile"}],
                            "broker": {"name": "Best Realty"},
                            "office": {"name": "Best Realty Newark", "phones": [{"number": "(973) 555-0200"}]}}]}
    out = advertiser_contacts(raw)
    assert {c["phone"] for c in out} == {"9735550100", "9735550200"}
    agent = next(c for c in out if c["phone"] == "9735550100")
    assert agent["phone_type"] == "wireless" and agent["company"] == "Best Realty" and agent["email"] == "pat@example.com"


SAMPLE_PASTE = """Quinn Q Sample
Age 61
Lives in Tampa, FL
(813) 555-0142 - Wireless
(973) 555-0199 Landline
Email: jqsmith@example.com
Possible Relatives: Mary Sample, Robert Sample

Associates: Someone Else
"""


def test_people_search_paste_parsing():
    parsed = parse_people_text(SAMPLE_PASTE)
    assert ("8135550142", "wireless") in [tuple(p) for p in parsed["phones"]]
    assert "9735550199" in [p[0] for p in parsed["phones"]]
    assert parsed["emails"] == ["jqsmith@example.com"]
    assert parsed["age"] == 61
    assert "Mary Sample" in parsed["relatives"]


def test_paste_import_creates_dnc_checked_contacts(db):
    lead = make_lead(db)
    result = import_text(db, lead, SAMPLE_PASTE, name="Quinn Q Sample", role="OWNER")
    assert result["contacts_added"] >= 2
    db.refresh(lead)
    phones = [c for c in lead.contacts if c.phone]
    assert {c.phone for c in phones} >= {"8135550142", "9735550199"}
    assert all(c.dnc_status == "not_checked" for c in phones)


def test_search_links_are_manual_urls():
    links = search_links("Quinn", "Sample", "Newark", "NJ", street="1 Test St", street_city="Newark, NJ")
    assert links["truepeoplesearch"].startswith("https://www.truepeoplesearch.com/results?name=Quinn%20Sample")
    assert "quinn-sample" in links["fastpeoplesearch"] and "address_search" in links


def test_municipal_layer_fixtures_normalize():
    for layer in LAYERS:
        data = json.loads((FIXTURES / "arcgis_owner_layers" / f"{layer.key}.json").read_text())
        recs = [layer.normalize(f["attributes"]) for f in data["features"]]
        assert recs and all(r["owner_name"].startswith("CITY OF") for r in recs)
        assert all(not set(f["attributes"]) & {"SocialSecu", "SpouseSoci"} for f in data["features"])
        assert all(len(r["owner_zip"]) == 5 for r in recs), layer.key
    jc = LAYERS[0].normalize(json.loads((FIXTURES / "arcgis_owner_layers/jersey_city_gis.json").read_text())
                             ["features"][0]["attributes"])
    assert jc["owner_zip"] == "07302" and jc["asof"] == date(2022, 8, 3) and jc["owner_city_state"].startswith("JERSEY CITY")
    pf = [LAYERS[2].normalize(f["attributes"]) for f in json.loads(
        (FIXTURES / "arcgis_owner_layers/plainfield_gis.json").read_text())["features"]]
    assert pf[0]["owner_zip"] == "07060" and any(r["deed_date"] for r in pf)


def test_layer_records_older_than_the_last_deed_are_skipped():
    assert pin_key("2012_618_21_ ") == "2012_618_21"
    rec = {"asof": date(2025, 9, 1), "deed_date": None}
    assert record_is_current(rec, date(2020, 1, 1), date(2025, 9, 1))
    assert not record_is_current(rec, date(2026, 3, 1), date(2025, 9, 1))
    assert record_is_current({"asof": None, "deed_date": date(2026, 2, 20)}, date(2026, 3, 1), date(2025, 1, 1))
