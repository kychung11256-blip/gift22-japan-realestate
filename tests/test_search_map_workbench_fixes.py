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


def test_reins_search_accepts_traditional_chinese_ward_suffix():
    plan = parse_rule_based("新宿區 3億以下 朝向朝南")
    assert plan["hard_filters"]["pref"] == "東京都"
    assert plan["hard_filters"]["city"] == "新宿区"
    assert plan["hard_filters"]["price_max"] == 30000
    assert plan["hard_filters"]["orientation"] == ["南"]
