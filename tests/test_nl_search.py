# -*- coding: utf-8 -*-
"""Unit tests for SearchPlan schema/validator + rule parser (offline, no LLM/REINS)."""
from search_plan import validate_plan, plan_summary, empty_plan
from nl_rule_parser import parse_rule_based
from nl_search import build_search_plan, plan_to_reins_filters


# ── validator ──

def test_validator_accepts_whitelist_hard_filters():
    raw = {'hard_filters': {'pref': '東京都', 'city': '中央区', 'price_max': 8000,
                            'layout_type': ['ＬＤＫ', 'ＤＫ'], 'orientation': ['南', '東']}}
    plan = validate_plan(raw)
    assert plan['hard_filters']['pref'] == '東京都'
    assert plan['hard_filters']['price_max'] == 8000
    assert plan['hard_filters']['layout_type'] == ['ＬＤＫ', 'ＤＫ']
    assert plan['hard_filters']['orientation'] == ['南', '東']


def test_validator_rejects_unknown_hard_filter_to_unsupported():
    raw = {'hard_filters': {'seaview': True, 'pref': '東京都'}}
    plan = validate_plan(raw)
    # 'seaview' 唔係 whitelist → 搬去 unsupported，唔會落 hard_filters
    assert 'seaview' not in plan['hard_filters']
    assert any('seaview' in u for u in plan['unsupported_preferences'])
    assert plan['hard_filters']['pref'] == '東京都'


def test_validator_rejects_bad_select_value():
    raw = {'hard_filters': {'orientation': ['火星']}}
    plan = validate_plan(raw)
    assert 'orientation' not in plan['hard_filters']
    assert plan['clarification_needed']  # 有提示


def test_validator_never_turns_soft_preference_into_hard_filter():
    # LLM 如果錯誤將「新啲」放落 hard_filters 做 building_age=5，validator 唔會特別擋
    # （building_age 唔喺 whitelist，所以會落 unsupported——呢個正係保護）
    raw = {'hard_filters': {'building_age': 5}, 'soft_preferences': ['新啲']}
    plan = validate_plan(raw)
    assert 'building_age' not in plan['hard_filters']
    assert '新啲' in plan['soft_preferences']


# ── rule parser ──

def test_rule_price_max():
    raw = parse_rule_based('中央区 8000万円以下 2LDK')
    assert raw is not None
    assert raw['hard_filters']['city'] == '中央区'
    assert raw['hard_filters']['price_max'] == 8000.0
    assert 'ＬＤＫ' in raw['hard_filters']['layout_type']
    assert raw['hard_filters']['room_count_min'] == 2


def test_rule_area_and_walk():
    raw = parse_rule_based('東京都 60㎡以上 徒歩10分以内')
    assert raw['hard_filters']['pref'] == '東京都'
    assert raw['hard_filters']['area_min'] == 60.0
    assert raw['hard_filters']['walk_min'] == 10


def test_rule_soft_preference_not_hard():
    raw = parse_rule_based('中央区 景觀好 高級感 3LDK')
    # 景觀/高級感 落 soft，唔落 hard
    assert '景觀好' in raw['soft_preferences'] or '景觀' in raw['soft_preferences']
    assert 'orientation' not in raw['hard_filters']
    assert 'ＬＤＫ' in raw['hard_filters']['layout_type']


def test_rule_tokyoto_not_orientation():
    # 「東京都」嘅「東」唔係方向
    raw = parse_rule_based('東京都 60㎡以上 景觀好')
    assert 'orientation' not in raw['hard_filters']


def test_rule_explicit_orientation():
    # 明確「向南」先係方向
    raw = parse_rule_based('中央区 向南 2LDK')
    assert raw['hard_filters']['orientation'] == ['南']


def test_rule_oku_price():
    # 「1億円以下」→ 10000万
    raw = parse_rule_based('港区 1億円以下')
    assert raw['hard_filters']['price_max'] == 10000.0


def test_rule_man_price_still_works():
    raw = parse_rule_based('中央区 8000万円以下')
    assert raw['hard_filters']['price_max'] == 8000.0


def test_rule_gibberish_returns_none():
    assert parse_rule_based('asdkfjhaslkdjf') is None


def test_rule_new_or_used_soft():
    # 「新啲」係模糊 → soft，唔係 hard built_year
    raw = parse_rule_based('中央区 新啲嘅樓')
    assert '新啲' in raw['soft_preferences']
    assert 'built_year_from' not in raw['hard_filters']


def test_rule_explicit_built_year_is_hard():
    # 「築15年以内」係明確 → hard
    raw = parse_rule_based('中央区 築15年以内')
    assert 'built_year_from' in raw['hard_filters']


# ── orchestrator plan → executor filters ──

def test_plan_to_reins_filters_only_supported():
    plan = validate_plan({'hard_filters': {
        'pref': '東京都', 'city': '中央区', 'price_max': 8000,
        'orientation': ['南'],  # executor 而家支援，但係 list 形式 → 轉 single
    }})
    f = plan_to_reins_filters(plan, page=1)
    assert f['pref'] == '東京都'
    assert f['city'] == '中央区'
    assert f['price_max'] == 8000
    # orientation 而家 executor 支援，會 map 落去（list → 由 search_properties 轉 single）
    assert f['orientation'] == ['南']


def test_build_search_plan_rule_path():
    plan, meta = build_search_plan('中央区 8000万円以下 2LDK')
    assert meta['parser'] == 'rule'
    assert plan['hard_filters']['city'] == '中央区'


def test_empty_query_needs_clarification():
    plan, meta = build_search_plan('')
    assert plan['clarification_needed']
