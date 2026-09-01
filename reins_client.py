"""
REINS (system.reins.jp) login bootstrap client.

- Playwright persistent session (storage_state) for member login.
- Credentials read ONLY from environment variables:
    REINS_LOGIN_URL, REINS_MEMBER_ID, REINS_PASSWORD
- Never logs / prints / raises credentials.
- If the site requires first-time manual login, 2FA, or captcha, we STOP
  and report — we do not attempt to bypass.
- Read-only smoke test: open REINS main menu, confirm 「売買物件検索」 link.
  No bulk search, no PDF download, no DB writes.
"""

import os
import json
import time

STORAGE_STATE_PATH = os.path.join(
    os.path.dirname(__file__), 'data', 'reins_storage_state.json'
)
DEFAULT_LOGIN_URL = 'https://system.reins.jp/'


class ReinsAuthError(Exception):
    """Login failed for a non-credential reason (site flow changed, blocked, etc.)."""
    pass


class ReinsManualInterventionRequired(Exception):
    """Site requires manual login / 2FA / captcha — stop and notify the user."""
    pass


def _get_credentials():
    """Read credentials from env. Raises ReinsAuthError if any are missing.
    Never includes the actual values in the error message."""
    url = os.environ.get('REINS_LOGIN_URL', '').strip() or DEFAULT_LOGIN_URL
    member_id = os.environ.get('REINS_MEMBER_ID', '').strip()
    password = os.environ.get('REINS_PASSWORD', '').strip()
    missing = [k for k, v in (
        ('REINS_MEMBER_ID', member_id), ('REINS_PASSWORD', password)
    ) if not v]
    if missing:
        raise ReinsAuthError('Missing env vars: ' + ', '.join(missing))
    return url, member_id, password


def _has_storage_state():
    return os.path.exists(STORAGE_STATE_PATH) and os.path.getsize(STORAGE_STATE_PATH) > 100


def _save_storage_state(context):
    os.makedirs(os.path.dirname(STORAGE_STATE_PATH), exist_ok=True)
    context.storage_state(path=STORAGE_STATE_PATH)
    # tighten permissions — file contains session cookies
    try:
        os.chmod(STORAGE_STATE_PATH, 0o600)
    except OSError:
        pass


def _normalize(s):
    import re as _r
    return _r.sub(r'\s+', '', s or '')


def _looks_like_login_page(page):
    """Heuristic: are we still on a login form?"""
    try:
        body = _normalize(page.content())
    except Exception:
        return False
    # 如果已經見到主選單嘅「売買物件検索」，就唔係 login 頁
    if '売買物件検索' in body:
        return False
    markers = ['ログイン', 'ユーザID', 'パスワード']
    return any(m in body for m in markers)


def _detect_manual_intervention(page):
    """Detect 2FA / captcha / first-time-manual-login screens. Return reason or None."""
    try:
        body = page.content()
    except Exception:
        return None
    body_lower = body.lower()
    if 'captcha' in body_lower or 'recaptcha' in body_lower or '画像認証' in body:
        return 'captcha'
    if '二段階認証' in body or '2段階認証' in body or '認証コード' in body or 'ワンタイム' in body:
        return '2fa'
    return None


def _find_main_menu(page):
    """After login, REINS lands on a menu page. Return True if 売買物件検索 link is present."""
    try:
        body = _normalize(page.content())
    except Exception:
        return False
    return '売買物件検索' in body


def login_and_verify(headless=True, timeout_ms=30000):
    """
    Bootstrap REINS session.

    Returns a dict (no credentials):
      {
        'login': True/False,
        'needs_manual': None | 'captcha' | '2fa',
        'storage_state_saved': bool,
        'landing_url': str,
        'has_sales_search': bool,   # 売買物件検索 visible
        'reused_session': bool,
        'error': str | None,
      }
    """
    result = {
        'login': False,
        'needs_manual': None,
        'storage_state_saved': False,
        'landing_url': '',
        'has_sales_search': False,
        'reused_session': False,
        'error': None,
    }

    try:
        login_url, member_id, password = _get_credentials()
    except ReinsAuthError as e:
        result['error'] = str(e)
        return result

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        # REINS 會擋 headless 自動化瀏覽器（503）— 要停用 automation 特徵
        browser = pw.chromium.launch(headless=headless, args=['--disable-blink-features=AutomationControlled'])
        try:
            context_kwargs = {
                'user_agent': ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                               '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'),
                'locale': 'ja-JP',
                'viewport': {'width': 1280, 'height': 800},
            }
            # Try reusing saved session first
            if _has_storage_state():
                context_kwargs['storage_state'] = STORAGE_STATE_PATH

            context = browser.new_context(**context_kwargs)
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = context.new_page()

            # ── Step 1: try existing session ──
            # storage_state 嘅 cookie 會話我哋已登入 — 但 REINS 首頁係介紹頁，
            # 要直接去 main menu 先驗證到 session 仲有冇效
            if 'storage_state' in context_kwargs:
                try:
                    page.goto(login_url, timeout=timeout_ms, wait_until='networkidle')
                    page.wait_for_timeout(2000)
                    # 直接去主選單 URL 試（REINS 會 redirect 未登入嘅 request 去 login）
                    try:
                        page.goto('https://system.reins.jp/main/KG/GKG003100',
                                  timeout=timeout_ms, wait_until='networkidle')
                        page.wait_for_timeout(2000)
                    except Exception:
                        pass
                    if not _looks_like_login_page(page):
                        result['login'] = True
                        result['reused_session'] = True
                        result['landing_url'] = page.url
                        result['has_sales_search'] = _find_main_menu(page)
                        _save_storage_state(context)
                        result['storage_state_saved'] = True
                        return result
                except Exception:
                    pass  # fall through to fresh login

            # ── Step 2: fresh login ──
            # REINS IP 首頁係介紹頁，要先撳「ログイン」連結去真正 login form
            page.goto(login_url, timeout=timeout_ms, wait_until='networkidle')
            page.wait_for_timeout(3000)

            # 如果仲係介紹頁（冇 password field），撳「ログイン」連結
            if not page.query_selector('input[type="password"]'):
                try:
                    login_link = page.query_selector('a:has-text("ログイン")')
                    if login_link:
                        login_link.click()
                        page.wait_for_load_state('networkidle', timeout=timeout_ms)
                        page.wait_for_timeout(3000)
                except Exception:
                    pass

            # Detect manual intervention before touching any field
            result['needs_manual'] = _detect_manual_intervention(page)
            if result['needs_manual']:
                result['landing_url'] = page.url
                return result

            # ── Fill login form (REINS IP uses dynamic __BVID__ ids; use input types) ──
            id_el = None
            try:
                id_el = page.query_selector('input[type="text"]:visible')
            except Exception:
                pass
            pw_el = None
            try:
                pw_el = page.query_selector('input[type="password"]:visible')
            except Exception:
                pass

            if id_el and pw_el:
                try:
                    id_el.fill(member_id)
                    pw_el.fill(password)
                    # REINS IP 要勾「所属機構の規程及びガイドラインを遵守します」先 enable login 掣
                    # （页面明文提示：「にチェックを入れてログインしてください」）
                    # Bootstrap custom-checkbox 嘅 label 覆蓋咗 input，要撳 label 先work
                    try:
                        checks = page.query_selector_all('input[type="checkbox"]')
                        for c in checks:
                            try:
                                if not c.is_checked():
                                    cid = c.get_attribute('id')
                                    label = page.query_selector(f'label[for="{cid}"]') if cid else None
                                    if label:
                                        label.click()
                                    else:
                                        c.click(force=True)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    page.wait_for_timeout(500)
                    filled = True
                except Exception as e:
                    result['error'] = f'Failed to fill login form: {type(e).__name__}'
                    result['landing_url'] = page.url
                    return result
            else:
                result['error'] = 'Login form fields not found (page structure may have changed)'
                result['landing_url'] = page.url
                return result

            # ── Submit ──
            # REINS IP 係 SPA（Vue/Bootstrap），login 係 <button type="button">ログイン</button>
            submitted = False
            for sel in ['button:has-text("ログイン")', 'button[type="submit"]',
                        'input[type="submit"]', 'input[value*="ログイン"]']:
                try:
                    btn = page.query_selector(sel)
                    if btn:
                        btn.click()
                        submitted = True
                        break
                except Exception:
                    continue
            if not submitted:
                try:
                    pw_el.press('Enter')
                    submitted = True
                except Exception:
                    pass

            if not submitted:
                result['error'] = 'Could not submit login form'
                result['landing_url'] = page.url
                return result

            # SPA 唔會即時 navigation — 等 networkidle + 額外時間畀後端驗證
            try:
                page.wait_for_load_state('networkidle', timeout=timeout_ms)
            except Exception:
                pass
            page.wait_for_timeout(5000)

            # Re-check for 2FA / captcha AFTER submit
            result['needs_manual'] = _detect_manual_intervention(page)
            result['landing_url'] = page.url
            if result['needs_manual']:
                return result

            if _looks_like_login_page(page):
                result['error'] = 'Still on login page after submit (wrong credentials or unknown flow)'
                return result

            result['login'] = True
            result['has_sales_search'] = _find_main_menu(page)
            _save_storage_state(context)
            result['storage_state_saved'] = True
            return result

        finally:
            try:
                browser.close()
            except Exception:
                pass


def _parse_price_man(s):
    """'9,000万円' → 9000 (万円 integer). Returns None if unparseable."""
    import re as _r
    m = _r.search(r'([\d,]+)\s*万円', s or '')
    if not m:
        return None
    return int(m.group(1).replace(',', ''))


def _parse_walk_min(s):
    """'徒歩　5分' → 5. Returns None if unparseable."""
    import re as _r
    m = _r.search(r'徒歩\s*(\d+)\s*分', s or '')
    return int(m.group(1)) if m else None


def _parse_floor(s):
    """'26階' → 26. Returns None if unparseable."""
    import re as _r
    m = _r.match(r'(\d+)\s*階', (s or '').strip())
    return int(m.group(1)) if m else None


def _parse_area(s):
    """'34.54㎡' → 34.54. Returns None if unparseable."""
    import re as _r
    m = _r.search(r'([\d.]+)\s*㎡', s or '')
    return float(m.group(1)) if m else None


def _parse_station_line(s):
    """'京葉線　八丁堀' → ('京葉線', '八丁堀')."""
    import re as _r
    parts = _r.split(r'[\s　]+', (s or '').strip())
    if len(parts) >= 2:
        return parts[0], parts[1]
    return (parts[0] if parts else ''), ''


def search_properties(filters, headless=True, timeout_ms=60000):
    """
    REINS 売買物件検索 — 回傳指定頁（page=N，每頁 50 件）。
    唔會逐頁全抓；每次只開用戶要求嗰一頁。

    filters: {
      'pref': '東京都',          # 都道府県名（完全一致）
      'city': '中央区',          # 所在地名１（完全一致）
      'property_type': '売マンション',  # 物件種別１（默认売マンション）
      'price_min': 3000,         # 万円
      'price_max': 15000,        # 万円
      'walk_min': 15,            # 駅徒歩（暂未接 — REINS 係沿線 section）
      'page': 1,                 # 頁碼（1-based）
    }

    Returns {'code': 1, 'results': [...], 'total_count': int, 'page': int,
             'page_size': 50, 'total_pages': int, 'hit_limit': bool}
    or {'code': 0, 'error': str}
    """
    if not _has_storage_state():
        return {'code': 0, 'error': 'No REINS session — run login_and_verify() first', 'auth': True}

    pref = (filters.get('pref') or '').strip()
    city = (filters.get('city') or '').strip()
    property_type = (filters.get('property_type') or '売マンション').strip()
    price_min = filters.get('price_min')
    price_max = filters.get('price_max')
    page_num = int(filters.get('page') or 1)
    # 擴充 hard filters（對應 reins_search_capabilities.json）
    area_min = filters.get('area_min')            # 専有面積 min
    area_max = filters.get('area_max')            # 専有面積 max
    layout_types = filters.get('layout_type') or []   # 間取タイプ multi
    if isinstance(layout_types, str):
        layout_types = [layout_types]
    orientation = filters.get('orientation')      # バルコニー方向 single
    if isinstance(orientation, list):
        orientation = orientation[0] if orientation else None
    walk_min = filters.get('walk_min')            # 駅から徒歩（分以内）
    has_drawing = filters.get('has_drawing')      # 図面ありのみ
    building_name = (filters.get('building_name') or '').strip()
    station = (filters.get('station') or '').strip()
    line = (filters.get('line') or '').strip()

    if not pref and not city and not station and not line and not building_name:
        return {'code': 0, 'error': 'pref/city/station/line/building_name 至少要一個'}

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=headless, args=['--disable-blink-features=AutomationControlled'])
        try:
            ctx = b.new_context(
                storage_state=STORAGE_STATE_PATH,
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
                locale='ja-JP', viewport={'width': 1280, 'height': 800})
            pg = ctx.new_page()

            # 主選單 → 売買物件検索
            pg.goto('https://system.reins.jp/main/KG/GKG003100', wait_until='networkidle', timeout=timeout_ms)
            pg.wait_for_timeout(2500)
            clicked = False
            for btn in pg.query_selector_all('button'):
                try:
                    import re as _r
                    if '売買物件検索' in _r.sub(r'\s+', '', btn.text_content() or ''):
                        btn.click(); clicked = True; break
                except Exception:
                    continue
            if not clicked:
                return {'code': 0, 'error': '主選單搵唔到「売買物件検索」掣'}
            pg.wait_for_load_state('networkidle', timeout=timeout_ms)
            pg.wait_for_timeout(3000)

            # 展開所有「検索条件を表示」section（詳細 filter 收埋咗）
            for btn in pg.query_selector_all('button, a'):
                try:
                    if '検索条件を表示' in (btn.text_content() or ''):
                        btn.click()
                        pg.wait_for_timeout(400)
                except Exception:
                    continue
            pg.wait_for_timeout(1500)

            def _check_custom_checkbox(pg, sel):
                """REINS Bootstrap custom checkbox：click label[for] 先 work。"""
                try:
                    el = pg.query_selector(sel)
                    if not el or el.is_checked():
                        return
                    bid = sel.lstrip('#')
                    lab = pg.query_selector(f'label[for="{bid}"]')
                    if lab:
                        lab.click()
                        pg.wait_for_timeout(200)
                    else:
                        el.click(force=True)
                        pg.wait_for_timeout(200)
                except Exception:
                    pass

            if _looks_like_login_page(pg):
                return {'code': 0, 'error': 'REINS session 已失效，需要重新 login', 'auth': True}

            # ── 填表 ──
            def _fill(sel, val):
                el = pg.query_selector(sel)
                if el:
                    el.fill(val)
                    el.dispatch_event('input')
                    el.dispatch_event('change')
                    return True
                return False

            if pref:
                _fill('#__BVID__346', pref)
            if city:
                _fill('#__BVID__350', city)
            try:
                pg.select_option('#__BVID__293', label=property_type)
            except Exception:
                pass
            if price_min is not None:
                _fill('#__BVID__477', str(price_min))
            if price_max is not None:
                _fill('#__BVID__479', str(price_max))

            # ── 擴充 hard filters ──
            # 所在地/建物名
            if building_name:
                _fill('#__BVID__363', building_name)
            # 沿線/駅（沿線１）
            if line:
                _fill('#__BVID__397', line)
            if station:
                _fill('#__BVID__401', station)
            # 駅から徒歩（分以内）— __BVID__406 係分數輸入，__BVID__408 係單位（分）
            if walk_min is not None:
                _fill('#__BVID__406', str(walk_min))
                try:
                    pg.select_option('#__BVID__408', label='分')
                except Exception:
                    pass
            # 価格已喺上面；専有面積（マンション用）
            if area_min is not None:
                _fill('#__BVID__507', str(area_min))
            if area_max is not None:
                _fill('#__BVID__509', str(area_max))
            # 間取タイプ（multi checkbox）：REINS 用 Bootstrap custom checkbox，
            # input 被 overlay 遮住，要 click label[for] 先 work（直接 check() 會 timeout）。
            _LAYOUT_SEL = {
                'ワンルーム': '#__BVID__522', 'Ｋ': '#__BVID__524', 'ＤＫ': '#__BVID__526',
                'ＬＫ': '#__BVID__528', 'ＬＤＫ': '#__BVID__530', 'ＳＫ': '#__BVID__532',
                'ＳＤＫ': '#__BVID__534', 'ＳＬＫ': '#__BVID__536', 'ＳＬＤＫ': '#__BVID__538',
            }
            for lt in layout_types:
                sel = _LAYOUT_SEL.get(lt)
                if sel:
                    _check_custom_checkbox(pg, sel)
            # バルコニー方向（single select）
            if orientation:
                try:
                    pg.select_option('#__BVID__546', label=orientation)
                except Exception:
                    pass
            # 図面ありのみ（custom checkbox，同樣 click label）
            if has_drawing:
                _check_custom_checkbox(pg, '#__BVID__323')
            pg.wait_for_timeout(500)

            # ── 撳「検索」──
            search_btn = None
            for btn in pg.query_selector_all('button'):
                try:
                    if (btn.text_content() or '').strip() == '検索' and not btn.is_disabled():
                        search_btn = btn; break
                except Exception:
                    continue
            if not search_btn:
                return {'code': 0, 'error': '搵唔到「検索」掣'}
            search_btn.click()
            pg.wait_for_timeout(3000)

            # ── 500 件確認 dialog → 撳 OK ──
            for btn in pg.query_selector_all('button'):
                try:
                    if (btn.text_content() or '').strip() == 'OK':
                        btn.click(); break
                except Exception:
                    continue
            pg.wait_for_load_state('networkidle', timeout=timeout_ms)
            pg.wait_for_timeout(6000)

            # ── 換頁（如 page > 1）：撳真實 pagination button ──
            # REINS 用 Bootstrap-Vue pagination：.p-pagination button[aria-label="Go to page N"]
            if page_num > 1:
                page_btn = pg.query_selector(
                    f'.p-pagination button[aria-label="Go to page {page_num}"]'
                )
                if not page_btn:
                    # 頁碼可能收埋喺 … 後面；先撳「下一頁」逐頁行到目標
                    # （REINS 頁碼 window 只顯示首幾頁 + …）
                    # 穩妥做法：撳 aria-label="Go to next page" 逐頁推
                    current = 1
                    while current < page_num:
                        nxt = pg.query_selector(
                            '.p-pagination [aria-label="Go to next page"]:not([aria-disabled="true"])'
                        )
                        if not nxt:
                            return {'code': 0, 'error': f'冇第 {page_num} 頁（得 {current} 頁）'}
                        nxt.click()
                        pg.wait_for_load_state('networkidle', timeout=timeout_ms)
                        pg.wait_for_timeout(4000)
                        current += 1
                else:
                    page_btn.click()
                    pg.wait_for_load_state('networkidle', timeout=timeout_ms)
                    pg.wait_for_timeout(4000)

            # ── 解析結果（指定頁）──
            # REINS 結果 row 嘅欄位數目會因應物件而變（例如冇所在階會少一行），
            # 所以唔可以用固定 index — 要用內容 pattern 逐行分類
            rows = pg.query_selector_all('.p-table-body > .p-table-body-row')
            results = []
            import re as _r
            for row in rows:
                lines = [l.strip() for l in (row.inner_text() or '').split('\n') if l.strip()]
                if len(lines) < 8:
                    continue
                item = {
                    'source': 'reins', 'reins_id': '', 'property_type': '', 'price': None,
                    'layout': '', 'area': None, 'address': '', 'building_name': '',
                    'line': '', 'station': '', 'walk_min': None, 'floor': None,
                    'built_date': '', 'transaction_type': '', 'transaction_status': '',
                    'drawing_available': False,
                }
                for l in lines:
                    if _r.match(r'^\d+$', l):
                        # No. 係細數字，物件番号係 1001 開頭 12 位
                        if len(l) >= 12:
                            item['reins_id'] = l
                        continue
                    elif 'マンション' in l or '土地' in l or '一戸建' in l or 'タウン' in l or 'リゾート' in l:
                        item['property_type'] = l
                    elif _r.search(r'万円', l) and item['price'] is None and not _r.search(r'[㎡坪]単価|管理費|修繕', l):
                        item['price'] = _parse_price_man(l)
                    elif _r.search(r'㎡', l) and item['area'] is None:
                        item['area'] = _parse_area(l)
                    elif l.startswith('東京都') or l.startswith('神奈川') or l.startswith('大阪') or '区' in l or '市' in l:
                        if not item['address'] and not _r.search(r'[0-9]万', l):
                            item['address'] = l
                    elif l in ('専任', '専属', '一般', '売主'):
                        item['transaction_type'] = l
                    elif l in ('公開中', '-') or '停止中' in l or '申し込み' in l:
                        item['transaction_status'] = l
                    elif _r.match(r'^[\d,]+円$', l):
                        pass  # 管理費 — 唔收
                    elif _r.match(r'^[\d.]+万円$', l) and item['price'] is not None:
                        pass  # ㎡単価 / 坪単価 — 唔收
                    elif '線' in l and ('　' in l or ' ' in l):
                        item['line'], item['station'] = _parse_station_line(l)
                    elif '徒歩' in l:
                        item['walk_min'] = _parse_walk_min(l)
                    elif _r.match(r'^\d+階$', l):
                        item['floor'] = _parse_floor(l)
                    elif _r.search(r'年[（(]', l):
                        item['built_date'] = l
                    elif _r.match(r'^[\d０-９ＬＤＫＳ]+[ＬＤＫ]?$', l) and not item['layout']:
                        item['layout'] = l
                    elif l not in ('概要', '詳細', '図面') and not item['building_name'] and len(l) > 2:
                        item['building_name'] = l
                item['drawing_available'] = '図面' in (row.inner_text() or '')
                results.append(item)

            # 總件數 — 由「1～50件 ／ 500件」呢類標題抽
            import re as _r
            body = pg.eval_on_selector('body', 'e => e.innerText')
            total_count = 0
            m = _r.search(r'(\d+)～(\d+)件\s*／\s*(\d+)件', body)
            if m:
                total_count = int(m.group(3))
            else:
                m2 = _r.search(r'\((\d+)\s*件\)', body)
                if m2:
                    total_count = int(m2.group(1))

            # 總頁數：pagination 嘅 aria-setsize（最後頁碼）；fallback 用 total_count/50
            page_size = 50
            total_pages = 0
            sp = pg.query_selector('.p-pagination [aria-setsize]')
            if sp:
                try:
                    total_pages = int(sp.get_attribute('aria-setsize'))
                except Exception:
                    total_pages = 0
            if not total_pages and total_count:
                total_pages = max(1, (total_count + page_size - 1) // page_size)

            # found=500 係 REINS 顯示上限，實際可能更多
            hit_limit = total_count >= 500

            return {
                'code': 1, 'results': results, 'total_count': total_count,
                'page': page_num, 'page_size': page_size, 'total_pages': total_pages,
                'hit_limit': hit_limit, 'returned': len(results),
            }

        except Exception as e:
            return {'code': 0, 'error': f'{type(e).__name__}: {str(e)[:200]}'}
        finally:
            try:
                b.close()
            except Exception:
                pass


def _pdf_label_value_map(page):
    """
    REINS 概要 PDF 係固定格式表格：每個欄位 label 同 value 係分開嘅 text span，
    擺喺唔同座標。用 y 座標 grouping：同一行入面，label 之後嘅 span 就係 value。
    Returns dict: {label_text: value_text}
    """
    import re as _r
    spans = []
    for b in page.get_text('dict')['blocks']:
        for ln in b.get('lines', []):
            for s in ln.get('spans', []):
                t = s['text'].strip()
                if t:
                    spans.append({'text': t, 'x': s['bbox'][0], 'y': s['bbox'][1]})
    # 按 y 座標分組（容差 3pt），每行內按 x 排序
    rows = {}
    for s in spans:
        y = round(s['y'] / 3) * 3
        rows.setdefault(y, []).append(s)
    result = {}
    for y in sorted(rows):
        items = sorted(rows[y], key=lambda s: s['x'])
        texts = [i['text'] for i in items]
        # label 通常係純日文欄位名（冇數字/冇萬円），value 係佢後面嘅嘢
        # 逐行掃：如果第一個係已知 label，後面就係 value
        known_labels = {
            '価格', '物件番号', '物件種別', '物件種目', '土地権利', '登録年月日',
            '最新変更年月日', '最新更新年月日', '面積計測方式', '沿線名', '最寄駅',
            '所在地', '用途地域', '現況', '引渡時期', '取引態様', '取引状況',
            '間取部屋数', '詳細間取', '間取タイプ', '築年月', '建物構造',
            '地上階層', '地下階層', '管理費', '修繕積立金', '専有面積',
            'マンション名', '所在階', '部屋番号', 'バルコニー方向', '棟総戸数',
            'バルコニー面積', '管理組合', '管理形態', '管理会社名', '管理人状況',
            '駐車場', '駐車場月額', '商号', '電話', '担当者', '連絡先', 'メール',
            '備考', '図面', '媒介契約年月日', '消費税', 'その他交通手段',
        }
        for i, t in enumerate(texts):
            if t in known_labels and i + 1 < len(texts):
                # value 係下一個（或之後幾個，如果係「万円」「円」呢類單位）
                val_parts = []
                for v in texts[i+1:]:
                    if v in known_labels:
                        break
                    val_parts.append(v)
                result[t] = ' '.join(val_parts).strip()
    return result


def parse_overview_pdf(path):
    """
    Production entry — 直接 call coordinate-based parser（reins_pdf_parser.py）。
    舊 raw_text/regex 版本保留做 _legacy_parse_overview_pdf 以便 rollback，
    但 production path 只用新 parser。
    """
    from reins_pdf_parser import parse_overview_pdf as _parse
    return _parse(path)


def _legacy_parse_overview_pdf(path):
    """
    [LEGACY — 唔好用] 舊 raw_text/regex parser，保留作 rollback 參考。
    Parse REINS 概要 PDF text layer into a normalized dict.
    唔用 OCR / vision。Parse 唔到 = None，禁止估值。
    """
    import pymupdf
    import re as _r

    doc = pymupdf.open(path)
    page = doc[0]
    raw_text = page.get_text()
    kv = _pdf_label_value_map(page)
    doc.close()

    def _int(s):
        m = _r.search(r'([\d,]+)', s or '')
        return int(m.group(1).replace(',', '')) if m else None

    def _float(s):
        m = _r.search(r'([\d.]+)', s or '')
        return float(m.group(1)) if m else None

    def _wan_yen(s):
        """'1億780万円' → 10780 (万円)；'9,000万円' → 9000"""
        s = (s or '').replace(',', '')
        m = _r.search(r'(\d+)億(?:(\d+)万)?', s)
        if m:
            oku = int(m.group(1))
            man = int(m.group(2) or 0)
            return oku * 10000 + man
        m = _r.search(r'(\d+)\s*万', s)
        return int(m.group(1)) if m else None

    # 間取：「2」+「ＬＤＫ」分開 → 砌返「2ＬＤＫ」
    layout_rooms = kv.get('間取部屋数', '')
    layout_type = kv.get('間取タイプ', '')
    room_layout = (layout_rooms + layout_type).strip() or kv.get('詳細間取', '')

    # 價格
    price_man = _wan_yen(kv.get('価格', ''))

    # 地址
    address = kv.get('所在地', '')

    # 沿線/駅
    line = kv.get('沿線名', '')
    station = kv.get('最寄駅', '')

    # 徒歩 — 「徒歩 X 分」喺 raw text（沿線名/最寄駅隔籬）
    walk_min = None
    m = _r.search(r'徒歩\s*(\d+)\s*分', raw_text)
    if m:
        walk_min = int(m.group(1))

    # 所在階
    floor = _int(kv.get('所在階', ''))

    # 面積 / 戸數 / 結構
    size_sqm = _float(kv.get('専有面積', ''))
    balcony_sqm = _float(kv.get('バルコニー面積', ''))
    total_units = _int(kv.get('棟総戸数', ''))
    floors_above = _int(kv.get('地上階層', ''))
    underground_floors = _int(kv.get('地下階層', ''))
    structure = kv.get('建物構造', '')
    if not structure:
        m = _r.search(r'(ＳＲＣ|ＲＣ|ＳＣ|鉄骨|木造)', raw_text)
        if m:
            structure = m.group(1)

    # 費用
    mgmt_fee = _int(kv.get('管理費', ''))
    repair_reserve = _int(kv.get('修繕積立金', ''))

    return {
        'reins_id': kv.get('物件番号', ''),
        'price': price_man,                          # 万円
        'property_type': kv.get('物件種目', ''),
        'address': address,
        'building_name': kv.get('マンション名', ''),
        'line': line,
        'station': station,
        'walk_min': walk_min,
        'room_layout': room_layout,
        'size_sqm': size_sqm,
        'built_date_full': kv.get('築年月', ''),
        'structure': kv.get('建物構造', ''),
        'floor': floor,
        'floors_above': floors_above,
        'underground_floors': underground_floors,
        'orientation': kv.get('バルコニー方向', ''),
        'balcony_sqm': balcony_sqm,
        'total_units': total_units,
        'land_rights': kv.get('土地権利', ''),
        'use_district': kv.get('用途地域', ''),
        'current_status': kv.get('現況', ''),
        'handover_timing': kv.get('引渡時期', ''),
        'transaction_type': kv.get('取引態様', ''),
        'mgmt_fee': mgmt_fee,
        'repair_reserve': repair_reserve,
        'management_company': kv.get('管理会社名', ''),
        'management_type': kv.get('管理形態', ''),
        'parking': kv.get('駐車場', ''),
        'registration_date': kv.get('登録年月日', ''),
        'latest_update_date': kv.get('最新変更年月日', '') or kv.get('最新更新年月日', ''),
        'notes_freetext': kv.get('備考', ''),
        '_raw_kv': kv,  # debug 用
    }


def download_overview_pdf(reins_id, headless=True, timeout_ms=60000):
    """
    用正常會員 UI 流程，為指定物件觸發「概要」PDF 下載。
    保存去 uploads/reins/<reins_id>/overview.pdf（atomic replace，唔會產生 duplicate）。
    返回 (web_path, None) 或 (None, error)。
    """
    if not _has_storage_state():
        return None, 'No REINS session'

    dest_dir = os.path.join(os.path.dirname(__file__), 'uploads', 'reins', reins_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, 'overview.pdf')

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=headless, args=['--disable-blink-features=AutomationControlled'])
        try:
            ctx = b.new_context(
                storage_state=STORAGE_STATE_PATH,
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
                locale='ja-JP', viewport={'width': 1280, 'height': 800},
                accept_downloads=True)
            pg = ctx.new_page()
            _go_to_search_page(pg, timeout_ms)

            # 用物件番号搜尋（最準確，唔使用地址）
            # 主選單有「物件番号検索」— 直接打物件番号最快
            pg.goto('https://system.reins.jp/main/KG/GKG003100', wait_until='networkidle', timeout=timeout_ms)
            pg.wait_for_timeout(2000)
            # 撳「物件番号検索」
            clicked = False
            for btn in pg.query_selector_all('button'):
                try:
                    import re as _r
                    if '物件番号検索' in _r.sub(r'\s+', '', btn.text_content() or ''):
                        btn.click(); clicked = True; break
                except Exception:
                    continue
            if clicked:
                pg.wait_for_load_state('networkidle', timeout=timeout_ms)
                pg.wait_for_timeout(2500)
                # 填物件番号
                filled = False
                for inp in pg.query_selector_all('input[type="text"]:visible'):
                    try:
                        inp.fill(reins_id)
                        inp.dispatch_event('input')
                        filled = True
                        break
                    except Exception:
                        continue
                if filled:
                    for btn in pg.query_selector_all('button'):
                        try:
                            if (btn.text_content() or '').strip() == '検索' and not btn.is_disabled():
                                btn.click(); break
                        except Exception:
                            continue
                    pg.wait_for_timeout(4000)
            else:
                # fallback：入売買物件検索再填物件番号（如果页面支持）
                _go_to_search_page(pg, timeout_ms)

            # 而家應該喺結果頁/詳細頁 — 搵「概要」掣
            overview_btn = None
            for btn in pg.query_selector_all('button'):
                try:
                    if (btn.text_content() or '').strip() == '概要' and not btn.is_disabled():
                        overview_btn = btn; break
                except Exception:
                    continue
            if not overview_btn:
                return None, f'搵唔到物件 {reins_id} 嘅「概要」掣'

            tmp = dest + '.tmp'
            try:
                with pg.expect_download(timeout=20000) as dl_info:
                    overview_btn.click()
                dl = dl_info.value
                dl.save_as(tmp)
                os.replace(tmp, dest)  # atomic replace
            except Exception as e:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return None, f'概要 PDF 下載失敗: {type(e).__name__}'

            return f'/uploads/reins/{reins_id}/overview.pdf', None
        except Exception as e:
            return None, f'{type(e).__name__}: {str(e)[:150]}'
        finally:
            try:
                b.close()
            except Exception:
                pass


def _go_to_search_page(pg, timeout_ms):
    """由主選單入売買物件検索（重用 storage state）。"""
    import re as _r
    pg.goto('https://system.reins.jp/main/KG/GKG003100', wait_until='networkidle', timeout=timeout_ms)
    pg.wait_for_timeout(2000)
    for btn in pg.query_selector_all('button'):
        try:
            if '売買物件検索' in _r.sub(r'\s+', '', btn.text_content() or ''):
                btn.click(); break
        except Exception:
            continue
    pg.wait_for_load_state('networkidle', timeout=timeout_ms)
    pg.wait_for_timeout(2500)


def download_drawing_pdf(reins_id, headless=True, timeout_ms=60000):
    """
    為指定物件觸發「図面」PDF 下載。
    保存去 uploads/reins/<reins_id>/drawing.pdf。
    返回 (web_path, None) 或 (None, error)。
    """
    if not _has_storage_state():
        return None, 'No REINS session'

    dest_dir = os.path.join(os.path.dirname(__file__), 'uploads', 'reins', reins_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, 'drawing.pdf')

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=headless, args=['--disable-blink-features=AutomationControlled'])
        try:
            ctx = b.new_context(
                storage_state=STORAGE_STATE_PATH,
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
                locale='ja-JP', viewport={'width': 1280, 'height': 800},
                accept_downloads=True)
            pg = ctx.new_page()
            _go_to_search_page(pg, timeout_ms)

            # 搜尋該物件（用物件番号最直接）
            pg.goto('https://system.reins.jp/main/KG/GKG003100', wait_until='networkidle', timeout=timeout_ms)
            pg.wait_for_timeout(2000)
            clicked = False
            for btn in pg.query_selector_all('button'):
                try:
                    import re as _r
                    if '物件番号検索' in _r.sub(r'\s+', '', btn.text_content() or ''):
                        btn.click(); clicked = True; break
                except Exception:
                    continue
            if clicked:
                pg.wait_for_load_state('networkidle', timeout=timeout_ms)
                pg.wait_for_timeout(2500)
                filled = False
                for inp in pg.query_selector_all('input[type="text"]:visible'):
                    try:
                        inp.fill(reins_id)
                        inp.dispatch_event('input')
                        filled = True
                        break
                    except Exception:
                        continue
                if filled:
                    for btn in pg.query_selector_all('button'):
                        try:
                            if (btn.text_content() or '').strip() == '検索' and not btn.is_disabled():
                                btn.click(); break
                        except Exception:
                            continue
                    pg.wait_for_timeout(4000)

            drawing_btn = None
            for btn in pg.query_selector_all('button'):
                try:
                    if (btn.text_content() or '').strip() == '図面' and not btn.is_disabled():
                        drawing_btn = btn; break
                except Exception:
                    continue
            if not drawing_btn:
                return None, f'物件 {reins_id} 冇「図面」掣（可能冇圖面）'

            tmp = dest + '.tmp'
            try:
                with pg.expect_download(timeout=20000) as dl_info:
                    drawing_btn.click()
                dl = dl_info.value
                dl.save_as(tmp)
                os.replace(tmp, dest)
            except Exception as e:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return None, f'図面 PDF 下載失敗: {type(e).__name__}'

            return f'/uploads/reins/{reins_id}/drawing.pdf', None
        except Exception as e:
            return None, f'{type(e).__name__}: {str(e)[:150]}'
        finally:
            try:
                b.close()
            except Exception:
                pass


def import_reins_listing(reins_id, drawing_available=False, headless=True):
    """
    單件 REINS import：download overview PDF → coordinate parser → normalize →
    upsert listings DB → geocode →（如有）download drawing PDF → 更新 DB。

    返回 dict：{'code': 1, 'id': listing_id, 'action': 'inserted'|'updated', ...}
    或 {'code': 0, 'error': ...}
    """
    # 1) Download overview PDF
    overview_web, err = download_overview_pdf(reins_id, headless=headless)
    if err:
        return {'code': 0, 'error': f'overview 下載失敗: {err}', 'reins_id': reins_id}

    overview_abs = os.path.join(
        os.path.dirname(__file__), 'uploads', 'reins', reins_id, 'overview.pdf'
    )
    if not os.path.exists(overview_abs):
        return {'code': 0, 'error': 'overview.pdf 落唔到 server', 'reins_id': reins_id}

    # 2) Parse with coordinate parser
    parsed = parse_overview_pdf(overview_abs)
    if not parsed.get('reins_id'):
        parsed['reins_id'] = reins_id
    if not parsed.get('price') or not parsed.get('address'):
        return {'code': 0, 'error': 'parser 抽唔到 price/address，唔會入 DB', 'reins_id': reins_id, 'parsed': parsed}

    # 3) Download drawing PDF（如有）
    drawing_web = ''
    if drawing_available:
        d_web, d_err = download_drawing_pdf(reins_id, headless=headless)
        if d_err:
            # 圖面冇唔係 fatal — 繼續 import，drawing_pdf 留空
            drawing_web = ''
        else:
            drawing_web = d_web

    # 3b) Render drawing PDF → images（本地腳本，唔用 AI/OCR）
    drawing_images = []
    drawing_abs = os.path.join(
        os.path.dirname(__file__), 'uploads', 'reins', reins_id, 'drawing.pdf'
    )
    if drawing_web and os.path.exists(drawing_abs):
        if not parsed.get('orientation'):
            try:
                from reins_pdf_parser import extract_orientation_from_pdf
                direction, direction_source, direction_confidence = extract_orientation_from_pdf(drawing_abs)
                if direction:
                    parsed['orientation'] = direction
                    parsed['orientation_source'] = direction_source
                    parsed['orientation_confidence'] = direction_confidence
            except Exception as e:
                print(f'[reins orientation] 圖面朝向抽取失敗: {type(e).__name__}', flush=True)
        try:
            from reins_pdf_render import render_drawing_pdf
            out_dir = os.path.dirname(drawing_abs)
            abs_paths = render_drawing_pdf(drawing_abs, out_dir)
            drawing_images = [
                '/uploads/reins/{}/{}'.format(reins_id, os.path.basename(p))
                for p in abs_paths
            ]
        except Exception as e:
            print(f'[reins render] drawing render 失敗: {e}', flush=True)
            drawing_images = []

    # 4) Geocode
    lat, lon = None, None
    try:
        from geocode_client import geocode
        lat, lon = geocode(parsed.get('address', ''))
    except Exception:
        pass

    # 5) Upsert DB
    lid, action = _upsert_reins_listing(parsed, overview_web, drawing_web, lat, lon, drawing_images)

    return {
        'code': 1, 'id': lid, 'action': action, 'reins_id': reins_id,
        'price': parsed.get('price'), 'address': parsed.get('address'),
        'building_name': parsed.get('building_name'),
        'overview_pdf': overview_web, 'drawing_pdf': drawing_web,
        'drawing_images': drawing_images,
        'lat': lat, 'lon': lon,
    }


def _upsert_reins_listing(parsed, overview_web, drawing_web, lat, lon, drawing_images=None):
    """
    Upsert listings row by reins_id。返回 (listing_id, 'inserted'|'updated')。
    DB 存 raw 值：price 用万円（platform 慣例 display 用万円，price_raw 唔需要因為
    parser 已經回万円）。
    """
    from db import get_db
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    reins_id = parsed.get('reins_id', '')
    now_iso = _dt.now(_tz.utc).isoformat()

    price = int(parsed.get('price') or 0)          # 万円
    size_sqm = float(parsed.get('size_sqm') or 0)
    price_per_sqm = round(price / size_sqm, 1) if size_sqm > 0 else 0

    # built_year / age：由 built_date_full 抽年份（令和7年 → 2025；平成16年 → 2004）
    built_year = _jp_year_to_western(parsed.get('built_date_full', ''))
    age = max(0, _dt.now().year - built_year) if built_year else 0

    fields = {
        'address': parsed.get('address', ''),
        'station': parsed.get('station', ''),
        'walk_min': int(parsed.get('walk_min') or 0),
        'price': price,
        'price_per_sqm': price_per_sqm,
        'size_sqm': size_sqm,
        'built_year': built_year,
        'age': age,
        'room_layout': parsed.get('room_layout', ''),
        'orientation': parsed.get('orientation', ''),
        'orientation_source': parsed.get('orientation_source', 'overview_field' if parsed.get('orientation') else ''),
        'orientation_confidence': float(parsed.get('orientation_confidence') or (1.0 if parsed.get('orientation') else 0)),
        'floor': int(parsed.get('floor') or 0),
        'floors_above': int(parsed.get('floors_above') or 0),
        'total_floors': int(parsed.get('floors_above') or 0),
        'underground_floors': int(parsed.get('underground_floors') or 0),
        'structure': parsed.get('structure', ''),
        'land_rights': parsed.get('land_rights', ''),
        'use_district': parsed.get('use_district', ''),
        'current_status': parsed.get('current_status', ''),
        'handover_timing': parsed.get('handover_timing', ''),
        'transaction_type': parsed.get('transaction_type', ''),
        'mgmt_fee': int(parsed.get('mgmt_fee') or 0),
        'repair_reserve': int(parsed.get('repair_reserve') or 0),
        'management_company': parsed.get('management_company', ''),
        'management_type': parsed.get('management_type', ''),
        'balcony_sqm': float(parsed.get('balcony_sqm') or 0),
        'total_units': int(parsed.get('total_units') or 0),
        'building_name': parsed.get('building_name', ''),
        'built_date_full': parsed.get('built_date_full', ''),
        'notes_freetext': parsed.get('notes_freetext', ''),
        'registration_no': parsed.get('registration_no', ''),
        'reins_registered_at': parsed.get('registration_date', ''),
        'reins_updated_at': parsed.get('latest_update_date', ''),
        'reins_overview_pdf': overview_web or '',
        'source': 'reins',
        'updated_at': now_iso,
    }
    if lat and lon:
        fields['latitude'] = lat
        fields['longitude'] = lon
    # drawing_pdf：如有新值先更新（唔好用空值 overwrite 已有）
    if drawing_web:
        fields['reins_drawing_pdf'] = drawing_web
    # rendered drawing images → floorplan_images（reuse 現有欄位，唔開新 table）
    # 格式：[{url, label}]，唔混 PDF path 入 image array
    if drawing_images:
        import json as _json
        fields['floorplan_images'] = _json.dumps(
            [{'url': u, 'label': f'REINS 図面 {i}'} for i, u in enumerate(drawing_images, 1)],
            ensure_ascii=False
        )

    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM listings WHERE source='reins' AND reins_id = ? LIMIT 1",
            (reins_id,)
        ).fetchone()

        if existing:
            lid = existing['id']
            set_cols = ', '.join(f'{k} = ?' for k in fields)
            conn.execute(
                f'UPDATE listings SET {set_cols} WHERE id = ?',
                (*fields.values(), lid)
            )
            conn.commit()
            return lid, 'updated'
        else:
            lid = 'REINS' + _dt.now(_tz.utc).strftime('%Y%m%d%H%M%S') + _uuid.uuid4().hex[:4].upper()
            fields['id'] = lid
            fields['reins_id'] = reins_id
            fields['status'] = 'draft'   # 新匯入必須經中介確認先發布
            fields['created_at'] = now_iso
            cols = ', '.join(fields.keys())
            placeholders = ', '.join('?' for _ in fields)
            conn.execute(
                f'INSERT INTO listings ({cols}) VALUES ({placeholders})',
                tuple(fields.values())
            )
            conn.commit()
            return lid, 'inserted'
    finally:
        conn.close()


def _jp_year_to_western(s):
    """和曆 → 西曆年份。令和7年→2025, 平成16年→2004, 昭和→+1925。
    只係 date normalization。"""
    import re as _r
    if not s:
        return 0
    m = _r.search(r'(令和|平成|昭和)\s*(\d+)\s*年', s)
    if not m:
        m2 = _r.search(r'(\d{4})\s*年', s)
        return int(m2.group(1)) if m2 else 0
    era, yr = m.group(1), int(m.group(2))
    base = {'令和': 2018, '平成': 1988, '昭和': 1925}.get(era, 0)
    return base + yr if base else 0
