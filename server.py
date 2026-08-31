"""
Flask backend for Johnny AI Platform.
JSON API: search, detail, upload, confirm, dashboard stats.
"""

import os, sys, json, uuid, random, threading, time, urllib.request, gzip as _gzip_mod
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory

# Load .env file manually if present
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())

# Fall back to the Hermes profile .env for shared service keys (e.g. CIYUAN_API_KEY)
_hermes_env = os.path.expanduser('~/.hermes/.env')
if os.path.exists(_hermes_env) and os.path.abspath(_hermes_env) != os.path.abspath(env_path):
    with open(_hermes_env, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())

sys.path.insert(0, os.path.dirname(__file__))
from db import get_db, init_db
from suumo_scraper import scrape_and_insert
from suumo_search import search as suumo_search, scrape_detail, import_to_db

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

# ── Inject venv via WSGI on startup ──
# (Not needed when running via .venv/bin/python directly)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

# Clean URLs for key pages
@app.route('/upload')
def upload_page():
    return send_from_directory('.', 'upload.html')

@app.route('/review')
def review_page():
    return send_from_directory('.', 'review.html')

@app.route('/mysok')
def mysok_page():
    return send_from_directory('.', 'mysok_import.html')

@app.route('/map')
def map_page():
    return send_from_directory('.', 'map.html')

@app.route('/collection')
def collection_page():
    return send_from_directory('.', 'collection.html')

@app.route('/listings')
def listings_page():
    return send_from_directory('.', 'listings.html')

# ── Agent Platform Proxy ────────────────────────────────────────
import urllib.request as _urllib_req
import urllib.error as _urllib_err

AGENT_API_BASE = os.environ.get('AGENT_API_BASE', 'http://localhost:3001')

@app.route('/api/agent/<path:subpath>', methods=['GET','POST','PUT','DELETE','PATCH'])
def agent_proxy(subpath):
    """
    Proxy requests to Agent Platform (port 3001).
    Frontend calls: /api/agent/v1/agent/search → subpath = 'v1/agent/search'
    Backend URL:    http://localhost:3001/v1/agent/search
    """
    url = f"{AGENT_API_BASE}/{subpath}"
    data = request.get_data()
    headers = {'Content-Type': request.content_type or 'application/json'}

    try:
        req = _urllib_req.Request(url, data=data, headers=headers, method=request.method)
        with _urllib_req.urlopen(req, timeout=30) as resp:
            resp_body = resp.read()
            return jsonify(json.loads(resp_body)), resp.status
    except _urllib_err.URLError as e:
        return jsonify({
            'success': False,
            'code': 'AGENT_UNAVAILABLE',
            'message': f'Agent Platform 不可達: {str(e.reason)}',
            'requestId': str(uuid.uuid4()),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }), 502
    except json.JSONDecodeError:
        return jsonify({
            'success': False,
            'code': 'AGENT_UNAVAILABLE',
            'message': 'Agent Platform 回傳非 JSON 格式',
            'requestId': str(uuid.uuid4()),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }), 502
    except Exception as e:
        return jsonify({
            'success': False,
            'code': 'AGENT_UNAVAILABLE',
            'message': str(e),
            'requestId': str(uuid.uuid4()),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }), 502

@app.route('/<path:filename>')
def static_files(filename):
    if os.path.isfile(os.path.join(os.path.dirname(__file__), filename)):
        return send_from_directory('.', filename)
    return jsonify({'error': 'not found'}), 404

# ── Drafts list API ──
@app.route('/api/drafts')
def list_drafts():
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM listings WHERE status='draft'
        ORDER BY created_at DESC
    """).fetchall()
    conn.close()
    return jsonify({'count': len(rows), 'listings': [_row_to_dict(r) for r in rows]})

# ── Update API ──
@app.route('/api/update/<listing_id>', methods=['POST'])
def update_listing(listing_id):
    data = request.get_json(force=True)
    if not data:
        return jsonify({'error': 'no data'}), 400
    conn = get_db()
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    # All editable columns
    scalars = ['address','station','walk_min','price','price_per_sqm','size_sqm','age',
               'room_layout','orientation','structure','type','land_rights','ownership_type',
               'land_area_sqm','land_area_tsubo','land_category','building_coverage_ratio',
               'floor_area_ratio','city_planning_zone','use_district','roof_type','floors_above',
               'built_date_full','total_floor_area_sqm','total_floor_area_tsubo','current_status',
               'handover_timing','transaction_type','commission_type','notes_freetext',
               'listing_agent_name','license_number','brokerage_type','yield_surface','yield_net',
               'built_year','floor','total_floors','disaster_flood','disaster_earthquake',
               'disaster_liquefaction','disaster_tsunami','latitude','longitude']

    json_cols = ['photos','floorplan_images','interior_photos','transit_lines',
                 'floor_area_by_level','ai_keywords','ai_generated_copy']

    sets = []
    vals = []

    for k in scalars:
        if k in data:
            sets.append(f"{k}=?")
            vals.append(data[k])

    for k in json_cols:
        if k in data:
            sets.append(f"{k}=?")
            vals.append(json.dumps(data[k], ensure_ascii=False))

    # Recompute price_per_sqm if price/size changed
    if 'price' in data or 'size_sqm' in data:
        price = data.get('price', row['price'] or 0)
        size = data.get('size_sqm', row['size_sqm'] or 0)
        if size > 0 and 'price_per_sqm' not in data:
            pp_sqm = round(price / size, 1)
            sets.append("price_per_sqm=?")
            vals.append(pp_sqm)

    if not sets:
        conn.close()
        return jsonify({'error': 'no valid fields'}), 400

    sets.append("updated_at=?")
    vals.append(datetime.now(timezone.utc).isoformat())
    vals.append(listing_id)
    conn.execute(f"UPDATE listings SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()
    return jsonify({'code': 1, 'id': listing_id})

# ── Search API ──
@app.route('/api/search')
def search():
    q = request.args.get('q', '').strip().lower()
    conn = get_db()
    if q:
        # Build query: search text fields + keywords
        results = conn.execute("""
            SELECT * FROM listings
            WHERE status = 'published'
            AND (
                address LIKE ? OR station LIKE ? OR room_layout LIKE ? OR
                ai_generated_copy LIKE ? OR ai_keywords LIKE ? OR
                structure LIKE ? OR type LIKE ? OR orientation LIKE ?
            )
            ORDER BY
                CASE WHEN address LIKE ? THEN 0 ELSE 1 END,
                price ASC
            LIMIT 8
        """, (f'%{q}%',) * 8 + (f'%{q}%',)).fetchall()
    else:
        results = conn.execute("""
            SELECT * FROM listings WHERE status='published'
            ORDER BY price DESC LIMIT 8
        """).fetchall()

    listings = [_row_to_dict(r) for r in results]
    conn.close()
    return jsonify({'count': len(listings), 'listings': listings})

@app.route('/api/market-intel')
def market_intel():
    """Return latest market intel report + parsed REINS data for dashboard."""
    outputs_dir = os.path.join(os.path.dirname(__file__), '..', 'outputs')
    intel_files = sorted(
        [f for f in os.listdir(outputs_dir) if f.startswith('intel-')],
        reverse=True
    )
    if not intel_files:
        return jsonify({'code': 0, 'error': 'no intel reports found'}), 404

    latest_path = os.path.join(outputs_dir, intel_files[0])
    with open(latest_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Parse report header
    header = ''
    report_date = ''
    for line in content.split('\n')[:6]:
        if '情報報告' in line:
            header = line.strip()
        if '來源' in line:
            pass

    # Extract date from filename
    import re as _re
    date_m = _re.search(r'intel-(\d{4}-\d{2}-\d{2})-(\d{4})', intel_files[0])
    if date_m:
        report_date = f"{date_m.group(1)} {date_m.group(2)[:2]}:{date_m.group(2)[2:]}"

    # Extract REINS section
    reins_summary = ''
    in_reins = False
    lines = []
    for line in content.split('\n'):
        if 'REINS' in line and '數據' in line:
            in_reins = True
            continue
        if in_reins:
            if line.strip().startswith('📌') or line.strip() == '':
                break
            if line.strip():
                lines.append(line.strip())
    reins_summary = '\n'.join(lines)

    # Extract key trends (first 3 ## sections)
    trends = []
    for line in content.split('\n'):
        if line.startswith('## ') and '熱門趨勢' not in line and '頻道' not in line:
            trends.append(line.replace('## ', '').strip())
            if len(trends) >= 4:
                break

    # Get file stats
    file_mtime = os.path.getmtime(latest_path)
    from datetime import datetime, timezone as _tz
    updated_at = datetime.fromtimestamp(file_mtime, tz=_tz.utc).isoformat()

    return jsonify({
        'code': 1,
        'report_date': report_date,
        'header': header,
        'file': intel_files[0],
        'updated_at': updated_at,
        'reins_summary': reins_summary,
        'trends': trends,
        'total_reports': len(intel_files),
    })

# ── Collection Search API ──
@app.route('/api/collection/cities')
def collection_cities():
    """Return prefecture + city hierarchy for the search dropdown."""
    from suumo_search import CITIES_BY_PREF
    return jsonify({'code': 1, 'prefectures': CITIES_BY_PREF})

# ── Natural Language Search API ──
@app.route('/api/nl-search', methods=['POST'])
def nl_search():
    """
    自然語言搜尋。body: {"query": "...", "page": 1}
    回傳 SearchPlan + 輕量搜尋結果 + explanation。
    LLM/parser 唔直接操作 browser；plan 經 deterministic validator。
    """
    data = request.get_json(force=True) or {}
    query = (data.get('query') or '').strip()
    page = int(data.get('page', 1) or 1)
    if not query:
        return jsonify({'code': 0, 'error': 'query required'}), 400

    from nl_search import run_search
    try:
        result = run_search(query, page=page, headless=True)
    except Exception as e:
        return jsonify({'code': 0, 'error': f'{type(e).__name__}: {str(e)[:200]}'}), 500

    # session expired → 明確 code
    if result.get('auth'):
        return jsonify({'code': 401, 'error': result.get('error'), 'auth': True,
                        'plan': result.get('plan'), 'listings': []}), 200
    return jsonify(result)


@app.route('/api/collection/search', methods=['POST'])
def collection_search():
    """Search listings. source='reins' → REINS; source='suumo' (default) → SUUMO."""
    data = request.get_json(force=True) or {}
    source = (data.get('source') or 'suumo').strip().lower()
    city = data.get('city', '港区')
    pref = data.get('pref', '')
    pmin = int(data.get('priceMin', 0) or 0)
    pmax = int(data.get('priceMax', 0) or 0)
    walk = int(data.get('walkMin', 0) or 0)
    max_results = int(data.get('maxProperties', 20) or 20)
    page_num = int(data.get('page', 1) or 1)

    if source == 'reins':
        try:
            from reins_client import search_properties as reins_search
        except ImportError as e:
            return jsonify({'code': 0, 'error': f'REINS client 未可用: {e}'}), 500
        result = reins_search({
            'pref': pref,
            'city': city,
            'property_type': '売マンション',
            'price_min': pmin or None,
            'price_max': pmax or None,
            'walk_min': walk or None,
            'page': page_num,
        })
        if result.get('code') != 1:
            # session expired → 明確 code，前端顯示重新登入，唔好 stealth retry
            if result.get('auth'):
                return jsonify({'code': 401, 'error': result.get('error'), 'auth': True, 'listings': []}), 200
            return jsonify({'code': 0, 'error': result.get('error', 'REINS 搜尋失敗'), 'listings': []}), 502
        # 標記邊啲 reins_id 已喺 DB（供前端顯示「已匯入」）
        listings = result.get('results', [])
        ids = [l.get('reins_id') for l in listings if l.get('reins_id')]
        existing = set()
        if ids:
            conn = get_db()
            q = "SELECT reins_id FROM listings WHERE source='reins' AND reins_id IN (%s)" % ','.join('?' * len(ids))
            existing = {r['reins_id'] for r in conn.execute(q, ids).fetchall()}
            conn.close()
        for l in listings:
            l['already_imported'] = l.get('reins_id') in existing
        return jsonify({
            'code': 1,
            'found': result.get('total_count', 0),
            'listings': listings,
            'source': 'reins',
            'page': result.get('page', 1),
            'page_size': result.get('page_size', 50),
            'total_pages': result.get('total_pages', 1),
            'hit_limit': result.get('hit_limit', False),
        })

    # default: SUUMO（原有行為保留）
    result = suumo_search(city=city, pref=pref, pmin=pmin, pmax=pmax, walk=walk, max_results=max_results)
    return jsonify({'code': 1, 'found': len(result.get('listings', [])), 'listings': result.get('listings', []), 'source': 'suumo'})

# ── Batch Import API ──
@app.route('/api/collection/import', methods=['POST'])
def collection_import():
    """Import selected listings into DB. source='reins' → REINS; default → SUUMO."""
    data = request.get_json(force=True) or {}
    source = data.get('source', 'suumo')

    if source == 'reins':
        return _collection_import_reins(data)
    return _collection_import_suumo(data)


def _collection_import_reins(data):
    """REINS 匯入：items = [{reins_id, drawing_available}, ...]。
    建立 background import job，立即回 job_id（唔阻塞 HTTP）。
    產品批次上限 20 件。concurrency=1 逐件處理。"""
    items = data.get('items', [])
    if not items:
        return jsonify({'code': 0, 'error': 'items required'}), 400
    if len(items) > 20:
        return jsonify({'code': 0, 'error': '每次最多匯入 20 件'}), 400

    from reins_import_jobs import create_job, start_job
    job = create_job(items)
    start_job(job['job_id'])
    return jsonify({'code': 1, 'job_id': job['job_id'], 'total': job['total']})


@app.route('/api/collection/import-status/<job_id>')
def collection_import_status(job_id):
    """查詢 background import job 狀態。"""
    from reins_import_jobs import get_job
    job = get_job(job_id)
    if not job:
        return jsonify({'code': 0, 'error': 'job not found'}), 404
    return jsonify({
        'code': 1,
        'job_id': job['job_id'],
        'status': job['status'],
        'total': job['total'],
        'done_count': job['done_count'],
        'current_index': job['current_index'],
        'error': job.get('error'),
        'items': job['items'],
    })


@app.route('/api/collection/import-resume/<job_id>', methods=['POST'])
def collection_import_resume(job_id):
    """Resume 一個 session_expired / 未完成 job。"""
    from reins_import_jobs import resume_job, get_job
    job = get_job(job_id)
    if not job:
        return jsonify({'code': 0, 'error': 'job not found'}), 404
    resume_job(job_id)
    return jsonify({'code': 1, 'job_id': job_id, 'status': 'running'})


def _collection_import_suumo(data):
    """Import selected SUUMO listings into DB."""
    urls = data.get('urls', [])
    if not urls:
        return jsonify({'code': 0, 'error': 'URLs required'}), 400

    import sys
    results = []
    total = len(urls)
    for i, url in enumerate(urls, 1):
        print(f"[import {i}/{total}] {url}", file=sys.stderr, flush=True)
        try:
            d = scrape_detail(url)
            if not d.get('price') and not d.get('address'):
                results.append({'url': url, 'code': 0, 'error': 'scrape 結果為空（可能俾 SUUMO block 咗）'})
                continue
            lid = import_to_db(d)
            results.append({'url': url, 'code': 1, 'id': lid, 'price': d.get('price'), 'address': d.get('address')})
        except Exception as e:
            print(f"[import {i}/{total}] ERROR: {e}", file=sys.stderr, flush=True)
            results.append({'url': url, 'code': 0, 'error': str(e)})

    imported = len([r for r in results if r['code'] == 1])
    failed = total - imported
    return jsonify({
        'code': 1,
        'imported': imported,
        'failed': failed,
        'total': total,
        'results': results,
        'note': 'SUUMO 有 rate limit，建議每次最多匯入 3 件，每件之間會自動 delay 3 秒',
    })

# ── Scrape API ──
@app.route('/api/scrape', methods=['POST'])
def scrape_listing():
    data = request.get_json(force=True)
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'code': 0, 'error': 'URLは必須です'}), 400
    if 'suumo.jp' not in url:
        return jsonify({'code': 0, 'error': 'SUUMOのURLのみ対応しています'}), 400
    try:
        result = scrape_and_insert(url)
        return jsonify({'code': 1, 'id': result['id'], 'price': result['price'],
                        'address': result['address'], 'photos': result['photos'],
                        'title': result['title'], 'url': '/listing/' + result['id']})
    except Exception as e:
        return jsonify({'code': 0, 'error': str(e)}), 500

# ── All Listings API (no limit) ──
@app.route('/api/listings')
def all_listings():
    conn = get_db()
    results = conn.execute("""
        SELECT * FROM listings WHERE status='published'
        ORDER BY price DESC
    """).fetchall()
    listings = [_row_to_dict(r) for r in results]
    conn.close()

    # Add raw price fields for consistency
    for l in listings:
        if 'price_raw' in l:
            l['price_per_sqm_raw'] = round(l['price_raw'] / l['size_sqm'], 1) if l.get('size_sqm', 0) > 0 else 0

    return jsonify({'count': len(listings), 'listings': listings})

# ── Leads API (akiya_bank preliminary clues) ──
@app.route('/api/listings/leads')
def leads_list():
    conn = get_db()
    results = conn.execute("""
        SELECT * FROM listings WHERE status='lead'
        ORDER BY price DESC
    """).fetchall()
    listings = [_row_to_dict(r) for r in results]
    conn.close()
    return jsonify({'count': len(listings), 'listings': listings})

# ── Detail API ──
@app.route('/listing/<listing_id>')
def listing_page(listing_id):
    return send_from_directory('.', 'listing.html')

@app.route('/api/listing/<listing_id>')
def listing_detail(listing_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    return jsonify(_row_to_dict(row))

@app.route('/api/listing/<listing_id>', methods=['DELETE'])
def delete_listing(listing_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'code': 0, 'error': 'not found'}), 404
    conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    conn.commit()
    conn.close()

    # Backup DB after delete
    try:
        import shutil
        db_path = os.path.join(os.path.dirname(__file__), 'data', 'listings.db')
        backup_dir = os.path.join(os.path.dirname(__file__), 'data', 'backup')
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        shutil.copy2(db_path, os.path.join(backup_dir, f'listings_delete_{ts}.db'))
    except Exception:
        pass

    return jsonify({'code': 1, 'id': listing_id, 'deleted': True})

# ── Upload API ──
@app.route('/api/upload', methods=['POST'])
def upload():
    data = request.get_json(force=True)
    if not data:
        return jsonify({'code': 0, 'error': 'リクエストデータがありません'}), 400

    # Required fields
    address = data.get('address', '').strip()
    try:
        price = int(data.get('price', 0))
    except (TypeError, ValueError):
        return jsonify({'code': 0, 'error': '価格は数値で入力してください'}), 400
    try:
        size_sqm = float(data.get('size_sqm', 0))
    except (TypeError, ValueError):
        return jsonify({'code': 0, 'error': '面積は数値で入力してください'}), 400

    if not address or price <= 0 or size_sqm <= 0:
        return jsonify({'code': 0, 'error': '地址、価格、面積は必須です'}), 400

    # Generate unique ID: UP{timestamp}{4 hex}
    ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    listing_id = f"UP{ts}{uuid.uuid4().hex[:4].upper()}"

    # Dedup: check reins_id
    reins_id = data.get('reins_id', '')
    conn = get_db()
    if reins_id:
        existing = conn.execute("SELECT id FROM listings WHERE reins_id = ? LIMIT 1", (reins_id,)).fetchone()
        if existing:
            conn.close()
            return jsonify({'code': 2, 'id': existing['id'], 'status': 'duplicate', 'message': 'Already exists'})

    # Optional fields with defaults
    station = data.get('station', '')
    walk_min = int(data.get('walk_min', 0) or 0)
    age = int(data.get('age', 0) or 0)
    built_year = int(data.get('built_year', 0) or 0)
    room_layout = data.get('room_layout', '')
    orientation = data.get('orientation', '')
    floor = int(data.get('floor', 0) or 0)
    total_floors = int(data.get('total_floors', 0) or 0)
    structure = data.get('structure', '')
    land_rights = data.get('land_rights', '')
    ptype = data.get('type', 'マンション')
    photos = data.get('photos', [])
    # Handle thumbnail: decode base64 → save to uploads/ → add to photos
    thumbnail = data.get('thumbnail', '')
    if thumbnail and thumbnail.startswith('data:'):
        import base64
        try:
            header, b64data = thumbnail.split(',', 1)
            img_bytes = base64.b64decode(b64data)
            # Determine extension
            ext = 'jpg'
            if 'image/png' in thumbnail: ext = 'png'
            elif 'image/gif' in thumbnail: ext = 'gif'
            elif 'image/webp' in thumbnail: ext = 'webp'
            ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
            fname = f"{ts}_thumb.{ext}"
            thumb_dir = os.path.join(os.path.dirname(__file__), 'uploads', 'thumbs')
            os.makedirs(thumb_dir, exist_ok=True)
            dest = os.path.join(thumb_dir, fname)
            with open(dest, 'wb') as f:
                f.write(img_bytes)
            thumb_url = f'/uploads/thumbs/{fname}'
            if isinstance(photos, list):
                photos = [thumb_url] + photos
            else:
                photos = [thumb_url]
        except Exception:
            pass  # thumb decode failed, continue without
    floorplan_url = data.get('floorplan_url', '')
    agent_id = data.get('agent_id', 'agent_001')

    # Compute price_per_sqm
    price_per_sqm = round(price / size_sqm, 1) if size_sqm > 0 else 0

    # Yield: monthly rent = 0.4% of price, surface yield = (12 * monthly_rent) / price * 100
    monthly_rent = price * 0.004  # 0.4% monthly
    yield_surface = round((monthly_rent * 12 / price) * 100, 1) if price > 0 else 0
    yield_net = round(yield_surface * 0.78, 1)  # 78% of surface

    # Default disaster risk
    disaster = {
        'flood': data.get('disaster_flood', 'low'),
        'earthquake': data.get('disaster_earthquake', 'low'),
        'liquefaction': data.get('disaster_liquefaction', 'low'),
        'tsunami': data.get('disaster_tsunami', 'low'),
    }

    # Always set status='draft' and source='upload' for new uploads
    status = 'draft'
    source = 'upload'

    photos_json = json.dumps(photos, ensure_ascii=False) if isinstance(photos, list) else (photos or '[]')
    ai_copy = data.get('ai_generated_copy', '')
    ai_keywords = data.get('ai_keywords', [])
    keywords_json = json.dumps(ai_keywords, ensure_ascii=False) if isinstance(ai_keywords, list) else (ai_keywords or '[]')
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    conn.execute("""
        INSERT INTO listings (
            id, agent_id, address, station, walk_min, price, price_per_sqm,
            size_sqm, built_year, age, room_layout, orientation, floor, total_floors,
            structure, land_rights, type, yield_surface, yield_net,
            source, photos, floorplan_url, reins_id, ai_generated_copy, ai_keywords,
            disaster_flood, disaster_earthquake, disaster_liquefaction, disaster_tsunami,
            status, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        listing_id, agent_id, address, station, walk_min, price, price_per_sqm,
        size_sqm, built_year, age, room_layout, orientation, floor, total_floors,
        structure, land_rights, ptype, yield_surface, yield_net,
        source, photos_json, floorplan_url, reins_id, ai_copy, keywords_json,
        disaster['flood'], disaster['earthquake'], disaster['liquefaction'], disaster['tsunami'],
        status, now_iso, now_iso
    ))
    conn.commit()
    conn.close()

    return jsonify({'code': 1, 'id': listing_id, 'status': status})

# ── Confirm API (draft → published + price validation + MLIT background check) ──
@app.route('/api/confirm/<listing_id>', methods=['POST'])
def confirm_listing(listing_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'code': 0, 'error': '物件が見つかりません'}), 404
    if row['status'] == 'published':
        conn.close()
        return jsonify({'code': 1, 'id': listing_id, 'status': 'published', 'message': 'already published'})
    
    # Price validation: prevent confirm if price/size mismatch
    # Use raw values from DB (not converted to 万円)
    price = row['price'] or 0
    size_sqm = row['size_sqm'] or 0
    price_per_sqm = row['price_per_sqm'] or 0

    if price > 0 and size_sqm > 0:
        expected_pp_sqm = price / size_sqm
        if expected_pp_sqm > 0:
            if price_per_sqm <= 0:
                conn.close()
                return jsonify({
                    'code': 0, 'error': '價格數字前後矛盾：price_per_sqm為0但price/size_sqm非零，請核對原圖後手動修正',
                    'expected_pp_sqm': round(expected_pp_sqm, 1),
                    'current_pp_sqm': price_per_sqm
                }), 400
            deviation = abs(price_per_sqm - expected_pp_sqm) / expected_pp_sqm
            if deviation > 0.05:  # 5% tolerance
                conn.close()
                return jsonify({
                    'code': 0, 'error': '價格數字前後矛盾：price/size_sqm與price_per_sqm偏差超過5%，請核對原圖後手動修正',
                    'expected_pp_sqm': round(expected_pp_sqm, 1),
                    'current_pp_sqm': price_per_sqm,
                    'deviation': f"{deviation*100:.1f}%"
                }), 400
    
    conn.execute(
        "UPDATE listings SET status='published', updated_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), listing_id)
    )
    conn.commit()

    # Backup DB to backup/ after every confirm
    try:
        import shutil
        db_path = os.path.join(os.path.dirname(__file__), 'data', 'listings.db')
        backup_dir = os.path.join(os.path.dirname(__file__), 'data', 'backup')
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        shutil.copy2(db_path, os.path.join(backup_dir, f'listings_{ts}.db'))
        # Keep only last 50 backups
        backups = sorted(os.listdir(backup_dir))
        for old in backups[:-50]:
            os.remove(os.path.join(backup_dir, old))
    except Exception:
        pass  # backup failure must not block confirm

    conn.close()
    
    # Trigger MLIT API background check (non-blocking)
    t = threading.Thread(target=_mlit_background_check, args=(listing_id,), daemon=True)
    t.start()
    
    return jsonify({'code': 1, 'id': listing_id, 'status': 'published'})


def _mlit_background_check(listing_id):
    """Run MLIT API queries in background after confirm. Rate-limited, non-blocking."""
    try:
        from mlit_client import (
            get_transactions, get_land_use_zone,
            check_disaster_risks, extract_use_district_from_geojson,
            extract_planning_from_transactions
        )
        from geocode_client import geocode
        
        conn = get_db()
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if not row:
            conn.close()
            return
        
        lat = row['latitude'] or 0
        lon = row['longitude'] or 0
        address = row['address'] or ''
        
        # Auto-geocode if no coordinates
        if (not lat or not lon) and address:
            glat, glon = geocode(address)
            if glat and glon:
                lat, lon = glat, glon
                conn.execute("UPDATE listings SET latitude=?, longitude=? WHERE id=?", (lat, lon, listing_id))
                conn.commit()
        
        updates = {}
        reference_data = {}
        checked = False
        
        # 1. XIT001: Transaction data (use area=13 Tokyo + city=13103 Minato-ku)
        try:
            tx_data = get_transactions(
                year="2024", quarter="1", area="13", city="13103",
                price_classification="02"
            )
            if not tx_data.get("error"):
                planning = extract_planning_from_transactions(tx_data)
                reference_data["xkt001"] = planning
                checked = True
        except Exception as e:
            reference_data["xkt001_error"] = str(e)[:200]
        
        # 2. XKT002: Land use zone (if we have coordinates)
        if lat and lon:
            try:
                lu_data = get_land_use_zone(lat, lon)
                if not lu_data.get("error"):
                    use_district, xkt002_cr, xkt002_fr = extract_use_district_from_geojson(lu_data, lat, lon)
                    reference_data["xkt002"] = {
                        "use_district": use_district,
                        "coverage_ratio": xkt002_cr,
                        "floor_area_ratio": xkt002_fr,
                        "features_count": len(lu_data.get("features", []))
                    }
                    if use_district:
                        updates["mlit_use_district"] = use_district
                    if xkt002_cr:
                        updates["mlit_coverage_ratio"] = xkt002_cr
                    if xkt002_fr:
                        updates["mlit_floor_area_ratio"] = xkt002_fr
                    checked = True
            except Exception as e:
                reference_data["xkt002_error"] = str(e)[:200]
            
            # 3. Disaster risk APIs
            try:
                risks = check_disaster_risks(lat, lon)
                reference_data["disaster_risks"] = risks
                updates["mlit_disaster_flood"] = risks.get("flood", "unknown")
                updates["mlit_disaster_high_tide"] = risks.get("high_tide", "unknown")
                updates["mlit_disaster_tsunami"] = risks.get("tsunami", "unknown")
                updates["mlit_disaster_landslide"] = risks.get("landslide", "unknown")
                checked = True
            except Exception as e:
                reference_data["disaster_error"] = str(e)[:200]
        
        if checked:
            updates["market_reference_data"] = json.dumps(reference_data, ensure_ascii=False)
            updates["mlit_checked_at"] = datetime.now(timezone.utc).isoformat()
            
            set_clauses = ", ".join(f"{k}=?" for k in updates)
            values = list(updates.values()) + [listing_id]
            conn.execute(f"UPDATE listings SET {set_clauses} WHERE id=?", values)
            conn.commit()
        
        conn.close()
    except Exception as e:
        print(f"[MLIT bg check error] {listing_id}: {e}", flush=True)


# ── Dashboard stats API ──
@app.route('/api/dashboard')
def dashboard():
    conn = get_db()
    published = conn.execute("SELECT COUNT(*) FROM listings WHERE status='published'").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    draft = conn.execute("SELECT COUNT(*) FROM listings WHERE status='draft'").fetchone()[0]
    leads = conn.execute("SELECT COUNT(*) FROM listings WHERE status='lead'").fetchone()[0]
    samples = conn.execute("SELECT COUNT(*) FROM listings WHERE source='sample'").fetchone()[0]
    uploads = conn.execute("SELECT COUNT(*) FROM listings WHERE source='upload'").fetchone()[0]
    avg_price = conn.execute("SELECT AVG(price) FROM listings WHERE status='published'").fetchone()[0] or 0
    conn.close()
    return jsonify({
        'published': published,
        'total': total,
        'draft': draft,
        'leads': leads,
        'samples': samples,
        'uploads': uploads,
        'avg_price': round(avg_price, 0)
    })

# ── AI Generate Copy API (server-side template generation) ──
@app.route('/api/ai-generate', methods=['POST'])
def ai_generate():
    data = request.get_json(force=True)
    if not data:
        return jsonify({'code': 0, 'error': 'リクエストデータがありません'}), 400

    address = data.get('address', '')
    price = data.get('price', '')
    room_layout = data.get('room_layout', '')
    size_sqm = data.get('size_sqm', '')
    station = data.get('station', '')
    walk_min = data.get('walk_min', '')
    orientation = data.get('orientation', '')
    age = data.get('age', '')
    structure = data.get('structure', '')
    floor = data.get('floor', '')
    total_floors = data.get('total_floors', '')
    land_rights = data.get('land_rights', '')
    ptype = data.get('type', 'マンション')

    # Build rich template-based copy (no LLM API needed)
    copy_ja = _build_copy_ja(address, price, room_layout, size_sqm, station, walk_min, orientation, age, structure, floor, total_floors, land_rights, ptype)
    copy_zh = _build_copy_zh(address, price, room_layout, size_sqm, station, walk_min, orientation, age, structure, floor, total_floors, land_rights, ptype)
    copy_en = _build_copy_en(address, price, room_layout, size_sqm, station, walk_min, orientation, age, structure, floor, total_floors, land_rights, ptype)

    keywords = _build_keywords_rich(address, room_layout, station, structure, age, orientation, ptype, walk_min)

    disclaimer = (
        "⚠️ ※AI生成画像・文章はイメージです。実際の物件とは異なる場合があります。"
        "掲載内容の正確性は仲介者ご自身でご確認ください。"
        "虚偽表示は宅地建物取引業法違反となります。"
    )

    return jsonify({
        'code': 1,
        'copy_ja': copy_ja,
        'copy_zh': copy_zh,
        'copy_en': copy_en,
        'keywords': keywords,
        'disclaimer': disclaimer
    })

def _build_copy_ja(address, price, room_layout, size_sqm, station, walk_min, orientation, age, structure, floor, total_floors, land_rights, ptype):
    """Build a rich Japanese listing copy."""
    lines = []
    # Title line
    title_parts = [address]
    if room_layout:
        title_parts.append(room_layout)
    if size_sqm:
        title_parts.append(f"{size_sqm}㎡")
    lines.append("【" + " ".join(title_parts) + "】")

    # Price
    lines.append(f"💰 価格：{price}万円")

    # Location
    location_parts = []
    if station:
        loc = station
        if walk_min:
            walk = int(walk_min) if isinstance(walk_min, str) else walk_min
            loc += f" 徒歩{walk}分"
        location_parts.append(loc)
    if location_parts:
        lines.append(f"📍 アクセス：{', '.join(location_parts)}")

    # Building info
    building_parts = []
    if ptype:
        building_parts.append(ptype)
    if structure:
        building_parts.append(f"{structure}造")
    if age:
        a = int(age) if isinstance(age, str) else age
        building_parts.append(f"築{a}年")
    if floor and total_floors:
        f = int(floor) if isinstance(floor, str) else floor
        tf = int(total_floors) if isinstance(total_floors, str) else total_floors
        building_parts.append(f"{f}階/{tf}階建")
    if building_parts:
        lines.append(f"🏢 物件概要：{' / '.join(building_parts)}")

    # Features
    feature_parts = []
    if orientation:
        feature_parts.append(f"{orientation}向き")
    if land_rights:
        feature_parts.append(land_rights)
    if feature_parts:
        lines.append(f"✨ 特徴：{'、'.join(feature_parts)}")

    # Summary
    summary_parts = []
    if station and walk_min:
        w = int(walk_min) if isinstance(walk_min, str) else walk_min
        if w <= 3:
            summary_parts.append("駅近の好立地")
        elif w <= 7:
            summary_parts.append("通勤・通学に便利な立地")
    if age:
        a = int(age) if isinstance(age, str) else age
        if a <= 5:
            summary_parts.append("築浅で設備充実")
        elif a <= 15:
            summary_parts.append("適度な築年数で管理良好")
    if size_sqm:
        s = float(size_sqm) if isinstance(size_sqm, str) else size_sqm
        if s >= 70:
            summary_parts.append("広々とした居住空間")
        elif s >= 55:
            summary_parts.append("ゆとりのある間取り")
    if summary_parts:
        lines.append(f"📝 {'。'.join(summary_parts)}。")

    return "\n".join(lines)


def _build_copy_zh(address, price, room_layout, size_sqm, station, walk_min, orientation, age, structure, floor, total_floors, land_rights, ptype):
    """Build a rich Chinese listing copy."""
    lines = []
    # Title
    title_parts = [address]
    if room_layout:
        title_parts.append(room_layout)
    if size_sqm:
        title_parts.append(f"{size_sqm}㎡")
    lines.append("【" + " ".join(title_parts) + "】")

    # Price
    lines.append(f"💰 价格：{price}万日元")

    # Location
    if station:
        loc = station
        if walk_min:
            w = int(walk_min) if isinstance(walk_min, str) else walk_min
            loc += f" 步行{w}分钟"
        lines.append(f"📍 交通：{loc}")

    # Building info
    building_parts = []
    if ptype:
        building_parts.append(ptype)
    if structure:
        building_parts.append(f"{structure}结构")
    if age:
        a = int(age) if isinstance(age, str) else age
        building_parts.append(f"楼龄{a}年")
    if floor and total_floors:
        f = int(floor) if isinstance(floor, str) else floor
        tf = int(total_floors) if isinstance(total_floors, str) else total_floors
        building_parts.append(f"第{f}层/共{tf}层")
    if building_parts:
        lines.append(f"🏢 建筑概况：{' / '.join(building_parts)}")

    # Features
    feature_parts = []
    if orientation:
        orient_map = {'南': '朝南', '南東': '朝东南', '南西': '朝西南', '東': '朝东', '西': '朝西', '北': '朝北', '北東': '朝东北', '北西': '朝西北'}
        feature_parts.append(orient_map.get(orientation, orientation + '朝向'))
    if land_rights:
        rights_map = {'所有権': '永久产权', '借地権': '借地权', '区分所有権': '区分所有权'}
        feature_parts.append(rights_map.get(land_rights, land_rights))
    if feature_parts:
        lines.append(f"✨ 特色：{'、'.join(feature_parts)}")

    # Summary
    summary_parts = []
    if station and walk_min:
        w = int(walk_min) if isinstance(walk_min, str) else walk_min
        if w <= 3:
            summary_parts.append("近车站，交通便利")
        elif w <= 7:
            summary_parts.append("通勤通学方便")
    if age:
        a = int(age) if isinstance(age, str) else age
        if a <= 5:
            summary_parts.append("次新房，设备完善")
        elif a <= 15:
            summary_parts.append("楼龄适中，管理良好")
    if size_sqm:
        s = float(size_sqm) if isinstance(size_sqm, str) else size_sqm
        if s >= 70:
            summary_parts.append("宽敞舒适的居住空间")
        elif s >= 55:
            summary_parts.append("舒适的户型")
    if summary_parts:
        lines.append(f"📝 {'。'.join(summary_parts)}。")

    return "\n".join(lines)


def _build_copy_en(address, price, room_layout, size_sqm, station, walk_min, orientation, age, structure, floor, total_floors, land_rights, ptype):
    """Build a rich English listing copy."""
    lines = []
    # Title
    title_parts = [address]
    if room_layout:
        title_parts.append(room_layout)
    if size_sqm:
        title_parts.append(f"{size_sqm}sqm")
    lines.append("[" + " ".join(title_parts) + "]")

    # Price (price is in 万円, convert to raw yen for display)
    price_yen = int(price) * 10000 if price else 0
    lines.append(f"💰 Price: ¥{price_yen:,}")

    # Location
    if station:
        loc = station
        if walk_min:
            w = int(walk_min) if isinstance(walk_min, str) else walk_min
            loc += f" ({w} min walk)"
        lines.append(f"📍 Access: {loc}")

    # Building info
    building_parts = []
    if ptype:
        building_parts.append(ptype)
    if structure:
        building_parts.append(f"{structure}")
    if age:
        a = int(age) if isinstance(age, str) else age
        building_parts.append(f"{a} years old")
    if floor and total_floors:
        f = int(floor) if isinstance(floor, str) else floor
        tf = int(total_floors) if isinstance(total_floors, str) else total_floors
        building_parts.append(f"Floor {f}/{tf}")
    if building_parts:
        lines.append(f"🏢 Building: {' / '.join(building_parts)}")

    # Features
    feature_parts = []
    if orientation:
        orient_map = {'南': 'South-facing', '南東': 'Southeast-facing', '南西': 'Southwest-facing', '東': 'East-facing', '西': 'West-facing', '北': 'North-facing', '北東': 'Northeast-facing', '北西': 'Northwest-facing'}
        feature_parts.append(orient_map.get(orientation, f'{orientation}-facing'))
    if land_rights:
        rights_map = {'所有権': 'Freehold', '借地権': 'Leasehold', '区分所有権': 'Sectional Ownership'}
        feature_parts.append(rights_map.get(land_rights, land_rights))
    if feature_parts:
        lines.append(f"✨ Features: {', '.join(feature_parts)}")

    # Summary
    summary_parts = []
    if station and walk_min:
        w = int(walk_min) if isinstance(walk_min, str) else walk_min
        if w <= 3:
            summary_parts.append("Excellent location near station")
        elif w <= 7:
            summary_parts.append("Convenient for commuting")
    if age:
        a = int(age) if isinstance(age, str) else age
        if a <= 5:
            summary_parts.append("Nearly new, well-maintained")
        elif a <= 15:
            summary_parts.append("Moderate age, good condition")
    if size_sqm:
        s = float(size_sqm) if isinstance(size_sqm, str) else size_sqm
        if s >= 70:
            summary_parts.append("Spacious living area")
        elif s >= 55:
            summary_parts.append("Comfortable layout")
    if summary_parts:
        lines.append(f"📝 {'. '.join(summary_parts)}.")

    return "\n".join(lines)


def _build_keywords_rich(address, room_layout, station, structure, age, orientation, ptype, walk_min):
    """Generate 5-8 rich keywords for the listing."""
    kw = []
    # Address-based keyword
    if address:
        # Extract ward/city name
        parts = address.replace('東京都', '').replace('区', '区').strip()
        kw.append(parts.split()[0] if parts else address)

    # Layout
    if room_layout:
        kw.append(room_layout)

    # Station proximity
    if station and walk_min:
        w = int(walk_min) if isinstance(walk_min, str) else walk_min
        if w <= 3:
            kw.append('駅近')
        elif w <= 5:
            kw.append('駅徒歩5分以内')
        elif w <= 10:
            kw.append('駅徒歩10分以内')

    # Building age
    if age:
        a = int(age) if isinstance(age, str) else age
        if a <= 5:
            kw.append('築浅')
        elif a <= 10:
            kw.append('築10年以内')
        elif a >= 20:
            kw.append('リノベーション向き')

    # Orientation
    if orientation:
        if '南' in orientation:
            kw.append('南向き')
            kw.append('日当たり良好')

    # Structure
    if structure:
        if 'RC' in structure:
            kw.append('RC造')
        elif 'SRC' in structure:
            kw.append('SRC造')
        elif '木造' in structure or 'W' in structure:
            kw.append('木造')

    # Property type
    if ptype:
        if 'マンション' in ptype:
            kw.append('マンション')
        elif '戸建' in ptype:
            kw.append('戸建て')
        elif 'アパート' in ptype:
            kw.append('アパート')

    # Floor info
    if orientation and '南' in orientation:
        kw.append('陽当り良好')

    # Investment angle
    kw.append('投資用')

    # Deduplicate while preserving order
    seen = set()
    result = []
    for k in kw:
        if k not in seen:
            seen.add(k)
            result.append(k)

    # Ensure 5-8 keywords
    if len(result) < 5:
        fallbacks = ['収益物件', '不動産投資', '都内', '駅近', '好立地']
        for f in fallbacks:
            if f not in seen:
                result.append(f)
                seen.add(f)
                if len(result) >= 5:
                    break

    return result[:8]

# ── Mysok File Upload API ──
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'uploads', 'mysok')

@app.route('/api/mysok-upload', methods=['POST'])
def mysok_upload():
    """Receive multipart/form-data files, save to uploads/mysok/, return paths."""
    if 'files' not in request.files:
        return jsonify({'code': 0, 'error': 'ファイルがありません'}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    saved = []
    files = request.files.getlist('files')

    for f in files:
        if f.filename == '':
            continue
        ts = datetime.now().strftime('%Y%m%d%H%M%S')
        safe_name = f"{ts}_{f.filename}"
        dest = os.path.join(UPLOAD_DIR, safe_name)
        f.save(dest)
        saved.append({
            'original_name': f.filename,
            'saved_path': f'/uploads/mysok/{safe_name}',
            'abs_path': dest,
            'size': os.path.getsize(dest)
        })

    return jsonify({
        'code': 1,
        'files': saved,
        'count': len(saved)
    })

# ── Photo Upload/Delete API (for review/edit page) ──
@app.route('/api/upload-photo/<listing_id>', methods=['POST'])
def upload_photo(listing_id):
    """Upload one photo to a listing, categorized as floorplan or interior."""
    if 'file' not in request.files:
        return jsonify({'code': 0, 'error': 'ファイルがありません'}), 400
    category = request.form.get('category', 'floorplan')  # floorplan or interior
    label = request.form.get('label', '')
    room_label = request.form.get('room_label', '')

    file = request.files['file']
    if file.filename == '':
        return jsonify({'code': 0, 'error': 'empty filename'}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    safe_name = f"{ts}_{file.filename}"
    dest = os.path.join(UPLOAD_DIR, safe_name)
    file.save(dest)
    url = f'/uploads/mysok/{safe_name}'

    conn = get_db()
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'code': 0, 'error': 'not found'}), 404

    col = 'floorplan_images' if category == 'floorplan' else 'interior_photos'
    existing = json.loads(row[col] or '[]')
    entry = {'url': url}
    if category == 'floorplan':
        entry['floor_label'] = label or '未標示'
    else:
        entry['photo_category'] = label or '未分類'
        entry['room_label'] = room_label or ''
    existing.append(entry)

    conn.execute(f"UPDATE listings SET {col}=?, updated_at=? WHERE id=?",
                 (json.dumps(existing, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), listing_id))
    conn.commit()
    conn.close()
    return jsonify({'code': 1, 'url': url, 'entry': entry, 'column': col})

@app.route('/api/delete-photo/<listing_id>', methods=['POST'])
def delete_photo(listing_id):
    """Delete a photo from a listing's floorplan_images or interior_photos."""
    data = request.get_json(force=True)
    category = data.get('category', 'floorplan')
    url = data.get('url', '')
    if not url:
        return jsonify({'code': 0, 'error': 'no url'}), 400

    col = 'floorplan_images' if category == 'floorplan' else 'interior_photos'
    conn = get_db()
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'code': 0, 'error': 'not found'}), 404

    existing = json.loads(row[col] or '[]')
    existing = [e for e in existing if e.get('url') != url]
    conn.execute(f"UPDATE listings SET {col}=?, updated_at=? WHERE id=?",
                 (json.dumps(existing, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), listing_id))
    conn.commit()
    conn.close()
    return jsonify({'code': 1, 'deleted': url, 'remaining': len(existing)})

# Serve uploaded files
@app.route('/uploads/mysok/<path:filename>')
def serve_mysok_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# ── AI Virtual Staging (Ciyuan image generation) ──────────────────────────
STAGED_DIR = os.path.join(os.path.dirname(__file__), 'uploads', 'staged')
CIYUAN_BASE_URL = os.environ.get('CIYUAN_BASE_URL', 'https://api.ciyuan-market.com/api/v1')
CIYUAN_API_KEY = os.environ.get('CIYUAN_API_KEY', '')
CIYUAN_IMAGE_MODEL = os.environ.get('CIYUAN_IMAGE_MODEL', 'gpt-image-2')

STAGING_STYLES = {
    'modern':  {'label': '現代簡約', 'en': 'modern minimalist Japanese interior design, clean lines, neutral tones'},
    'nordic':  {'label': '北歐',     'en': 'Scandinavian nordic style, light wood, white walls, cozy textiles'},
    'wamodern':{'label': '和モダン', 'en': 'Japanese wa-modern style, tatami accents, shoji-inspired details, natural wood'},
    'hotel':   {'label': 'ホテルライク','en': 'luxury hotel-like interior, elegant lighting, premium fabrics'},
}
STAGING_ROOMS = {
    'living':  {'label': '客廳',   'en': 'living room'},
    'bedroom': {'label': '睡房',   'en': 'bedroom'},
    'kitchen': {'label': '廚房',   'en': 'kitchen'},
    'dining':  {'label': '餐廳',   'en': 'dining room'},
    'study':   {'label': '書房',   'en': 'study room'},
    'ldk':     {'label': 'LDK',    'en': 'LDK (combined living-dining-kitchen)'},
}

def _staging_prompt(room_type, style, mode):
    room_en = STAGING_ROOMS.get(room_type, {}).get('en', 'room')
    style_en = STAGING_STYLES.get(style, {}).get('en', 'modern minimalist')
    if mode == 'clear':
        return (
            f"Remove all furniture and movable objects from this {room_en} photo. "
            "Keep the room structure exactly as original: same walls, windows, floor, "
            "ceiling, doors and fixed fixtures, same camera angle and perspective. "
            "Show the empty room with clean bare surfaces, natural lighting, "
            "photorealistic real-estate photography quality."
        )
    # furnish mode
    return (
        f"Virtual home staging for a {room_en}. Preserve the room geometry exactly: "
        "same walls, windows, floor, ceiling, doors and camera perspective as the "
        "original photo. Only add or replace furniture and decor in "
        f"{style_en}. Photorealistic, professional real-estate staging photo, "
        "consistent natural lighting and shadows."
    )

def _burn_cg_notice(image_path):
    """Burn CG/AI-generated notice text into the image file itself (bottom-left)."""
    from PIL import Image, ImageDraw, ImageFont
    notice = "※CG/AI生成イメージ"
    img = Image.open(image_path).convert('RGB')
    w, h = img.size
    font_size = max(18, w // 42)
    font = None
    for cand in ('/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf',
                 '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
        if os.path.exists(cand):
            try:
                font = ImageFont.truetype(cand, font_size)
                break
            except Exception:
                continue
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    bbox = draw.textbbox((0, 0), notice, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = 12, h - th - 16
    pad = 8
    draw.rectangle([x - pad, y - pad, x + tw + pad, y + th + pad], fill=(0, 0, 0, 150))
    draw.text((x, y), notice, font=font, fill=(255, 255, 255, 235))
    img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')
    img.save(image_path, quality=92)

def _fetch_bytes(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def _ciyuan_image_request(prompt, ref_image_url=None, timeout=25):
    """Submit image generation task to Ciyuan. Returns (task_id, None) or (None, error_str)."""
    if not CIYUAN_API_KEY:
        return None, 'CIYUAN_API_KEY 未設定'
    payload = {
        'model': CIYUAN_IMAGE_MODEL,
        'text': prompt,
        'count': 1,
        'ratio': '3:2',
        'imageUrls': [ref_image_url] if ref_image_url else [],
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{CIYUAN_BASE_URL}/image-generations', data=data,
        headers={'Authorization': f'Bearer {CIYUAN_API_KEY}',
                 'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        return None, f'API 連接失敗: {e}'
    if body.get('code') != 200:
        return None, f"API 錯誤: {body.get('message') or body}"
    task_id = (body.get('data') or {}).get('taskId')
    if not task_id:
        return None, f'API 冇回 taskId: {body}'
    return task_id, None

def _ciyuan_poll(task_id, max_wait=150, interval=3):
    """Poll Ciyuan task until success/failed. Returns (image_url, None) or (None, error)."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        req = urllib.request.Request(
            f'{CIYUAN_BASE_URL}/image-generations/{task_id}',
            headers={'Authorization': f'Bearer {CIYUAN_API_KEY}'})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read())
        except Exception as e:
            return None, f'輪詢失敗: {e}'
        d = body.get('data') or {}
        status = d.get('status')
        if status == 'success':
            images = d.get('images')
            if isinstance(images, str):
                try:
                    images = json.loads(images)
                except Exception:
                    images = []
            if images:
                return images[0], None
            return None, '任務成功但冇圖片 URL'
        if status == 'failed':
            return None, f"生成失敗: {d.get('errorMessage') or 'unknown'}"
        time.sleep(interval)
    return None, f'超時（{max_wait}s 內未完成）'

@app.route('/api/staging-generate', methods=['POST'])
def staging_generate():
    """Generate an AI virtual staging image for an interior photo (not yet saved)."""
    data = request.get_json(force=True)
    if not data:
        return jsonify({'code': 0, 'error': 'リクエストデータがありません'}), 400
    listing_id = data.get('listing_id', '')
    photo_url = data.get('photo_url', '')
    room_type = data.get('room_type', 'living')
    style = data.get('style', 'modern')
    mode = data.get('mode', 'furnish')  # furnish | clear

    if not listing_id or not photo_url:
        return jsonify({'code': 0, 'error': 'listing_id 同 photo_url 必填'}), 400
    if mode not in ('furnish', 'clear'):
        return jsonify({'code': 0, 'error': 'mode 必須係 furnish 或 clear'}), 400
    if style not in STAGING_STYLES:
        return jsonify({'code': 0, 'error': '未知風格'}), 400
    if room_type not in STAGING_ROOMS:
        return jsonify({'code': 0, 'error': '未知房間類型'}), 400

    conn = get_db()
    row = conn.execute("SELECT id FROM listings WHERE id = ?", (listing_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'code': 0, 'error': 'not found'}), 404

    prompt = _staging_prompt(room_type, style, mode)
    t0 = time.time()
    task_id, err = _ciyuan_image_request(prompt, ref_image_url=photo_url)
    if err:
        return jsonify({'code': 0, 'error': err, 'prompt': prompt}), 502
    img_url, err = _ciyuan_poll(task_id)
    if err:
        return jsonify({'code': 0, 'error': err, 'prompt': prompt, 'task_id': task_id}), 502

    # Download + burn CG notice + save locally
    os.makedirs(STAGED_DIR, exist_ok=True)
    fname = f"staged_{listing_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}.jpg"
    dest = os.path.join(STAGED_DIR, fname)
    try:
        raw = _fetch_bytes(img_url)
        with open(dest, 'wb') as f:
            f.write(raw)
    except Exception as e:
        return jsonify({'code': 0, 'error': f'下載生成圖失敗: {e}', 'prompt': prompt}), 502
    try:
        _burn_cg_notice(dest)
    except Exception as e:
        print(f"[staging burn-in warn] {e}", flush=True)

    elapsed = round(time.time() - t0, 1)
    local_url = f'/uploads/staged/{fname}'
    return jsonify({
        'code': 1,
        'staged_url': local_url,
        'original_url': photo_url,
        'room_type': room_type,
        'room_label': STAGING_ROOMS[room_type]['label'],
        'style': style,
        'style_label': STAGING_STYLES[style]['label'],
        'mode': mode,
        'elapsed_sec': elapsed,
        'prompt': prompt,
    })

@app.route('/uploads/staged/<path:filename>')
def serve_staged(filename):
    return send_from_directory(STAGED_DIR, filename)

@app.route('/api/staging-accept/<listing_id>', methods=['POST'])
def staging_accept(listing_id):
    """Persist an approved staged image into listing.staged_photos[]."""
    data = request.get_json(force=True)
    if not data:
        return jsonify({'code': 0, 'error': 'no data'}), 400
    staged_url = data.get('staged_url', '')
    original_url = data.get('original_url', '')
    if not staged_url or not original_url:
        return jsonify({'code': 0, 'error': 'staged_url 同 original_url 必填'}), 400

    conn = get_db()
    row = conn.execute("SELECT staged_photos FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'code': 0, 'error': 'not found'}), 404
    existing = json.loads(row['staged_photos'] or '[]')
    entry = {
        'url': staged_url,
        'original_url': original_url,
        'room_type': data.get('room_type', ''),
        'room_label': data.get('room_label', ''),
        'style': data.get('style', ''),
        'style_label': data.get('style_label', ''),
        'mode': data.get('mode', ''),
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    existing.append(entry)
    conn.execute("UPDATE listings SET staged_photos=?, updated_at=? WHERE id=?",
                 (json.dumps(existing, ensure_ascii=False),
                  datetime.now(timezone.utc).isoformat(), listing_id))
    conn.commit()
    conn.close()
    return jsonify({'code': 1, 'entry': entry, 'total': len(existing)})

@app.route('/api/staging-delete/<listing_id>', methods=['POST'])
def staging_delete(listing_id):
    """Remove one staged photo entry from listing.staged_photos[]."""
    data = request.get_json(force=True)
    url = (data or {}).get('url', '')
    if not url:
        return jsonify({'code': 0, 'error': 'no url'}), 400
    conn = get_db()
    row = conn.execute("SELECT staged_photos FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'code': 0, 'error': 'not found'}), 404
    existing = json.loads(row['staged_photos'] or '[]')
    existing = [e for e in existing if e.get('url') != url]
    conn.execute("UPDATE listings SET staged_photos=?, updated_at=? WHERE id=?",
                 (json.dumps(existing, ensure_ascii=False),
                  datetime.now(timezone.utc).isoformat(), listing_id))
    conn.commit()
    conn.close()
    return jsonify({'code': 1, 'deleted': url, 'remaining': len(existing)})

# ── Mysok Parse API ──
DISCLAIMER = (
    "⚠️ ※AI生成画像・文章はイメージです。実際の物件とは異なる場合があります。"
    "虚偽表示は宅地建物取引業法違反となります。"
)

@app.route('/api/mysok-parse', methods=['POST'])
def mysok_parse():
    """Receive structured JSON from client, auto-calculate fields, return full 56-column JSON."""
    data = request.get_json(force=True)
    if not data:
        return jsonify({'code': 0, 'error': 'リクエストデータがありません'}), 400

    # ── Extract core fields ──
    address = data.get('address', '').strip()
    try:
        price = int(data.get('price', 0))
    except (TypeError, ValueError):
        price = 0
    try:
        size_sqm = float(data.get('size_sqm', 0))
    except (TypeError, ValueError):
        size_sqm = 0.0
    try:
        land_area_sqm = float(data.get('land_area_sqm', 0))
    except (TypeError, ValueError):
        land_area_sqm = 0.0

    # ── Auto-calculate: price_per_sqm ──
    price_per_sqm = round(price / size_sqm, 1) if size_sqm > 0 else 0

    # ── Auto-calculate: yield ──
    monthly_rent = price * 0.004
    yield_surface = round((monthly_rent * 12 / price) * 100, 1) if price > 0 else 0
    yield_net = round(yield_surface * 0.78, 1)

    # ── Auto-calculate: land_area_tsubo ──
    land_area_tsubo = round(land_area_sqm / 3.305785, 2) if land_area_sqm > 0 else 0

    # ── Auto-calculate: total_floor_area_tsubo ──
    try:
        total_floor_area_sqm = float(data.get('total_floor_area_sqm', 0))
    except (TypeError, ValueError):
        total_floor_area_sqm = 0.0
    total_floor_area_tsubo = round(total_floor_area_sqm / 3.305785, 2) if total_floor_area_sqm > 0 else 0

    # ── Parse transit_lines ──
    transit_lines = data.get('transit_lines', [])
    if isinstance(transit_lines, str):
        try:
            transit_lines = json.loads(transit_lines)
        except (json.JSONDecodeError):
            transit_lines = []

    # ── Parse floorplan_images ──
    floorplan_images = data.get('floorplan_images', [])
    if isinstance(floorplan_images, str):
        try:
            floorplan_images = json.loads(floorplan_images)
        except (json.JSONDecodeError):
            floorplan_images = []

    # ── Parse interior_photos ──
    interior_photos = data.get('interior_photos', [])
    if isinstance(interior_photos, str):
        try:
            interior_photos = json.loads(interior_photos)
        except (json.JSONDecodeError):
            interior_photos = []

    # ── Parse floor_area_by_level ──
    floor_area_by_level = data.get('floor_area_by_level', [])
    if isinstance(floor_area_by_level, str):
        try:
            floor_area_by_level = json.loads(floor_area_by_level)
        except (json.JSONDecodeError):
            floor_area_by_level = []

    # ── Build full 56-column result ──
    result = {
        # Core (1-5)
        'id': data.get('id', ''),
        'agent_id': data.get('agent_id', 'agent_001'),
        'address': address,
        'station': data.get('station', ''),
        'walk_min': int(data.get('walk_min', 0) or 0),
        # Price (6-8)
        'price': price,
        'price_per_sqm': price_per_sqm,
        'size_sqm': size_sqm,
        # Building (9-13)
        'built_year': int(data.get('built_year', 0) or 0),
        'age': int(data.get('age', 0) or 0),
        'room_layout': data.get('room_layout', ''),
        'orientation': data.get('orientation', ''),
        'floor': int(data.get('floor', 0) or 0),
        # Structure (14-17)
        'total_floors': int(data.get('total_floors', 0) or 0),
        'structure': data.get('structure', ''),
        'land_rights': data.get('land_rights', ''),
        'type': data.get('type', 'マンション'),
        # Yield (18-19)
        'yield_surface': yield_surface,
        'yield_net': yield_net,
        # Source (20)
        'source': 'mysok',
        # Media (21-23)
        'photos': data.get('photos', []),
        'floorplan_url': data.get('floorplan_url', ''),
        'ai_generated_copy': data.get('ai_generated_copy', ''),
        # Keywords (24)
        'ai_keywords': data.get('ai_keywords', []),
        # Disaster (25-28)
        'disaster_flood': data.get('disaster_flood', 'low'),
        'disaster_earthquake': data.get('disaster_earthquake', 'low'),
        'disaster_liquefaction': data.get('disaster_liquefaction', 'low'),
        'disaster_tsunami': data.get('disaster_tsunami', 'low'),
        # Status (29-31)
        'status': 'draft',
        'created_at': '',
        'updated_at': '',
        # v2.0: Transit (32)
        'transit_lines': transit_lines,
        # v2.0: Ownership (33)
        'ownership_type': data.get('ownership_type', ''),
        # v2.0: Land (34-37)
        'land_area_sqm': land_area_sqm,
        'land_area_tsubo': land_area_tsubo,
        'land_category': data.get('land_category', ''),
        'building_coverage_ratio': int(data.get('building_coverage_ratio', 0) or 0),
        # v2.0: Planning (38-41)
        'floor_area_ratio': int(data.get('floor_area_ratio', 0) or 0),
        'city_planning_zone': data.get('city_planning_zone', ''),
        'use_district': data.get('use_district', ''),
        'roof_type': data.get('roof_type', ''),
        # v2.0: Building detail (42-46)
        'floors_above': int(data.get('floors_above', 0) or 0),
        'built_date_full': data.get('built_date_full', ''),
        'total_floor_area_sqm': total_floor_area_sqm,
        'total_floor_area_tsubo': total_floor_area_tsubo,
        'floor_area_by_level': floor_area_by_level,
        # v2.0: Transaction (47-52)
        'current_status': data.get('current_status', ''),
        'handover_timing': data.get('handover_timing', ''),
        'transaction_type': data.get('transaction_type', ''),
        'commission_type': data.get('commission_type', ''),
        'notes_freetext': data.get('notes_freetext', ''),
        # v2.0: Images (53-54)
        'floorplan_images': floorplan_images,
        'interior_photos': interior_photos,
        # v2.0: Agent (55-56)
        'listing_agent_name': data.get('listing_agent_name', ''),
        'license_number': data.get('license_number', ''),
        'brokerage_type': data.get('brokerage_type', ''),
    }

    return jsonify({
        'code': 1,
        'data': result,
        'disclaimer': DISCLAIMER,
        'auto_calculated': {
            'price_per_sqm': price_per_sqm,
            'yield_surface': yield_surface,
            'yield_net': yield_net,
            'land_area_tsubo': land_area_tsubo,
            'total_floor_area_tsubo': total_floor_area_tsubo,
        }
    })


# ── Mysok Parse-Table API (accepts pre-parsed or image_path, auto-calculates) ──
@app.route('/api/mysok-parse-table', methods=['POST'])
def mysok_parse_table():
    """Accept structured data or image_path, auto-calculate derived fields."""
    data = request.get_json(force=True)
    if not data:
        return jsonify({'code': 0, 'error': 'no data'}), 400
    
    # If image_path provided, we can't call vision model from server
    # Instead, return a signal to the frontend to use Hermes vision_analyze
    if 'image_path' in data and not data.get('address'):
        return jsonify({
            'code': 2, 
            'needs_vision': True,
            'image_path': data['image_path'],
            'message': 'Server cannot call vision model. Use Hermes vision_analyze tool.'
        })

    # Parse and auto-calculate from structured data
    parsed = dict(data)
    price = int(parsed.get('price', 0) or 0)
    size_sqm = float(parsed.get('size_sqm', 0) or 0)
    parsed['price_per_sqm'] = round(price / size_sqm, 1) if size_sqm > 0 else 0
    parsed['type'] = parsed.get('type', 'マンション')
    return jsonify({'code': 1, 'data': parsed})

# ── Mysok Parse-Floorplan API ──
@app.route('/api/mysok-parse-floorplan', methods=['POST'])
def mysok_parse_floorplan():
    """Accept floorplan data, return structured."""
    data = request.get_json(force=True)
    if not data:
        return jsonify({'code': 0, 'error': 'no data'}), 400
    if 'image_path' in data and not data.get('room_layout'):
        return jsonify({'code': 2, 'needs_vision': True, 'image_path': data['image_path']})
    return jsonify({'code': 1, 'data': dict(data)})

# ── Mysok Import API ──
@app.route('/api/mysok-import', methods=['POST'])
def mysok_import():
    """Receive full 56-column JSON, write to listings table as draft, source='mysok'."""
    data = request.get_json(force=True)
    if not data:
        return jsonify({'code': 0, 'error': 'リクエストデータがありません'}), 400

    address = data.get('address', '').strip()
    try:
        price = int(data.get('price', 0))
    except (TypeError, ValueError):
        price = 0
    try:
        size_sqm = float(data.get('size_sqm', 0))
    except (TypeError, ValueError):
        size_sqm = 0.0

    if not address or price <= 0 or size_sqm <= 0:
        return jsonify({'code': 0, 'error': '地址、価格、面積は必須です'}), 400

    # Generate unique ID
    ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    listing_id = data.get('id') or f"MK{ts}{uuid.uuid4().hex[:4].upper()}"

    now_iso = datetime.now(timezone.utc).isoformat()

    def _json_dump(val):
        if isinstance(val, (list, dict)):
            return json.dumps(val, ensure_ascii=False)
        return str(val) if val else '[]'

    conn = get_db()
    conn.execute("""
        INSERT INTO listings (
            id, agent_id, address, station, walk_min, price, price_per_sqm,
            size_sqm, built_year, age, room_layout, orientation, floor, total_floors,
            structure, land_rights, type, yield_surface, yield_net,
            source, photos, floorplan_url, ai_generated_copy, ai_keywords,
            disaster_flood, disaster_earthquake, disaster_liquefaction, disaster_tsunami,
            status, created_at, updated_at,
            transit_lines, ownership_type, land_area_sqm, land_area_tsubo,
            land_category, building_coverage_ratio, floor_area_ratio,
            city_planning_zone, use_district, roof_type, floors_above,
            built_date_full, total_floor_area_sqm, total_floor_area_tsubo,
            floor_area_by_level, current_status, handover_timing,
            transaction_type, commission_type, notes_freetext,
            floorplan_images, interior_photos,
            listing_agent_name, license_number, brokerage_type
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
    """, (
        listing_id,
        data.get('agent_id', 'agent_001'),
        address,
        data.get('station', ''),
        int(data.get('walk_min', 0) or 0),
        price,
        float(data.get('price_per_sqm', 0)) or round(price / size_sqm, 1) if size_sqm > 0 else 0,
        size_sqm,
        int(data.get('built_year', 0) or 0),
        int(data.get('age', 0) or 0),
        data.get('room_layout', ''),
        data.get('orientation', ''),
        int(data.get('floor', 0) or 0),
        int(data.get('total_floors', 0) or 0),
        data.get('structure', ''),
        data.get('land_rights', ''),
        data.get('type', 'マンション'),
        float(data.get('yield_surface', 0)),
        float(data.get('yield_net', 0)),
        'mysok',
        _json_dump(data.get('photos', [])),
        data.get('floorplan_url', ''),
        data.get('ai_generated_copy', ''),
        _json_dump(data.get('ai_keywords', [])),
        data.get('disaster_flood', 'low'),
        data.get('disaster_earthquake', 'low'),
        data.get('disaster_liquefaction', 'low'),
        data.get('disaster_tsunami', 'low'),
        'draft',
        now_iso,
        now_iso,
        # v2.0 columns
        _json_dump(data.get('transit_lines', [])),
        data.get('ownership_type', ''),
        float(data.get('land_area_sqm', 0)),
        float(data.get('land_area_tsubo', 0)),
        data.get('land_category', ''),
        int(data.get('building_coverage_ratio', 0) or 0),
        int(data.get('floor_area_ratio', 0) or 0),
        data.get('city_planning_zone', ''),
        data.get('use_district', ''),
        data.get('roof_type', ''),
        int(data.get('floors_above', 0) or 0),
        data.get('built_date_full', ''),
        float(data.get('total_floor_area_sqm', 0)),
        float(data.get('total_floor_area_tsubo', 0)),
        _json_dump(data.get('floor_area_by_level', [])),
        data.get('current_status', ''),
        data.get('handover_timing', ''),
        data.get('transaction_type', ''),
        data.get('commission_type', ''),
        data.get('notes_freetext', ''),
        _json_dump(data.get('floorplan_images', [])),
        _json_dump(data.get('interior_photos', [])),
        data.get('listing_agent_name', ''),
        data.get('license_number', ''),
        data.get('brokerage_type', ''),
    ))
    conn.commit()
    conn.close()

    return jsonify({'code': 1, 'id': listing_id, 'status': 'draft', 'source': 'mysok'})


# ── Upload V2 API (extended 56-column support) ──
@app.route('/api/upload-v2', methods=['POST'])
def upload_v2():
    """Extended upload endpoint supporting all 56 columns (backward compatible)."""
    data = request.get_json(force=True)
    if not data:
        return jsonify({'code': 0, 'error': 'リクエストデータがありません'}), 400

    address = data.get('address', '').strip()
    try:
        price = int(data.get('price', 0))
    except (TypeError, ValueError):
        return jsonify({'code': 0, 'error': '価格は数値で入力してください'}), 400
    try:
        size_sqm = float(data.get('size_sqm', 0))
    except (TypeError, ValueError):
        return jsonify({'code': 0, 'error': '面積は数値で入力してください'}), 400

    if not address or price <= 0 or size_sqm <= 0:
        return jsonify({'code': 0, 'error': '地址、価格、面積は必須です'}), 400

    ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    listing_id = f"UP{ts}{uuid.uuid4().hex[:4].upper()}"

    def _jd(val):
        if isinstance(val, (list, dict)):
            return json.dumps(val, ensure_ascii=False)
        return str(val) if val else '[]'

    # Compute derived fields
    price_per_sqm = round(price / size_sqm, 1) if size_sqm > 0 else 0
    monthly_rent = price * 0.004
    yield_surface = round((monthly_rent * 12 / price) * 100, 1) if price > 0 else 0
    yield_net = round(yield_surface * 0.78, 1)

    now_iso = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    conn.execute("""
        INSERT INTO listings (
            id, agent_id, address, station, walk_min, price, price_per_sqm,
            size_sqm, built_year, age, room_layout, orientation, floor, total_floors,
            structure, land_rights, type, yield_surface, yield_net,
            source, photos, floorplan_url, ai_generated_copy, ai_keywords,
            disaster_flood, disaster_earthquake, disaster_liquefaction, disaster_tsunami,
            status, created_at, updated_at,
            transit_lines, ownership_type, land_area_sqm, land_area_tsubo,
            land_category, building_coverage_ratio, floor_area_ratio,
            city_planning_zone, use_district, roof_type, floors_above,
            built_date_full, total_floor_area_sqm, total_floor_area_tsubo,
            floor_area_by_level, current_status, handover_timing,
            transaction_type, commission_type, notes_freetext,
            floorplan_images, interior_photos,
            listing_agent_name, license_number, brokerage_type
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
    """, (
        listing_id,
        data.get('agent_id', 'agent_001'),
        address,
        data.get('station', ''),
        int(data.get('walk_min', 0) or 0),
        price,
        price_per_sqm,
        size_sqm,
        int(data.get('built_year', 0) or 0),
        int(data.get('age', 0) or 0),
        data.get('room_layout', ''),
        data.get('orientation', ''),
        int(data.get('floor', 0) or 0),
        int(data.get('total_floors', 0) or 0),
        data.get('structure', ''),
        data.get('land_rights', ''),
        data.get('type', 'マンション'),
        yield_surface,
        yield_net,
        data.get('source', 'upload'),
        _jd(data.get('photos', [])),
        data.get('floorplan_url', ''),
        data.get('ai_generated_copy', ''),
        _jd(data.get('ai_keywords', [])),
        data.get('disaster_flood', 'low'),
        data.get('disaster_earthquake', 'low'),
        data.get('disaster_liquefaction', 'low'),
        data.get('disaster_tsunami', 'low'),
        'draft',
        now_iso,
        now_iso,
        # v2.0 columns
        _jd(data.get('transit_lines', [])),
        data.get('ownership_type', ''),
        float(data.get('land_area_sqm', 0)),
        float(data.get('land_area_tsubo', 0)),
        data.get('land_category', ''),
        int(data.get('building_coverage_ratio', 0) or 0),
        int(data.get('floor_area_ratio', 0) or 0),
        data.get('city_planning_zone', ''),
        data.get('use_district', ''),
        data.get('roof_type', ''),
        int(data.get('floors_above', 0) or 0),
        data.get('built_date_full', ''),
        float(data.get('total_floor_area_sqm', 0)),
        float(data.get('total_floor_area_tsubo', 0)),
        _jd(data.get('floor_area_by_level', [])),
        data.get('current_status', ''),
        data.get('handover_timing', ''),
        data.get('transaction_type', ''),
        data.get('commission_type', ''),
        data.get('notes_freetext', ''),
        _jd(data.get('floorplan_images', [])),
        _jd(data.get('interior_photos', [])),
        data.get('listing_agent_name', ''),
        data.get('license_number', ''),
        data.get('brokerage_type', ''),
    ))
    conn.commit()
    conn.close()

    return jsonify({'code': 1, 'id': listing_id, 'status': 'draft', 'source': data.get('source', 'upload')})


# ── Helper ──
def _row_to_dict(row):
    d = dict(row)

    # All JSON columns that need parsing
    json_columns = [
        'photos',
        'ai_keywords',
        'transit_lines',
        'floor_area_by_level',
        'floorplan_images',
        'interior_photos',
        'staged_photos',
        'market_reference_data',
    ]

    for col in json_columns:
        val = d.get(col, '{}' if col == 'market_reference_data' else '[]')
        if isinstance(val, str):
            try:
                d[col] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                d[col] = {} if col == 'market_reference_data' else []
        elif val is None:
            d[col] = {} if col == 'market_reference_data' else []

    # Normalize price to 万円 (some uploads store raw yen, e.g. 65000000)
    # Threshold 10M: any price >10,000,000 is raw yen, not 万円
    # (10M 万円 = 1,000億円, no single property costs this much)
    # NOTE: keep raw value for calculations; convert only for display
    if 'price' in d and d['price'] > 10000000:
        d['price_raw'] = d['price']  # preserve raw yen for calculations
        d['price'] = d['price'] // 10000

        # Recalculate price_per_sqm from raw values to ensure consistency
        if d.get('size_sqm') and d['size_sqm'] > 0:
            d['price_per_sqm_raw'] = round(d['price_raw'] / d['size_sqm'], 1)

    return d

# ── Route planning (Google Maps Directions API) ──
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
_route_cache = {}
ROUTE_CACHE_TTL = 21600  # 6 hours

def _decode_polyline(encoded):
    """Decode Google's encoded polyline into list of (lat, lng)."""
    points = []
    index, lat, lng = 0, 0, 0
    while index < len(encoded):
        for coord in ('lat', 'lng'):
            result = shift = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if (result & 1) else (result >> 1)
            if coord == 'lat':
                lat += d
            else:
                lng += d
        points.append((lat / 1e5, lng / 1e5))
    return points

def _route_cache_key(origin_lat, origin_lon, ids, departure, traffic_model):
    return f"{origin_lat:.5f},{origin_lon:.5f}|{','.join(sorted(ids))}|{departure}|{traffic_model}"

def _route_cache_get(key):
    e = _route_cache.get(key)
    if e and time.time() - e["ts"] < ROUTE_CACHE_TTL:
        return e["data"]
    return None

def _route_cache_set(key, data):
    if len(_route_cache) > 200:
        cutoff = time.time() - ROUTE_CACHE_TTL
        for k in [k for k, v in _route_cache.items() if v["ts"] < cutoff]:
            _route_cache.pop(k, None)
    _route_cache[key] = {"ts": time.time(), "data": data}

@app.route('/api/route-geocode')
def route_geocode():
    """Geocode a free-text origin address (GSI) for the route planner."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({"code": 0, "error": "q 必填"}), 400
    try:
        from geocode_client import geocode
        lat, lon = geocode(q)
    except Exception as e:
        return jsonify({"code": 0, "error": f"geocode 失敗: {e}"}), 502
    if not lat or not lon:
        return jsonify({"code": 0, "error": "搵唔到呢個地址嘅座標，試下詳細啲（例如 東京都千代田区丸の内1-1）"}), 404
    return jsonify({"code": 1, "lat": lat, "lon": lon, "query": q})

@app.route('/api/route-plan', methods=['POST'])
def route_plan():
    """
    Input: {"listing_ids": [...], "origin": {"lat":..,"lon":..,"label":..},
            "departure_time": "now" | ISO, "traffic_model": "best_guess"}
    Output: suggested visit order, per-leg duration with traffic, total, route polyline.
    Uses Google Maps Directions API (waypoints optimize:true + traffic_model).
    """
    data = request.get_json(force=True) or {}
    listing_ids = data.get("listing_ids", [])
    origin = data.get("origin") or {}
    origin_lat, origin_lon = origin.get("lat"), origin.get("lon")
    origin_label = origin.get("label") or "出發點"
    departure_time = data.get("departure_time", "now")
    traffic_model = data.get("traffic_model", "best_guess")

    if not isinstance(listing_ids, list) or len(listing_ids) < 1:
        return jsonify({"code": 0, "error": "需要至少 1 個 listing ID"}), 400
    if not origin_lat or not origin_lon:
        return jsonify({"code": 0, "error": "需要出發點座標（origin.lat / origin.lon）"}), 400

    conn = get_db()
    placeholders = ",".join("?" * len(listing_ids))
    rows = conn.execute(
        f"SELECT id, address, price, latitude, longitude FROM listings WHERE id IN ({placeholders})",
        listing_ids,
    ).fetchall()
    conn.close()

    by_id = {r["id"]: dict(r) for r in rows}
    stops = []
    for lid in listing_ids:
        r = by_id.get(lid)
        if not r:
            return jsonify({"code": 0, "error": f"搵唔到 listing: {lid}"}), 404
        lat, lon = r.get("latitude") or 0, r.get("longitude") or 0
        if not lat or not lon:
            return jsonify({"code": 0, "error": f"listing {lid} 冇座標，請先 geocode"}), 400
        stops.append({"id": lid, "address": r["address"], "price": r["price"], "lat": lat, "lon": lon})

    if not GOOGLE_MAPS_API_KEY:
        return jsonify({"code": 0, "error": "GOOGLE_MAPS_API_KEY 未設定（Johnny 要喺 Google Cloud Console 開 Directions API）"}), 503

    # Cache check — 同一組出發點+listing+出發時間 bucket 6h 內唔重複收費 call
    dep_bucket = "now" if departure_time == "now" else departure_time[:13]  # hour bucket
    ck = _route_cache_key(origin_lat, origin_lon, listing_ids, dep_bucket, traffic_model)
    cached = _route_cache_get(ck)
    if cached:
        cached["cache_hit"] = True
        return jsonify(cached)

    # Build Directions request
    # destination 係最後一個 stop；waypoints 淨係包前面嗰啲（避免 destination 重複做 waypoint 產生 0km 假 leg）
    origin_str = f"{origin_lat},{origin_lon}"
    dest_str = f"{stops[-1]['lat']},{stops[-1]['lon']}"
    middle = stops[:-1]
    wp = "|".join(f"{s['lat']},{s['lon']}" for s in middle)
    wp_param = f"waypoints=optimize:true|{wp}" if middle else ""
    params = [
        f"origin={origin_str}",
        f"destination={dest_str}",
        wp_param,
        "mode=driving",
        f"traffic_model={traffic_model}",
        f"language=ja",
        f"key={GOOGLE_MAPS_API_KEY}",
    ]
    params = [p for p in params if p]
    if departure_time == "now":
        params.append("departure_time=now")
    else:
        try:
            ts = int(datetime.fromisoformat(departure_time.replace('Z', '+00:00')).timestamp())
            params.append(f"departure_time={ts}")
        except Exception:
            params.append("departure_time=now")

    url = "https://maps.googleapis.com/maps/api/directions/json?" + "&".join(params)
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            g = json.loads(resp.read())
    except Exception as e:
        return jsonify({"code": 0, "error": f"Google Directions 呼叫失敗: {e}"}), 502

    if g.get("status") != "OK" or not g.get("routes"):
        return jsonify({"code": 0, "error": f"Google Directions 錯誤: {g.get('status')} {g.get('error_message','')}"}), 502

    route = g["routes"][0]
    wp_order = route.get("waypoint_order", list(range(len(middle))))
    # wp_order index 返入 middle（waypoints）；destination 永遠排最後
    ordered_stops = [middle[i] for i in wp_order] + [stops[-1]]

    # Build visit order: seq 0 = 出發點, then optimized waypoints, destination last
    order = [{"seq": 0, "id": "__origin__", "address": origin_label, "price": 0,
              "lat": origin_lat, "lon": origin_lon, "is_origin": True}]
    for i, s in enumerate(ordered_stops):
        order.append({"seq": i + 1, "id": s["id"], "address": s["address"], "price": s["price"],
                      "lat": s["lat"], "lon": s["lon"]})

    legs = []
    for i, leg in enumerate(route.get("legs", [])):
        dur_traffic = (leg.get("duration_in_traffic") or {}).get("value")
        legs.append({
            "from_seq": i,
            "to_seq": i + 1,
            "from_addr": leg.get("start_address", ""),
            "to_addr": leg.get("end_address", ""),
            "duration_sec": leg.get("duration", {}).get("value", 0),
            "duration_min": round(leg.get("duration", {}).get("value", 0) / 60, 1),
            "duration_traffic_sec": dur_traffic,
            "duration_traffic_min": round(dur_traffic / 60, 1) if dur_traffic else None,
            "distance_m": leg.get("distance", {}).get("value", 0),
            "distance_km": round(leg.get("distance", {}).get("value", 0) / 1000, 1),
        })

    total_traffic_sec = sum(l["duration_traffic_sec"] or l["duration_sec"] for l in legs)
    overview = route.get("overview_polyline", {}).get("points", "")
    coords = _decode_polyline(overview) if overview else []
    geometry = {"type": "LineString", "coordinates": [[lng, lat] for lat, lng in coords]}

    result = {
        "code": 1,
        "provider": "google_directions",
        "traffic_included": True,
        "traffic_model": traffic_model,
        "departure_time": departure_time,
        "order": order,
        "legs": legs,
        "total_duration_min": round(total_traffic_sec / 60, 1),
        "total_distance_km": round(sum(l["distance_m"] for l in legs) / 1000, 1),
        "geometry": geometry,
        "cache_hit": False,
    }
    _route_cache_set(ck, result)
    return jsonify(result)

# ── 市場情報 panel API（讀情報官報告 + 地產新聞 RSS）──
import glob as _glob
import re as _re

_NEWS_SOURCES = [
    ("不動産テック協会", "https://retechjapan.org/feed/"),
    ("Yahoo!経済", "https://news.yahoo.co.jp/rss/categories/business.xml"),
]

def _parse_rss_titles(xml_text, limit=5):
    """Extract <title> from RSS/Atom items (skip channel title)."""
    # RSS: <item><title>... ; Atom: <entry><title>...
    items = _re.findall(r'<item[^>]*>.*?</item>', xml_text, _re.DOTALL) or \
            _re.findall(r'<entry[^>]*>.*?</entry>', xml_text, _re.DOTALL)
    titles = []
    for it in items:
        m = _re.search(r'<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', it, _re.DOTALL)
        if m:
            t = m.group(1).strip()
            t = _re.sub(r'<[^>]+>', '', t)  # strip any inner tags
            if t:
                titles.append(t)
        if len(titles) >= limit:
            break
    return titles

@app.route('/api/market-news')
def market_news():
    """
    首頁市場情報 panel 用：
    - highlights: 情報官最新報告嘅日本房地產重點（讀 intel-*.md 入面嘅房產 section）
    - news: 地產相關新聞標題（RSS，2 個 source）
    - updated_at: 最新情報官報告時間
    """
    # 1. 情報官報告房產重點
    highlights = []
    updated_at = ''
    intel_files = sorted(_glob.glob('/home/ubuntu/ai-team/outputs/intel-*.md'), reverse=True)
    if intel_files:
        latest = intel_files[0]
        updated_at = _re.search(r'intel-(\d{4}-\d{2}-\d{2}-\d{4})', latest).group(1) if _re.search(r'intel-(\d{4}-\d{2}-\d{2}-\d{4})', latest) else ''
        try:
            with open(latest, encoding='utf-8') as f:
                text = f.read()
            # 情報官報告入面嘅日本房地產 section 可能係 ## 或 ### 級別，標題含「日本房地產」
            m = _re.search(r'#{2,3}\s*\d*\.?\s*日本房地[產产][^\n]*\n(.*?)(?=\n#{2,3}\s|\Z)', text, _re.DOTALL)
            if m:
                body = m.group(1)
                # 拆句：每句做一個 bullet；清走 markdown 符號同分隔線
                sents = []
                for s in _re.split(r'[。\n]', body):
                    s = s.strip()
                    s = _re.sub(r'^[-*═\s]+', '', s)  # 開頭嘅 -、*、═
                    s = _re.sub(r'\*\*', '', s)       # bold 符號
                    s = s.strip()
                    if len(s) > 15 and not set(s) <= set('═─-='):
                        sents.append(s)
                highlights = sents[:5]
        except Exception:
            pass

    # 2. 地產新聞 RSS
    news = []
    for source_name, url in _NEWS_SOURCES:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_text = resp.read().decode('utf-8', errors='ignore')
            for title in _parse_rss_titles(xml_text, limit=4):
                news.append({'source': source_name, 'title': title})
        except Exception as e:
            print(f"[market-news] {source_name} fetch failed: {e}", flush=True)

    return jsonify({
        'code': 1,
        'updated_at': updated_at,
        'highlights': highlights,
        'news': news,
    })

# ── GeoJSON endpoint (published listings as points) ──
@app.route('/api/listings/geojson')
def listings_geojson():
    conn = get_db()
    rows = conn.execute("""
        SELECT id, address, price, station, walk_min, room_layout, type, source,
               latitude, longitude, mlit_use_district
        FROM listings WHERE status='published' AND latitude != 0
    """).fetchall()
    conn.close()
    features = []
    for r in rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r['longitude'], r['latitude']]},
            "properties": {
                "id": r['id'], "address": r['address'],
                "price": r['price'] // 10000 if r['price'] > 10000000 else r['price'],
                "station": r['station'] or '', "walk_min": r['walk_min'] or 0,
                "room_layout": r['room_layout'] or '', "type": r['type'] or '',
                "source": r['source'] or '',
                "mlit_use_district": r['mlit_use_district'] or '',
            }
        })
    return jsonify({"type": "FeatureCollection", "features": features})

# ── Map tile proxy (MLIT layer → PBF tile, with 6h cache) ──
_tile_cache = {}

LAYER_TO_API = {
    "zoning": "XKT002",
    "flood": "XKT026",
    "storm_surge": "XKT027",
    "tsunami": "XKT028",
    "landslide": "XKT029",
}

@app.route('/api/map-tile/<layer>/<int:z>/<int:x>/<int:y>')
def map_tile_proxy(layer, z, x, y):
    api_id = LAYER_TO_API.get(layer)
    if not api_id:
        return jsonify({"error": "unknown layer"}), 400

    cache_key = f"{layer}:{z}:{x}:{y}"
    now = time.time()

    if cache_key in _tile_cache:
        entry = _tile_cache[cache_key]
        if now - entry["ts"] < 21600:  # 6 hours
            return entry["data"], 200, {"Content-Type": "application/vnd.mapbox-vector-tile", "Cache-Hit": "true"}

    api_key = os.environ.get("REINFOLIB_API_KEY", "")
    if not api_key:
        return jsonify({"error": "no API key"}), 500

    url = f"https://www.reinfolib.mlit.go.jp/ex-api/external/{api_id}?response_format=geojson&z={z}&x={x}&y={y}"
    req = urllib.request.Request(url)
    req.add_header("Ocp-Apim-Subscription-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    _tile_cache[cache_key] = {"ts": now, "data": data}
    # Evict old entries if cache too large
    if len(_tile_cache) > 2000:
        cutoff = now - 21600
        _tile_cache.clear()
        # Only keep recent entries
        _tile_cache[cache_key] = {"ts": now, "data": data}

    return data, 200, {"Content-Type": "application/vnd.mapbox-vector-tile", "Cache-Hit": "false"}

# ── Main ──
if __name__ == '__main__':
    init_db()
    print("🚀 Johnny AI Platform running on http://0.0.0.0:8900")
    app.run(host='0.0.0.0', port=8900, debug=False)