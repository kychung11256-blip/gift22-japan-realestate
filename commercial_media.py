"""Local-only derivatives; approval binds source and derivative bytes."""
import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit, unquote
from flask import jsonify, request, abort, send_file
from PIL import Image
import db
import authz as az
ROOT = Path(__file__).resolve().parent
UPLOADS = ROOT / 'uploads'
FIELDS = ('photos','interior_photos','floorplan_images','staged_photos')

def canonical_source(url):
    if isinstance(url,dict): url=url.get('url')
    if not isinstance(url,str): return None
    parsed=urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment: return None
    path=unquote(parsed.path)
    if not path.startswith('/uploads/') or '\\' in path: return None
    resolved=(ROOT/path.lstrip('/')).resolve()
    if not resolved.is_relative_to(UPLOADS.resolve()) or not resolved.is_file(): return None
    if resolved.is_relative_to((UPLOADS/'customer_safe').resolve()): return None
    return resolved

def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def derivative_path(row):
    p=(ROOT/(row.get('safe_url') or '').lstrip('/')).resolve()
    return p if p.is_relative_to((UPLOADS/'customer_safe').resolve()) and p.is_file() else None

def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS media_assets(
      asset_id TEXT PRIMARY KEY, listing_id TEXT NOT NULL, original_url TEXT NOT NULL,
      canonical_source TEXT NOT NULL, category TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'needs_review',
      safe_url TEXT, source_fingerprint TEXT, derivative_fingerprint TEXT,
      fingerprint TEXT, reviewed_fingerprint TEXT, approved_source_fingerprint TEXT,
      approved_derivative_fingerprint TEXT, crop_bottom_pct REAL NOT NULL DEFAULT 15,
      reviewed_by TEXT, reviewed_at TEXT, updated_at TEXT DEFAULT (datetime('now')),
      UNIQUE(listing_id, canonical_source))''')
    columns={r[1] for r in conn.execute('PRAGMA table_info(media_assets)')}
    for name,definition in {'canonical_source':'TEXT','source_fingerprint':'TEXT','derivative_fingerprint':'TEXT','fingerprint':'TEXT','reviewed_fingerprint':'TEXT','approved_source_fingerprint':'TEXT','approved_derivative_fingerprint':'TEXT','crop_bottom_pct':'REAL DEFAULT 15','reviewed_by':'TEXT','reviewed_at':'TEXT'}.items():
        if name not in columns: conn.execute('ALTER TABLE media_assets ADD COLUMN '+name+' '+definition)
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS media_canonical_unique ON media_assets(listing_id,canonical_source)')

def generate_asset(conn, listing_id, source_url, category='photo', crop_bottom_pct=15):
    if isinstance(source_url,dict): source_url=source_url.get('url')
    source=canonical_source(source_url)
    if not source: raise ValueError('local source missing or external')
    pct=float(crop_bottom_pct)
    if not 0<=pct<=50: raise ValueError('crop_bottom_pct must be 0..50')
    row=conn.execute('SELECT * FROM media_assets WHERE listing_id=? AND canonical_source=?',(listing_id,str(source))).fetchone()
    asset_id=row['asset_id'] if row else uuid.uuid4().hex
    directory=UPLOADS/'customer_safe'; directory.mkdir(parents=True,exist_ok=True)
    # New filename on every generation, including identical-source recrops.
    target=directory/(asset_id+'-'+uuid.uuid4().hex+'.jpg')
    with Image.open(source) as image:
        image=image.convert('RGB'); image=image.crop((0,0,image.width,max(1,round(image.height*(1-pct/100)))))
        image.save(target,'JPEG',quality=92)
    sf=fingerprint(source); df=fingerprint(target); combined=hashlib.sha256((sf+':'+df).encode()).hexdigest()
    url='/uploads/customer_safe/'+target.name
    conn.execute('''INSERT INTO media_assets(asset_id,listing_id,original_url,canonical_source,category,safe_url,source_fingerprint,derivative_fingerprint,fingerprint,crop_bottom_pct,visibility)
      VALUES (?,?,?,?,?,?,?,?,?,?,'needs_review') ON CONFLICT(listing_id,canonical_source) DO UPDATE SET
      category=excluded.category,safe_url=excluded.safe_url,source_fingerprint=excluded.source_fingerprint,
      derivative_fingerprint=excluded.derivative_fingerprint,fingerprint=excluded.fingerprint,crop_bottom_pct=excluded.crop_bottom_pct,
      visibility='needs_review',reviewed_fingerprint=NULL,approved_source_fingerprint=NULL,approved_derivative_fingerprint=NULL,
      reviewed_by=NULL,reviewed_at=NULL,updated_at=datetime('now')''',
      (asset_id,listing_id,source_url,str(source),category,url,sf,df,combined,pct))
    return dict(conn.execute('SELECT * FROM media_assets WHERE asset_id=?',(asset_id,)).fetchone())

def batch_existing_published(crop_bottom_pct=15):
    """Explicit parent-callable batch: scan only published listing references, never approve."""
    conn=db.get_db(); ensure_schema(conn); report={'generated':[],'skipped':[],'listing_count':0}
    rows=conn.execute("SELECT * FROM listings WHERE status='published'").fetchall(); report['listing_count']=len(rows)
    for listing in rows:
        seen=set()
        for field in FIELDS:
            try: urls=json.loads(listing[field] or '[]')
            except (TypeError,ValueError): urls=[]
            if not isinstance(urls,list): continue
            for url in urls:
                path=canonical_source(url)
                if not path: report['skipped'].append({'listing_id':listing['id'],'source':url,'reason':'missing_or_external'}); continue
                if str(path) in seen: continue
                seen.add(str(path))
                existing=conn.execute('SELECT asset_id FROM media_assets WHERE listing_id=? AND canonical_source=?',(listing['id'],str(path))).fetchone()
                if existing: report['skipped'].append({'listing_id':listing['id'],'source':url,'reason':'already_registered'});continue
                try: report['generated'].append(generate_asset(conn,listing['id'],url,'floorplan' if field=='floorplan_images' else 'photo',crop_bottom_pct if field=='floorplan_images' else 0))
                except (OSError,ValueError) as exc: report['skipped'].append({'listing_id':listing['id'],'source':url,'reason':str(exc)})
    conn.commit(); conn.close(); report['generated_count']=len(report['generated']); return report

def current_bytes(row):
    source=canonical_source(row['original_url']); derivative=derivative_path(row)
    return bool(source and derivative and fingerprint(source)==row.get('source_fingerprint') and fingerprint(derivative)==row.get('derivative_fingerprint'))

def is_safe(row):
    return bool(row['visibility']=='customer_safe' and row.get('approved_source_fingerprint')==row.get('source_fingerprint') and row.get('approved_derivative_fingerprint')==row.get('derivative_fingerprint') and row.get('reviewed_fingerprint')==row.get('fingerprint') and current_bytes(row))

def safe_assets_for_listing(listing_id):
    conn=db.get_db()
    try: rows=[dict(r) for r in conn.execute('SELECT * FROM media_assets WHERE listing_id=?',(listing_id,))]
    finally: conn.close()
    return [{'id':r['asset_id'],'category':r['category'],'url':'/api/customer-safe-image/'+r['asset_id']} for r in rows if is_safe(r)]

def register(app):
    def asset(listing_id,asset_id):
        u=az.effective_user()
        if not az.listing_allowed(u,listing_id): az.deny_row('listing',listing_id)
        conn=db.get_db(); row=conn.execute('SELECT * FROM media_assets WHERE asset_id=? AND listing_id=?',(asset_id,listing_id)).fetchone();conn.close()
        if not row: abort(404)
        return dict(row)
    @app.get('/api/media-review/listings')
    @az.require_roles('admin','agent')
    def review_list():
        conn=db.get_db();rows=conn.execute('SELECT DISTINCT listing_id FROM media_assets').fetchall();conn.close()
        conn = db.get_db()
        groups = []
        for r in rows:
            lid = r['listing_id']
            if not az.listing_allowed(az.effective_user(), lid):
                continue
            listing = conn.execute('SELECT address FROM listings WHERE id=?', (lid,)).fetchone()
            states = [x[0] for x in conn.execute('SELECT visibility FROM media_assets WHERE listing_id=?', (lid,))]
            groups.append({'id': lid, 'address': listing['address'] if listing else lid, 'total': len(states),
                           'needs_review': states.count('needs_review'), 'customer_safe': states.count('customer_safe'),
                           'internal_only': states.count('internal_only')})
        conn.close()
        return jsonify(items=groups, listings=groups)
    @app.get('/api/media-review/listing/<listing_id>')
    @az.require_roles('admin','agent')
    def review_detail(listing_id):
        if not az.listing_allowed(az.effective_user(),listing_id): az.deny_row('listing',listing_id)
        conn=db.get_db();rows=[dict(r) for r in conn.execute('SELECT * FROM media_assets WHERE listing_id=?',(listing_id,))];conn.close()
        for r in rows:
            r.pop('canonical_source',None)
            r['original_url']='/api/media-review/listing/'+listing_id+'/asset/'+r['asset_id']+'/original'
            r['safe_url']='/api/media-review/listing/'+listing_id+'/asset/'+r['asset_id']+'/derivative'
            r['original_preview_url']='/api/admin/media-review/image/'+listing_id+'/'+r['asset_id']
            r['derivative_preview_url']='/api/admin/media-review/derivative/'+listing_id+'/'+r['asset_id']
            r['manifest']={'crop_bottom_pct': r['crop_bottom_pct']}
        conn=db.get_db(); listing=conn.execute('SELECT id,address FROM listings WHERE id=?',(listing_id,)).fetchone();conn.close()
        return jsonify(listing_id=listing_id,listing=dict(listing) if listing else {},assets=rows)
    @app.get('/api/media-review/listing/<listing_id>/asset/<asset_id>/<kind>')
    @az.require_roles('admin','agent')
    def review_image(listing_id,asset_id,kind):
        r=asset(listing_id,asset_id)
        if kind not in {'original','derivative'}: abort(404)
        p=canonical_source(r['original_url']) if kind=='original' else derivative_path(r)
        if not p: abort(404)
        return send_file(p)
    @app.post('/api/media-review/listing/<listing_id>/asset/<asset_id>/<action>')
    @az.require_roles('admin','agent')
    def review_action(listing_id,asset_id,action):
        r=asset(listing_id,asset_id); d=request.get_json(silent=True) or {};conn=db.get_db()
        try:
            if action=='regenerate':
                try: r=generate_asset(conn,listing_id,r['original_url'],r['category'],d.get('crop_bottom_pct',15))
                except (OSError,ValueError): abort(400)
            elif action=='approve':
                if d.get('confirmed') is not True:
                    abort(400)
                match = d.get('derivative_fingerprint') == r['derivative_fingerprint'] or d.get('reviewed_fingerprint') == r['fingerprint']
                if not match or not current_bytes(r):
                    az.audit('media_approve_stale','media',asset_id); abort(409)
                conn.execute("UPDATE media_assets SET visibility='customer_safe',reviewed_fingerprint=fingerprint,approved_source_fingerprint=source_fingerprint,approved_derivative_fingerprint=derivative_fingerprint,reviewed_by=?,reviewed_at=datetime('now') WHERE asset_id=?",(az.effective_user()['id'],asset_id))
            elif action=='reject': conn.execute("UPDATE media_assets SET visibility='internal_only',redaction_status='rejected',reviewed_fingerprint=NULL,approved_source_fingerprint=NULL,approved_derivative_fingerprint=NULL,reviewed_by=?,reviewed_at=datetime('now') WHERE asset_id=?",(az.effective_user()['id'],asset_id))
            else: abort(404)
            conn.commit();az.audit('media_'+action,'media',asset_id,True)
            result=dict(conn.execute('SELECT * FROM media_assets WHERE asset_id=?',(asset_id,)).fetchone());result.pop('canonical_source',None)
            return jsonify(code=1,asset=result)
        finally:conn.close()
    # Contract aliases used by the commercial frontend.
    app.add_url_rule('/api/admin/media-review/listings','admin_review_listings',review_list)
    app.add_url_rule('/api/admin/media-review/listing/<listing_id>','admin_review_detail',review_detail)
    app.add_url_rule('/api/admin/media-review/listing/<listing_id>/asset/<asset_id>/<kind>','admin_review_image',review_image,methods=['GET'])
    app.add_url_rule('/api/admin/media-review/listing/<listing_id>/asset/<asset_id>/<action>','admin_review_mutate',review_action,methods=['POST'])
    app.add_url_rule('/api/admin/media-review/listings/<listing_id>','commercial_review_detail',review_detail)
    for kind in ('image', 'derivative'):
        def image_alias(listing_id, asset_id, _kind=kind):
            return review_image(listing_id, asset_id, 'original' if _kind == 'image' else 'derivative')
        app.add_url_rule('/api/admin/media-review/'+kind+'/<listing_id>/<asset_id>', 'commercial_review_'+kind, image_alias)
    for action in ('regenerate', 'approve', 'reject'):
        def action_alias(listing_id, asset_id, _action=action):
            return review_action(listing_id, asset_id, _action)
        app.add_url_rule('/api/admin/media-review/'+action+'/<listing_id>/<asset_id>', 'commercial_review_'+action, action_alias, methods=['POST'])
    @app.get('/api/customer-safe-image/<asset_id>')
    def safe_image(asset_id):
        conn=db.get_db();r=conn.execute('SELECT m.*,l.status AS listing_status FROM media_assets m JOIN listings l ON l.id=m.listing_id WHERE m.asset_id=?',(asset_id,)).fetchone();conn.close()
        if not r: abort(404)
        r=dict(r); u=az.effective_user()
        if r['listing_status']!='published' or not is_safe(r): abort(404)
        if u and u['role']!='admin' and not az.listing_allowed(u,r['listing_id']): az.deny_row('listing',r['listing_id'])
        response=send_file(derivative_path(r));response.headers['Cache-Control']='private, no-store';return response
