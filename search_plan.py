# -*- coding: utf-8 -*-
"""
SearchPlan JSON schema + deterministic validator.

核心原則：
- SearchPlan 係 LLM / rule-based parser 嘅 output，同 REINS 表單之間嘅中間層。
- Validator 係 deterministic：只接受 reins_search_capabilities.json 入面嘅 hard_filter。
- LLM / parser 唔可以自創 filter、唔可以將 soft preference 變 hard cutoff。
- soft_preferences / unsupported_preferences / clarification_needed 唔會去 REINS。

SearchPlan JSON schema:
{
  "hard_filters":     { <johnny_key>: <value>, ... },   # 直接去 REINS 表單
  "soft_preferences": [ "<自由文字>", ... ],            # ranking 用，唔係 hard cutoff
  "post_filters":     { <key>: <spec>, ... },           # REINS 冇表單但 result/PDF 有資料
  "unsupported_preferences": [ "<自由文字>", ... ],     # REINS 完全做唔到
  "clarification_needed":   [ "<問題>", ... ],          # parser 唔確定，要問返用戶
  "ranking":          { "<signal>": <weight>, ... }     # post-filter 後點排（optional）
}
"""
import json
import os

_CAP_PATH = os.path.join(os.path.dirname(__file__), 'reins_search_capabilities.json')
_cap_cache = None


def _load_capabilities():
    global _cap_cache
    if _cap_cache is None:
        with open(_CAP_PATH, encoding='utf-8') as f:
            _cap_cache = json.load(f)
    return _cap_cache


# hard_filters 入面每個 johnny_key 嘅允許值/型別。
# 呢個係 deterministic whitelist——LLM output 嘅 key/value 唔喺度就 reject/降級。
# type: 'text' | 'range' | 'select' | 'multi_select' | 'bool' | 'number'
_HARD_FILTER_SPECS = {
    'target':           {'type': 'select', 'values': ['在庫', '成約']},
    'property_type':    {'type': 'select', 'values': ['売土地', '売一戸建', '売マンション', '売外全(住宅以外建物全部)', '売外一(住宅以外建物一部)']},
    'new_or_used':      {'type': 'select', 'values': ['指定なし', '新築', '中古']},
    'land_rights':      {'type': 'select', 'values': ['指定なし', '所有権のみ', '借地権のみ']},
    'has_drawing':      {'type': 'bool'},
    'has_image':        {'type': 'bool'},
    'transaction_status': {'type': 'select', 'values': ['指定なし', '公開中のみ', '書面による購入申し込みありのみ', '売主都合で一時紹介停止中のみ']},
    'pref':             {'type': 'text'},
    'city':             {'type': 'text'},
    'building_name':    {'type': 'text'},
    'line':             {'type': 'text'},
    'station':          {'type': 'text'},
    'walk_min':         {'type': 'number', 'min': 0, 'max': 120},          # 駅から徒歩（分）
    'price_min':        {'type': 'number', 'min': 0},                       # 万円
    'price_max':        {'type': 'number', 'min': 0},                       # 万円
    'area_min':         {'type': 'number', 'min': 0},                       # ㎡（専有面積）
    'area_max':         {'type': 'number', 'min': 0},                       # ㎡
    'room_count_min':   {'type': 'number', 'min': 0, 'max': 20},            # 室
    'room_count_max':   {'type': 'number', 'min': 0, 'max': 20},
    'layout_type':      {'type': 'multi_select', 'values': ['ワンルーム', 'Ｋ', 'ＤＫ', 'ＬＫ', 'ＬＤＫ', 'ＳＫ', 'ＳＤＫ', 'ＳＬＫ', 'ＳＬＤＫ']},
    'corner_room':      {'type': 'bool'},
    'floor_min':        {'type': 'number', 'min': -10, 'max': 200},         # 階
    'floor_max':        {'type': 'number', 'min': -10, 'max': 200},
    'orientation':      {'type': 'multi_select', 'values': ['北', '北東', '東', '南東', '南', '南西', '西', '北西']},
    'city_planning':    {'type': 'select', 'values': ['市街', '調整', '非線引き', '域外', '準都市']},
    'use_district':     {'type': 'select', 'values': ['一低', '二中', '二住', '近商', '商業', '準工', '工業', '工専', '二低', '一中', '一住', '準住', '田園', '定めなし']},
    'owner_change':     {'type': 'select', 'values': ['オーナーチェンジのみ', 'オーナーチェンジを除く']},
    'parking':          {'type': 'select', 'values': ['有／空有', '無／空無', '近隣確保']},
    'built_year_from':  {'type': 'number', 'min': 1926, 'max': 2028},       # 築年月（西曆年）
    'built_year_to':    {'type': 'number', 'min': 1926, 'max': 2028},
    'equipment_text':   {'type': 'text'},                                    # 設備・条件 free text（部分一致）
    'notes_text':       {'type': 'text'},                                    # 備考１ free text
    'neighborhood_text': {'type': 'text'},                                   # 周辺環境 free text
}

# post_filters 入面嘅 key（REINS result/PDF 有，表單冇）。spec: {'min': x, 'max': y} 或值。
_POST_FILTER_KEYS = {
    'mgmt_fee', 'repair_reserve', 'total_units', 'floors_above',
    'underground_floors', 'balcony_sqm', 'structure', 'management_company',
    'management_type', 'yield',
}


def empty_plan():
    return {
        'hard_filters': {},
        'soft_preferences': [],
        'post_filters': {},
        'unsupported_preferences': [],
        'clarification_needed': [],
        'ranking': {},
    }


def _err(plan, msg):
    plan['clarification_needed'].append(msg)


def _validate_value(plan, key, spec, value):
    """Validate 單個 hard_filter value。成功 → 加入 plan['hard_filters']；失敗 → clarification_needed。"""
    t = spec['type']
    if t == 'bool':
        plan['hard_filters'][key] = bool(value)
        return
    if t == 'text':
        s = str(value).strip()
        if s:
            plan['hard_filters'][key] = s
        return
    if t == 'number':
        try:
            n = float(value)
        except (TypeError, ValueError):
            _err(plan, f'{key} 唔係數字：{value!r}')
            return
        if 'min' in spec and n < spec['min']:
            n = spec['min']
        if 'max' in spec and n > spec['max']:
            n = spec['max']
        plan['hard_filters'][key] = int(n) if n == int(n) else n
        return
    if t == 'select':
        if value in spec['values']:
            plan['hard_filters'][key] = value
        else:
            _err(plan, f'{key} 值 {value!r} 唔喺允許範圍：{spec["values"]}')
        return
    if t == 'multi_select':
        vals = value if isinstance(value, list) else [value]
        ok = [v for v in vals if v in spec['values']]
        bad = [v for v in vals if v not in spec['values']]
        if ok:
            plan['hard_filters'][key] = ok
        if bad:
            _err(plan, f'{key} 有唔識嘅值：{bad}（允許：{spec["values"]}）')
        return


def validate_plan(raw):
    """
    Validate + sanitize 一個 raw plan（dict）。
    - 只保留 whitelist 入面嘅 hard_filter key。
    - 唔識嘅 hard_filter key → 搬去 unsupported_preferences（唔會靜靜雞 drop，令用戶知）。
    - soft_preferences / unsupported / clarification 確保係 list of str。
    返回一個乾淨 plan（永遠唔會 throw；壞嘢落 clarification_needed）。
    """
    plan = empty_plan()
    if not isinstance(raw, dict):
        _err(plan, 'SearchPlan 唔係一個 JSON object')
        return plan

    # hard_filters
    hf = raw.get('hard_filters') or {}
    if isinstance(hf, dict):
        for key, value in hf.items():
            spec = _HARD_FILTER_SPECS.get(key)
            if not spec:
                # 唔識嘅 hard filter → 唔准去 REINS，搬去 unsupported
                plan['unsupported_preferences'].append(f'{key}={value}')
                continue
            _validate_value(plan, key, spec, value)

    # soft_preferences
    sp = raw.get('soft_preferences') or []
    if isinstance(sp, list):
        plan['soft_preferences'] = [str(x) for x in sp if str(x).strip()]

    # post_filters
    pf = raw.get('post_filters') or {}
    if isinstance(pf, dict):
        for key, spec in pf.items():
            if key in _POST_FILTER_KEYS:
                plan['post_filters'][key] = spec
            else:
                plan['unsupported_preferences'].append(f'post:{key}={spec}')

    # unsupported_preferences
    up = raw.get('unsupported_preferences') or []
    if isinstance(up, list):
        plan['unsupported_preferences'].extend(str(x) for x in up if str(x).strip())

    # clarification_needed
    cn = raw.get('clarification_needed') or []
    if isinstance(cn, list):
        plan['clarification_needed'].extend(str(x) for x in cn if str(x).strip())

    # ranking
    rk = raw.get('ranking') or {}
    if isinstance(rk, dict):
        plan['ranking'] = {str(k): v for k, v in rk.items()}

    return plan


def plan_summary(plan):
    """俾 UI / log 用嘅人讀 summary。"""
    lines = []
    hf = plan.get('hard_filters', {})
    if hf:
        lines.append('Hard filters: ' + ', '.join(f'{k}={v}' for k, v in hf.items()))
    if plan.get('soft_preferences'):
        lines.append('偏好: ' + '、'.join(plan['soft_preferences']))
    if plan.get('post_filters'):
        lines.append('Post-filter: ' + ', '.join(f'{k}={v}' for k, v in plan['post_filters'].items()))
    if plan.get('unsupported_preferences'):
        lines.append('做唔到: ' + '、'.join(plan['unsupported_preferences']))
    if plan.get('clarification_needed'):
        lines.append('要確認: ' + '；'.join(plan['clarification_needed']))
    return '\n'.join(lines) if lines else '（冇任何條件）'
