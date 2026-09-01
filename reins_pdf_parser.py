# -*- coding: utf-8 -*-
"""
REINS 概要 PDF coordinate-based parser.

設計原則（對應任務要求）：
- 唔 flatten 成 raw_text；唔用「label → 下一行」推斷欄位關係。
- 用 PyMuPDF span 嘅 (x0, y0) 做版面定位。
- REINS 概要 PDF 係固定雙欄版面：
    左欄 label x≈25.4, 左欄 value x≈127–216
    右欄 label x≈306.0, 右欄 value x≈408–497
- 每個欄位用「label 文字 + 佢嘅 y 行」做錨，再喺同一 y 行（±Y_TOL）
  收集 x 落喺該欄 value 區間嘅 span，砌成 value。
- regex 只用於：數字正規化、金額去逗號、去單位（万円/円/㎡/階/戸）。
  唔用 regex 判斷欄位歸屬。
"""
import re
import pymupdf

# ---- 版面常數（由實際 PDF span dump 歸納） ----
LABEL_X_LEFT = 25.4      # 左欄 label x0
LABEL_X_RIGHT = 306.0    # 右欄 label x0
# 左欄 value 大致 x 範圍 / 右欄 value 大致 x 範圍
LEFT_VALUE_X_MIN = 100.0
LEFT_VALUE_X_MAX = 305.0
RIGHT_VALUE_X_MIN = 380.0
RIGHT_VALUE_X_MAX = 520.0
# 同一行判定嘅 y 容差（pt）
Y_TOL = 2.0


def _spans(page):
    """抽出所有非空 span，回傳 list[dict]，按 (y, x) 排序。"""
    out = []
    for b in page.get_text('dict')['blocks']:
        for ln in b.get('lines', []):
            for s in ln.get('spans', []):
                t = s['text'].strip()
                if not t:
                    continue
                x0, y0, x1, y1 = s['bbox']
                out.append({'text': t, 'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1})
    out.sort(key=lambda s: (round(s['y0'], 1), s['x0']))
    return out


def _find_label_y(spans, label):
    """搵 label span 嘅 y0。label 可能出現多於一次，取第一個。"""
    for s in spans:
        if s['text'] == label:
            return s['y0']
    return None


def _value_on_row(spans, y, x_min, x_max):
    """收集同一行（y±Y_TOL）且 x0 落喺 [x_min, x_max] 嘅 span，按 x 砌成字串。"""
    if y is None:
        return ''
    parts = [
        s for s in spans
        if abs(s['y0'] - y) <= Y_TOL and x_min <= s['x0'] <= x_max
    ]
    parts.sort(key=lambda s: s['x0'])
    return ''.join(p['text'] for p in parts).strip()


def _left_value(spans, label):
    y = _find_label_y(spans, label)
    return _value_on_row(spans, y, LEFT_VALUE_X_MIN, LEFT_VALUE_X_MAX)


def _right_value(spans, label):
    y = _find_label_y(spans, label)
    return _value_on_row(spans, y, RIGHT_VALUE_X_MIN, RIGHT_VALUE_X_MAX)


# ---- regex 只用作單位/數字正規化，唔用作欄位定位 ----

def _num(s):
    """由字串抽第一個數字（去逗號）。冇 → None。"""
    if not s:
        return None
    m = re.search(r'[\d,]+(?:\.\d+)?', s)
    if not m:
        return None
    return m.group(0).replace(',', '')


def _to_int(s):
    n = _num(s)
    return int(float(n)) if n is not None else None


def _to_float(s):
    n = _num(s)
    return float(n) if n is not None else None


def _strip_unit(s, units):
    """移除尾隨單位（万円/円/㎡/階/戸/分 等）同空白。"""
    if not s:
        return ''
    out = s
    for u in units:
        out = out.replace(u, '')
    return out.strip()


def _price_to_man(s):
    """
    '1億780万円' → 10780（万円）；'9,000万円' → 9000。
    只係數字/單位正規化，唔涉及版面判斷。
    """
    if not s:
        return None
    s = s.replace(',', '').replace(' ', '')
    m = re.search(r'(\d+)億(?:(\d+)万)?', s)
    if m:
        oku = int(m.group(1))
        man = int(m.group(2) or 0)
        return oku * 10000 + man
    m = re.search(r'(\d+)\s*万', s)
    if m:
        return int(m.group(1))
    return _to_int(s)


_DIRECTIONS = ("南東", "南西", "北東", "北西", "南", "北", "東", "西")


def infer_orientation_from_text(value):
    """Read an orientation only when the surrounding label is explicit."""
    text = re.sub(r"\s+", "", str(value or ""))
    patterns = (
        r"(?:バルコニー方向|主要採光面|開口部方向|方角|朝向)[:：]?(?:朝|向)?(南東|南西|北東|北西|南|北|東|西)",
        r"(南東|南西|北東|北西|南|北|東|西)(?:向き|向|面バルコニー)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def extract_orientation_from_pdf(path, allow_ocr=True):
    """Extract labelled orientation from PDF text, then OCR image-only pages."""
    doc = pymupdf.open(path)
    try:
        for page in doc:
            direction = infer_orientation_from_text(page.get_text())
            if direction:
                return direction, "pdf_text", 0.9
        if allow_ocr:
            for page in doc:
                try:
                    textpage = page.get_textpage_ocr(language="jpn+eng", dpi=150, full=True)
                    direction = infer_orientation_from_text(page.get_text(textpage=textpage))
                    if direction:
                        return direction, "pdf_ocr", 0.75
                except Exception:
                    continue
    finally:
        doc.close()
    return "", "missing", 0.0


def parse_overview_pdf(path):
    """
    Parse REINS 概要 PDF → dict。
    只用座標對位；regex 只用於單位/數字正規化。
    """
    doc = pymupdf.open(path)
    try:
        page = doc[0]
        spans = _spans(page)
        page_text = page.get_text()
    finally:
        doc.close()

    # --- 用座標對位攞 raw value ---
    reins_id = _left_value(spans, '物件番号')
    price_raw = _left_value(spans, '価格')
    mgmt_fee_raw = _left_value(spans, '管理費')
    repair_raw = _right_value(spans, '修繕積立金')
    size_raw = _left_value(spans, '専有面積')
    address = _left_value(spans, '所在地')
    building_name = _left_value(spans, 'マンション名')
    floor_raw = _left_value(spans, '所在階')
    layout_rooms = _left_value(spans, '間取部屋数')
    layout_type = _right_value(spans, '間取タイプ')
    built_raw = _left_value(spans, '築年月')
    structure = _left_value(spans, '建物構造')
    floors_above_raw = _right_value(spans, '地上階層')
    total_units_raw = _left_value(spans, '棟総戸数')
    underground_raw = _right_value(spans, '地下階層')
    orientation = _left_value(spans, 'バルコニー方向') or infer_orientation_from_text(page_text)
    balcony_raw = _right_value(spans, 'バルコニー面積')

    # --- schema 相容欄位（舊 parse_overview_pdf 有回傳嘅） ---
    property_type = _right_value(spans, '物件種目')
    line = _left_value(spans, '沿線名')
    station = _right_value(spans, '最寄駅')
    land_rights = _right_value(spans, '土地権利')
    use_district = _left_value(spans, '用途地域')
    current_status = _left_value(spans, '現況')
    handover_timing = _left_value(spans, '引渡時期')
    transaction_type = _left_value(spans, '取引態様')
    management_company = _left_value(spans, '管理会社名')
    management_type = _right_value(spans, '管理形態')
    parking = _left_value(spans, '駐車場')
    registration_date = _left_value(spans, '登録年月日')
    # 舊係 最新変更年月日 or 最新更新年月日。實際 PDF：「最新変更年月日」屬右欄 label
    # 但佢嘅 value 喺 L 欄「最新更新年月日」嗰行。兩個都試，取有值嗰個。
    latest_update_date = (
        _right_value(spans, '最新変更年月日')
        or _left_value(spans, '最新更新年月日')
        or _left_value(spans, '最新変更年月日')
    )

    # walk_min：最寄駅行下面嗰行（バス/分/歩 X 分 行），攞「歩」之後、
    # 「分」之前嘅數字。固定子位置：歩@~373，數字@~385-400，分@~401。
    walk_min = _walk_minutes(spans)

    # notes_freetext：備考 係一個多行區塊。由「備考」label 行開始，
    # 到頁尾 legend（間取タイプ、詳細間取にS…）之前，抽左 value 欄所有行。
    notes_freetext = _notes_block(spans)

    # registration_no：REINS 習慣 — 冇其他備考時，備考欄會填登録No.
    # （全形英數，通常 8 位，例如 Ｆ２ＦＢＱＡ０Ａ）。
    # 如果備考內容符合登録No. 格式，就同時抽出做 registration_no；
    # 否則 registration_no 係 ''。notes_freetext 保持如實讀取備考欄。
    registration_no = _extract_registration_no(notes_freetext)

    # --- 正規化（只係數字/單位處理） ---
    room_layout = (layout_rooms + layout_type).strip() or _left_value(spans, '詳細間取')

    return {
        'reins_id': reins_id,
        'price': _price_to_man(price_raw),
        'property_type': property_type,
        'address': address,
        'building_name': building_name,
        'line': line,
        'station': station,
        'walk_min': walk_min,
        'room_layout': room_layout,
        'size_sqm': _to_float(_strip_unit(size_raw, ['㎡'])),
        'built_date': built_raw,            # 新名
        'built_date_full': built_raw,       # 舊 schema 名（相容）
        'structure': structure,
        'floor': _to_int(_strip_unit(floor_raw, ['階'])),
        'floors_above': _to_int(_strip_unit(floors_above_raw, ['階'])),
        'underground_floors': _to_int(_strip_unit(underground_raw, ['階'])),
        'orientation': orientation,
        'balcony_sqm': _to_float(_strip_unit(balcony_raw, ['㎡'])),
        'total_units': _to_int(_strip_unit(total_units_raw, ['戸'])),
        'land_rights': land_rights,
        'use_district': use_district,
        'current_status': current_status,
        'handover_timing': handover_timing,
        'transaction_type': transaction_type,
        'mgmt_fee': _to_int(_strip_unit(mgmt_fee_raw, ['円', ','])),
        'repair_reserve': _to_int(_strip_unit(repair_raw, ['円', ','])),
        'management_company': management_company,
        'management_type': management_type,
        'parking': parking,
        'registration_date': registration_date,
        'latest_update_date': latest_update_date,
        'notes_freetext': notes_freetext,
        'registration_no': registration_no,
    }


def _extract_registration_no(notes):
    """由備考文字抽登録No.。REINS 登録No. 係全形英數（Ａ-Ｚ０-９），通常 6-10 位。
    只有當備考「成段就係一個登録No.」（冇其他文字）先抽出，避免誤抽真備考入面嘅代碼。
    純格式正規化，唔係版面判斷。"""
    if not notes:
        return ''
    s = notes.strip()
    # 全形英數 → 半形
    half = s.translate(str.maketrans(
        'ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ０１２３４５６７８９',
        'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'))
    # 成段係純英數且長度 6-10 → 當係登録No.
    if re.fullmatch(r'[A-Z0-9]{6,10}', half):
        return half
    return ''


def _walk_minutes(spans):
    """最寄駅行下面嗰行有「… 歩 X 分 …」結構，X 係徒歩分鐘。
    攞最寄駅 label 下一行（y ≈ 最寄駅 y + 12~13）、x 落喺 380–401 嘅純數字。
    parse 唔到 → None。唔用 regex 判斷版面，只用座標。"""
    ek_y = _find_label_y(spans, '最寄駅')
    if ek_y is None:
        return None
    # 徒歩行通常喺最寄駅行之下 ~12.8pt
    target_y = ek_y + 12.8
    for s in spans:
        if abs(s['y0'] - target_y) <= Y_TOL and 380.0 <= s['x0'] <= 401.0:
            n = _to_int(s['text'])
            if n is not None:
                return n
    return None


# 頁尾 legend 行嘅標記文字（唔係備考內容）
_LEGEND_MARKER = '間取タイプ、詳細間取にSが含まれる場合'


def _notes_block(spans):
    """備考區塊：由「備考」label 行起，到頁尾 legend 行之前，
    收集 x 落喺左 value 欄（>= LEFT_VALUE_X_MIN）嘅所有 span，按 (y,x) 砌返。
    保留原文順序；多行用 newline 連接。唔用全文 regex。"""
    notes_y = _find_label_y(spans, '備考')
    if notes_y is None:
        return ''
    # legend 行 y（如果存在）
    legend_y = None
    for s in spans:
        if _LEGEND_MARKER in s['text']:
            legend_y = s['y0']
            break
    upper = legend_y if legend_y is not None else notes_y + 60.0
    rows = {}
    for s in spans:
        if notes_y - Y_TOL <= s['y0'] < upper - Y_TOL and s['x0'] >= LEFT_VALUE_X_MIN:
            # 唔好收 label 本身（label x0 ~25.4，已經被 >= LEFT_VALUE_X_MIN 過濾）
            rows.setdefault(round(s['y0'], 1), []).append(s)
    lines = []
    for y in sorted(rows):
        parts = sorted(rows[y], key=lambda s: s['x0'])
        line_txt = ''.join(p['text'] for p in parts).strip()
        if line_txt:
            lines.append(line_txt)
    return '\n'.join(lines)
