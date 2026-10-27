from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from engine import compliance
from tests.factories import add_contact, make_lead

ET = ZoneInfo("America/New_York")


def test_parse_dnc_lines_accepts_ftc_formats():
    lines = ["2015551234", "201,5551235", "201-555-1236", "(973) 555-1237", "garbage", ""]
    assert list(compliance.parse_dnc_lines(lines)) == ["2015551234", "2015551235", "2015551236", "9735551237"]


def test_calling_window_is_8am_to_9pm_eastern():
    assert not compliance.within_calling_hours(datetime(2026, 10, 27, 7, 59, tzinfo=ET))
    assert compliance.within_calling_hours(datetime(2026, 10, 27, 8, 0, tzinfo=ET))
    assert compliance.within_calling_hours(datetime(2026, 10, 27, 20, 59, tzinfo=ET))
    assert not compliance.within_calling_hours(datetime(2026, 10, 27, 21, 0, tzinfo=ET))


def test_ftc_load_classifies_numbers_and_flags_contacts(db, tmp_path):
    lead = make_lead(db)
    owner = add_contact(db, lead, phone="9735551234")
    agent = add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735559999")
    # nothing loaded yet: an owner number is unscrubbed, an agent's advertised number is B2B
    assert compliance.classify_number(db, owner.phone) == "not_checked"
    assert compliance.classify_number(db, agent.phone, b2b=True) == "b2b"
    f = tmp_path / "973.txt"
    f.write_text("9735551234\n9735550000\n")
    assert compliance.load_ftc_file(db, f, list_date=date.today()) == 2
    db.refresh(owner)
    assert owner.dnc_status == "dnc"
    assert compliance.classify_number(db, "9735557777") == "clear"            # 973 covered by fresh data
    assert compliance.classify_number(db, "2015557777") == "not_checked"      # 201 not downloaded
    ok, why = compliance.can_call(db, owner, datetime(2026, 10, 27, 12, tzinfo=ET))
    assert not ok and "do-not-call" in why


def test_stale_ftc_data_is_not_a_scrub(db, tmp_path):
    f = tmp_path / "973.txt"
    f.write_text("9735550000\n")
    compliance.load_ftc_file(db, f, list_date=date.today() - timedelta(days=40))
    assert not compliance.ftc_data_status(db)["fresh"]
    assert compliance.classify_number(db, "9735557777") == "not_checked"


def test_internal_and_daniels_law_suppression(db):
    lead = make_lead(db)
    c = add_contact(db, lead, phone="2015550101")
    compliance.add_internal(db, "(201) 555-0101", source="daniels_law", notes="nondisclosure request")
    db.refresh(c)
    assert c.dnc_status == "dnc"
    assert compliance.suppressed_for_daniels_law(db, "201-555-0101")
    assert compliance.add_internal(db, "12") is None
