"""Virtual inspection dossier and direct-mail letters (kinds, suppression, proof mode, Click2Mail CSV)."""
import csv
import json
from datetime import date

import pytest

from core.config import settings
from core.dates import long_date
from core.text import postal_title
from db.models import Listing
from engine import compliance
from engine import direct_mail as dm
from engine.virtual_inspector import build_dossier, condition_flags, map_links
from tests.factories import add_contact, make_lead

BANNED = ("drive by", "door-knock", "stop by the house", "i'll come see it")


def test_postal_title_and_long_date():
    assert postal_title("306 13TH STREET") == "306 13th Street"
    assert postal_title("BROOKLYN, NY 11215") == "Brooklyn, NY 11215"
    assert postal_title("PO BOX 12") == "PO Box 12" and postal_title("7 O'BRIEN PL") == "7 O'Brien Pl"
    assert long_date(date(2026, 11, 6)) == "November 6, 2026" and long_date(date(2026, 11, 6), year=False) == "November 6"
    assert dm.split_city_state_zip("Closter, NJ 07624") == ("Closter", "NJ", "07624")


def test_map_links_and_condition_flags():
    links = map_links("12 Test St, Newark, NJ 07106", 40.73, -74.2, "https://listing.test/1")
    assert "map_action=pano" in links["street_view"] and "basemap=satellite" in links["satellite"]
    assert links["listing_photos"] == "https://listing.test/1" and "msc.fema.gov" in links["fema_flood"]
    assert "street_view" not in map_links("12 Test St", None, None)
    flags = {f["flag"] for f in condition_flags("Sold as-is. Knob and tube, oil tank in basement, roof leaks. "
                                                "Tenant occupied, month to month.", [], 1925)}
    assert {"as_is", "electrical", "oil_tank", "roof", "tenant", "lead_paint"} <= flags
    assert "old_wiring_risk" not in flags and "oil_tank_risk" not in flags     # already flagged from the text
    assert {f["flag"] for f in condition_flags("", [], 1950)} == {"lead_paint", "old_wiring_risk", "oil_tank_risk"}


def test_dossier_for_a_listing(db):
    lead = make_lead(db, sheriff_days=None, stale_listing=True, year_built=1950)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100")
    sig = lead.property.active_signals[0]
    db.add(Listing(source="homeharvest", source_ref=sig.source_ref, address="1 Test St", zip_code="07106",
                   description="Investor special. Wet basement, sump pump. Oil heat.", status="for_sale"))
    db.flush()
    d = build_dossier(db, lead)
    kinds = {f["flag"] for f in d.flags}
    assert {"water", "oil_tank", "as_is", "lead_paint"} <= kinds
    assert d.checklist[0].startswith("PRIORITY") and "street_view" in d.links
    assert "sms_agent" in d.requests and "Investor special" in d.listing_excerpt
    text = " ".join(d.requests.values()).lower()
    assert "video" in text and not any(b in text for b in BANNED)


@pytest.fixture()
def operator(monkeypatch):
    op = settings()["operator"]
    for k, v in {"name": "Jordan Operator", "phone": "9735550199", "mailing_address": "PO Box 10, Newark, NJ 07101",
                 "company_name": "Garden State Home Buyers LLC"}.items():
        monkeypatch.setitem(op, k, v)


def test_letter_kinds_and_compliant_wording(db, operator):
    pre = make_lead(db, owner_name="ROE, JANE", owner_mailing_address="306 13TH STREET",
                    owner_city_state_zip="BROOKLYN, NY 11215")
    estate = make_lead(db, owner_name="DOE, JOHN ESTATE OF", is_estate_owned=True)
    landlord = make_lead(db, sheriff_days=None, absentee=True, owner_name="ROE, RICHARD")
    letters = {lt.kind: lt for lt in (dm.build_letter(x) for x in (pre, estate, landlord))}
    assert set(letters) == {"pre_auction", "estate", "landlord"}
    p = letters["pre_auction"]
    assert p.address_lines == ["306 13th Street", "Brooklyn, NY 11215"]
    body = " ".join(p.paragraphs)
    assert "cannot stop, postpone or negotiate your foreclosure" in body
    assert "1-800-569-4287" in body and "assign our contract" in body and "attorney review" in body
    assert "STOP" in body and "Foreclosure Rescue Fraud Prevention Act" in body
    assert "Personal Representative" in letters["estate"].recipient or "Family" in letters["estate"].recipient
    for lt in letters.values():
        assert not any(b in " ".join(lt.paragraphs).lower() for b in BANNED)


def test_eligibility_suppression_and_callable_skip(db, operator):
    mail_me = make_lead(db, owner_name="ROE, JANE")
    callable_lead = make_lead(db, owner_name="ROE, JOHN")
    add_contact(db, callable_lead, phone="9735550123")
    assert dm.eligible(db, mail_me) == (True, "ok")
    assert dm.eligible(db, callable_lead) == (False, "has a phone — call instead")
    assert dm.eligible(db, callable_lead, include_callable=True)[0]
    p = mail_me.property
    compliance.suppress_address(db, f"{p.street_address}, {p.city}", source="daniels_law")
    assert dm.eligible(db, mail_me) == (False, "address suppressed")


def test_batch_files_csv_and_mark_mailed(db, tmp_path, operator, monkeypatch):
    lead = make_lead(db, owner_name="ROE, JANE", owner_mailing_address="306 13TH STREET",
                     owner_city_state_zip="BROOKLYN, NY 11215")
    db.flush()
    out, letters = dm.write_batch(db, 10, tmp_path / "batch")
    assert [lt.lead_id for lt in letters] == [lead.id]
    assert (out / "pre_auction.pdf").read_bytes().startswith(b"%PDF") and (out / "letters.html").exists()
    rows = list(csv.DictReader((out / "mailing_list.csv").open()))
    assert rows[0]["Name"] == "Jane Roe" and rows[0]["City"] == "Brooklyn" and rows[0]["State"] == "NY"
    assert rows[0]["Zip"] == "11215" and rows[0]["Address1"] == "306 13th Street"
    assert json.loads((out / "manifest.json").read_text())["proof"] is False
    assert dm.mark_mailed(db, out) == 1
    assert any(i.outcome == "LETTER_SENT" for i in lead.interactions)


def test_proof_batch_is_watermarked_and_cannot_be_marked(db, tmp_path, monkeypatch):
    monkeypatch.setitem(settings()["operator"], "mailing_address", "")
    make_lead(db, owner_name="ROE, JANE")
    db.flush()
    out, letters = dm.write_batch(db, 10, tmp_path / "proof")
    assert letters and dm.PROOF_MARK in (out / "letters.html").read_text()
    with pytest.raises(ValueError, match="PROOF"):
        dm.mark_mailed(db, out)
