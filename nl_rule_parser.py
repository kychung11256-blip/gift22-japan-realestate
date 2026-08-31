# -*- coding: utf-8 -*-
"""
Rule-based natural-language intent parser → raw SearchPlan dict。

只係 deterministic 關鍵字/數字/地名抽取，處理簡單常見 query。
唔識 / 太複雜 → 返 None（俾上層 fallback 去 LLM）。

原則：
- 數字/地名/戶型呢啲明確嘅先落 hard_filters。
- 「新啲」「平啲」「景觀好」呢啲模糊嘅 → soft_preferences，唔會變 hard cutoff。
- 唔會自己發明 filter。
"""
import re

# 都道府県（日本全 47）
_PREFS = [
    '北海道', '青森県', '岩手県', '宮城県', '秋田県', '山形県', '福島県',
    '茨城県', '栃木県', '群馬県', '埼玉県', '千葉県', '東京都', '神奈川県',
    '新潟県', '富山県', '石川県', '福井県', '山梨県', '長野県', '岐阜県',
    '静岡県', '愛知県', '三重県', '滋賀県', '京都府', '大阪府', '兵庫県',
    '奈良県', '和歌山県', '鳥取県', '島根県', '岡山県', '広島県', '山口県',
    '徳島県', '香川県', '愛媛県', '高知県', '福岡県', '佐賀県', '長崎県',
    '熊本県', '大分県', '宮崎県', '鹿児島県', '沖縄県',
]

# 東京23区（常見搜尋）
_TOKYO_WARDS = [
    '千代田区', '中央区', '港区', '新宿区', '文京区', '台東区', '墨田区',
    '江東区', '品川区', '目黒区', '大田区', '世田谷区', '渋谷区', '中野区',
    '杉並区', '豊島区', '北区', '荒川区', '板橋区', '練馬区', '足立区',
    '葛飾区', '江戸川区',
]

# 戶型關鍵字 → REINS layout_type
_LAYOUT_MAP = {
    'ワンルーム': 'ワンルーム', '1R': 'ワンルーム', 'one room': 'ワンルーム',
    'Ｋ': 'Ｋ', 'K': 'Ｋ', '1K': 'Ｋ',
    'ＤＫ': 'ＤＫ', 'DK': 'ＤＫ', '1DK': 'ＤＫ',
    'ＬＫ': 'ＬＫ', 'LK': 'ＬＫ',
    'ＬＤＫ': 'ＬＤＫ', 'LDK': 'ＬＤＫ',
    '1LDK': 'ＬＤＫ', '2LDK': 'ＬＤＫ', '3LDK': 'ＬＤＫ', '4LDK': 'ＬＤＫ',
    'ＳＬＤＫ': 'ＳＬＤＫ', 'SLDK': 'ＳＬＤＫ',
}

# 方向關鍵字 → REINS orientation
_ORIENTATION_MAP = {
    '北': '北', '北東': '北東', '東': '東', '南東': '南東',
    '南': '南', '南西': '南西', '西': '西', '北西': '北西',
    '向南': '南', '朝南': '南', '向東': '東', '朝東': '東',
    '南向': '南', '東向': '東',
}

# 模糊偏好關鍵字 → soft_preferences（唔變 hard cutoff）
_SOFT_KEYWORDS = [
    '新啲', '新净', '新一點', '較新', '新しい', '新築感',
    '平', '平啲', '便宜', '安い', 'お手頃',
    '景觀', '景觀好', '開揚', '眺望', '見晴らし',
    '安靜', '靜', '閑静', '静か',
    '高級', '高級感', '豪華', 'ブランド', 'タワー',
    '方便', '便利', '生活便利', '買い物',
    '採光', '光猛', '日当たり', '採光好',
    '投資', '投資向き', '收租', '利回り', '收益率',
    '管理', '管理質素', '管理が行き届いている',
    '家庭', '子育て', '育兒', 'ファミリー',
    '大', '大啲', '広い', '宽敞',
]


def _extract_numbers(text):
    """抽所有數字（含小數）。"""
    return [float(m) for m in re.findall(r'\d+(?:\.\d+)?', text)]


def parse_rule_based(query):
    """
    嘗試用 rule 解析 query。
    成功 → raw plan dict（未 validate）。
    太簡短 / 完全唔識 → None（俾上層 fallback LLM）。
    """
    if not query or not query.strip():
        return None
    q = query.strip()
    plan_hf = {}
    soft = []
    matched_any = False

    # ── 都道府県 ──
    for p in _PREFS:
        if p in q:
            plan_hf['pref'] = p
            matched_any = True
            break

    # ── 市区町村（東京23区 / 一般「XX区」「XX市」）──
    for w in _TOKYO_WARDS:
        if w in q:
            plan_hf['city'] = w
            if 'pref' not in plan_hf:
                plan_hf['pref'] = '東京都'
            matched_any = True
            break

    # ── 戶型（LDK 等）──
    layouts = []
    # 先抽「NLDK / NDK / NK / N R」pattern
    for m in re.finditer(r'(\d+)\s*(LDK|ＬＤＫ|DK|ＤＫ|K|Ｋ|R)', q, re.IGNORECASE):
        n = int(m.group(1))
        typ = m.group(2).upper().replace('Ｌ', 'L').replace('Ｄ', 'D').replace('Ｋ', 'K')
        if typ == 'R':
            layouts.append('ワンルーム')
        else:
            layouts.append('ＬＤＫ' if 'L' in typ else ('ＤＫ' if 'D' in typ else 'Ｋ'))
        plan_hf['room_count_min'] = n
        matched_any = True
    # 抽房數如果係「N房」「N室」
    m = re.search(r'(\d+)\s*[房室]', q)
    if m:
        plan_hf['room_count_min'] = int(m.group(1))
        matched_any = True
    if layouts:
        # 去重
        plan_hf['layout_type'] = sorted(set(layouts))
        matched_any = True

    # ── 價格 ──
    # 支援「万」同「億」（1億 = 10000万）。「X万円以下」「X億以下」「X-X万」
    m = re.search(r'(\d+(?:\.\d+)?)\s*[～~\-至到]\s*(\d+(?:\.\d+)?)\s*万', q)
    if m:
        plan_hf['price_min'] = float(m.group(1))
        plan_hf['price_max'] = float(m.group(2))
        matched_any = True
    else:
        # 億（優先於万，因為「1億」都含「1」但單位唔同）
        m = re.search(r'(\d+(?:\.\d+)?)\s*億円?\s*以下', q)
        if m:
            plan_hf['price_max'] = float(m.group(1)) * 10000
            matched_any = True
        else:
            m = re.search(r'(\d+(?:\.\d+)?)\s*億円?\s*以上', q)
            if m:
                plan_hf['price_min'] = float(m.group(1)) * 10000
                matched_any = True
            else:
                m = re.search(r'(\d+(?:\.\d+)?)\s*万円?\s*以下', q)
                if m:
                    plan_hf['price_max'] = float(m.group(1))
                    matched_any = True
                else:
                    m = re.search(r'(\d+(?:\.\d+)?)\s*万円?\s*以上', q)
                    if m:
                        plan_hf['price_min'] = float(m.group(1))
                        matched_any = True

    # ── 面積 ──
    m = re.search(r'(\d+(?:\.\d+)?)\s*[～~\-至到]\s*(\d+(?:\.\d+)?)\s*(?:㎡|平米|平方米?)', q)
    if m:
        plan_hf['area_min'] = float(m.group(1))
        plan_hf['area_max'] = float(m.group(2))
        matched_any = True
    else:
        m = re.search(r'(\d+(?:\.\d+)?)\s*(?:㎡|平米|平方米?)\s*以上', q)
        if m:
            plan_hf['area_min'] = float(m.group(1))
            matched_any = True

    # ── 徒歩 ──
    m = re.search(r'徒歩\s*(\d+)\s*分\s*以内', q)
    if m:
        plan_hf['walk_min'] = int(m.group(1))
        matched_any = True
    else:
        m = re.search(r'(\d+)\s*分\s*以内', q)
        if m and '徒歩' in q:
            plan_hf['walk_min'] = int(m.group(1))
            matched_any = True

    # ── 方向 ──
    # 方向關鍵字要配合「向/朝」先算（例如「向南」「朝南」「東向」），
    # 唔好淨係睇「東」字（會誤抽「東京都」「関東」等）。
    for kw, val in _ORIENTATION_MAP.items():
        # 只接受「X向」「X朝」「向X」「朝X」嘅明確方向寫法
        if (kw + '向' in q) or (kw + '朝' in q) or ('向' + kw in q) or ('朝' + kw in q):
            plan_hf['orientation'] = [val]
            matched_any = True
            break

    # ── 築年 ──
    m = re.search(r'築\s*(\d+)\s*年\s*以内', q)
    if m:
        # 「築N年以内」係明確 hard filter（用戶明講）→ built_year_from
        import datetime
        yr = datetime.datetime.now().year - int(m.group(1))
        plan_hf['built_year_from'] = yr
        matched_any = True

    # ── 物件種別 ──
    if 'マンション' in q or '公寓' in q or '住宅大廈' in q:
        plan_hf['property_type'] = '売マンション'
        matched_any = True
    elif '戸建' in q or '一戸建' in q or '獨立屋' in q:
        plan_hf['property_type'] = '売一戸建'
        matched_any = True
    elif '土地' in q:
        plan_hf['property_type'] = '売土地'
        matched_any = True

    # ── 有圖 ──
    if '有圖' in q or '有図' in q or '図面あり' in q or '帶圖' in q:
        plan_hf['has_drawing'] = True
        matched_any = True

    # ── 模糊偏好 → soft_preferences ──
    for kw in _SOFT_KEYWORDS:
        if kw in q:
            soft.append(kw)
            matched_any = True  # 有偏好都係有內容

    # ── 咩都冇抽到 → 俾上層 fallback LLM ──
    if not matched_any:
        return None

    return {
        'hard_filters': plan_hf,
        'soft_preferences': sorted(set(soft)),
        'post_filters': {},
        'unsupported_preferences': [],
        'clarification_needed': [],
        'ranking': {},
    }
