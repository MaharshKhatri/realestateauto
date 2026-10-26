from datetime import date

from agents.scrapers.njgis_client import estimate_sqft, feature_to_property, same_street, stories_from_desc
from db.repository import make_parcel_id, normalize_block_lot


def test_parcel_ids():
    assert normalize_block_lot("00335") == "335"
    assert normalize_block_lot("78.00") == "78"
    assert normalize_block_lot("1234.01") == "1234.01"
    assert make_parcel_id("Essex", "0702", "335", "78") == "ESSEX-0702-335-78"
    assert make_parcel_id("Middlesex", "1216", "136", "1.04", "C A310") == "MIDDLESEX-1216-136-1.04-CA310"


def test_same_street_handles_ranges_and_spelling():
    assert same_street("718-720 N 8TH ST", "718 722 NO 8TH STREET")
    assert same_street("186 NEWARK AVENUE", "186 NEWARK AVE")
    assert not same_street("730 N 8TH ST", "1399 WOOD VALLEY RD")


def test_feature_mapping_absentee_and_out_of_state():
    feature = {"attributes": {
        "PAMS_PIN": "1225_1_1", "PCL_MUN": "1225", "PCLBLOCK": "1", "PCLLOT": "1", "PCLQCODE": " ",
        "PROP_CLASS": "2", "COUNTY": "MIDDLESEX", "MUN_NAME": "WOODBRIDGE TWP", "PROP_LOC": "1 WALLACE ST",
        "ST_ADDRESS": "5315 ARTHUR KILL RD", "CITY_STATE": "STATEN ISLAND  NY", "ZIP_CODE": "10307",
        "NET_VALUE": 51000, "LAST_YR_TX": 6000.0, "BLDG_DESC": "1S F O", "YR_CONSTR": 1926,
        "DEED_DATE": "240726", "SALE_PRICE": 200000, "DWELL": 1, "CALC_ACRE": 0.1},
        "centroid": (-74.28, 40.55)}
    d = feature_to_property(feature)
    assert d["parcel_id"] == "MIDDLESEX-1225-1-1" and d["qual"] is None
    assert d["is_absentee"] and d["is_out_of_state"] and d["owner_state"] == "NY"
    assert d["last_sale_date"] == date(2024, 7, 26)
    assert d["lat"] == 40.55 and d["county"] == "Middlesex"


def test_sqft_estimate_from_building_description():
    assert stories_from_desc("2SF2UG") == 2.0
    assert stories_from_desc("1S AL F 1UG") == 1.0
    assert estimate_sqft("2SF", 1) == 1700
    assert estimate_sqft("3-FAM", 3) == 3150
