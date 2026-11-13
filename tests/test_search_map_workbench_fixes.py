import itertools

import pytest

from property_search import (
    filter_local_listings,
    infer_direction,
    listing_role,
    parse_query,
)
from reins_pdf_parser import infer_orientation_from_text
from nl_rule_parser import parse_rule_based
from workbench_api import standardize_property


def listing(listing_id, price, orientation="", **extra):
    item = {
        "id": listing_id,
        "price": price,
        "status": "published",
        "address": "東京都港区",
        "orientation": orientation,
        "room_layout": "2LDK",
        "size_sqm": 60,
    }
    item.update(extra)
    return item


def test_three_oku_ceiling_is_a_hard_numeric_constraint():
    items = [
        listing("under", 29999),
        listing("edge", 30000),
        listing("over", 30001),
        listing("far-over", 69500),
    ]
    assert [item["id"] for item in filter_local_listings(items, "3億以下")] == ["under", "edge"]


def test_exact_south_excludes_southeast_southwest_and_missing():
    items = [
        listing("south", 10000, "南"),
        listing("southeast", 10000, "南東"),
        listing("southwest", 10000, "南西"),
        listing("unknown", 10000, ""),
    ]
    assert [item["id"] for item in filter_local_listings(items, "朝向朝南")] == ["south"]


def test_direction_can_use_explicit_pdf_evidence_but_not_tokyo_name():
    direction, source, confidence = infer_direction(
        listing("pdf", 10000, "", notes_freetext="バルコニー方向：南")
    )
    assert (direction, source, confidence) == ("南", "notes", 0.85)
    assert infer_direction(listing("tokyo", 10000, "", notes_freetext="東京都港区"))[0] == ""


def test_pdf_text_orientation_requires_an_explicit_label():
    assert infer_orientation_from_text("主要採光面 南向き") == "南"
    assert infer_orientation_from_text("所在地 東京都港区") == ""


def test_listing_role_uses_transaction_evidence():
    assert listing_role({"transaction_type": "売主"})["code"] == "direct"
    assert listing_role({"transaction_type": "専任媒介"})["code"] == "agent"
    assert listing_role({"listing_agent_name": "山田不動産"})["code"] == "agent"
    assert listing_role({})["code"] == "unknown"


def test_workbench_exposes_role_direction_and_complete_details():
    item = standardize_property(listing(
        "P1", 30000, "南",
        transaction_type="専任媒介",
        listing_agent_name="山田不動産",
        building_name="テストマンション",
        management_company="テスト管理",
        repair_reserve=12000,
        reins_overview_pdf="/uploads/overview.pdf",
    ))
    assert item["listingRoleLabel"] == "仲介／代理房"
    assert item["direction"] == "南"
    assert item["details"]["managementCompany"] == "テスト管理"
    assert item["details"]["repairReserveYen"] == 12000
    assert item["details"]["reinsOverviewPdf"] == "/uploads/overview.pdf"


def test_query_parser_returns_explicit_constraints():
    assert parse_query("3億以下 朝向朝南")["price_max"] == 30000
    assert parse_query("3億以下 朝向朝南")["direction"] == "南"


def test_commercial_query_exposes_every_hard_constraint():
    constraints = parse_query("8000-15000萬預算，所有權，2010年以后建成，60平以上，3樓以上")
    expected = {
        "priceMin": 8000, "priceMax": 15000, "tenure": "所有權",
        "builtYearMin": 2010, "areaMin": 60, "floorMin": 3,
    }
    assert {key: constraints.get(key) for key in expected} == expected
    assert constraints["price_min"] == constraints["priceMin"]
    assert constraints["price_max"] == constraints["priceMax"]
    assert constraints["terms"] == []


COMMERCIAL_QUERY = "8000-15000萬預算，所有權，2010年以后建成，60平以上，3樓以上"


def commercial_listing(listing_id="match", **extra):
    return listing(listing_id, 8000, land_rights="所有権", built_year=2010, floor=3, **extra)


@pytest.mark.parametrize("separator,after,area_unit,floor_unit", list(itertools.product(
    ("-", "–", "〜", "～"), ("以后", "以後"),
    ("平", "平方米", "㎡", "m²"), ("樓", "层", "階"),
)))
def test_commercial_query_spelling_variants(separator, after, area_unit, floor_unit):
    query = f"8000{separator}15000萬預算，所有權，2010年{after}建成，60{area_unit}以上，3{floor_unit}以上"
    constraints = parse_query(query)
    assert [constraints.get(key) for key in (
        "priceMin", "priceMax", "tenure", "builtYearMin", "areaMin", "floorMin",
    )] == [8000, 15000, "所有權", 2010, 60, 3]
    assert constraints["terms"] == []
    assert [item["id"] for item in filter_local_listings([commercial_listing()], query)] == ["match"]


def test_commercial_query_applies_all_canonical_fields_and_yen_prices():
    match = commercial_listing()
    items = [
        match,
        dict(match, id="upper", price=15000, land_rights="所有權"),
        dict(match, id="yen", price=100000000),
        dict(match, id="normalized-yen", price=12000, price_raw=120000000),
        dict(match, id="low-price", price=7999),
        dict(match, id="high-price", price=15001),
        dict(match, id="high-yen", price=150000001),
        dict(match, id="rounded-high-yen", price=15000, price_raw=150000001),
        dict(match, id="leasehold", land_rights="借地権", tenure="所有權"),
        dict(match, id="old", built_year=2009, builtYear=2020, age=0),
        dict(match, id="small", size_sqm=59.9, area=100, land_area_sqm=100),
        dict(match, id="low-floor", floor=2, floors_above=20, total_floors=20),
    ]
    for item in items:
        # Free text must not rescue a failed structured constraint.
        item["notes_freetext"] = COMMERCIAL_QUERY
    assert [item["id"] for item in filter_local_listings(items, COMMERCIAL_QUERY)] == [
        "match", "yen", "normalized-yen", "upper",
    ]


@pytest.mark.parametrize("field", ("price", "land_rights", "built_year", "size_sqm", "floor"))
@pytest.mark.parametrize("missing", (None, "", 0, "unknown"))
def test_commercial_constraints_exclude_missing_or_invalid_canonical_data(field, missing):
    item = commercial_listing()
    item[field] = missing
    assert filter_local_listings([item], COMMERCIAL_QUERY) == []


@pytest.mark.parametrize("field", ("price", "land_rights", "built_year", "size_sqm", "floor"))
def test_commercial_constraints_exclude_absent_fields(field):
    item = commercial_listing()
    del item[field]
    assert filter_local_listings([item], COMMERCIAL_QUERY) == []


@pytest.mark.parametrize("query", ("3億以下", "15000萬以下", "8000萬以上"))
def test_price_constraints_keep_legacy_aliases_and_exclude_unknown_price(query):
    constraints = parse_query(query)
    assert constraints["priceMin"] == constraints["price_min"]
    assert constraints["priceMax"] == constraints["price_max"]
    items = [listing("valid", 10000)] + [listing("missing", value) for value in (None, "", 0, "bad", float("nan"), float("inf"))]
    assert [item["id"] for item in filter_local_listings(items, query)] == ["valid"]


def test_commercial_filters_accept_numeric_strings_but_keep_unparsed_location_terms():
    item = commercial_listing()
    item.update(price="100000000", built_year="2010", size_sqm="60.5", floor="3")
    assert len(filter_local_listings([item], "港区 " + COMMERCIAL_QUERY)) == 1
    assert filter_local_listings([item], "新宿区 " + COMMERCIAL_QUERY) == []


def test_reins_search_accepts_traditional_chinese_ward_suffix():
    plan = parse_rule_based("新宿區 3億以下 朝向朝南")
    assert plan["hard_filters"]["pref"] == "東京都"
    assert plan["hard_filters"]["city"] == "新宿区"
    assert plan["hard_filters"]["price_max"] == 30000
    assert plan["hard_filters"]["orientation"] == ["南"]
