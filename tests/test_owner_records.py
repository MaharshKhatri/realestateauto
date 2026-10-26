"""County tax-board export parsing (fixtures mirror the real export column layout)."""
from datetime import date

from agents.scrapers.owner_records_agent import (deed_rows, find_flips, is_arms_length, owner_rows,
                                                 split_city_state_zip)
from conftest import FIXTURES

TR = FIXTURES / "taxrecords"


def test_city_state_zip_variants():
    assert split_city_state_zip("ELIZABETH, N J  07201") == ("ELIZABETH, NJ", "07201")
    assert split_city_state_zip("NEWARK, N.J.  07102") == ("NEWARK, NJ", "07102")
    assert split_city_state_zip("LYNDHURST , NJ  07071") == ("LYNDHURST, NJ", "07071")


def test_owner_export_rows_map_to_parcel_ids():
    rows = list(owner_rows(TR / "owners.zip"))
    assert rows[0]["parcel_id"] == "HUDSON-0906-1001-12"
    assert rows[0]["owner_name"] == "DOE, JANE EST OF" and rows[0]["sqft"] == 1680
    assert rows[1]["parcel_id"] == "HUDSON-0906-1002-3-C0001"
    assert rows[1]["sqft"] is None                       # 0 sq ft = unknown


def test_deed_export_and_flip_detection():
    deeds = list(deed_rows(TR / "deeds.zip"))
    assert deeds[0]["grantee"] == "FLIP CO LLC" and deeds[0]["deed_date"] == date(2025, 11, 1)
    assert is_arms_length(deeds[0]) and not is_arms_length(deeds[2])   # $1 / NU code 25
    flips = find_flips(deeds)
    assert len(flips) == 1
    f = flips[0]
    assert f["flipper"] == "FLIP CO LLC" and f["bought"] == 300000 and f["sold"] == 450000 and f["days_held"] == 212
