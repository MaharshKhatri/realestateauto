"""engine/partner_principals.py: mailing-address families, principal evidence, ranking and the partner skip-trace."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from db.models import CashBuyer, Contact, DeedTransfer, JVPartner
from engine import clipboard_skiptrace as cs
from engine import compliance
from engine import jv_partner_vault as vault
from engine import partner_principals as pp
from tests.factories import make_lead

FIX = Path(__file__).parent / "fixtures" / "skiptrace"
TODAY = date(2026, 10, 29)


def buyer(db, key, name, mailing, purchases, contact=None, flips=0, source="njgin_mailing_cluster"):
    b = CashBuyer(entity_key=key, entity_name=name, contact_name=contact, mailing_address=mailing,
                  recent_purchases_json=purchases, cash_purchases_count=len(purchases), flips_count=flips, source=source)
    db.add(b)
    db.flush()
    return b


def buy(address, price, when="2025-06-01", town="NEWARK CITY", county="Essex"):
    return {"address": address, "municipality": town, "county": county, "price": price, "deed_date": when, "class": "2"}


def test_display_name_and_person_rules():
    assert pp.display_name("CHULANI,JITESH M & DEVNANI,SANGEETA") == "Jitesh M Chulani & Sangeeta Devnani"
    assert pp.display_name("RIOS, DANIEL") == "Daniel Rios"
    assert pp._person("QUINN Q SAMPLE") and not pp._person("SAMPLE HOLDINGS LLC") and not pp._person("OWNER @ 9 MAIL ST")
    assert not pp._person("77 CLERK ST.CONDOMINIUM ASSOC.") and pp._entity("413 LIBERTY, LLC")


def test_family_merge_principal_evidence_and_rank(db):
    # one operator, two LLCs, two spellings of the same mailing address ("N J" / "DRIVE" vs "DR")
    a = buyer(db, "A", "SAMPLE ONE LLC", "199 EXAMPLE DRIVE, CEDAR GROVE, N J",
              [buy("1 A ST", 300000, "2025-01-10"), buy("2 B ST", 350000, "2025-03-01")])
    b = buyer(db, "B", "SAMPLE TWO LLC", "199 EXAMPLE DR, CEDAR GROVE, NJ",
              [buy("3 C ST", 410000, "2026-02-01"), buy("1 A ST", 300000, "2025-01-10")])
    lone = buyer(db, "C", "ONE HOUSE LLC", "5 OTHER RD, EDISON, NJ", [buy("9 Z ST", 250000, "2024-01-01")])
    # a person at the same mailing address on a recorded deed, and a contact elsewhere
    db.add(DeedTransfer(parcel_id="UNION-2004-1-1", county="Union", property_location="7 Q ST", grantor="SELLER, X",
                        grantee="SAMPLE, QUINN Q", grantee_address="199 EXAMPLE DR, CEDAR GROVE, NJ", sale_price=300000,
                        deed_date=date(2025, 5, 1), source="test"))
    db.add(Contact(contact_type="OWNER", name="SAMPLE, QUINN Q", mailing_address="199 EXAMPLE DRIVE, CEDAR GROVE, NJ",
                   source="test", confidence_score=0.5))
    db.flush()
    for x in (a, b, lone):
        db.add(JVPartner(buyer_id=x.id, entity_name=x.entity_name, status="TARGET"))
    db.flush()
    rows = pp.refresh(db, idx=pp.build_index(db, imports=Path("/nonexistent")), today=TODAY)
    assert len(rows) == 2                                  # the second LLC of the same operator was merged away
    top = rows[0]
    assert top.buyer_id == a.id and top.family_stats["purchases"] == 3          # 1 A ST counted once
    assert top.family_stats["deployed"] == 1060000 and set(top.family_entities) >= {"Sample One LLC", "Sample Two LLC"}
    assert top.principal_name == "Quinn Q Sample" and top.family_stats["principal_confidence"] == 0.75
    assert "recorded deed: same mailing address" in top.principal_basis
    r = pp.desk_row(top, 1)
    assert r["icebreaker"]["address"] == "3 C St" and r["links"]["truepeoplesearch"].startswith("https://www.truepeoplesearch.com")
    assert "Cedar%20Grove%2C%20NJ" in r["links"]["truepeoplesearch_address"] and "opencorporates" in r["links"]
    assert rows[1].principal_name is None and "address search" in rows[1].principal_basis
    assert rows[0].velocity_score > rows[1].velocity_score


def test_shared_office_and_own_name_and_suppression(db):
    office = buyer(db, "O", "TOWER HOLDINGS LLC", "500 MAIN ST STE 200, NEWARK, NJ", [buy("1 A ST", 300000)])
    for i in range(6):
        db.add(Contact(contact_type="OWNER", name=f"PERSON{i}, ALEX", mailing_address="500 MAIN ST STE 200, NEWARK, NJ",
                       source="test", confidence_score=0.5))
    person = buyer(db, "P", "SAMPLE, JORDAN", "12 HOME LN, LINDEN, NJ", [buy("4 D ST", 200000), buy("5 E ST", 220000)])
    hidden = buyer(db, "H", "QUIET LLC", "8 PRIVATE CT, WAYNE, NJ", [buy("6 F ST", 280000)])
    compliance.suppress_address(db, "8 PRIVATE CT, WAYNE, NJ", source="daniels_law")
    for x in (office, person, hidden):
        db.add(JVPartner(buyer_id=x.id, entity_name=x.entity_name, status="TARGET"))
    db.flush()
    pp.refresh(db, idx=pp.build_index(db, imports=Path("/nonexistent")), today=TODAY)
    by = {p.entity_name: p for p in db.query(JVPartner)}
    assert by["TOWER HOLDINGS LLC"].principal_name is None and by["TOWER HOLDINGS LLC"].family_stats["shared_office"]
    assert by["TOWER HOLDINGS LLC"].mailing_kind == "mailbox / suite"
    assert by["SAMPLE, JORDAN"].principal_name == "Jordan Sample"
    q = by["QUIET LLC"]
    assert q.mailing_kind.startswith("suppressed") and "truepeoplesearch_address" not in pp.lookup_links(q)


def test_partner_skiptrace_queue_and_paste(db):
    b = buyer(db, "A", "SAMPLE ONE LLC", "199 EXAMPLE DR, CEDAR GROVE, NJ", [buy("1 A ST", 300000), buy("2 B ST", 350000)])
    db.add(JVPartner(buyer_id=b.id, entity_name=b.entity_name, status="TARGET"))
    db.flush()
    pp.refresh(db, idx=pp.build_index(db, imports=Path("/nonexistent")), today=TODAY)
    make_lead(db, sheriff_days=20, upset=150000, vacant=True, verdict="STRONG")
    q = cs.partner_queue(db)
    assert q and q[0]["kind"] == "partner" and q[0]["look_up"]
    pid = q[0]["partner_id"]
    compliance.add_internal(db, "2015550103", notes="stop")
    out = cs.trace_partner(db, pid, (FIX / "truepeoplesearch_detail.txt").read_text())
    p = db.get(JVPartner, pid)
    assert p.phone == "9735550101" and p.phone_status == "not_checked" and p.email
    assert p.principal_name == "Quinn Q Sample" and "pasted" in p.principal_basis
    assert any("unscrubbed" in n for n in out["saved"]["notes"]) and "[dnc]" in p.notes
    assert cs.partner_queue(db) == []                      # has a phone now: off the queue
    assert vault.outreach(db, p).first == "Quinn"
