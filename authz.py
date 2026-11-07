"""Session authorization and row-level policy; Basic is explicit bootstrap only."""
import hmac
import os
import secrets
import sqlite3
import time
from functools import wraps
from collections import defaultdict, deque
from flask import request, session, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash
import db

ROLES = {'admin', 'agent', 'client'}
_attempts = defaultdict(deque)

def init_auth_schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
      email TEXT NOT NULL UNIQUE COLLATE NOCASE,
      role TEXT NOT NULL CHECK(role IN ('admin','agent','client')),
      password_hash TEXT NOT NULL, agent_id TEXT DEFAULT '', client_id TEXT UNIQUE,
      status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
      created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS agent_client_assignments (
      agent_id TEXT NOT NULL, client_id TEXT NOT NULL,
      created_at TEXT DEFAULT (datetime('now')), PRIMARY KEY(agent_id,client_id),
      FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id TEXT DEFAULT '', actor_role TEXT DEFAULT '',
      action TEXT NOT NULL,target_type TEXT DEFAULT '',target_id TEXT DEFAULT '',allowed INTEGER NOT NULL DEFAULT 0,
      ip TEXT DEFAULT '', user_agent TEXT DEFAULT '',created_at TEXT DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS media_assets (
      asset_id TEXT PRIMARY KEY, listing_id TEXT NOT NULL, original_url TEXT NOT NULL,
      safe_url TEXT DEFAULT '',visibility TEXT NOT NULL DEFAULT 'internal_only'
      CHECK(visibility IN ('internal_only','customer_safe','needs_review')),
      category TEXT DEFAULT '',redaction_status TEXT DEFAULT 'none',ocr_flags TEXT DEFAULT '[]',manifest_json TEXT DEFAULT '{}',
      created_at TEXT DEFAULT (datetime('now')),updated_at TEXT DEFAULT (datetime('now')),
      FOREIGN KEY(listing_id) REFERENCES listings(id) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS idx_assign_client ON agent_client_assignments(client_id);
    CREATE INDEX IF NOT EXISTS idx_media_listing ON media_assets(listing_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_media_source_unique ON media_assets(listing_id,original_url);
    ''')
    for table in ('listings','clients'):
        if 'agent_id' not in {r[1] for r in conn.execute('PRAGMA table_info('+table+')')}:
            conn.execute('ALTER TABLE '+table+" ADD COLUMN agent_id TEXT DEFAULT ''")
    from commercial_media import ensure_schema as media_schema
    media_schema(conn)

def ensure_schema():
    with db.get_db() as conn:
        init_auth_schema(conn)
    conn.close()

def password_hash(password):
    return generate_password_hash(password, method='pbkdf2:sha256:600000')

def verify_password(password, stored):
    try:
        return check_password_hash(stored, password)
    except (ValueError, TypeError):
        return False

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    conn = db.get_db()
    row = conn.execute("SELECT * FROM users WHERE id=? AND status='active'", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def effective_user():
    user = current_user()
    if user:
        return user
    auth = request.authorization
    expected = os.environ.get('WORKBENCH_PASSWORD', '')
    if os.environ.get('BASIC_AUTH_BOOTSTRAP_ENABLED', '0').lower() in {'1','true'} and expected and auth and auth.type.lower() == 'basic' and hmac.compare_digest(auth.username or '', os.environ.get('WORKBENCH_USER', 'johnny')) and hmac.compare_digest(auth.password or '', expected):
        return {'id':'basic-auth-admin','bootstrap':True,'role':'admin','username':'basic-auth-admin','agent_id':'','client_id':None}
    return None

def public_user(user):
    return {k:user.get(k) for k in ('id','username','email','role','agent_id','client_id','status')} if user else None

def audit(action, target_type='', target_id='', allowed=False):
    user = effective_user() or {}
    conn = db.get_db()
    conn.execute('INSERT INTO audit_log(actor_user_id,actor_role,action,target_type,target_id,allowed,ip,user_agent) VALUES (?,?,?,?,?,?,?,?)',
        (user.get('id',''),user.get('role',''),action,str(target_type)[:200],str(target_id)[:200],int(allowed),request.remote_addr or '',request.headers.get('User-Agent','')[:200]))
    conn.commit(); conn.close()

def csrf_token():
    if not session.get('csrf_token'):
        session['csrf_token'] = secrets.token_urlsafe(32)
    return session['csrf_token']

def csrf_valid():
    user=effective_user()
    if user and user.get('bootstrap'): return True
    data = request.get_json(silent=True)
    supplied = request.headers.get('X-CSRF-Token') or (data.get('csrf_token') if isinstance(data, dict) else None) or request.form.get('csrf_token')
    expected = session.get('csrf_token')
    return bool(isinstance(supplied,str) and expected and hmac.compare_digest(expected,supplied))

def require_roles(*roles):
    def deco(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            u = effective_user()
            if not u or u['role'] not in roles:
                audit('auth_denied', request.path, '', False)
                abort(403 if u else 401)
            if request.method in {'POST','PUT','PATCH','DELETE'} and u['id'] != 'basic-auth-admin' and not csrf_valid():
                audit('csrf_denied',request.path); abort(403)
            return fn(*args, **kwargs)
        return inner
    return deco

def login_user(user):
    session.clear(); session['user_id'] = user['id']; session.permanent = True; csrf_token()

def check_login(identifier, password, roles):
    now = time.monotonic()
    # IP-wide budget prevents bypassing by changing usernames.
    key = request.remote_addr or 'unknown'
    queue = _attempts[key]
    while queue and queue[0] < now - 300: queue.popleft()
    if len(queue) >= 8: return None, 'rate_limited'
    queue.append(now)
    if len(_attempts) > 10000:
        for k in list(_attempts):
            if not _attempts[k] or _attempts[k][-1] < now - 300: del _attempts[k]
    conn = db.get_db()
    row = conn.execute("SELECT * FROM users WHERE status='active' AND (username=? COLLATE NOCASE OR email=? COLLATE NOCASE)", (identifier,identifier)).fetchone(); conn.close()
    if not row or row['role'] not in roles or not verify_password(password,row['password_hash']): return None,'invalid'
    return dict(row),None

def client_allowed(user, client_id):
    if not user: return False
    if user['role'] == 'admin': return True
    if user['role'] == 'client': return user.get('client_id') == client_id
    conn = db.get_db()
    row = conn.execute('SELECT 1 FROM agent_client_assignments WHERE agent_id=? AND client_id=?',(user.get('agent_id') or user['id'],client_id)).fetchone(); conn.close()
    return bool(row)

def listing_allowed(user, listing_id):
    if not user: return False
    if user['role'] == 'admin': return True
    conn = db.get_db()
    if user['role'] == 'agent':
        row = conn.execute('''SELECT 1 FROM listings l WHERE l.id=? AND (l.agent_id=? OR EXISTS(
          SELECT 1 FROM client_shortlists cs JOIN agent_client_assignments a ON a.client_id=cs.client_id
          WHERE cs.listing_id=l.id AND a.agent_id=?))''',(listing_id,user.get('agent_id') or user['id'],user.get('agent_id') or user['id'])).fetchone()
    else:
        row = conn.execute('SELECT 1 FROM client_shortlists WHERE listing_id=? AND client_id=?',(listing_id,user.get('client_id'))).fetchone()
    conn.close(); return bool(row)

def deny_row(kind, identity):
    audit('row_denied',kind,identity); abort(403)
