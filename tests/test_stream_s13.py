"""Stream 13: the builder teardown digest (engine/streams/s13_builder_teardowns.py)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest

from engine.streams import s13_builder_teardowns as s13
from engine.streams.base import SAMPLE_FULL_ROWS, StreamContext, sample_path
from tests.factories import make_lead

OP = {"name": "Sam Operator", "first": "Sam", "phone": "(973) 555-0199", "email": "sam@example.com",
      "postal": "1 Main St, Newark, NJ 07102"}
NOW = datetime.now().replace(microsecond=0)


def _ctx(tmp_path, send: bool = True, now: datetime | None = None, **limits) -> StreamContext:
    return StreamContext(now=now or NOW, out_dir=tmp_path / "out", operator=OP, send=send, limits=limits)


def _text(path) -> str:
    """The PDF's text (ReportLab writes the mask's bullet as 0x7F, which pypdf extracts as "\\x7f")."""
    from pypdf import PdfReader
    return "\n".join(p.extract_text() for p in PdfReader(str(path)).pages).replace("\x7f", "•")


def _flat(path) -> str:
    return " ".join(_text(path).split())


def _sentences(body: str) -> list[str]:
    """The sentences above the sign-off: the "Hello," line and the operator's first name don't count."""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if lines and re.fullmatch(r"(?i)(hi|hello)\b[^.?!]*,", lines[0]):
        lines = lines[1:]
    if lines and lines[-1] == OP["first"]:
        lines = lines[:-1]
    return [x for x in re.split(r"(?<=[.?!])\s+(?=[A-Z])", " ".join(lines)) if x]


def _plain_touch(subject: str, body: str) -> None:
    """Every Touch 1: 2 plain sentences of 25-45 words with a question last, a greeting, no price / link / hype; the
    copy linter passes on our own words (a quote is the listing's own words, e.g. "builder's opportunity")."""
    from engine import human_copy as hc
    sents = _sentences(body)
    assert len(sents) == 2 and sents[1].endswith("?") and sents[1].startswith("Want this week's"), sents
    assert body.splitlines()[0] in ("Hello,",) or body.splitlines()[0].startswith("Hi "), body
    assert body.splitlines()[-1] == "Sam"
    words = hc.word_count(" ".join(sents))
    assert 25 <= words <= 45, (words, body)
    ours = re.sub(r'"[^"]*"', '"x"', body)
    assert hc.lint(ours) == [], hc.lint(ours)
    low = (subject + " " + body).lower()
    for bad in ("$", "http", "www.", "!", "attached", "ad:", "referral", "guarantee", "per month", "a month", "worth",
                "arv", "below market", "equity", "value is", "deal"):
        if bad == "value is" and '"value is in the land"' in body.lower():
            continue
        assert bad not in low.replace('"value is in the land"', ""), bad
    assert "free" not in subject.lower() and len(subject) <= 60 and not subject.lower().startswith("re:")


# -- the classifier -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    # listings that address builders
    ("Builders opportunity! Large lot.", {s13.BUILDER}),
    ("BUILDER'S SPECIAL on a quiet street.", {s13.BUILDER}),
    ("Ideal for a builder.", {s13.BUILDER}),
    ("Attention builders and investors.", {s13.BUILDER}),
    ("Calling all builders!", {s13.BUILDER}),
    ("A rare opportunity for investors, builders, or buyers looking to expand.", {s13.BUILDER}),
    ("Perfect for an investor, builder, or end user.", {s13.BUILDER}),
    ("Whether you are a builder looking for your next project.", {s13.BUILDER}),
    ("Bring your builder and your vision.", {s13.BUILDER}),
    ("Builders Alert: level lot.", {s13.BUILDER}),
    ("10-year builder warranty, new construction.", set()),
    ("Built by renowned Sample Builders with custom finishes.", set()),
    ("Model home interior design by Builders Design.", set()),
    ("Builder is available to construct your custom-designed home.", set()),
    ("Builder grade finishes throughout.", set()),
    # teardowns
    ("Tear down or renovate.", {s13.TEARDOWN}),
    ("Most likely a knock down.", {s13.TEARDOWN}),
    ("Teardown on a large lot.", {s13.TEARDOWN}),
    ("Ready for renovation or knock-down.", {s13.TEARDOWN}),
    ("Knock down a wall to open the kitchen.", set()),
    ("Tear down the partition for an open plan.", set()),
    ("Not a tear down.", set()),
    ("No teardown needed.", set()),
    # build new
    ("Build your dream home on this lot.", {s13.DREAM}),
    ("Build the custom home of your dreams.", {s13.DREAM}),
    ("Build new memories here.", set()),
    ("Make this dream home yours.", set()),
    ("Build your dream kitchen.", set()),
    # land value
    ("Value is in the land.", {s13.LAND_VALUE}),
    ("Being sold strictly as land value only.", {s13.LAND_VALUE}),
    ("Priced at land value.", {s13.LAND_VALUE}),
    ("Sold for land value.", {s13.LAND_VALUE}),
    ("Land only, no structure.", {s13.LAND_VALUE}),
    ("Room to grow or rare land value, this turnkey gem.", set()),
    # subdivision
    ("Potential to subdivide.", {s13.SUBDIVIDE}),
    ("Possible subdivision.", {s13.SUBDIVIDE}),
    ("The lot can be subdivided.", {s13.SUBDIVIDE}),
    ("The lots have already been subdivided.", {s13.SUBDIVIDE}),
    ("Located in the Oaks subdivision.", set()),
    ("The area was subdivided into the North End section.", set()),
    ("The lot cannot be subdivided.", set()),
    # buildable, lots, zoning
    ("Buildable lot.", {s13.BUILDABLE}),
    ("Non-buildable lot.", set()),
    ("Two lots being sold together.", {s13.TWO_LOTS}),
    ("Situated on a double lot.", {s13.TWO_LOTS}),
    ("Located 2 lots from the corner.", set()),
    ("Oversized lot.", {s13.OVERSIZED}),
    ("Located in an R-2 zone.", {s13.ZONE}),
    ("Zoned R3.", {s13.ZONE}),
    ("Zoned heating and central air.", set()),
])
def test_classifier_words_and_negations(text, expected):
    assert set(s13.find_hits(text)) == expected


def test_quotes_modes_and_small_helpers():
    assert s13.quote_text("BUILDERS  Opportunity") == "builders opportunity"
    assert s13.quote_text("Located in an r-2 Zone") == "located in an R-2 zone"
    assert s13.quote_mode("builder's opportunity", s13.BUILDER) == "noun"
    assert s13.quote_mode("tear down", s13.TEARDOWN) == "noun"
    assert s13.quote_mode("ideal for a builder", s13.BUILDER) == "says"
    assert s13.quote_mode("investors, builders", s13.BUILDER) == "marketed"
    assert s13.quote_mode("value is in the land", s13.LAND_VALUE) == "says"
    assert s13.quote_mode("can be subdivided", s13.SUBDIVIDE) == "says_it"
    assert s13.quote_mode("subdivide", s13.SUBDIVIDE) == "mentions"
    assert s13.land_share(type("P", (), {"assessed_land": 300000, "assessed_improvement": 100000,
                                         "assessed_total": 400000})()) == 0.75
    assert s13.land_share(type("P", (), {"assessed_land": 50000, "assessed_improvement": None,
                                         "assessed_total": 200000})()) == 0.25
    assert s13.land_share(type("P", (), {"assessed_land": 0, "assessed_improvement": 5, "assessed_total": 5})()) is None
    assert s13.lot_words(0.3101) == "0.31-acre" and s13.lot_words(1.25) == "1.2-acre" and s13.lot_words(2.0) == "2-acre"
    assert s13.lot_words(0.0626) == "2,700-square-foot" and s13.lot_words(None) == ""
    assert s13.lot_cell(0.31) == "0.31 ac (13,500 sq ft)" and s13.lot_cell(None) == "-"
    assert s13.number_word(9) == "nine"
    assert s13.scrub("Two lots being sold (437 & 439 E Sample St). Owner") == "Two lots being sold. Owner"
    assert s13.scrub("Block 12 Lot 3 is buildable") == "is buildable"
    quote = "...141 Samplewood offers a rare find, with 127 Oakview included"
    assert s13.hide_street(quote, "141 Samplewood Ave") == "... offers a rare find, with included"
    assert s13.hide_street("2 Lots and 3 Bedrooms", "9 Sample St") == "2 Lots and 3 Bedrooms"
    assert s13.addressable("12 Sample St") and s13.addressable("141-143 Sample Ave")
    for bad in ("Sample Ave", "432 And 434 Sample St", "0-SE 10th St", "12 Sample St Unit 2"):
        assert not s13.addressable(bad), bad
    assert s13.retail("Fully renovated colonial, sold as-is.")
    assert not s13.retail("Renovated? No: estate sale, needs work.")
    assert s13.new_build("New construction with a 10-year builder warranty.")
    assert s13.new_build("Attention builders: a rare chance to own a newer-built two-family.")
    assert s13.new_build("Colonial built in 2019.") and not s13.new_build("Colonial built in 1925, sold as-is.")
    assert s13.same_listing_text("Builder's dream! 2 lots sold (437 & 439 Sample St). Owner will finance part.",
                                 "Builder's dream! 2 lots sold (433 & 435 Sample St). Owner will finance part.")
    assert not s13.same_listing_text("A charming cape with a big yard and a new roof, near the train.",
                                     "Builder's dream! 2 lots sold together, bring plans and your crew today.")
    assert s13.brokerage("SAMPLE REALTY GROUP DONT REOPEN") == "SAMPLE REALTY GROUP"
    assert s13.brokerage("Quinn Sample") == ""                         # could be a person: left out
    assert s13.town_name("Elizabeth City") == "Elizabeth" and s13.town_name("JERSEY CITY") == "Jersey City"


def test_excerpt_keeps_decimal_points_and_drops_contacts():
    t = s13.normalize("Great location. This .79 acre buildable lot is ready. Call Pat at 973-555-0100.")
    h = s13.find_hits(t)[s13.BUILDABLE]
    assert s13.excerpt(t, h.start, h.end) == "This .79 acre buildable lot is ready"
    t = s13.normalize("Builders opportunity for the right buyer, contact Pat Agent 973-555-0100 or pat@x.example today")
    h = s13.find_hits(t)[s13.BUILDER]
    got = s13.excerpt(t, h.start, h.end)
    assert got.startswith("Builders opportunity") and "Pat" not in got and "555" not in got and "@" not in got


# -- Touch 1 / Touch 2 copy ---------------------------------------------------------------------------------------
def _row(street="12 Sample St", town="Westfield", quote="builder's opportunity", kind=s13.BUILDER, acres=0.31,
         county="Union", **kw) -> s13.Row:
    return s13.Row(county=county, town=town, street=street, acres=acres, quote=quote, quote_kind=kind,
                   kinds=[kind] if kind else [], words=quote, by_words=bool(quote), **kw)


def test_touch1_shapes_follow_the_listing_words():
    subject, body = s13.touch1("Union", _row(), 9, 9, "Hello,", "Sam")
    _plain_touch(subject, body)
    assert _sentences(body) == [
        '12 Sample St in Westfield is listed as "builder\'s opportunity" on a 0.31-acre lot, one of 9 teardown and lot '
        "listings I found in Union County this week from the public listings and tax records.",
        "Want this week's Union County sheet, free?"]
    assert subject == "[Westfield] teardown lots"
    _, says = s13.touch1("Union", _row(quote="value is in the land", kind=s13.LAND_VALUE), 9, 9, "Hello,", "Sam")
    _plain_touch(subject, says)
    assert '"value is in the land" (a 0.31-acre lot), one of 9' in says
    assert says.count("The listing for 12 Sample St") == 1
    _, mk = s13.touch1("Union", _row(quote="investors, builders", acres=None), 9, 9, "Hello,", "Sam")
    _plain_touch(subject, mk)
    assert "12 Sample St in Westfield is marketed to builders and investors, one of 9" in mk and '"' not in mk
    _, sub = s13.touch1("Union", _row(quote="subdivide", kind=s13.SUBDIVIDE), 4, 9, "Hello,", "Sam")
    _plain_touch(subject, sub)
    assert "The listing for 12 Sample St in Westfield mentions subdividing (a 0.31-acre lot), one of 4" in sub
    _, lot = s13.touch1("Union", _row(quote="two lots", kind=s13.TWO_LOTS), 4, 9, "Hello,", "Sam")
    assert 'is listed as "two lots", one of 4' in lot                      # no "on a ... lot" after a lot quote


def test_touch1_tax_record_row_and_all_county_sheet_stay_under_45_words():
    facts = _row(quote="", kind="", acres=0.42, year=1925, share=0.72, by_share=True)
    subject, body = s13.touch1("Union", facts, 12, 9, "Hello,", "Sam")
    _plain_touch(subject, body)
    assert "12 Sample St in Westfield, built in 1925 on a 0.42-acre lot with the land at 72% of its tax assessment, " \
           "is one of 12 teardown and lot listings" in body
    long_row = _row(street="1000-16 Samplewood Ave", town="Berkeley Heights",
                    quote="opportunity for a developer or builder", kind=s13.BUILDER)
    subject, body = s13.touch1(s13.ALL, long_row, 149, 9, "Hello,", "Sam")
    _plain_touch(subject, body)
    assert "in the nine counties I cover this week" in body and body.count("Want this week's all-county sheet, free?")


def test_sample_body_names_both_prices_with_sales_tax():
    from engine import human_copy as hc
    lim = {**s13.DEFAULTS}
    county = s13.sample_body("Union", lim, True, 9)
    assert county == ("Attached is the Union County sheet: the first 2 listings in full, the rest with the street "
                      "hidden. It's $249 a month plus NJ sales tax for one county or $499 plus NJ sales tax for all "
                      "nine, with no commission or per-deal fee. Reply \"start\" and I'll set it up.")
    every = s13.sample_body(s13.ALL, lim, True, 9)
    assert every.startswith("Attached is this week's sheet for all nine counties: the first 2 listings in full")
    assert "plus NJ sales tax" in every and "$499" in every
    assert s13.sample_body("Union", lim, False, 9).startswith("Attached is the Union County sheet. It's $249")
    for sb in (county, every):
        assert hc.lint(sb) == [] and 2 <= len(re.split(r"(?<=[.?!])\s+", sb)) <= 4


# -- the stream on a database -------------------------------------------------------------------------------------
def _listed(db, street: str, text: str | None, *, county="Union", muni="WESTFIELD TOWN", city="Westfield",
            zip_code="07090", land: int | None = 300000, imp: int | None = 250000, acres: float | None = 0.31,
            year: int | None = 1925, ptype="single_family", price=650000, days=120, broker="Sample Realty LLC",
            qual=None, cls="2"):
    """A live listed house: property (owner name on file, never printed) + STALE_LISTING signal + Listing row."""
    from db.models import Listing
    lead = make_lead(db, sheriff_days=None, stale_listing=True, listing_text=text or "", verdict=None,
                     list_price=price, street_address=street.upper(), county=county, municipality=muni, city=city,
                     zip_code=zip_code, assessed_land=land, assessed_improvement=imp,
                     assessed_total=(land or 0) + (imp or 0) or None, lot_size=acres, year_built=year,
                     owner_name="QUINN Q SAMPLE", qual=qual, property_class=cls)
    prop = lead.property
    sig = next(s for s in prop.signals if s.signal_type == "STALE_LISTING")
    sig.raw_metadata_json = {**sig.raw_metadata_json, "listing_broker": broker}
    db.execute(Listing.__table__.update().where(Listing.source_ref == sig.source_ref).values(
        address=street, city=city, zip_code=zip_code, property_type=ptype,
        list_date=date.today() - timedelta(days=days)))
    db.flush()
    return lead


def _desk(db):
    """Union: three word rows, one tax-record row, and the houses that must stay off the sheet."""
    rows = {
        "teardown": _listed(db, "12 Sample St",
                            "Charming 1920s cape. Tear down or renovate. Call Pat Agent 973-555-0100."),
        "builder": _listed(db, "14 Sample Ave", "Builders opportunity! Large level lot in town.", acres=0.52),
        "land": _listed(db, "16 Sample Rd", "Value is in the land. Sold as-is.", land=420000, imp=60000),
        "facts": _listed(db, "18 Sample Ter", "Cozy colonial with a big yard.", land=330000, imp=170000, year=1938),
    }
    off = {
        "new": _listed(db, "20 Sample Pl", "New construction, builder's warranty, build your dream home.", year=2025),
        "condo": _listed(db, "22 Sample Ct", "Builders opportunity!", ptype="condos"),
        "retail_lot": _listed(db, "24 Sample Way", "Fully renovated colonial on an oversized lot.", land=100000),
        "wall": _listed(db, "26 Sample Ln", "Knock down a wall to open the kitchen.", land=100000),
        "young": _listed(db, "28 Sample Dr", "Updated ranch.", land=330000, imp=170000, year=1975),
        "shop": _listed(db, "30 Sample Blvd", "Builders opportunity!", cls="4A"),
    }
    return rows, off


def test_rows_pdf_facts_and_privacy(db, tmp_path):
    rows, off = _desk(db)
    res = s13.STREAM.run(db, _ctx(tmp_path))
    m = res.metrics
    assert m["rows"] == 4 and m["word_hits"] == 3 and m["land_share_hits"] == 2      # "land" is both
    assert m["pdfs"] == 2 and m["counties"] == 1 and m["listings_seen"] == 10
    assert m["skipped_type"] == 2 and m["skipped_new_build"] == 1 and m["skipped_suppressed"] == 0
    for key in ("listings_seen", "word_hits", "land_share_hits", "rows", "pdfs", "pitches", "skipped_suppressed"):
        assert key in m
    out = tmp_path / "out"
    pdf = out / f"s13_teardowns_union_{NOW:%Y-%m-%d}.pdf"
    allc = out / f"s13_teardowns_all_counties_{NOW:%Y-%m-%d}.pdf"
    assert pdf.exists() and allc.exists()
    assert not sample_path(pdf).exists() and not sample_path(allc).exists()   # samples are built per pitch
    text = _flat(pdf)
    assert text.startswith("NJ Teardown and Lot Listings: Union County")
    assert s13.TOP_LINE in text and "not an appraisal, BPO or CMA; no opinion of value" in text
    assert "Sources: public listing descriptions (Realtor.com), NJ MOD-IV tax records" in text
    for street in ("12 Sample St", "14 Sample Ave", "16 Sample Rd", "18 Sample Ter"):
        assert street in text
    for street in ("20 Sample Pl", "22 Sample Ct", "24 Sample Way", "26 Sample Ln", "28 Sample Dr", "30 Sample Blvd"):
        assert street not in text, street
    assert '"Tear down or renovate"' in text and "(tax-record match: no listing words)" in text
    assert "0.52 ac (22,650 sq ft)" in text and "88%" in text and "Sample Realty LLC" in text
    assert "$249/month for one county or $499/month for all nine, flat plus NJ sales tax" in text
    for secret in ("QUINN", "Pat Agent", "555-0100", "9735550100"):
        assert secret not in text, secret
    assert "Week of" in text and "no referral fees" in text and "no more" in text
    blob = repr(res.metrics) + repr(res.notes)                     # the public run log: counts only
    assert "Sample" not in blob and "@" not in blob


def test_sample_masks_rows_three_and_up_and_their_quotes(db, tmp_path):
    from db.models import FirmTarget
    _desk(db)
    _listed(db, "40 Samplewood Ave", "Investors, builders: 40 Samplewood offers a level lot for a new home.")
    db.add(FirmTarget(kind="builder_developer", stream="s13", org="Sample Homes", email="info@samplehomes.example",
                      county="Union", stage="NEW"))
    db.flush()
    [ob] = s13.STREAM.run(db, _ctx(tmp_path)).outbound
    [smp] = ob.sample_attachments
    full, sample = _flat(smp.parent.parent / smp.name.replace(".sample.pdf", ".pdf")), _flat(smp)
    streets = re.findall(r"\b\d+ Sample\w* (?:St|Ave|Rd|Ter)\b", full)
    assert len(streets) == 5 and smp.name.endswith(".sample.pdf")
    assert all(st in sample for st in streets[:SAMPLE_FULL_ROWS])
    assert not any(st in sample for st in streets[SAMPLE_FULL_ROWS:])
    assert "•" in sample and "Sample: 2 of 5 rows in full" in sample
    assert "Samplewood offers" in full and "Samplewood offers" not in sample      # its quote hid the street name


def test_suppressed_address_never_appears(db, tmp_path):
    from db.models import FirmTarget
    from engine.compliance import suppress_address
    rows, _ = _desk(db)
    p = rows["teardown"].property
    suppress_address(db, f"{p.street_address}, {p.city} NJ {p.zip_code}", "daniels_law")
    db.add(FirmTarget(kind="builder_developer", stream="s13", org="Sample Homes", email="info@samplehomes.example",
                      county="Union", stage="NEW"))
    db.flush()
    res = s13.STREAM.run(db, _ctx(tmp_path))
    assert res.metrics["skipped_suppressed"] == 1 and res.metrics["rows"] == 3
    for path in (tmp_path / "out").rglob("*.pdf"):
        assert "12 Sample St" not in _text(path), path.name
    [ob] = res.outbound
    assert "12 Sample St" not in ob.body and "12 Sample St" not in ob.subject


def test_off_market_pending_and_wrong_parcel(db, tmp_path):
    from db.models import Listing
    rows, _ = _desk(db)
    teardown, builder, land = rows["teardown"].property, rows["builder"].property, rows["land"].property
    sig = next(s for s in teardown.signals if s.signal_type == "STALE_LISTING")
    sig.active = False                                         # retired: the listing left the market
    ref = next(s for s in builder.signals if s.signal_type == "STALE_LISTING").source_ref
    db.execute(Listing.__table__.update().where(Listing.source_ref == ref).values(status="pending"))
    ref = next(s for s in land.signals if s.signal_type == "STALE_LISTING").source_ref
    db.execute(Listing.__table__.update().where(Listing.source_ref == ref).values(lat=40.0, lon=-74.9))
    db.flush()
    res = s13.STREAM.run(db, _ctx(tmp_path, min_rows=1))
    assert res.metrics["skipped_off_market"] == 1 and res.metrics["wrong_parcel"] == 1
    text = _flat(tmp_path / "out" / f"s13_teardowns_union_{NOW:%Y-%m-%d}.pdf")
    assert "12 Sample St" not in text and "14 Sample Ave" not in text
    assert "16 Sample Rd" in text and "88%" not in text      # still listed as land value; not that parcel's facts


def test_listing_without_a_parcel_gets_its_county_from_the_zip_and_its_lot_from_the_listing(db, tmp_path):
    from db.models import Listing, utcnow
    _desk(db)
    db.add(Listing(source="homeharvest", source_ref="LX1", status="for_sale", address="50 Sample St", city="Westfield",
                   zip_code="07090", property_type="land", list_price=399000, days_on_market=40,
                   description="Buildable lot on a quiet block.", raw_json={"lot_sqft": 10890}, last_seen_at=utcnow()))
    db.add(Listing(source="homeharvest", source_ref="LX2", status="for_sale", address="52 Sample St", city="Elsewhere",
                   zip_code="99999", property_type="land", description="Buildable lot.", last_seen_at=utcnow()))
    db.add(Listing(source="homeharvest", source_ref="LX3", status="for_sale", address="54 Sample St", city="Westfield",
                   zip_code="07090", property_type="land", description="Buildable lot.",
                   last_seen_at=utcnow() - timedelta(days=45)))     # not seen by the harvest in 45 days: not live
    db.flush()
    res = s13.STREAM.run(db, _ctx(tmp_path))
    assert res.metrics["no_county"] == 1
    text = _flat(tmp_path / "out" / f"s13_teardowns_union_{NOW:%Y-%m-%d}.pdf")
    assert "50 Sample St" in text and "0.25 ac (10,890 sq ft)" in text and "vacant land" in text
    assert "52 Sample St" not in text and "54 Sample St" not in text


def test_package_of_lots_listed_one_by_one_appears_once(db, tmp_path):
    _desk(db)
    for a, b in ((437, 439), (441, 443), (445, 447)):
        _listed(db, f"{a} Sample Pkwy", f"Builder's dream and great investment! 2 lots being sold ({a} & {b} Sample "
                                        "Pkwy). Owner will consider all offers on the package.", price=640000)
    res = s13.STREAM.run(db, _ctx(tmp_path))
    text = _flat(tmp_path / "out" / f"s13_teardowns_union_{NOW:%Y-%m-%d}.pdf")
    assert len(re.findall(r"\b4\d\d Sample Pkwy\b", text)) == 1 and res.metrics["skipped_duplicate"] == 2


def _targets(db):
    from db.models import FirmTarget
    made = {}
    for name, kind, county, stage, score in (
            ("union", "builder_developer", "Union", "NEW", 9), ("ocean", "builder_developer", "Ocean", "NEW", 8),
            ("lost", "builder_developer", "Union", "LOST", 20), ("roofer", "roofer", "Union", "NEW", 30),
            ("union2", "builder_developer", "Union", "NEW", 1)):
        t = FirmTarget(kind=kind, stream="s13" if kind == "builder_developer" else "s4", org=f"{name} co",
                       email=f"office@{name}.example", county=county, stage=stage, score=score)
        db.add(t)
        made[name] = t
    db.flush()
    return made


def test_pitches_county_sheet_first_else_all_county_with_a_neighbor_street(db, tmp_path):
    _desk(db)
    _listed(db, "60 Sample St", "Calling all builders! Level lot near the beach.", county="Monmouth",
            muni="LONG BRANCH CITY", city="Long Branch", zip_code="07740", acres=0.2)
    t = _targets(db)
    res = s13.STREAM.run(db, _ctx(tmp_path))
    assert res.metrics["pitches"] == 2 and [o.target_id for o in res.outbound] == [t["union"].id, t["ocean"].id]
    y, w, _ = NOW.isocalendar()
    union, ocean = res.outbound
    for ob in res.outbound:
        assert ob.stream == "s13" and ob.kind == "pitch" and ob.priority == 3 and ob.attachments == []
        assert ob.in_reply_to is None and ob.key == f"s13:pitch:{ob.target_id}:{y}-W{w:02d}"
        _plain_touch(ob.subject, ob.body)
        assert "plus NJ sales tax" in ob.sample_body and "$249 a month" in ob.sample_body
        [sample] = ob.sample_attachments
        assert sample.exists() and sample.name.endswith(".sample.pdf")
    # Union: the county sheet, led by its best quoted row (listing words + the tax record), shown in full on top
    assert union.to == "office@union.example" and union.subject == "[Westfield] teardown lots"
    assert _sentences(union.body)[0] == ('The listing for 16 Sample Rd in Westfield says "value is in the land" (a '
                                         "0.31-acre lot), one of 4 teardown and lot listings I found in Union County "
                                         "this week from the public listings and tax records.")
    assert "union" in union.sample_attachments[0].name
    first_rows = _flat(union.sample_attachments[0]).split("Listing brokerage", 1)[1]
    assert first_rows.lstrip().startswith("16 Sample Rd Westfield")
    # Ocean (no Ocean sheet): the all-county sheet, led by the Monmouth street next door, shown in full in its sample
    assert "all_counties" in ocean.sample_attachments[0].name
    assert _sentences(ocean.body)[0].startswith(
        'The listing for 60 Sample St in Long Branch says "calling all builders"')
    assert "one of 5 teardown and lot listings I found in the nine counties I cover" in ocean.body
    assert ocean.sample_body.startswith("Attached is this week's sheet for all nine counties")
    lead = _flat(ocean.sample_attachments[0])
    assert "60 Sample St" in lead and "Long Branch (Monmouth)" in lead
    for p in (tmp_path / "out").glob("*.pdf"):                    # the lead-first copy never reaches a subscriber
        assert "60_sample_st" not in p.name


def test_pitch_cap_keys_and_the_outbox_holds_the_sample(db, tmp_path):
    from db.models import FirmOutbox
    from engine import firm_outbox as fo
    _desk(db)
    t = _targets(db)
    capped = s13.STREAM.run(db, _ctx(tmp_path, pitches_per_run=1))
    assert [o.target_id for o in capped.outbound] == [t["union"].id]
    dry = s13.STREAM.run(db, _ctx(tmp_path, send=False))
    assert dry.outbound == [] and dry.products == 2                 # a dry run still writes the sheets
    [ob, _] = s13.STREAM.run(db, _ctx(tmp_path)).outbound
    row = fo.enqueue(db, ob)
    held = db.query(FirmOutbox).filter_by(key=f"{ob.key}:sample").one()
    assert row.status == "queued" and row.attachments_json == []
    assert held.status == "held" and held.attachments_json[0]["name"].endswith(".sample.pdf")
    assert "$499 plus NJ sales tax for all nine" in held.body
    again = s13.STREAM.run(db, _ctx(tmp_path))
    assert t["union"].id not in [o.target_id for o in again.outbound]   # this week's key is used / mail queued


def test_target_in_a_sequence_gets_no_second_touch(db, tmp_path):
    from db.models import FirmTarget
    _desk(db)
    t = FirmTarget(kind="builder_developer", stream="s13", org="Sample Homes", email="info@samplehomes.example",
                   county="Union", stage="CONTACTED", touches=1, last_contact_at=NOW)
    db.add(t)
    db.flush()
    week2 = _ctx(tmp_path, now=NOW + timedelta(days=7))
    assert s13.STREAM.run(db, week2).outbound == []                  # the outbox's in-thread nudges follow up


def test_sheet_names_match_subscriber_delivery(db, tmp_path):
    from engine import firm_outbox as fo
    _desk(db)
    s13.STREAM.run(db, _ctx(tmp_path))
    out = tmp_path / "out"
    assert [p.name for p in fo.delivery_files(out, "Union")] == [f"s13_teardowns_union_{NOW:%Y-%m-%d}.pdf"]
    assert fo.delivery_files(out, None)[0].name == f"s13_teardowns_all_counties_{NOW:%Y-%m-%d}.pdf"
    assert fo.delivery_files(out, "Essex") == fo.delivery_files(out, None)    # no Essex sheet: the newest files


def test_sheets_keep_every_row_and_samples_are_one_first_page(db, tmp_path):
    """Subscribers get every row (pages as needed); the in-thread nudge calls the sample "one page", so a pitch's sample
    is the sheet's first page with Touch 1's street on top, and Touch 2 says how much of the sheet it is."""
    from db.models import FirmTarget
    from engine.streams.s4_contractor_leads import page_count
    for i in range(34):
        _listed(db, f"{100 + 2 * i} Sample St", f"Builders opportunity, lot {i}: bring your plans, investor special, "
                                                "sold strictly as-is, needs a full gut or a teardown and new build.",
                price=500000 + 1000 * i)
    for name, county in (("union", "Union"), ("shore", "Ocean")):
        db.add(FirmTarget(kind="builder_developer", stream="s13", org=f"{name} homes", email=f"info@{name}.example",
                          county=county, stage="NEW"))
    db.flush()
    res = s13.STREAM.run(db, _ctx(tmp_path))
    out = tmp_path / "out"
    for scope in ("union", "all_counties"):
        path = out / f"s13_teardowns_{scope}_{NOW:%Y-%m-%d}.pdf"
        assert page_count(path) >= 2 and len(re.findall(r"\b\d+ Sample St\b", _flat(path))) == 34
    union, shore = res.outbound
    for ob, sheet in ((union, "the Union County sheet"), (shore, "this week's sheet for all nine counties")):
        [teaser] = ob.sample_attachments
        assert page_count(teaser) == 1 and teaser.parent.parent.name == "leads"
        shown = len(re.findall(r"\b\d+ Sample St\b|\d•+ S•+ St\b", _flat(teaser)))
        assert 8 <= shown < 34 and f"first page: {shown} of the 34 active listings" in _flat(teaser)
        assert ob.sample_body.startswith(f"Attached is the first page of {sheet} ({shown} of 34 listings): the first 2 "
                                         "listings in full, the rest with the street hidden.")
        lead = re.search(r"\b(\d+ Sample St)\b", ob.body).group(1)
        assert _flat(teaser).split("Listing brokerage", 1)[1].lstrip().startswith(lead)
    assert "one of 34 teardown and lot listings I found in Union County" in union.body
    assert "one of 34 teardown and lot listings I found in the nine counties I cover" in shore.body


def test_empty_database(db, tmp_path):
    res = s13.STREAM.run(db, _ctx(tmp_path))
    assert (res.processed, res.found, res.products, res.outbound) == (0, 0, 0, [])
    assert res.metrics["rows"] == 0 and res.metrics["pdfs"] == 0


def test_harvest_keeps_the_listing_lot_size(db, monkeypatch):
    """agents/scrapers/stale_listing_distress_agent.py stores the listing's lot size for the land-area column."""
    from types import SimpleNamespace

    from agents.scrapers import stale_listing_distress_agent as sl
    from db.models import Listing
    from engine import homeharvest_provider as hh

    class _GIS:
        def __init__(self, *a, **kw):
            self.http = SimpleNamespace(stats_summary=lambda: {})

        def resolve_municipality(self, county, city):
            return None

        def find_by_address(self, county, street, mun_code):
            return None

    row = {"property_id": "LOT1", "url": "https://listing.test/LOT1", "status": "for_sale", "address": "70 Sample St",
           "city": "Westfield", "zip": "07090", "property_type": "land", "beds": None, "baths": None, "sqft": None,
           "lot_sqft": 13504, "year_built": None, "list_price": 300000, "list_date": None, "days_on_market": 20,
           "price_reduced": None, "description": "Buildable lot.", "lat": None, "lon": None, "mls": "GSNJ",
           "raw": {"location": {"county": {"name": "Union"}}, "advertisers": []}}
    monkeypatch.setattr(hh, "active_listings", lambda zip_code, past_days=None: [dict(row)])
    monkeypatch.setattr(sl, "NJGISClient", _GIS)
    sl.StaleListingDistressAgent(zips=["07090"]).execute()
    db.expire_all()
    assert db.query(Listing).filter_by(source_ref="LOT1").one().raw_json["lot_sqft"] == 13504
