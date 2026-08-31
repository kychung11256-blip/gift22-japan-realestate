"""
SUUMO Scraper — direct HTML parsing, no Browser Core needed.
Extracts: price, address, layout, area, floor, structure, images, floorplan, transit.
"""
import re, json, urllib.request, urllib.error, sqlite3, os, time, uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'listings.db')
USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'


def fetch(url, timeout=20):
    """Fetch URL with retries, return HTML string."""
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1)


def scrape_suumo(url):
    """Scrape a SUUMO property page, return dict ready for DB insert."""
    html = fetch(url)
    
    data = {}
    
    # ── Extract from JS data block (gapSuumoPcForKr) ──
    js_match = re.search(r'gapSuumoPcForKr\s*=\s*\[(.*?)\];', html, re.DOTALL)
    if js_match:
        js_text = js_match.group(1)
        def _js_val(key):
            m = re.search(rf'{key}\s*:\s*"([^"]*)"', js_text)
            return m.group(1) if m else ''
        
        data['price'] = _js_val('kakakuDisp')
        data['layout'] = _js_val('madoriDisp')
        data['area'] = _js_val('senyuMensekiDisp')
        data['built_date'] = _js_val('kanseiDateDisp')
        data['units'] = _js_val('soKukakusuDisp')
        data['station'] = _js_val('ekiNm1')
        data['line'] = _js_val('ensenNm1')
        data['walk_min'] = _js_val('tohoJikan1')
        data['pref'] = _js_val('todofukenNm')
        data['city'] = _js_val('shikugunNm')
        data['orientation'] = _js_val('muki')
    
    # ── Extract from HTML body ──
    title_match = re.search(r'<title>【SUUMO】(.*?)中古マンション物件情報</title>', html)
    data['title'] = title_match.group(1).strip() if title_match else ''
    
    # Build full address
    data['address'] = (data.get('pref', '') + data.get('city', '')).strip()
    
    # ── Extract from table rows ──
    table_rows = re.findall(r'<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>', html, re.DOTALL)
    for th, td in table_rows:
        th_text = re.sub(r'<[^>]+>', '', th).strip()
        td_text = re.sub(r'<[^>]+>', '', td).strip().replace('\r', '').replace('\n', '')
        td_text = ' '.join(td_text.split())
        
        if '所在階' in th_text and '構造' in th_text:
            parts = td_text.split('/')
            for p in parts:
                m = re.match(r'(\d+)階', p)
                if m:
                    data['floor'] = m.group(1)
                m2 = re.match(r'(\D+)(\d+)階建', p)
                if m2:
                    data['structure'] = m2.group(1).strip()
                    data['total_floors'] = m2.group(2)
        elif '所在階' in th_text and '構造' not in th_text:
            m = re.match(r'(\d+)階', td_text)
            if m:
                data['floor'] = m.group(1)
        elif '構造' in th_text and '階建' in th_text:
            m = re.match(r'(\D+)(\d+)階建', td_text)
            if m:
                data['structure'] = m.group(1).strip()
                data['total_floors'] = m.group(2)
        elif '構造' in th_text:
            data['structure'] = td_text
        elif '完成時期' in th_text or '築年月' in th_text:
            data['built_date_full'] = td_text
        elif '総戸数' in th_text:
            data['units'] = td_text.replace('戸', '').strip()
        elif '管理費' in th_text:
            data['management_fee'] = td_text
        elif '専有面積' in th_text:
            data['area'] = td_text.replace('m2', '').replace('（', '').split('（')[0].strip()
        elif 'バルコニー' in th_text:
            data['balcony'] = td_text
        elif '敷地面積' in th_text:
            data['land_area'] = td_text
        elif '現況' in th_text:
            data['current_status'] = td_text
        elif '引渡' in th_text:
            data['handover'] = td_text
        elif '取引態様' in th_text:
            data['transaction_type'] = td_text
        elif '土地権利' in th_text:
            data['land_rights'] = td_text
        elif '用途地域' in th_text:
            data['use_district'] = td_text
    
    # ── Extract images ──
    all_images = []
    for pattern in [r'data-src="(https?://[^"]+\.(?:jpg|jpeg|png|webp))"', r'src="(https?://[^"]+\.(?:jpg|jpeg|png|webp))"']:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            url = m.group(1)
            if 'suumo.jp' in url and 'img' in url and 'icon' not in url.lower() and 'logo' not in url.lower():
                all_images.append(url)
    
    # Deduplicate while preserving order
    seen = set()
    data['photos'] = []
    for u in all_images:
        if u not in seen:
            seen.add(u)
            data['photos'].append(u)
    
    # ── Identify floorplan ──
    data['floorplan_url'] = ''
    data['floorplan_images'] = []
    for u in data['photos']:
        if '0001.jpg' in u:
            data['floorplan_url'] = u
            data['floorplan_images'] = [u]
            break
    
    # ── Build transit_lines ──
    data['transit_lines'] = []
    if data.get('line') and data.get('station'):
        data['transit_lines'].append({
            'line': data['line'],
            'station': data['station'] + '駅',
            'walk_min': int(data.get('walk_min', 0) or 0),
            'direction': ''
        })
    
    # ── Build notes ──
    notes_parts = [data.get('title', '')]
    if data.get('line'):
        notes_parts.append(f"{data['line']} {data.get('station','')}駅 徒歩{data.get('walk_min','')}分")
    if data.get('orientation'):
        notes_parts.append(f"{data['orientation']}向き")
    if data.get('layout'):
        notes_parts.append(data['layout'])
    if data.get('area'):
        notes_parts.append(f"{data['area']}㎡")
    if data.get('built_date_full'):
        notes_parts.append(f"{data['built_date_full']}築")
    if data.get('units'):
        notes_parts.append(f"{data['units']}戸")
    if data.get('management_fee'):
        notes_parts.append(f"管理費{data['management_fee']}")
    if data.get('balcony'):
        notes_parts.append(f"バルコニー{data['balcony']}")
    data['notes'] = ' | '.join(notes_parts)
    
    return data


def insert_to_db(data):
    """Insert scraped data into listings DB, return listing ID."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    
    ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    listing_id = f"SU{ts}{uuid.uuid4().hex[:4].upper()}"
    
    price = int(data.get('price', 0) or 0)
    try:
        size_sqm = float(data.get('area', 0) or 0)
    except ValueError:
        size_sqm = 0
    try:
        walk_min = int(data.get('walk_min', 0) or 0)
    except ValueError:
        walk_min = 0
    try:
        floor = int(data.get('floor', 0) or 0)
    except ValueError:
        floor = 0
    try:
        total_floors = int(data.get('total_floors', 0) or 0)
    except ValueError:
        total_floors = 0
    
    # Calculate age from built_date
    built_date = data.get('built_date', '')
    built_year = 0
    if len(built_date) >= 4:
        built_year = int(built_date[:4])
    age = 2026 - built_year if built_year > 0 else 0
    
    price_per_sqm = round(price / size_sqm, 1) if size_sqm > 0 else 0
    yield_surface = 4.8
    yield_net = 3.7
    
    now_iso = datetime.now(timezone.utc).isoformat()
    
    conn.execute("""
        INSERT INTO listings (
            id, agent_id, address, station, walk_min, price, price_per_sqm,
            size_sqm, built_year, age, room_layout, orientation, floor, total_floors,
            structure, land_rights, type, yield_surface, yield_net,
            source, photos, floorplan_url, reins_id, ai_generated_copy, ai_keywords,
            disaster_flood, disaster_earthquake, disaster_liquefaction, disaster_tsunami,
            status, created_at, updated_at, transit_lines, floorplan_images,
            current_status, handover_timing, transaction_type, notes_freetext,
            built_date_full, use_district
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        listing_id, 'agent_001', data.get('address', ''), data.get('station', ''),
        walk_min, price, price_per_sqm,
        size_sqm, built_year, age, data.get('layout', ''), data.get('orientation', ''),
        floor, total_floors,
        data.get('structure', ''), data.get('land_rights', ''), 'マンション',
        yield_surface, yield_net,
        'suumo',
        json.dumps(data.get('photos', []), ensure_ascii=False),
        data.get('floorplan_url', ''),
        '', '', '[]',
        'low', 'low', 'low', 'low',
        'published', now_iso, now_iso,
        json.dumps(data.get('transit_lines', []), ensure_ascii=False),
        json.dumps(data.get('floorplan_images', []), ensure_ascii=False),
        data.get('current_status', ''),
        data.get('handover', ''),
        data.get('transaction_type', ''),
        data.get('notes', ''),
        data.get('built_date_full', ''),
        data.get('use_district', ''),
    ))
    conn.commit()
    conn.close()
    
    return listing_id


def scrape_and_insert(url):
    """Full pipeline: scrape SUUMO URL → insert into DB → return listing ID."""
    data = scrape_suumo(url)
    listing_id = insert_to_db(data)
    return {'id': listing_id, 'price': data.get('price'), 'address': data.get('address'), 'photos': len(data.get('photos', [])), 'title': data.get('title')}
