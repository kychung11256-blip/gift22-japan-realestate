# -*- coding: utf-8 -*-
"""
NL → REINS Search orchestrator（hybrid）。

流程：
User query
→ rule-based parse（nl_rule_parser）
→ 太簡短/唔識 → LLM fallback（nl_llm_parser）
→ search_plan.validate_plan（deterministic whitelist）
→ map hard_filters 到 search_properties 支援嘅欄位
→ REINS search
→ post_filter + ranking
→ explanation

LLM / parser 唔直接操作 browser，唔自創 filter。
"""
from search_plan import validate_plan, plan_summary
from nl_rule_parser import parse_rule_based
from nl_llm_parser import parse_llm


def build_search_plan(query):
    """
    Hybrid parse query → validated SearchPlan。
    返回 (plan, meta)。meta 記低用咗邊個 parser、有冇 fallback。
    """
    meta = {'parser': None, 'fallback': False, 'raw_ok': False}

    # 1) rule-based 先
    raw = parse_rule_based(query)
    if raw is not None:
        meta['parser'] = 'rule'
        meta['raw_ok'] = True
    else:
        # 2) LLM fallback
        raw = parse_llm(query)
        if raw is not None:
            meta['parser'] = 'llm'
            meta['fallback'] = True
            meta['raw_ok'] = True
        else:
            meta['parser'] = 'none'

    if raw is None:
        # 兩個都唔識 → 空 plan + clarification
        plan = validate_plan({})
        plan['clarification_needed'].append(
            '我唔太明白你嘅需求。可唔可以講詳細啲？例如：邊個區、預算幾多、想要幾多房、面積要求。'
        )
        return plan, meta

    # 3) deterministic validate（呢步一定行，唔理係 rule 定 LLM 出嘅）
    plan = validate_plan(raw)
    return plan, meta


# search_properties 而家真正支援嘅 hard filter 欄位（REINS 表單層）。
# 其他 validate 過嘅 hard filter 會保留喺 plan，但執行時只用支援嘅（避免 call 唔存在嘅表單欄位）。
_EXECUTOR_SUPPORTED = {
    'pref', 'city', 'property_type', 'price_min', 'price_max',
    'area_min', 'area_max', 'layout_type', 'orientation', 'walk_min',
    'has_drawing', 'building_name', 'station', 'line',
}


def plan_to_reins_filters(plan, page=1):
    """
    將 validated plan 嘅 hard_filters map 到 search_properties 嘅 filters dict。
    只用 executor 支援嘅欄位；其餘 hard filter 留喺 plan（之後擴充 executor）。
    """
    hf = plan.get('hard_filters', {})
    filters = {'page': page}
    for key in _EXECUTOR_SUPPORTED:
        if key in hf and hf[key] not in (None, '', []):
            filters[key] = hf[key]
    # property_type 預設 売マンション（search_properties 都係咁）
    if 'property_type' not in filters:
        filters['property_type'] = '売マンション'
    return filters


def run_search(query, page=1, headless=True):
    """
    完整 pipeline：query → plan → REINS search → 回傳結果 + plan + explanation。
    """
    from reins_client import search_properties

    plan, meta = build_search_plan(query)
    filters = plan_to_reins_filters(plan, page=page)

    # 如果冇任何可用 hard filter 而且冇地點/駅/建物名，直接返 clarification（唔好白費一次 browser search）
    if not any(filters.get(k) for k in ('pref', 'city', 'station', 'line', 'building_name')):
        return {
            'code': 0,
            'need_clarification': True,
            'plan': plan,
            'summary': plan_summary(plan),
            'error': '需要至少一個地點條件（都道府県 / 市区町村 / 駅 / 沿線 / 建物名）先可以搜尋',
            'listings': [],
        }

    result = search_properties(filters, headless=headless)
    if result.get('code') != 1:
        return {
            'code': 0,
            'plan': plan,
            'summary': plan_summary(plan),
            'error': result.get('error', 'REINS 搜尋失敗'),
            'auth': result.get('auth', False),
            'listings': [],
        }

    listings = result.get('results', [])
    ranked = _post_filter_and_rank(listings, plan)

    return {
        'code': 1,
        'plan': plan,
        'meta': meta,
        'summary': plan_summary(plan),
        'found': result.get('total_count', 0),
        'page': result.get('page', 1),
        'total_pages': result.get('total_pages', 1),
        'hit_limit': result.get('hit_limit', False),
        'listings': ranked,
    }


def _post_filter_and_rank(listings, plan):
    """
    Post-filter（REINS result row 有嘅資料）+ ranking。
    而家 result row 欄位有限（price/area/layout/floor/walk_min/line/station），
    soft preference 唔會變 hard cutoff——只做排序提示，唔會 filter out。
    """
    pf = plan.get('post_filters', {})
    out = []
    for l in listings:
        # post_filter：數字範圍（如果 result row 有呢個欄）
        keep = True
        for key, spec in pf.items():
            val = l.get(key)
            if val is None:
                continue  # result row 冇呢個欄 → 唔 filter（留返 overview 階段先處理）
            if isinstance(spec, dict):
                if 'min' in spec and val < spec['min']:
                    keep = False
                    break
                if 'max' in spec and val > spec['max']:
                    keep = False
                    break
        if keep:
            out.append(l)

    # ranking：soft preference 唔參與淘汰，只係之後 ranking 層嘅 signal。
    # 而家 result row 冇足夠資料做真正 preference ranking，保持 REINS 原順序，
    # 並附上 soft_preferences 俾 UI 顯示「呢啲係你嘅偏好，會喺詳情階段再評估」。
    return out
