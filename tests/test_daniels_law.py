"""Daniel's Law takedown inbox (engine/daniels_law.py): a nondisclosure request from any sender is read, every NJ
address / phone in it is suppressed, our own parcels at that street drop out of every report, nothing is answered by
itself, and the calendar carries a deadline until the operator confirms.

Every name, address and phone below is invented."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime

from db.models import DncEntry, MailSuppression
from engine import compliance
from engine import daniels_law as dl
from engine import gmail_autopilot as gm
from engine.streams.base import address_ok
from tests.factories import make_lead
from tests.test_agent_outreach import FakeSMTP
from tests.test_command_center import setup_paths  # noqa: F401 - pytest fixture
from tests.test_gmail_autopilot import ME, T0, FakeIMAP, live  # noqa: F401 - pytest fixture

REQUEST = """To whom it may concern,

Pursuant to Daniel's Law, N.J.S.A. 56:8-166.1, the covered person below requests that you not disclose or re-disclose
the home address or unpublished home telephone number listed.

Home address: {street}, Newark, NJ 07106
Second address: 22 N Maple Ave Apt 3, Jersey City New Jersey 07306
Home phone: (973) 555-0142

Thank you,
Privacy Desk
"""


def request(uid: int, body: str, *, frm: str = "Privacy Desk <notices@privacy.example>",
            subject: str = "Nondisclosure Request", when: datetime = T0 - timedelta(hours=2),
            attachment: tuple[bytes, str, str] | None = None) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, ME, subject
    m["Date"] = format_datetime(when)
    m["Message-ID"] = f"<dl-{uid}@privacy.example>"
    m.set_content(body)
    if attachment:
        data, maintype, name = attachment
        main, sub = maintype.split("/")
        m.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return m.as_bytes(policy=policy.SMTP)


# -- parsing ----------------------------------------------------------------------------------------------------------
def test_addresses_and_phones_are_read_from_the_request():
    text = REQUEST.format(street="14 Quinn Court") + "\nAlso 7 Broadway, Newark NJ\nand 101 Elm St\nOur line 609.555.0100"
    full, bare = dl.addresses(text)
    assert ("14 Quinn Court", "Newark", "07106") in full
    assert ("22 N Maple Ave Apt 3", "Jersey City", "07306") in full
    assert ("7 Broadway", "Newark", "") in full
    assert bare == ["101 Elm St"]
    assert dl.phones(text, exclude=["6095550100"]) == ["9735550142"]


def test_which_headers_are_opened_and_which_bodies_count():
    assert dl.candidate("Nondisclosure Request", "a@b.example")
    assert dl.candidate("Daniel’s Law notice", "a@b.example")
    assert dl.candidate("Data removal request", "a@b.example")
    assert dl.candidate("Hello", "notices@atlasdataprivacy.example")
    assert not dl.candidate("Re: [Edison] oil-tank listings", "a@b.example")
    assert not dl.confirmed("Your privacy request was received", "Thanks, we got your request.")
    assert dl.confirmed("Privacy request", "Under N.J.S.A. 56:8-166.1 do not disclose my home address")


# -- the inbox ---------------------------------------------------------------------------------------------------------
def test_a_request_from_an_unknown_sender_is_honored_and_never_answered(live):
    db = live
    lead = make_lead(db, sheriff_days=None, stale_listing=True)
    prop = lead.property
    street = prop.street_address.title()
    assert address_ok(db, prop)
    db.commit()
    msgs = {21: request(21, REQUEST.format(street=street)),
            22: request(22, "Lunch Friday?", frm="Friend <friend@mail.test>", subject="lunch")}
    fake = FakeIMAP(msgs)
    gm.set_imap_factory(lambda: fake)
    r = gm.poll_gmail_replies(now=T0, smtp_factory=FakeSMTP)
    assert r["ok"] and r["daniels_law"] == 1 and r["matched"] == 0
    # The request body was read (the sender is unknown); the lunch e-mail never was.
    assert [f[0] for f in fake.fetches if "HEADER.FIELDS" not in f[1]] == ["21"]
    assert FakeSMTP.sent == []                                      # nothing goes back to the requester
    db.expire_all()
    assert not address_ok(db, prop)                                 # our parcel drops out of every report
    assert compliance.address_suppressed(db, "22 N Maple Ave Apt 3, Jersey City NJ 07306") == ["daniels_law"]
    assert db.query(DncEntry).filter_by(phone="9735550142", source="daniels_law").count() == 1
    [rec] = dl.open_requests(db, T0.date())
    assert rec["addresses"] == 2 and rec["our_parcels"] >= 1 and rec["phones"] == 1 and not rec["needs_review"]
    assert rec["deadline"] == "2026-11-19"                          # 10 NJ business days (Veterans Day skipped)
    assert rec["from_domain"] == "privacy.example" and street.upper() not in str(rec).upper()
    # A second check never records it twice.
    fake2 = FakeIMAP(msgs)
    gm.set_imap_factory(lambda: fake2)
    assert not gm.poll_gmail_replies(now=T0 + timedelta(minutes=5), smtp_factory=FakeSMTP).get("daniels_law")
    assert len(dl.open_requests(db, T0.date())) == 1


def test_an_unparseable_request_asks_the_operator_to_look(live):
    db = live
    db.commit()
    fake = FakeIMAP({31: request(31, "Under Daniel's Law please stop disclosing my information. See attached.",
                                 attachment=(b"\x00\x01binary", "image/png", "scan.png"))})
    gm.set_imap_factory(lambda: fake)
    assert gm.poll_gmail_replies(now=T0, smtp_factory=FakeSMTP)["daniels_law"] == 1
    db.expire_all()
    [rec] = dl.open_requests(db, T0.date())
    assert rec["needs_review"] and "READ IT IN GMAIL" in dl.summary_line(rec)


def test_a_csv_attachment_is_read_to_the_end(live):
    db = live
    rows = "name,address,city,state,zip\n" + "".join(f"[individual],{n} Sample Rd,Edison,NJ,08817\n" for n in range(1, 31))
    rec = dl.handle(db, message_id="<csv@privacy.example>", from_email="notices@privacy.example",
                    subject="Daniel's Law nondisclosure request", text=dl.full_text(request(
                        41, "Daniel's Law requests attached.", attachment=(rows.encode(), "text/csv", "list.csv"))),
                    received=T0)
    assert rec["addresses"] == 30 and rec["suppressed"] == 30
    assert compliance.address_suppressed(db, "30 Sample Rd, Edison NJ 08817") == ["daniels_law"]


def test_a_bare_street_suppresses_our_parcels_in_the_named_town_only(live):
    db = live
    here = make_lead(db, sheriff_days=None, street_address="5 OAK TREE RD", city="Edison", zip_code="08820",
                     municipality="EDISON TWP", county="Middlesex")
    there = make_lead(db, sheriff_days=None, street_address="5 OAK TREE RD", city="Newark", zip_code="07106")
    dl.handle(db, message_id="<bare@privacy.example>", from_email="a@privacy.example", subject="Daniel's Law",
              text="Daniel's Law request. My home in Edison: 5 Oak Tree Rd", received=T0)
    assert not address_ok(db, here.property) and address_ok(db, there.property)


# -- calendar + chat ---------------------------------------------------------------------------------------------------
def test_the_deadline_is_on_the_calendar_until_the_operator_confirms(live, monkeypatch):
    db = live
    from core.config import settings
    from engine import cloud_desk, gcal_sync
    monkeypatch.setitem(settings()["calendar"], "mode", "firm")
    rec = dl.handle(db, message_id="<cal@privacy.example>", from_email="a@privacy.example", subject="Daniel's Law",
                    text=REQUEST.format(street="14 Quinn Court"), received=T0)
    db.commit()
    now = datetime(2026, 11, 10, 9, 0)
    want = gcal_sync.desired_events(db, now=now)
    ev = want[f"daniels:{rec['id']}"]
    assert ev.starts_at == datetime(2026, 11, 19, 9, 0) and "Quinn" not in str(ev.body)
    assert "⚖️" in ev.body["summary"] and "stop list" in ev.body["description"]
    assert any(rec["id"] in line for line in want[f"warm:{now:%Y%m%d}"].body["description"].splitlines())
    assert rec["id"] in want[f"desk:{now:%Y%m%d}"].body["description"]
    key = "k" * 32
    blob = cloud_desk.seal_note([{"daniels_law": rec["id"], "done": True},
                                 {"suppress_address": "9 Hidden Ln, Westfield NJ 07090"}],
                                cloud_desk.public_hex(key))
    out = cloud_desk.apply_note(blob, key, now=T0)
    assert out["note_done"] == 2 and not out.get("note_errors"), out
    db.expire_all()
    assert dl.open_requests(db, now.date()) == []
    assert f"daniels:{rec['id']}" not in gcal_sync.desired_events(db, now=now)
    assert db.query(MailSuppression).filter_by(source="daniels_law").filter(
        MailSuppression.address.like("9 Hidden Ln%")).count() == 1


def test_nj_business_days_cover_the_deadline():
    from core.dates import nj_business_days_after
    assert nj_business_days_after(date(2026, 11, 4), 10) == date(2026, 11, 19)
