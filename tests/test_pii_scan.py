from core import pii_scan
from core.pii_scan import person_names, scan
from tests.factories import add_contact, make_lead


def test_scan_finds_database_names_in_files(db, tmp_path, monkeypatch):
    lead = make_lead(db, defendant="ZEPHYRA QUILLON, ET AL", owner_name="ZEPHYRA QUILLON")
    add_contact(db, lead, name="Orvil Brandywine", phone=None)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agentname", phone="9735550100")
    names = person_names(db)
    assert ("ZEPHYRA", "QUILLON") in names and ("ORVIL", "BRANDYWINE") in names
    assert ("PAT", "AGENTNAME") not in names            # listing agents are business contacts, not private owners
    leaky = tmp_path / "notes.md"
    leaky.write_text("Called the owner.\nQUILLON, ZEPHYRA said maybe.\nOrvil   Brandywine is the heir.\n")
    clean = tmp_path / "clean.md"
    clean.write_text("Quillon Road and Brandywine Ave are streets.\n")
    monkeypatch.setattr(pii_scan, "ROOT", tmp_path)
    hits = scan(db, [leaky, clean])
    assert sorted((h.path, h.line) for h in hits) == [("notes.md", 2), ("notes.md", 3)]


def test_lender_and_trust_fragments_are_not_people(db):
    lead = make_lead(db, defendant="SAMPLE BANK NATIONAL TRUST COMPANY, AS TRUSTEE FOR ASSET-BACKED CERTIFICATES")
    add_contact(db, lead, name="Asset-Backed Pass Through Certificates", phone=None)
    add_contact(db, lead, name="Mortgage Pass-Through Certificates Series", phone=None)
    assert not {n for n in person_names(db) if "CERTIFICATES" in n or "ASSET-BACKED" in n}
