# -*- coding: utf-8 -*-
"""Coordinate parser tests using explicitly SYNTHETIC, temporary PDFs.

The original real-PDF expectations below remain historical evidence, NOT proof
that real fixtures were tested. Both real fixture paths are reported separately
in pytest properties. Synthetic documents are created only under tmp_path,
contain visibly fictional identifiers/addresses, and never enter any DB/import.
Synthetic coverage does not replace validation against authorized real PDFs.
"""
from pathlib import Path
import pymupdf
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

# Keep real expectations above unchanged for future authorized real-PDF checks.
# These expectations are independent of the handwritten PDF input rows below.
SYNTHETIC_NIHONBASHI_EXPECTED = {
    **NIHONBASHI_EXPECTED,
    'reins_id': 'SYNTHETIC-N-0001',
    'building_name': '合成試験専用マンションＮ',
    'address': '架空県架空市試験町Ｎ（実在しません）',
    'line': '架空試験線Ｎ',
    'station': '架空試験駅Ｎ',
    'notes_freetext': 'SYNTHETIC TEST ONLY（実在物件ではありません）\n合成試験用の備考二行目',
}
SYNTHETIC_HARUMI_EXPECTED = {
    **HARUMI_EXPECTED,
    'reins_id': 'SYNTHETIC-H-0002',
    'building_name': '合成試験専用マンションＨ',
    'address': '架空県架空市試験町Ｈ（実在しません）',
    'line': '架空試験線Ｈ',
    'station': '架空試験駅Ｈ',
    'management_company': '架空合成試験管理会社',
    'notes_freetext': 'ＴＥＳＴ０００２',
    'registration_no': 'TEST0002',
}

# (left label, raw left text, right label, raw right text). Literal raw
# inputs deliberately include units, commas and fullwidth characters: do NOT
# derive them from expected outputs or call/mock the parser while constructing.
_SYNTHETIC_ROWS = {
    'nihonbashi': [
        ('物件番号', 'SYNTHETIC-N-0001', '物件種目', '中古マンション'),
        ('価格', '1億780万円', '修繕積立金', '30,610円'),
        ('管理費', '17,400円', '土地権利', '所有権'),
        ('専有面積', '55.08㎡', '地上階層', '10階'),
        ('所在地', '架空県架空市試験町Ｎ（実在しません）', '地下階層', ''),
        ('マンション名', '合成試験専用マンションＮ', 'バルコニー面積', ''),
        ('所在階', '9階', '管理形態', '管理会社に全部委託'),
        ('間取部屋数', '2', '間取タイプ', 'ＬＤＫ'),
        ('築年月', '平成16年 9月', '最寄駅', '架空試験駅Ｎ'),
        ('建物構造', 'ＳＲＣ', '最新変更年月日', '令和 8年 8月30日'),
        ('棟総戸数', '', '', ''),
        ('バルコニー方向', '', '', ''),
        ('沿線名', '架空試験線Ｎ', '', ''),
        ('用途地域', '商業', '', ''),
        ('現況', '空家', '', ''),
        ('引渡時期', '相談', '', ''),
        ('取引態様', '専任', '', ''),
        ('管理会社名', '', '', ''),
        ('駐車場', '', '', ''),
        ('登録年月日', '令和 8年 8月28日', '', ''),
    ],
    'harumi': [
        ('物件番号', 'SYNTHETIC-H-0002', '物件種目', '中古マンション'),
        ('価格', '13,990万円', '修繕積立金', '13,380円'),
        ('管理費', '27,540円', '土地権利', '所有権'),
        ('専有面積', '70.06㎡', '地上階層', '50階'),
        ('所在地', '架空県架空市試験町Ｈ（実在しません）', '地下階層', '1階'),
        ('マンション名', '合成試験専用マンションＨ', 'バルコニー面積', '16.26㎡'),
        ('所在階', '7階', '管理形態', '管理会社に全部委託'),
        ('間取部屋数', '3', '間取タイプ', 'ＬＤＫ'),
        ('築年月', '令和 7年 8月', '最寄駅', '架空試験駅Ｈ'),
        ('建物構造', 'ＲＣ', '最新変更年月日', '令和 8年 8月30日'),
        ('棟総戸数', '722戸', '', ''),
        ('バルコニー方向', '東', '', ''),
        ('沿線名', '架空試験線Ｈ', '', ''),
        ('用途地域', '商業', '', ''),
        ('現況', '空家', '', ''),
        ('引渡時期', '相談', '', ''),
        ('取引態様', '一般', '', ''),
        ('管理会社名', '架空合成試験管理会社', '', ''),
        ('駐車場', '', '', ''),
        ('登録年月日', '令和 8年 8月28日', '', ''),
    ],
}


def _synthetic_pdf(tmp_path, profile, record_property):
    """Write a genuine text PDF in pytest temp storage; never return parsed data."""
    path = tmp_path / f'SYNTHETIC_TEST_ONLY_{profile}.pdf'
    record_property('fixture_source', 'synthetic_pytest_tmp_path_not_real_REINS')
    for name, real_path in [('nihonbashi', NIHONBASHI_PDF), ('harumi', HARUMI_PDF)]:
        record_property(f'real_fixture_{name}_path', real_path)
        record_property(f'real_fixture_{name}_present', str(Path(real_path).is_file()).lower())
    record_property('synthetic_pdf_path', str(path))
    with pymupdf.open() as doc:
        page = doc.new_page(width=700, height=800)

        def put(x, y, text):
            if text:
                page.insert_text((x, y), text, fontname='japan', fontsize=7)

        put(25.4, 24, 'SYNTHETIC TEST ONLY / 合成試験専用 / NOT A REAL LISTING')
        for index, (left_label, left_text, right_label, right_text) in enumerate(_SYNTHETIC_ROWS[profile]):
            y = 60 + index * 24
            put(25.4, y, left_label)
            put(127, y, left_text)
            put(306, y, right_label)
            put(408, y, right_text)
            if right_label == '最寄駅':
                put(373, y + 12.8, '歩')
                put(388, y + 12.8, '5' if profile == 'nihonbashi' else '15')
                put(401, y + 12.8, '分')
            # Nonempty text on each row OUTSIDE both value columns ensures
            # empty-field tests cannot pass by flattening nearby PDF text.
            put(550, y, 'SYNTHETIC DECOY 999')
        put(25.4, 650, '備考')
        if profile == 'nihonbashi':
            put(127, 650, 'SYNTHETIC TEST ONLY（実在物件ではありません）')
            put(127, 664, '合成試験用の備考二行目')
        else:
            put(127, 650, 'ＴＥＳＴ０００２')
        put(25.4, 700, '間取タイプ、詳細間取にSが含まれる場合')
        put(127, 724, 'SYNTHETIC FOOTER MUST NOT BECOME NOTES')
        doc.set_metadata({'title': 'SYNTHETIC TEST ONLY - NOT REAL REINS DATA',
                          'subject': 'Authorized synthetic parser fixture; no database/import'})
        doc.save(path)
    # Reopen actual bytes, not an in-memory fake. Verify CJK text survived and
    # all empty-cell labels are physically present before the original asserts.
    with pymupdf.open(path) as doc:
        text = doc[0].get_text()
        assert 'SYNTHETIC TEST ONLY' in text
        for row in _SYNTHETIC_ROWS[profile]:
            for label in (row[0], row[2]):
                if label:
                    assert label in text
    return path


@pytest.fixture
def nihonbashi_pdf(tmp_path, record_property):
    return _synthetic_pdf(tmp_path, 'nihonbashi', record_property)


@pytest.fixture
def harumi_pdf(tmp_path, record_property):
    return _synthetic_pdf(tmp_path, 'harumi', record_property)

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


def test_nihonbashi_known_fields(nihonbashi_pdf):
    _, failures = _check(nihonbashi_pdf, SYNTHETIC_NIHONBASHI_EXPECTED)
    assert not failures, f'failed fields: {failures}'


def test_nihonbashi_empty_fields_are_blank(nihonbashi_pdf):
    d = parse_overview_pdf(nihonbashi_pdf)
    for field in NIHONBASHI_EMPTY:
        actual = d.get(field)
        assert actual in (None, ''), f'{field}: expected empty, got {actual!r}'


def test_harumi_known_fields(harumi_pdf):
    _, failures = _check(harumi_pdf, SYNTHETIC_HARUMI_EXPECTED)
    assert not failures, f'failed fields: {failures}'


def test_harumi_empty_fields_are_blank(harumi_pdf):
    d = parse_overview_pdf(harumi_pdf)
    for field in HARUMI_EMPTY:
        actual = d.get(field)
        assert actual in (None, ''), f'{field}: expected empty, got {actual!r}'


# --- B. schema compatibility：舊 parser 有嘅 key，新 parser 必須全部有 ---
def _schema_report(pdf):
    new_keys = set(parse_overview_pdf(pdf).keys())
    missing = OLD_PARSER_KEYS - new_keys      # 舊有、新冇 → 會整傷 downstream
    extra = new_keys - OLD_PARSER_KEYS        # 新有、舊冇 → 新增，唔會整傷
    return new_keys, missing, extra


def test_schema_compat_nihonbashi(nihonbashi_pdf):
    new_keys, missing, extra = _schema_report(nihonbashi_pdf)
    print(f'\nold_keys={len(OLD_PARSER_KEYS)} new_keys={len(new_keys)}')
    print(f'missing={sorted(missing)}')
    print(f'extra={sorted(extra)}')
    assert not missing, f'missing keys (downstream 會整傷): {sorted(missing)}'

def test_schema_compat_harumi(harumi_pdf):
    new_keys, missing, extra = _schema_report(harumi_pdf)
    print(f'\nold_keys={len(OLD_PARSER_KEYS)} new_keys={len(new_keys)}')
    print(f'missing={sorted(missing)}')
    print(f'extra={sorted(extra)}')
    assert not missing, f'missing keys (downstream 會整傷): {sorted(missing)}'
