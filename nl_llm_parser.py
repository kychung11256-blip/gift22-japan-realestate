# -*- coding: utf-8 -*-
"""
LLM fallback parser for complex NL queries → raw SearchPlan dict.

- 用 platform .env 嘅 CIYUAN_API_KEY（OpenAI-compatible chat completions）。
- LLM 只係 parse intent 出 strict JSON，唔直接操作 browser、唔自創 filter。
- 輸出一定經 search_plan.validate_plan() 先至去 REINS。
- LLM 失敗 / 冇 key → 返 None（上層處理）。
"""
import os
import json
import urllib.request
import urllib.error

_CIYUAN_BASE = os.environ.get('CIYUAN_BASE_URL', 'https://api.ciyuan-market.com/api/v1')
_CIYUAN_KEY = os.environ.get('CIYUAN_API_KEY', '')
# 用一個快嘅 model 做 intent parse（deepseek-v4-pro TTFT ~11s，太耐；優先 qwen）
_MODEL = os.environ.get('NL_PARSE_MODEL', 'qwen3.6-plus')

_SYSTEM = """你係一個日本不動產搜尋意圖解析器。將用戶自然語言查詢轉換成嚴格嘅 SearchPlan JSON。

規則：
1. 只輸出 JSON，唔好有任何其他文字、markdown、解釋。
2. hard_filters 只可以用以下 key（值要符合型別）：
   - pref (text, 都道府県 例如 東京都)
   - city (text, 市区町村 例如 中央区)
   - building_name (text)
   - line (text, 沿線名), station (text, 駅名)
   - walk_min (number, 駅から徒歩 分)
   - price_min, price_max (number, 万円)
   - area_min, area_max (number, ㎡)
   - room_count_min, room_count_max (number, 室)
   - layout_type (array, 值喺 [ワンルーム,Ｋ,ＤＫ,ＬＫ,ＬＤＫ,ＳＫ,ＳＤＫ,ＳＬＫ,ＳＬＤＫ])
   - corner_room (bool)
   - floor_min, floor_max (number, 階)
   - orientation (array, 值喺 [北,北東,東,南東,南,南西,西,北西])
   - property_type (值喺 [売土地,売一戸建,売マンション,売外全(住宅以外建物全部),売外一(住宅以外建物一部)])
   - new_or_used (值喺 [指定なし,新築,中古])
   - land_rights (值喺 [指定なし,所有権のみ,借地権のみ])
   - has_drawing (bool), has_image (bool)
   - city_planning (值喺 [市街,調整,非線引き,域外,準都市])
   - use_district (text)
   - owner_change (值喺 [オーナーチェンジのみ,オーナーチェンジを除く])
   - parking (值喺 [有／空有,無／空無,近隣確保])
   - built_year_from, built_year_to (number, 西曆年 1926-2028)
   - equipment_text, notes_text, neighborhood_text (text, 部分一致)
3. 模糊嘅偏好（例如「新啲」「平啲」「景觀好」「安靜」「高級感」「適合投資」「管理質素好」「生活便利」「採光好」「適合家庭」）→ 放入 soft_preferences（array of string），**唔好**變成 hard_filters 嘅數字 cutoff。
4. 明確嘅數字條件（例如「5000万円以下」「60㎡以上」「2LDK」「徒歩10分以内」「築15年以内」）→ 先落 hard_filters。
5. REINS 做唔到嘅（例如「景觀」「安靜」嘅主觀評價）→ 放 soft_preferences 或 unsupported_preferences。
6. 如果資訊唔足以決定關鍵 hard filter（例如冇講邊區），放一句落 clarification_needed。
7. 輸出格式：
{"hard_filters":{},"soft_preferences":[],"post_filters":{},"unsupported_preferences":[],"clarification_needed":[],"ranking":{}}
"""


def parse_llm(query, timeout=45):
    """
    Call LLM parse query → raw plan dict（未 validate）。
    失敗 / 冇 key / 唔係 valid JSON → None。
    """
    if not _CIYUAN_KEY:
        return None
    url = _CIYUAN_BASE.rstrip('/') + '/chat/completions'
    body = {
        'model': _MODEL,
        'messages': [
            {'role': 'system', 'content': _SYSTEM},
            {'role': 'user', 'content': query},
        ],
        'temperature': 0,
        'max_tokens': 800,
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {_CIYUAN_KEY}',
                'Content-Type': 'application/json',
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        content = data['choices'][0]['message']['content']
        return _extract_json(content)
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, Exception):
        return None


def _extract_json(text):
    """由 LLM output 抽第一個 JSON object（容忍 ```json fence 同前後文字）。"""
    if not text:
        return None
    # 去 markdown fence
    t = text.strip()
    if '```' in t:
        # 攞 ``` 之間嘅內容
        parts = t.split('```')
        for p in parts:
            p = p.strip()
            if p.startswith('json'):
                p = p[4:].strip()
            if p.startswith('{'):
                t = p
                break
    # 搵第一個 { 到最後一個 }
    start = t.find('{')
    end = t.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None
