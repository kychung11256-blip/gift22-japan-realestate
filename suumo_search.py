"""
SUUMO Search — find listings, extract data, batch import to DB.
Mobile UA bypasses rate limiting. No Playwright/browser needed.
"""
import hashlib, re, urllib.request, urllib.parse, time, json, sqlite3, os, uuid
from datetime import datetime, timezone

MOBILE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'listings.db')

PREF_ROMAN = {
    '東京都': 'tokyo', '神奈川県': 'kanagawa', '埼玉県': 'saitama', '千葉県': 'chiba',
    '大阪府': 'osaka', '京都府': 'kyoto', '兵庫県': 'hyogo', '愛知県': 'aichi',
    '福岡県': 'fukuoka', '北海道': 'hokkaido', '宮城県': 'miyagi', '広島県': 'hiroshima',
    '沖縄県': 'okinawa',
}

CITY_ROMAN = {
    '港区': 'minato', '渋谷区': 'shibuya', '新宿区': 'shinjuku', '中央区': 'chuo',
    '千代田区': 'chiyoda', '目黒区': 'meguro', '世田谷区': 'setagaya', '品川区': 'shinagawa',
    '中野区': 'nakano', '杉並区': 'suginami', '文京区': 'bunkyo', '台東区': 'taito',
    '墨田区': 'sumida', '江東区': 'koto', '大田区': 'ota', '豊島区': 'toshima',
    '北区': 'kita', '荒川区': 'arakawa', '板橋区': 'itabashi', '練馬区': 'nerima',
    '足立区': 'adachi', '葛飾区': 'katsushika', '江戸川区': 'edogawa',
    '八王子市': 'hachioji', '立川市': 'tachikawa', '武蔵野市': 'musashino',
    '三鷹市': 'mitaka', '府中市': 'fuchu', '調布市': 'chofu', '町田市': 'machida',
    '小金井市': 'koganei', '小平市': 'kodaira', '国分寺市': 'kokubunji', '国立市': 'kunitachi',
    '西東京市': 'nishitokyo', '多摩市': 'tama',
    '横浜市': 'yokohama', '川崎市': 'kawasaki', '相模原市': 'sagamihara',
    '横須賀市': 'yokosuka', '藤沢市': 'fujisawa', '鎌倉市': 'kamakura',
    '茅ヶ崎市': 'chigasaki', '大和市': 'yamato', '海老名市': 'ebina',
    '逗子市': 'zushi', '葉山町': 'hayama',
    'さいたま市': 'saitama', '川越市': 'kawagoe', '越谷市': 'koshigaya',
    '所沢市': 'tokorozawa', '春日部市': 'kasukabe', '上尾市': 'ageo',
    '新座市': 'niiza', '朝霞市': 'asaka', '和光市': 'wako', '戸田市': 'toda',
    '千葉市': 'chiba', '船橋市': 'funabashi', '松戸市': 'matsudo',
    '市川市': 'ichikawa', '柏市': 'kashiwa', '浦安市': 'urayasu',
    '流山市': 'nagareyama', '習志野市': 'narashino',
    '大阪市': 'osaka', '堺市': 'sakai', '吹田市': 'suita', '豊中市': 'toyonaka',
    '茨木市': 'ibaraki', '高槻市': 'takatsuki', '枚方市': 'hirakata',
    '京都市': 'kyoto', '宇治市': 'uji', '長岡京市': 'nagaokakyo', '向日市': 'muko',
    '神戸市': 'kobe', '西宮市': 'nishinomiya', '芦屋市': 'ashiya',
    '尼崎市': 'amagasaki', '宝塚市': 'takarazuka', '伊丹市': 'itami', '明石市': 'akashi',
    '名古屋市': 'nagoya', '豊田市': 'toyota', '岡崎市': 'okazaki',
    '一宮市': 'ichinomiya', '春日井市': 'kasugai', '刈谷市': 'kariya',
    '福岡市': 'fukuoka', '北九州市': 'kitakyushu', '久留米市': 'kurume', '大牟田市': 'omuta',
    '札幌市': 'sapporo', '旭川市': 'asahikawa', '函館市': 'hakodate', '小樽市': 'otaru',
    '仙台市': 'sendai', '広島市': 'hiroshima',
    '那覇市': 'naha', '浦添市': 'urasoe', '宜野湾市': 'ginowan',
}

CITIES_BY_PREF = {
    '東京都': ['港区', '渋谷区', '新宿区', '中央区', '千代田区', '目黒区',
               '世田谷区', '品川区', '中野区', '杉並区', '文京区', '台東区',
               '墨田区', '江東区', '大田区', '豊島区', '北区', '荒川区',
               '板橋区', '練馬区', '足立区', '葛飾区', '江戸川区',
               '八王子市', '立川市', '武蔵野市', '三鷹市', '府中市', '調布市',
               '町田市', '小金井市', '国立市', '多摩市'],
    '神奈川県': ['横浜市', '川崎市', '相模原市', '横須賀市', '藤沢市', '鎌倉市',
                '茅ヶ崎市', '大和市', '逗子市'],
    '埼玉県': ['さいたま市', '川越市', '越谷市', '所沢市', '新座市', '朝霞市',
               '和光市', '戸田市'],
    '千葉県': ['千葉市', '船橋市', '松戸市', '市川市', '柏市', '浦安市',
               '流山市', '習志野市'],
    '大阪府': ['大阪市', '堺市', '吹田市', '豊中市', '茨木市', '高槻市', '枚方市'],
    '京都府': ['京都市', '宇治市', '長岡京市'],
    '兵庫県': ['神戸市', '西宮市', '芦屋市', '尼崎市', '宝塚市', '明石市'],
    '愛知県': ['名古屋市', '豊田市', '岡崎市', '一宮市', '春日井市'],
    '福岡県': ['福岡市', '北九州市', '久留米市'],
    '北海道': ['札幌市', '旭川市', '函館市'],
    '宮城県': ['仙台市'],
    '広島県': ['広島市'],
    '沖縄県': ['那覇市', '浦添市', '宜野湾市'],
}

def _fetch(url, ua=MOBILE_UA, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': ua, 'Accept-Language': 'ja-JP'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='replace')

def _strip(s):
    """Strip HTML tags and normalize whitespace."""
    s = re.sub(r'<[^>]+>', '', s)
    return ' '.join(s.split())

def _parse_card(body, city):
    """Extract listing data from a card body (handles both cassette and KR formats)."""
    price_str = layout_str = area_str = addr_str = age_str = ''
    station_str = line_str = walk_str = ''
    img_str = ''

    # Price
    pm = re.search(r'bukken-cassette__kakaku[^>]*>\s*([\d,]+)万円', body)
    if not pm: pm = re.search(r'pickup-bukken__kakaku[^>]*>\s*([\d,]+)万円', body)
    if not pm: pm = re.search(r'__kakaku[^>]*>\s*([\d,]+)万円', body)
    if pm: price_str = pm.group(1).replace(',', '')

    # Layout, area, age from cassette format
    lm = re.search(r'bukken-cassette__madori[^>]*>\s*([^<\s]+)', body)
    if lm: layout_str = lm.group(1)
    am = re.search(r'bukken-cassette__menseki[^>]*>\s*([\d.]+)m', body)
    if am: area_str = am.group(1)
    agem = re.search(r'築(\d+)年', body)
    if agem: age_str = agem.group(1)

    # Fallback: KR pickup format for layout/area/age
    if not layout_str or not area_str:
        other_m = re.search(r'__other-info[^>]*>\s*([^<]+)', body)
        if other_m:
            other = other_m.group(1)
            l = re.search(r'([\d][LDKS]+)', other)
            a = re.search(r'([\d.]+)m²', other)
            ag = re.search(r'築(\d+)年', other)
            if l: layout_str = l.group(1)
            if a: area_str = a.group(1)
            if ag: age_str = ag.group(1)

    # Address
    addr = re.search(r'bukken-cassette__address[^>]*>\s*([^<]+)', body)
    addr_str = addr.group(1).strip() if addr else city

    # Transport
    kotsu = re.search(r'bukken-cassette__kotsu[^>]*>\s*(.+?)(?:</div>|$)', body)
    if not kotsu: kotsu = re.search(r'__kotsu[^>]*>\s*([^<]+)', body)
    if kotsu:
        k = kotsu.group(1)
        st = re.search(r'「([^」]+)」', k)
        if st: station_str = st.group(1)
        ln = re.match(r'([^\s「]+)', k)
        if ln: line_str = ln.group(1)
        w = re.search(r'歩(\d+)分', k)
        if w: walk_str = w.group(1)

    # Image
    img = re.search(r'<img[^>]*src="([^"]+)"', body)
    if img: img_str = img.group(1)

    return {
        'price': price_str, 'layout': layout_str, 'area': area_str,
        'address': addr_str, 'station': station_str, 'line': line_str,
        'walk_min': walk_str, 'age': age_str, 'thumbnail': img_str,
    }

def search(city='港区', pref='', pmin=3000, pmax=10000, walk=10, max_results=20):
    """Search SUUMO and return listing cards with basic info."""
    # Auto-detect prefecture
    if not pref:
        for p, cities in CITIES_BY_PREF.items():
            if city in cities:
                pref = p
                break
        else:
            pref = '東京都'

    params = []

    # Tokyo: use sc_ pattern (cassette format). Others: national search with kw=
    if pref == '東京都':
        sc = CITY_ROMAN.get(city, 'minato')
        url = f'https://suumo.jp/ms/chuko/tokyo/sc_{sc}/'
    else:
        url = 'https://suumo.jp/ms/chuko/'
        params.append(f'kw={urllib.parse.quote(city)}')

    if pmin: params.append(f'cb={pmin}')
    if pmax: params.append(f'ct={pmax}')
    if walk: params.append(f'et={walk}')
    if params: url += '?' + '&'.join(params)

    time.sleep(2)
    try:
        html = _fetch(url)
    except Exception as e:
        return {'error': str(e), 'listings': []}

    if '503' in html[:100] or '表示できません' in html[:500]:
        return {'error': 'SUUMO rate limit', 'listings': []}

    results = []
    seen = set()

    # Try cassette format first (Tokyo sc_ pages)
    cards = re.findall(
        r'<a class="[^"]*bukken-cassette__bukken-info-section[^"]*"\s+href="(/ms/chuko/[^"]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL
    )

    if not cards:
        # Try pickup carousel format (national search / non-Tokyo)
        cards = re.findall(
            r'<a href="(/ms/chuko/[a-z]+/(sc_\w+/nc_\d+)/)"[^>]*(?:new-pickup|kr-chukomansion)[^>]*>',
            html
        )

    if cards:
        card_is_tuple = isinstance(cards[0], tuple)

        for card in cards:
            if card_is_tuple:
                href = card[0]
                body = card[1] if len(card) > 1 else ''
                if len(card) == 2 and '/' in card[1] and 'sc_' in card[1]:
                    body = ''
                    idx = html.find(card[1])
                    if idx > 0:
                        body = html[idx:idx+800]
            else:
                href = card
                body = ''

            if href in seen: continue
            seen.add(href)
            if len(results) >= max_results: break

            data = _parse_card(body, city)
            data['url'] = 'https://suumo.jp' + href
            results.append(data)

    return {'listings': results, 'total': len(cards)}


def scrape_detail(url):
    """Scrape a SUUMO detail page, return full data dict."""
    time.sleep(3)  # 3s delay to avoid SUUMO rate limit
    html = _fetch(url, ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
    data = {}

    # ── JS data block (gapSuumoPcForKr) ──
    js = re.search(r'gapSuumoPcForKr\s*=\s*\[(.*?)\];', html, re.DOTALL)
    if js:
        jst = js.group(1)
        def g(k):
            m = re.search(rf'{k}\s*:\s*\["([^"]*)"\]', jst)
            if m and m.group(1): return m.group(1)
            m = re.search(rf'{k}\s*:\s*"([^"]*)"', jst)
            return m.group(1) if m and m.group(1) else ''
        data['price'] = g('kakakuDisp') or g('headerKakakuDisp')
        if not data['price']:
            m = re.search(rf'kakakuDisp\s*:\s*\["(\d+)"\]', jst)
            if m: data['price'] = m.group(1)
        data['layout'] = g('madoriDisp')
        data['area'] = g('senyuMensekiDisp')
        data['built'] = g('kanseiDateDisp')
        data['units'] = g('soKukakusuDisp')
        data['station'] = g('ekiNm1')
        data['line'] = g('ensenNm1')
        data['walk'] = g('tohoJikan1')
        data['pref'] = g('todofukenNm')
        data['city'] = g('shikugunNm')
        data['ori'] = g('muki')
        # Extra JS fields
        data['station2'] = g('ekiNm2')
        data['line2'] = g('ensenNm2')
        data['walk2'] = g('tohoJikan2')
        data['station3'] = g('ekiNm3')
        data['line3'] = g('ensenNm3')
        data['walk3'] = g('tohoJikan3')
        data['building_name'] = g('title')
        data['company_name'] = g('kaisha_nm')
        data['land_area'] = g('shikichiMensekiDisp')
        data['building_area'] = g('tatemonoMensekiDisp')

    # ── Fallback regexes ──
    if not data.get('price'):
        pm = re.search(r'(\d[\d,]*)\s*万円', html)
        if pm: data['price'] = pm.group(1).replace(',', '')
    if not data.get('layout'):
        lm = re.search(r'間取り[：:]\s*([^\s<]+)', html)
        if lm: data['layout'] = lm.group(1)
    if not data.get('area'):
        am = re.search(r'専有面積[：:]\s*([\d.]+)', html)
        if am: data['area'] = am.group(1)

    # ── Address: use 所在地 table row (detailed) > pref+city (coarse) ──
    data['address'] = (data.get('pref', '') + data.get('city', '')).strip()
    loc = re.search(r'所在地</div></th>\s*<td[^>]*>(.*?)</td>', html, re.DOTALL)
    if loc:
        loc_t = _strip(loc.group(1))
        loc_t = re.sub(r'（.*?）|\s*※.*$', '', loc_t).strip()
        if loc_t:
            data['address'] = loc_t

    # ── Building name from title tag ──
    tm = re.search(r'<title>【SUUMO】(.*?)(?:中古|物件情報)</title>', html)
    if tm:
        data['building_name'] = tm.group(1).strip().split('|')[0].strip()

    # ── Full table extraction (all th/td pairs) ──
    for th, td in re.findall(r'<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>', html, re.DOTALL):
        th_t = _strip(th)
        td_t = _strip(td)
        if not th_t or not td_t or td_t == '-' or td_t == 'ヒント: -':
            continue

        # Remove ヒント: prefix if present
        td_t = re.sub(r'^ヒント:\s*', '', td_t).strip()
        if not td_t or td_t == '-':
            continue

        if '所在階' in th_t and '構造' in th_t:
            for p in td_t.split('/'):
                m = re.match(r'(\d+)階', p)
                if m: data['floor'] = m.group(1)
                m = re.match(r'(\D+)(\d+)階建', p)
                if m: data['structure'], data['total_floors'] = m.group(1).strip(), m.group(2)
        elif '構造' in th_t and '階建' in th_t:
            m = re.match(r'(\D+)(\d+)階建', td_t)
            if m: data['structure'], data['total_floors'] = m.group(1).strip(), m.group(2)
        elif '完成時期' in th_t or '築年月' in th_t:
            data['built_full'] = td_t
        elif '総戸数' in th_t:
            data['units'] = td_t.replace('戸', '').strip()
        elif '管理費' in th_t:
            data['mgmt_fee'] = td_t
        elif '修繕積立基金' in th_t:
            data['repair_fund'] = td_t
        elif '修繕積立金' in th_t:
            data['repair_reserve'] = td_t
        elif '諸費用' in th_t:
            data['other_costs'] = td_t
        elif '専有面積' in th_t:
            m = re.match(r'([\d.]+)', td_t)
            if m: data['area'] = m.group(1)
        elif 'その他面積' in th_t:
            # バルコニー面積：1.8m2
            m = re.search(r'バルコニー面積[：:]\s*([\d.]+)', td_t)
            if m: data['balcony_sqm'] = m.group(1)
        elif '現況' in th_t:
            data['current_status'] = td_t
        elif '引渡' in th_t:
            data['handover'] = td_t
        elif '取引態様' in th_t or '取引形態' in th_t:
            data['transaction_type'] = td_t
        elif '土地権利' in th_t or '敷地の権利形態' in th_t:
            data['land_rights'] = td_t
        elif '用途地域' in th_t:
            data['use_district'] = td_t
        elif '駐車場' in th_t:
            data['parking'] = td_t
        elif 'リフォーム' in th_t:
            data['renovation'] = td_t
        elif '敷地面積' in th_t:
            m = re.match(r'([\d.]+)', td_t)
            if m: data['land_area'] = m.group(1)
        elif '所在階' in th_t and '構造' not in th_t:
            m = re.match(r'(\d+)階', td_t)
            if m: data['floor'] = m.group(1)
        elif '向き' in th_t or '向' in th_t:
            data['ori'] = td_t
        elif '物件名' in th_t:
            data['building_name'] = td_t
        elif '情報提供日' in th_t:
            data['info_date'] = td_t
        elif '次回更新予定日' in th_t:
            data['next_update'] = td_t
        elif '問い合わせ先' in th_t or 'お問い合せ先' in th_t:
            data['contact_info'] = td_t
        elif '免許番号' in th_t:
            data['license_number'] = td_t
        elif '担当者' in th_t:
            data['agent_person'] = td_t

    # ── 交通 (up to 3 lines) ──
    data['transit_lines'] = []
    for i in range(1, 4):
        ln = data.get(f'line{i}' if i > 1 else 'line', '')
        st = data.get(f'station{i}' if i > 1 else 'station', '')
        wk = data.get(f'walk{i}' if i > 1 else 'walk', '')
        if ln and st:
            data['transit_lines'].append({
                'line': ln, 'station': st + '駅',
                'walk_min': int(wk or 0), 'direction': ''
            })

    # ── Photos ──
    imgs = re.findall(r'(?:data-src|src)="(https://[^"]+\.(?:jpg|jpeg|png|webp))"', html, re.I)
    data['photos'] = list(dict.fromkeys([u for u in imgs if 'suumo.jp' in u and 'icon' not in u.lower() and 'logo' not in u.lower()]))
    data['floorplan_url'] = next((u for u in data['photos'] if '0001.jpg' in u), '')
    data['floorplan_images'] = [data['floorplan_url']] if data['floorplan_url'] else []

    return data


def import_to_db(data):
    """Insert scraped data into listings DB with all available fields."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    source_url = (data.get('source_url') or data.get('url') or '').strip()
    suumo_key = ''
    if source_url:
        m = re.search(r'/nc_?([0-9A-Za-z_-]+)/', source_url)
        suumo_key = (m.group(1) if m else hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:24])
        existing = conn.execute(
            "SELECT id FROM listings WHERE source='suumo' AND reins_id=? LIMIT 1",
            (suumo_key,),
        ).fetchone()
        if existing:
            conn.close()
            return existing['id'], 'existing'

    ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    lid = f"SU{ts}{uuid.uuid4().hex[:4].upper()}"

    price = int(data.get('price', 0) or 0)
    if price > 100000: price //= 10000
    size = float(data.get('area', 0) or 0)
    walk = int(data.get('walk', 0) or 0)
    floor = int(data.get('floor', 0) or 0)
    tf = int(data.get('total_floors', 0) or 0)
    built = data.get('built', '')
    by = int(built[:4]) if len(built) >= 4 else 0
    age = 2026 - by if by > 0 else 0
    pps = round(price / size, 1) if size > 0 else 0
    balcony = float(data.get('balcony_sqm', 0) or 0)
    land_area = float(data.get('land_area', 0) or 0)
    total_units = int(data.get('units', 0) or 0)
    now = datetime.now(timezone.utc).isoformat()

    # Build comprehensive notes
    notes_parts = [
        data.get('building_name', ''),
        f"{data.get('line','')} {data.get('station','')}駅 徒歩{data.get('walk','')}分",
        f"{data.get('ori','')}向き" if data.get('ori') else '',
        data.get('layout', ''),
        f"{data.get('area','')}㎡",
        f"{data.get('built_full','')}築" if data.get('built_full') else '',
        f"総戸数{data.get('units','')}戸" if data.get('units') else '',
        f"管理費: {data.get('mgmt_fee','')}" if data.get('mgmt_fee') else '',
        f"修繕積立金: {data.get('repair_reserve','')}" if data.get('repair_reserve') else '',
        f"駐車場: {data.get('parking','')}" if data.get('parking') else '',
        f"リフォーム: {data.get('renovation','')}" if data.get('renovation') else '',
        f"情報提供日: {data.get('info_date','')}" if data.get('info_date') else '',
    ]
    notes = ' | '.join(filter(None, notes_parts))

    conn.execute("""INSERT INTO listings (
        id, agent_id, address, station, walk_min, price, price_per_sqm,
        size_sqm, built_year, age, room_layout, orientation, floor, total_floors,
        structure, land_rights, type, yield_surface, yield_net,
        source, photos, floorplan_url, reins_id, notes_freetext, transit_lines, floorplan_images,
        current_status, handover_timing, transaction_type, built_date_full, use_district,
        status, created_at, updated_at,
        mgmt_fee, repair_reserve, repair_fund, other_costs, renovation, parking,
        balcony_sqm, total_units, info_date, next_update,
        listing_agent_name, license_number, land_area_sqm
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        lid, 'agent_001', data.get('address',''), data.get('station',''), walk, price, pps,
        size, by, age, data.get('layout',''), data.get('ori',''), floor, tf,
        data.get('structure',''), data.get('land_rights',''), 'マンション', 4.8, 3.7,
        'suumo', json.dumps(data.get('photos',[]), ensure_ascii=False),
        data.get('floorplan_url',''), suumo_key, notes,
        json.dumps(data.get('transit_lines',[]), ensure_ascii=False),
        json.dumps(data.get('floorplan_images',[]), ensure_ascii=False),
        data.get('current_status',''), data.get('handover',''), data.get('transaction_type',''),
        data.get('built_full',''), data.get('use_district',''),
        'draft', now, now,
        data.get('mgmt_fee',''), data.get('repair_reserve',''), data.get('repair_fund',''),
        data.get('other_costs',''), data.get('renovation',''), data.get('parking',''),
        balcony, total_units,
        data.get('info_date',''), data.get('next_update',''),
        data.get('company_name',''), data.get('license_number',''), land_area,
    ))
    conn.commit()
    conn.close()

    # 匯入後即場 geocode（唔使等 confirm）
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(DB_PATH) + '/..')
        from geocode_client import geocode
        addr = data.get('address', '')
        if addr:
            glat, glon = geocode(addr)
            if glat and glon:
                conn2 = sqlite3.connect(DB_PATH)
                conn2.execute("UPDATE listings SET latitude=?, longitude=? WHERE id=?", (glat, glon, lid))
                conn2.commit()
                conn2.close()
    except Exception:
        pass  # geocode 失敗唔好擋住匯入

    return lid, 'inserted'
