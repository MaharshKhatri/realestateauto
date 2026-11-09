"""The flat-fee AI Deal Desk (2026-11-09 pivot): buyer boxes from recorded deeds and one-page Weekly Deal Digests."""
from __future__ import annotations

from datetime import date, timedelta
from email import message_from_bytes

from db.models import CashBuyer, ContactType
from engine import deal_desk as dd
from tests.factories import add_contact, make_lead

TODAY = date.today()


def _buyer(db, name: str, contact: str | None, n: int, *, price: int = 330000, mun: str = "0714",
           town: str = "NEWARK CITY", mailing: str | None = None) -> CashBuyer:
    buys = [{"address": f"{10 + i} SAMPLE AVE", "municipality": town, "county": "Essex", "mun_code": mun,
             "price": price + 1000 * i, "deed_date": (TODAY - timedelta(days=60 * (i + 1))).isoformat(), "units": 1}
            for i in range(n)]
    b = CashBuyer(entity_key=name.upper(), entity_name=name, contact_name=contact,
                  mailing_address=mailing or f"{n} {name.split()[0]} PLAZA, NEWARK NJ 07102",
                  cash_purchases_count=n, recent_purchases_json=buys, last_purchase_date=TODAY - timedelta(days=60))
    db.add(b)
    db.flush()
    return b


def test_buyer_boxes_come_from_recorded_purchases_and_named_principals_come_first(db):
    _buyer(db, "QUIET RIVER LLC", None, 9)
    _buyer(db, "SAMPLE HOMES LLC", "QUINN SAMPLE", 4)
    _buyer(db, "ONE OFF LLC", None, 1)
    ps = dd.select_buyers(db, named=34, llcs=20, today=TODAY)
    assert [p.entity for p in ps] == ["SAMPLE HOMES LLC", "QUIET RIVER LLC"]      # a single purchase isn't a buyer box
    named, llc = ps
    assert named.kind == "named" and named.principal == "QUINN SAMPLE" and llc.kind == "llc"
    assert llc.towns[0][:2] == ("0714", "Newark") and llc.purchases_36m == 9
    assert 330000 <= llc.price_lo <= llc.price_med <= llc.price_hi <= 339000
    assert "Newark" in llc.box_line() and "9 purchases" in llc.box_line()


def test_package_deeds_dont_set_the_price_band(db):
    b = _buyer(db, "BULK BUYS LLC", None, 4)
    b.recent_purchases_json = b.recent_purchases_json + [
        {"address": f"{i} BULK ST", "municipality": "NEWARK CITY", "county": "Essex", "mun_code": "0714",
         "price": 2_000_000, "deed_date": TODAY.isoformat(), "units": 1} for i in range(3)]
    p = dd.profile([b], "k", TODAY)
    assert p.purchases_36m == 4 and p.price_hi < 400000


def test_a_digest_lists_deals_in_the_buyers_towns_with_max_buy_and_the_agent_and_says_what_it_is(db, tmp_path):
    _buyer(db, "QUIET RIVER LLC", None, 6, price=380000)
    lead = make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000,
                     listing_text="Estate sale, sold as-is, needs TLC", assumptions={"quant": {"buyer_max": 420000}})
    add_contact(db, lead, contact_type=ContactType.LISTING_AGENT, name="PAT AGENT", phone="9735550100",
                email="pat@brokerage.example", company="SAMPLE REALTY")
    sale = make_lead(db, sheriff_days=30, upset=250000, assumptions={"quant": {"buyer_max": 410000}})
    far = make_lead(db, sheriff_days=None, stale_listing=True, list_price=900000,
                    assumptions={"quant": {"buyer_max": 420000}})            # a flipper can't win it at any price
    db.commit()
    ps = dd.select_buyers(db, today=TODAY)
    [d] = dd.build_digests(db, ps, TODAY)
    ids = [r.lead_id for r in d.rows]
    assert lead.id in ids and sale.id in ids and far.id not in ids
    mls = next(r for r in d.rows if r.lead_id == lead.id)
    assert mls.kind == "MLS fixer" and mls.max_buy == 420000 and mls.room == -30000
    assert "pat@brokerage.example" in mls.agent and "(973) 555-0100" in mls.agent and "QUINN" not in mls.agent
    sheriff = next(r for r in d.rows if r.lead_id == sale.id)
    assert sheriff.kind == "Sheriff sale" and "Upset" in sheriff.price_label and sheriff.agent == ""
    pdf = dd.render_pdf(d, tmp_path / "d.pdf", {"name": "Sam Operator", "phone": "(908) 555-0100",
                                                "email": "sam@example.com", "postal": "1 Main St, Iselin, NJ 08830"})
    from pypdf import PdfReader
    pages = PdfReader(str(pdf)).pages
    text = pages[0].extract_text()
    assert len(pages) == 1
    for must in ("Weekly Deal Digest for QUIET RIVER LLC", "$500 flat", "$1,000/month", "Zero commission",
                 "not a licensed real estate broker", "your own inbox", "you are the buyer", "no more",
                 "1 Main St, Iselin"):
        assert must in text.replace("\n", " "), must
    assert "QUINN Q SAMPLE" not in text                                     # never a defendant / owner name


def test_a_suppressed_address_never_reaches_a_digest(db):
    from engine.compliance import suppress_address
    _buyer(db, "QUIET RIVER LLC", None, 6, price=380000)
    lead = make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000,
                     assumptions={"quant": {"buyer_max": 420000}})
    p = lead.property
    suppress_address(db, f"{p.street_address}, {p.city} NJ {p.zip_code}", source="daniels_law")
    db.commit()
    [d] = dd.build_digests(db, dd.select_buyers(db, today=TODAY), TODAY)
    assert lead.id not in [r.lead_id for r in d.rows]


def test_the_desk_mails_the_digests_to_the_operator_only_and_logs_counts(db, monkeypatch, tmp_path):
    from engine import cloud_desk as cd
    _buyer(db, "QUIET RIVER LLC", None, 6, price=380000)
    make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000, assumptions={"quant": {"buyer_max": 420000}})
    db.commit()
    sent = []

    class FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, msg):
            sent.append(msg)

    r = cd.send_digests("me@example.com", "pw", smtp_factory=FakeSMTP, out_dir=tmp_path)
    assert r == {"digests": 1, "digest_rows": 1, "digest_mails": 1}
    [msg] = sent
    assert msg["To"] == "me@example.com" and msg["Subject"].startswith("[njw-digest] ")
    m = message_from_bytes(msg.as_bytes())
    files = [p.get_filename() for p in m.walk() if p.get_filename()]
    assert files == ["01_QUIET_RIVER_LLC.pdf"]
