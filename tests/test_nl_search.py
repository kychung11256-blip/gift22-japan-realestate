# -*- coding: utf-8 -*-
"""Unit tests for SearchPlan schema/validator + rule parser (offline, no LLM/REINS)."""
import sys
import types
from pathlib import Path

import pytest

from search_plan import validate_plan, plan_summary, empty_plan
from nl_rule_parser import parse_rule_based
from nl_search import build_search_plan, plan_to_reins_filters, _post_filter_and_rank


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


def test_hk_natural_language_budget_rights_year_area_floor():
    raw = parse_rule_based('東京都港区，8000-15000萬預算，所有權，2010年以后建成，60平以上，3樓以上')
    hf = raw['hard_filters']
    assert hf['pref'] == '東京都'
    assert hf['city'] == '港区'
    assert hf['price_min'] == 8000
    assert hf['price_max'] == 15000
    assert hf['land_rights'] == '所有権のみ'
    assert hf['built_year_from'] == 2010
    assert hf['area_min'] == 60
    assert hf['floor_min'] == 3
    assert '平' not in raw['soft_preferences']


@pytest.mark.parametrize('query,expected', [
    ('新宿區 9000至12000万 70平方米以上 5层以上 所有权 2015年之后',
     {'city': '新宿区', 'price_min': 9000, 'price_max': 12000, 'area_min': 70,
      'floor_min': 5, 'land_rights': '所有権のみ', 'built_year_from': 2015}),
    ('港区 1億円以下 60m2以上 3階以上 借地權',
     {'city': '港区', 'price_max': 10000, 'area_min': 60,
      'floor_min': 3, 'land_rights': '借地権のみ'}),
])
def test_chinese_japanese_synonyms(query, expected):
    hf = parse_rule_based(query)['hard_filters']
    for key, value in expected.items():
        assert hf[key] == value


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


def test_plan_to_reins_filters_applies_rights_year_and_floor():
    plan = validate_plan({'hard_filters': {
        'pref': '東京都', 'city': '港区', 'land_rights': '所有権のみ',
        'built_year_from': 2010, 'floor_min': 3, 'area_min': 60,
        'price_min': 8000, 'price_max': 15000,
    }})
    f = plan_to_reins_filters(plan)
    assert f['land_rights'] == '所有権のみ'
    assert f['built_year_from'] == 2010
    assert f['floor_min'] == 3
    assert f['area_min'] == 60
    assert f['price_min'] == 8000
    assert f['price_max'] == 15000


def test_build_search_plan_rule_path():
    plan, meta = build_search_plan('中央区 8000万円以下 2LDK')
    assert meta['parser'] == 'rule'
    assert plan['hard_filters']['city'] == '中央区'


def test_empty_query_needs_clarification():
    plan, meta = build_search_plan('')
    assert plan['clarification_needed']


def test_natural_language_filters_reach_reins_form_without_network(monkeypatch):
    """Prove the parsed HK query reaches the real REINS selector mapping offline."""
    import reins_client

    calls = {'fills': {}, 'selects': {}, 'clicks': []}

    class FakeOption:
        def __init__(self, label, value):
            self.label = label
            self.value = value

        def text_content(self):
            return self.label

        def get_attribute(self, name):
            return self.value if name == 'value' else None

    class FakeElement:
        def __init__(self, selector='', text='', options=None):
            self.selector = selector
            self.text = text
            self.options = options or []

        def text_content(self):
            return self.text

        def inner_text(self):
            return self.text

        def fill(self, value):
            calls['fills'][self.selector] = value

        def dispatch_event(self, _name):
            return None

        def click(self, force=False):
            calls['clicks'].append(self.selector or self.text)

        def is_checked(self):
            return False

        def is_disabled(self):
            return False

        def query_selector_all(self, selector):
            return self.options if selector == 'option' else []

        def get_attribute(self, _name):
            return None

    class FakePage:
        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, _ms):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def content(self):
            return '<body>売買物件検索</body>'

        def query_selector_all(self, selector):
            if selector == 'button':
                return [
                    FakeElement(text='売買物件検索'),
                    FakeElement(text='検索'),
                    FakeElement(text='OK'),
                ]
            if selector == 'button, a':
                return []
            if selector == '.p-table-body > .p-table-body-row':
                return []
            return []

        def query_selector(self, selector):
            if selector.startswith('label[for="'):
                return FakeElement(selector=selector)
            if selector in ('#__BVID__582', '#__BVID__586'):
                return FakeElement(selector=selector, options=[
                    FakeOption('2010年(平成22年)', '2010'),
                ])
            if selector.startswith('#__BVID__'):
                return FakeElement(selector=selector)
            return None

        def select_option(self, selector, **kwargs):
            calls['selects'][selector] = kwargs

        def eval_on_selector(self, selector, _script):
            assert selector == 'body'
            return '0件'

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        def new_context(self, **_kwargs):
            return FakeContext()

        def close(self):
            return None

    class FakeChromium:
        def launch(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightCM:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(reins_client, '_has_storage_state', lambda: True)
    monkeypatch.setitem(
        sys.modules,
        'playwright.sync_api',
        types.SimpleNamespace(sync_playwright=lambda: FakePlaywrightCM()),
    )

    plan, _meta = build_search_plan(
        '東京都港区，8000-15000萬預算，所有權，2010年以后建成，60平以上，3樓以上'
    )
    result = reins_client.search_properties(plan_to_reins_filters(plan), timeout_ms=100)

    assert result['code'] == 1
    assert calls['fills']['#__BVID__477'] == '8000'
    assert calls['fills']['#__BVID__479'] == '15000'
    assert calls['fills']['#__BVID__507'] == '60'
    assert calls['fills']['#__BVID__541'] == '3'
    assert 'label[for="__BVID__319"]' in calls['clicks']
    assert calls['selects']['#__BVID__582'] == {'value': '2010'}


def test_search_plan_ui_uses_customer_friendly_labels():
    html = (Path(__file__).resolve().parents[1] / 'index.html').read_text(encoding='utf-8')
    assert '已理解並套用以下搜尋條件' in html
    assert "price_min:'預算下限'" in html
    assert "land_rights:'土地權利'" in html
    assert "built_year_from:'築年由'" in html
    assert "floor_min:'樓層至少'" in html


def test_post_filter_marks_reins_orientation_filter_source():
    plan = validate_plan({'hard_filters': {'orientation': ['南']}})
    rows = [{'reins_id': '1001', 'building_name': 'テスト物件'}]
    ranked = _post_filter_and_rank(rows, plan)
    assert ranked[0]['effective_orientation'] == '南'
    assert ranked[0]['orientation_source'] == 'reins_search_filter'
    assert ranked[0]['orientation_filter_applied'] is True
    assert 'effective_orientation' not in rows[0]
