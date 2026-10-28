"""Lis pendens agent: UCPA / PRESS parsers on saved real pages (party names replaced with invented ones), CSV
imports, parcel / owner-name matching, discharges, and signals that stack."""
import shutil
from datetime import date

from sqlalchemy import select

from agents.scrapers import lis_pendens_agent as lp
from db.models import ClerkFiling, DistressSignal, SignalType
from db.repository import upsert_property
from tests.conftest import FIXTURES
from tests.factories import make_lead

CLERK = FIXTURES / "clerk"


def test_parse_ucpa_union_foreclosures():
    filings = lp.parse_ucpa((CLERK / "union_ucpa_foreclosure.html").read_text())
    assert len(filings) == 44
    f = filings[0]
    assert f.county == "Union" and f.doc_type == "Foreclosure" and f.instrument == "2859-R"
    assert f.recorded == date(2026, 9, 28) and len(f.defendants) == 1 and f.defendants[0].endswith(", QUINN")
    assert f.plaintiffs == ["PENNY MAC LOAN SERVICES LLC"] and "NameDetails?dt=17" in f.url
    assert sum(x.estate for x in filings) == 3 and not any(x.discharge for x in filings)
    assert len({x.instrument for x in filings}) == 44


def test_parse_press_essex_grid_and_pager():
    rows, pager = lp.parse_press((CLERK / "essex_press_lp23.html").read_text(), "Essex")
    assert len(rows) == 58 and pager == []
    filings = lp.group_press(rows, "Essex", url=lp.PRESS)
    assert len(filings) == 13
    f = next(x for x in filings if x.instrument == "2026063996")
    assert f.town == "NEWARK" and f.recorded == date(2026, 9, 28) and f.doc_type == "LIS PENDENS FORECLOSURE"
    assert f.block is None and len(f.defendants) == 2 and f.plaintiffs[0].startswith("WILMINGTON SAVINGS")
    rows25, pager25 = lp.parse_press((CLERK / "essex_press_lp25_page1.html").read_text(), "Essex")
    assert len(rows25) == 100 and pager25 == [f"ctl00$ContentPlaceHolder1$dgdDeedMort$ctl01$ctl0{i}" for i in (1, 2, 3)]
    assert len(lp.group_press(rows25, "Essex")) == 30


def test_csv_import_flexible_columns():
    text = ("Municipality,Defendant,Plaintiff,Date Recorded,Instrument #,Document Type,Block,Lot,Property Address\n"
            "PATERSON CITY,SAMPLE QUINN,MIDFIRST BANK,10/01/2026,2026033641,FORECLOSURE LIS PENDENS,4512,7,"
            "12 Sample St\n"
            "CLIFTON CITY,TESTER CASEY,CAPITAL TL TRUST,10/02/2026,2026033700,LIS PENDENS,,,\n")
    filings = lp.parse_import_csv(text, "Passaic")
    assert [f.instrument for f in filings] == ["2026033641", "2026033700"]
    a, b = filings
    assert a.county == "Passaic" and a.town == "PATERSON CITY" and a.block == "4512" and a.lot == "7"
    assert a.address == "12 Sample St" and a.recorded == date(2026, 10, 1) and a.defendants == ["SAMPLE QUINN"]
    assert b.tax_lien and not a.tax_lien
    assert lp.parse_import_csv("foo,bar\n1,2\n", "Passaic") == []


def test_name_matching_rules():
    assert lp.names_match(lp.name_tokens("SAMPLE, QUINN R"), lp.name_tokens("SAMPLE QUINN"), False)
    assert lp.names_match(lp.name_tokens("SAMPLE QUINN (ESTATE OF)"), lp.name_tokens("QUINN SAMPLE"), False)
    assert not lp.names_match(lp.name_tokens("SAMPLE, QUINN"), lp.name_tokens("SAMPLE, ROBIN"), False)
    assert lp.names_match(lp.name_tokens("906 SAMPLE ST LLC"), lp.name_tokens("906 SAMPLE ST, L.L.C."), True)
    assert lp.JUNIOR_DEFENDANT_RE.search("STATE OF NEW JERSEY") and lp.JUNIOR_DEFENDANT_RE.search("CITY OF NEWARK")


def _essex_imports(tmp_path):
    root = tmp_path / "lis_pendens"
    (root / "essex").mkdir(parents=True, exist_ok=True)
    shutil.copy(CLERK / "essex_press_lp23.html", root / "essex" / "lp23.html")
    return root


def test_agent_matches_unique_owner_in_town_and_stacks(db, tmp_path):
    rows, _ = lp.parse_press((CLERK / "essex_press_lp23.html").read_text(), "Essex")
    target = next(f for f in lp.group_press(rows, "Essex") if f.town == "NEWARK" and not f.estate
                  and any(not lp.JUNIOR_DEFENDANT_RE.search(d) and "LLC" not in d for d in f.defendants))
    person = next(d for d in target.defendants if not lp.JUNIOR_DEFENDANT_RE.search(d) and "LLC" not in d)
    last, first = person.split()[:2]
    lead = make_lead(db, sheriff_days=None, absentee=True, owner_name=f"{last}, {first}")
    same_name_elsewhere = make_lead(db, sheriff_days=None, owner_name=f"{last}, {first}", municipality="NUTLEY TWP",
                                    city="Nutley", mun_code="0716")
    db.commit()
    run = lp.LisPendensAgent(import_only=True, offline=True, imports=_essex_imports(tmp_path),
                             counties=["Essex"]).execute()
    assert run.status == "success" and run.records_processed == 13
    db.expire_all()
    sig = db.scalar(select(DistressSignal).where(DistressSignal.signal_type == SignalType.LIS_PENDENS))
    assert sig.property_id == lead.property_id and sig.source_ref == f"Essex:{target.instrument}"
    assert sig.raw_metadata_json["match"] == "owner_name_town" and sig.filing_date == target.recorded
    assert same_name_elsewhere.property_id != sig.property_id          # the town keeps a namesake from matching
    row = db.scalar(select(ClerkFiling).where(ClerkFiling.instrument == target.instrument))
    assert row.property_id == lead.property_id
    assert db.query(ClerkFiling).count() == 13
    lp.LisPendensAgent(import_only=True, offline=True, imports=_essex_imports(tmp_path), counties=["Essex"]).execute()
    db.expire_all()
    assert db.query(ClerkFiling).count() == 13                                     # idempotent
    assert db.query(DistressSignal).filter_by(signal_type=SignalType.LIS_PENDENS).count() == 1


def test_ambiguous_names_stay_unmatched_and_block_lot_import_matches(db, tmp_path):
    make_lead(db, sheriff_days=None, owner_name="SAMPLE, QUINN")
    make_lead(db, sheriff_days=None, owner_name="QUINN SAMPLE")             # two Newark parcels, same owner name
    exact = make_lead(db, sheriff_days=None, block="4512", lot="7")
    db.commit()
    root = tmp_path / "lp"
    (root / "essex").mkdir(parents=True)
    (root / "essex" / "export.csv").write_text(
        "Town,Defendant,Plaintiff,Recorded,Instrument,Type,Block,Lot\n"
        "NEWARK,SAMPLE QUINN,BIG BANK NA,10/01/2026,2026070001,LIS PENDENS FORECLOSURE,,\n"
        "NEWARK,TESTER CASEY,FCR TL TRUST,10/02/2026,2026070002,LIS PENDENS FORECLOSURE,4512,7\n"
        "NEWARK,TESTER CASEY,FCR TL TRUST,10/20/2026,2026070003,LIS PENDENS DISCHARGE,,\n")
    lp.LisPendensAgent(import_only=True, offline=True, imports=root, counties=["Essex"]).execute()
    db.expire_all()
    amb = db.scalar(select(ClerkFiling).where(ClerkFiling.instrument == "2026070001"))
    assert amb.property_id is None and amb.match_method == "ambiguous_name"
    hit = db.scalar(select(ClerkFiling).where(ClerkFiling.instrument == "2026070002"))
    assert hit.property_id == exact.property_id and hit.match_method == "block_lot" and hit.tax_lien
    sig = db.scalar(select(DistressSignal).where(DistressSignal.source_ref == "Essex:2026070002"))
    assert sig.severity_weight == 0.75 and sig.status.startswith("discharged")    # the discharge closed it
    assert not sig.active


def test_union_county_wide_match_and_estate_signal(db, tmp_path):
    filings = lp.parse_ucpa((CLERK / "union_ucpa_foreclosure.html").read_text())
    est = next(f for f in filings if f.estate)
    words = est.defendants[0].replace("ESTATE OF ", "").split()
    p, _ = upsert_property(db, {"parcel_id": "UNION-2004-1-1", "county": "Union", "municipality": "ELIZABETH CITY",
                                "mun_code": "2004", "street_address": "1 SAMPLE AVE", "city": "Elizabeth",
                                "property_class": "2", "owner_name": f"{words[-1]}, {' '.join(words[:-1])}"},
                           source="test")
    db.commit()
    root = tmp_path / "lp"
    (root / "union").mkdir(parents=True)
    shutil.copy(CLERK / "union_ucpa_foreclosure.html", root / "union" / "foreclosures.html")
    lp.LisPendensAgent(import_only=True, offline=True, imports=root, counties=["Union"]).execute()
    db.expire_all()
    types = {s.signal_type for s in db.scalars(select(DistressSignal).where(DistressSignal.property_id == p.id))}
    assert types == {SignalType.LIS_PENDENS, SignalType.ESTATE_OWNER}
    assert db.get(type(p), p.id).lead is not None


def test_parse_press_monmouth_defendant_headers_with_block_lot():
    rows, pager = lp.parse_press((CLERK / "monmouth_oprs_lpfo.html").read_text(), "Monmouth")
    assert len(rows) == 100 and len(pager) == 2
    filings = lp.group_press(rows, "Monmouth", url=lp.PRESS_SITES["Monmouth"]["url"])
    f = next(x for x in filings if x.instrument == "2026079023")
    assert f.town == "OCEAN TOWNSHIP" and (f.block, f.lot) == ("38", "43") and f.recorded == date(2026, 9, 29)
    assert f.doc_type == "FORECLOSURE LISPENDENS" and f.plaintiffs == ["JPMORGAN CHASE BANK NA"]
    assert sum(1 for x in filings if x.block) == 21 and "Monmouth" in lp.AUTOMATED


def test_town_names_keep_their_municipal_type():
    from agents.scrapers.njgis_client import match_municipality as m
    munis = {"FREEHOLD BORO": "1316", "FREEHOLD TWP": "1317", "UPPER FREEHOLD TWP": "1351", "NEPTUNE TWP": "1335",
             "NEPTUNE CITY BORO": "1336", "OCEAN TWP": "1337", "JERSEY CITY CITY": "0906", "ORANGE CITY TWP": "0717"}
    assert m(munis, "FREEHOLD TWP") == ("FREEHOLD TWP", "1317")
    assert m(munis, "Borough of Freehold") == ("FREEHOLD BORO", "1316")
    assert m(munis, "FREEHOLD") is None                                   # ambiguous: never guess
    assert m(munis, "NEPTUNE CITY") == ("NEPTUNE CITY BORO", "1336")
    assert m(munis, "NEPTUNE TOWNSHIP") == ("NEPTUNE TWP", "1335")
    assert m(munis, "OCEAN TOWNSHIP") == ("OCEAN TWP", "1337") and m(munis, "JERSEY CITY") == ("JERSEY CITY CITY", "0906")
    assert m(munis, "ORANGE") == ("ORANGE CITY TWP", "0717")
