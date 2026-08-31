import json
from pathlib import Path

from workbench_api import filter_properties, property_stats, sort_properties, standardize_property


def _raw_row():
    return {
        "id": "REINS001",
        "source": "reins",
        "building_name": "テスト タワー",
        "address": "東京都港区六本木1丁目",
        "station": "六本木一丁目",
        "walk_min": 4,
        "price": 69500,
        "mgmt_fee": "55,250円",
        "size_sqm": 86.15,
        "room_layout": "２ＬＤＫ",
        "floor": 8,
        "total_floors": 47,
        "built_date_full": "平成24年 8月",
        "orientation": "北西",
        "structure": "ＳＲＣ",
        "status": "published",
        "photos": "[]",
        "floorplan_images": '[{"url":"/uploads/reins/1/drawing_page_1.jpg"}]',
        "interior_photos": "[]",
        "ai_keywords": '["所有権"]',
        "updated_at": "2026-08-31 06:56:22",
    }


def test_standardize_property_contract_and_money_units():
    item = standardize_property(_raw_row())
    assert item["priceYen"] == 695_000_000
    assert item["managementFeeYen"] == 55_250
    assert item["layout"] == "2LDK"
    assert item["structure"] == "SRC"
    assert item["prefecture"] == "東京都"
    assert item["city"] == "港区"
    assert item["images"][0]["type"] == "floorplan"
    assert item["updatedAt"].endswith("Z")


def test_filter_sort_and_stats_use_standardized_values():
    first = standardize_property(_raw_row())
    second_raw = _raw_row()
    second_raw.update({"id": "REINS002", "price": 18000, "address": "東京都中央区晴海2丁目", "status": "draft", "size_sqm": 70})
    second = standardize_property(second_raw)
    items = [first, second]

    assert [x["id"] for x in filter_properties(items, area="中央区")] == ["REINS002"]
    assert [x["id"] for x in filter_properties(items, status="draft")] == ["REINS002"]
    assert [x["id"] for x in sort_properties(items, "price_asc")] == ["REINS002", "REINS001"]

    stats = property_stats(items)
    assert stats["total"] == 2
    assert stats["byStatus"] == {"draft": 1, "published": 1, "archived": 0, "lead": 0}
    assert stats["bySource"] == {"reins": 2}


def test_repository_samples_match_contract_when_available():
    sample_path = Path(__file__).resolve().parents[1] / "sample-properties.json"
    if not sample_path.exists():
        return
    samples = json.loads(sample_path.read_text(encoding="utf-8"))
    required = {
        "id", "source", "title", "prefecture", "city", "ward", "address",
        "station", "walkMinutes", "priceYen", "managementFeeYen", "areaSqm",
        "layout", "floor", "totalFloors", "builtAt", "direction", "structure",
        "status", "completeness", "missingFields", "features", "images", "updatedAt",
    }
    assert samples
    assert all(required.issubset(sample) for sample in samples)
