# -*- coding: utf-8 -*-
"""Unit test for reins_pdf_parser against a real REINS overview PDF.

Fixture: /tmp/reins_overview_test.pdf (日本橋三越前アムフラット, 100140509385).
Expected values read directly from that PDF's span dump / render.
"""
import os
import pytest

from reins_pdf_parser import parse_overview_pdf

NIHONBASHI_PDF = '/tmp/reins_overview_test.pdf'
HARUMI_PDF = '/tmp/reins_overview_harumi.pdf'

# 日本橋三越前アムフラット（100140509385）— expected 由該 PDF span dump 讀出
NIHONBASHI_EXPECTED = {
    'reins_id': '100140509385',
    'price': 10780,
    'room_layout': '2ＬＤＫ',
    'size_sqm': 55.08,
    'building_name': '日本橋三越前アムフラット',
    'floor': 9,
    'floors_above': 10,
    'structure': 'ＳＲＣ',
    'mgmt_fee': 17400,
    'repair_reserve': 30610,
    'built_date': '平成16年 9月',
    'address': '東京都中央区日本橋堀留町１丁目３－１２',
    # schema 相容欄位
    'property_type': '中古マンション',
    'line': '東京地下鉄日比谷線',
    'station': '人形町',
    'walk_min': 5,
    'land_rights': '所有権',
    'use_district': '商業',
    'current_status': '空家',
    'handover_timing': '相談',
    'transaction_type': '専任',
    'management_type': '管理会社に全部委託',
    'registration_date': '令和 8年 8月28日',
    'latest_update_date': '令和 8年 8月30日',
    'notes_freetext': '現在、内装リノベーション工事中完成予定：２０２６年１２月上旬\n頃',
    'registration_no': '',   # 日本橋備考係真文字，唔係登録No.
}
# 呢份 PDF 呢幾格 label 喺度但 value 真係空白
NIHONBASHI_EMPTY = [
    'underground_floors', 'total_units', 'balcony_sqm', 'orientation',
    'management_company', 'parking',
]

# HARUMI FLAG PARK VILLAGE T棟（100140505492）— expected 由 Johnny 提供
HARUMI_EXPECTED = {
    'reins_id': '100140505492',
    'price': 13990,
    'room_layout': '3ＬＤＫ',
    'size_sqm': 70.06,
    'building_name': 'ＨＡＲＵＭＩ　ＦＬＡＧ　ＰＡＲＫ　ＶＩＬＬＡＧＥ　Ｔ棟',
    'floor': 7,
    'floors_above': 50,
    'underground_floors': 1,
    'total_units': 722,
    'balcony_sqm': 16.26,
    'structure': 'ＲＣ',
    'orientation': '東',
    'mgmt_fee': 27540,
    'repair_reserve': 13380,
    'built_date': '令和 7年 8月',
    'address': '東京都中央区晴海５丁目',
    # schema 相容欄位
    'property_type': '中古マンション',
    'line': '東京都大江戸線',
    'station': '勝どき',
    'walk_min': 15,
    'land_rights': '所有権',
    'use_district': '商業',
    'current_status': '空家',
    'handover_timing': '相談',
    'transaction_type': '一般',
    'management_company': '三井不動産レジデンシャルサービス株式会社',
    'management_type': '管理会社に全部委託',
    'registration_date': '令和 8年 8月28日',
    'latest_update_date': '令和 8年 8月30日',
    'notes_freetext': 'Ｆ２ＦＢＱＡ０Ａ',
    'registration_no': 'F2FBQA0A',   # 備考欄係登録No. → 半形正規化
}
HARUMI_EMPTY = ['parking']

# 舊 reins_client.parse_overview_pdf 設計上想回傳嘅 keys（reins_client.py return dict）。
# 舊版 runtime 其實會 NameError(size_sqm) crash，所以呢個係佢嘅 intended schema。
OLD_PARSER_KEYS = {
    'reins_id', 'price', 'property_type', 'address', 'building_name', 'line',
    'station', 'walk_min', 'room_layout', 'size_sqm', 'built_date_full',
    'structure', 'floor', 'floors_above', 'underground_floors', 'orientation',
    'balcony_sqm', 'total_units', 'land_rights', 'use_district', 'current_status',
    'handover_timing', 'transaction_type', 'mgmt_fee', 'repair_reserve',
    'management_company', 'management_type', 'parking', 'registration_date',
    'latest_update_date', 'notes_freetext', 'registration_no',
    # '_raw_kv' 係舊版 debug 用，唔係 downstream schema，新 parser 唔需要提供
}


def _check(pdf, expected):
    d = parse_overview_pdf(pdf)
    failures = []
    for field, exp in expected.items():
        actual = d.get(field)
        ok = actual == exp
        print(f'{field:20s} expected={exp!r:50s} actual={actual!r:50s} {"pass" if ok else "FAIL"}')
        if not ok:
            failures.append(field)
    return d, failures


@pytest.mark.skipif(not os.path.exists(NIHONBASHI_PDF), reason='nihonbashi PDF not present')
def test_nihonbashi_known_fields():
    _, failures = _check(NIHONBASHI_PDF, NIHONBASHI_EXPECTED)
    assert not failures, f'failed fields: {failures}'


@pytest.mark.skipif(not os.path.exists(NIHONBASHI_PDF), reason='nihonbashi PDF not present')
def test_nihonbashi_empty_fields_are_blank():
    d = parse_overview_pdf(NIHONBASHI_PDF)
    for field in NIHONBASHI_EMPTY:
        actual = d.get(field)
        assert actual in (None, ''), f'{field}: expected empty, got {actual!r}'


@pytest.mark.skipif(not os.path.exists(HARUMI_PDF), reason='harumi PDF not present')
def test_harumi_known_fields():
    _, failures = _check(HARUMI_PDF, HARUMI_EXPECTED)
    assert not failures, f'failed fields: {failures}'


@pytest.mark.skipif(not os.path.exists(HARUMI_PDF), reason='harumi PDF not present')
def test_harumi_empty_fields_are_blank():
    d = parse_overview_pdf(HARUMI_PDF)
    for field in HARUMI_EMPTY:
        actual = d.get(field)
        assert actual in (None, ''), f'{field}: expected empty, got {actual!r}'


# --- B. schema compatibility：舊 parser 有嘅 key，新 parser 必須全部有 ---
def _schema_report(pdf):
    new_keys = set(parse_overview_pdf(pdf).keys())
    missing = OLD_PARSER_KEYS - new_keys      # 舊有、新冇 → 會整傷 downstream
    extra = new_keys - OLD_PARSER_KEYS        # 新有、舊冇 → 新增，唔會整傷
    return new_keys, missing, extra


@pytest.mark.skipif(not os.path.exists(NIHONBASHI_PDF), reason='nihonbashi PDF not present')
def test_schema_compat_nihonbashi():
    new_keys, missing, extra = _schema_report(NIHONBASHI_PDF)
    print(f'\nold_keys={len(OLD_PARSER_KEYS)} new_keys={len(new_keys)}')
    print(f'missing={sorted(missing)}')
    print(f'extra={sorted(extra)}')
    assert not missing, f'missing keys (downstream 會整傷): {sorted(missing)}'


@pytest.mark.skipif(not os.path.exists(HARUMI_PDF), reason='harumi PDF not present')
def test_schema_compat_harumi():
    new_keys, missing, extra = _schema_report(HARUMI_PDF)
    print(f'\nold_keys={len(OLD_PARSER_KEYS)} new_keys={len(new_keys)}')
    print(f'missing={sorted(missing)}')
    print(f'extra={sorted(extra)}')
    assert not missing, f'missing keys (downstream 會整傷): {sorted(missing)}'
