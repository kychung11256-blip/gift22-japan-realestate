"""Commercial security acceptance, isolated from live DB/media and servers.

Image tests copy existing local inputs; no generated stand-in media or real
staging approval. Missing real inputs are explicit skips, never fake passes.
"""
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
import secrets
import shutil
import sqlite3

import pytest
from PIL import Image

import authz
import commercial_media as media
import commercial_migrations as migrations
import db
import server

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = Path('/home/ubuntu/ai-team/brand-assets/official')


@pytest.fixture(scope='module')
def credentials():
    password = secrets.token_urlsafe(24)
    return password, authz.password_hash(password)


@pytest.fixture
def client(tmp_path, monkeypatch, credentials):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'acceptance.db'))
    monkeypatch.setenv('BASIC_AUTH_BOOTSTRAP_ENABLED', '0')
    monkeypatch.setattr(authz, '_attempts', defaultdict(deque))
    for key, value in {'TESTING': True, 'SECRET_KEY': secrets.token_hex(32)}.items():
        monkeypatch.setitem(server.app.config, key, value)
    db.init_db()
    conn = db.get_db()
    try:
        conn.execute("INSERT INTO listings(id,address,price,status,agent_id) VALUES ('L1','Acceptance local fixture',8000,'published','A1')")
        conn.execute("INSERT INTO clients(id,name,agent_id) VALUES ('C1','Acceptance client','A1')")
        conn.execute("INSERT INTO client_shortlists(client_id,listing_id) VALUES ('C1','L1')")
        conn.execute("INSERT INTO agent_client_assignments(agent_id,client_id) VALUES ('A1','C1')")
        for uid, role, aid, cid in [('admin','admin','',None), ('agent','agent','A1',None), ('client','client','','C1')]:
            conn.execute('INSERT INTO users(id,username,email,role,password_hash,agent_id,client_id) VALUES (?,?,?,?,?,?,?)',
                         (uid, uid, uid+'@example.test', role, credentials[1], aid, cid))
        conn.commit()
    finally:
        conn.close()
    return server.app.test_client()


def csrf(client):
    return client.get('/api/auth/session').json['csrf_token']


def login(client, credentials, uid='admin', endpoint='staff', token=None, **kwargs):
    return client.post('/login/'+endpoint,
                       json={'identifier': uid, 'password': credentials[0]},
                       headers={'X-CSRF-Token': token or csrf(client)}, **kwargs)


def impersonate(client, uid='admin'):
    with client.session_transaction() as session:
        session.clear()
        session['user_id'] = uid
        session['csrf_token'] = 'acceptance-csrf'
    return {'X-CSRF-Token': 'acceptance-csrf'}


def asset_row(asset_id):
    conn = db.get_db()
    try:
        return dict(conn.execute('SELECT * FROM media_assets WHERE asset_id=?', (asset_id,)).fetchone())
    finally:
        conn.close()


@pytest.mark.parametrize('uid', [None, 'admin', 'agent', 'client'])
@pytest.mark.parametrize('path,filename', [('/privacy','privacy.html'), ('/terms','terms.html')])
def test_legal_pages_are_public_html_for_every_role(client, uid, path, filename):
    if uid:
        impersonate(client, uid)
    response = client.get(path)
    assert response.status_code == 200
    assert response.mimetype == 'text/html'
    assert response.data == (ROOT / filename).read_bytes()


@pytest.mark.parametrize('path,filename,uid,status', [
    ('/media-review','media_review.html',None,401),
    ('/media-review','media_review.html','admin',200),
    ('/media-review','media_review.html','agent',200),
    ('/media-review','media_review.html','client',403),
    ('/admin/accounts','admin_users.html',None,401),
    ('/admin/accounts','admin_users.html','admin',200),
    ('/admin/accounts','admin_users.html','agent',403),
    ('/admin/accounts','admin_users.html','client',403)])
def test_staff_page_role_matrix_serves_html_not_json(client, path, filename, uid, status):
    if uid:
        impersonate(client, uid)
    response = client.get(path)
    assert response.status_code == status
    if status == 200:
        assert response.mimetype == 'text/html'
        assert response.data == (ROOT / filename).read_bytes()


@pytest.mark.parametrize('kind', ['whatsapp', 'wechat'])
def test_qr_fixed_mapping_ignores_user_supplied_paths(client, monkeypatch, kind):
    official = OFFICIAL / (kind+'-qr.jpg')
    if not official.is_file():
        pytest.skip('real official QR input unavailable: '+kind)
    monkeypatch.setenv('PUBLIC_'+kind.upper()+'_QR_PATH', str(official))
    response = client.get('/api/public-contact-qr/'+kind,
                          query_string={'filename': '/etc/passwd', 'path': '../../.env', 'kind': 'other'})
    assert response.status_code == 200
    assert response.data == official.read_bytes()
    assert response.mimetype == 'image/jpeg'
    assert response.headers['Cache-Control'] == 'public, max-age=3600'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'


@pytest.mark.parametrize('kind', ['unknown', 'whatsapp-qr.jpg', '..', '%2e%2e%2f.env', 'whatsapp%2f..%2fwechat', '%2fetc%2fpasswd'])
def test_qr_rejects_unknown_kinds_and_traversal(client, kind):
    assert client.get('/api/public-contact-qr/'+kind).status_code == 404


@pytest.mark.parametrize('kind', ['whatsapp', 'wechat'])
def test_qr_missing_fixed_file_is_404(client, monkeypatch, tmp_path, kind):
    monkeypatch.setenv('PUBLIC_'+kind.upper()+'_QR_PATH', str(tmp_path / 'missing.jpg'))
    assert client.get('/api/public-contact-qr/'+kind).status_code == 404


def test_login_budget_is_ip_wide_across_identifiers_and_endpoints(client, monkeypatch, credentials):
    clock = SimpleNamespace(monotonic=lambda: 1000.0)
    monkeypatch.setattr(authz, 'time', clock)
    token = csrf(client)
    for index in range(8):
        response = client.post('/login/'+('staff' if index % 2 else 'client'),
                               json={'identifier': 'unknown-'+str(index), 'password': credentials[0]},
                               headers={'X-CSRF-Token': token})
        assert response.status_code == 401
    # Valid credentials must not bypass an exhausted IP budget.
    assert login(client, credentials, token=token).status_code == 429
    assert client.get('/api/auth/session').json['user'] is None
    other = server.app.test_client()
    assert login(other, credentials, environ_overrides={'REMOTE_ADDR': '192.0.2.2'}).status_code == 200
    clock.monotonic = lambda: 1301.0
    assert login(client, credentials, token=token).status_code == 200


@pytest.mark.parametrize('uid,endpoint,role,destination', [
    ('admin','staff','admin','/workbench'), ('agent','staff','agent','/workbench'),
    ('client','client','client','/client/portal')])
def test_login_rotates_session_and_csrf_without_exposing_password(client, credentials, uid, endpoint, role, destination):
    old_token = csrf(client)
    with client.session_transaction() as session:
        session['prelogin_marker'] = 'must disappear'
    response = login(client, credentials, uid, endpoint, old_token)
    assert response.status_code == 200
    assert response.json['role'] == role
    assert response.json['redirect'] == destination
    new_token = response.json['csrf_token']
    assert new_token != old_token
    assert 'password_hash' not in response.json['user']
    cookie = response.headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie and 'Secure' in cookie
    with client.session_transaction() as session:
        assert session['user_id'] == uid and session.permanent
        assert 'prelogin_marker' not in session
    assert client.post('/logout', headers={'X-CSRF-Token': old_token}).status_code == 403
    assert client.get('/api/auth/session').json['user']['id'] == uid
    assert client.get('/logout').status_code == 405
    assert client.post('/logout', headers={'X-CSRF-Token': new_token}).status_code == 200
    assert client.get('/api/auth/session').json['user'] is None
    assert client.get('/api/admin/users').status_code == 401


@pytest.mark.parametrize('uid,endpoint', [('client','staff'), ('admin','client'), ('agent','client')])
def test_login_rejects_wrong_role_portal(client, credentials, uid, endpoint):
    assert login(client, credentials, uid, endpoint).status_code == 401
    assert client.get('/api/auth/session').json['user'] is None


def test_missing_or_invalid_login_csrf_does_not_consume_rate_budget(client, credentials):
    csrf(client)
    for headers in ({}, {'X-CSRF-Token': 'wrong-token'}):
        assert client.post('/login/staff', json={'identifier': 'admin', 'password': credentials[0]}, headers=headers).status_code == 403
    assert not authz._attempts
    assert login(client, credentials).status_code == 200


def test_disabled_account_invalidates_existing_session_and_blocks_login(client, credentials):
    assert login(client, credentials).status_code == 200
    conn = db.get_db()
    conn.execute("UPDATE users SET status='disabled' WHERE id='admin'")
    conn.commit()
    conn.close()
    assert client.get('/api/auth/session').json['user'] is None
    assert client.get('/api/admin/users').status_code == 401
    assert login(client, credentials).status_code == 401


@pytest.fixture
def local_asset(client, tmp_path, monkeypatch):
    # Read-only discovery of an existing, decodable local image, never network.
    candidates = sorted((ROOT / 'uploads').rglob('*.jpg'))
    source = None
    for candidate in candidates:
        if 'customer_safe' in candidate.parts:
            continue
        try:
            with Image.open(candidate) as image:
                image.verify()
            source = candidate
            break
        except (OSError, ValueError):
            continue
    if source is None:
        pytest.skip('no real existing local JPEG input available')
    assert source is not None
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    copied = uploads / 'source.jpg'
    shutil.copyfile(source, copied)
    monkeypatch.setattr(media, 'ROOT', tmp_path)
    monkeypatch.setattr(media, 'UPLOADS', uploads)
    conn = db.get_db()
    try:
        row = media.generate_asset(conn, 'L1', '/uploads/source.jpg', 'floorplan', 15)
        conn.commit()
    finally:
        conn.close()
    return row


def review_url(row, action):
    return '/api/media-review/listing/L1/asset/'+row['asset_id']+'/'+action


def approve(client, row, headers):
    return client.post(review_url(row, 'approve'), json={'reviewed_fingerprint': row['fingerprint'], 'confirmed': True}, headers=headers)


def test_media_requires_exact_reviewed_fingerprint_and_csrf(client, local_asset):
    row = local_asset
    safe_url = '/api/customer-safe-image/'+row['asset_id']
    assert row['visibility'] == 'needs_review'
    assert client.get(safe_url).status_code == 404
    headers = impersonate(client)
    assert client.post(review_url(row, 'approve'), json={'reviewed_fingerprint': row['fingerprint']}, headers=headers).status_code == 400
    assert approve(client, row, {}).status_code == 403
    for value in (None, '', 'stale-fingerprint'):
        assert client.post(review_url(row, 'approve'), json={'reviewed_fingerprint': value, 'confirmed': True}, headers=headers).status_code == 409
    assert asset_row(row['asset_id'])['visibility'] == 'needs_review'
    assert approve(client, row, headers).status_code == 200
    approved = asset_row(row['asset_id'])
    assert approved['approved_source_fingerprint'] == row['source_fingerprint']
    assert approved['approved_derivative_fingerprint'] == row['derivative_fingerprint']
    assert approved['reviewed_fingerprint'] == row['fingerprint']
    assert approved['reviewed_by'] == 'admin' and approved['reviewed_at']
    anonymous = server.app.test_client()
    response = anonymous.get(safe_url)
    assert response.status_code == 200
    derivative = media.derivative_path(approved)
    assert derivative is not None
    assert response.data == derivative.read_bytes()
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert media.safe_assets_for_listing('L1')[0]['url'] == safe_url


@pytest.mark.parametrize('target', ['source', 'derivative'])
@pytest.mark.parametrize('already_approved', [False, True])
def test_media_changed_bytes_fail_closed_even_with_matching_stored_fingerprint(client, local_asset, target, already_approved):
    row = local_asset
    headers = impersonate(client)
    if already_approved:
        assert approve(client, row, headers).status_code == 200
    path = media.canonical_source(row['original_url']) if target == 'source' else media.derivative_path(row)
    assert path is not None
    path.write_bytes(path.read_bytes()+b'acceptance-test-tampering')
    assert approve(client, row, headers).status_code == 409
    assert server.app.test_client().get('/api/customer-safe-image/'+row['asset_id']).status_code == 404
    assert media.safe_assets_for_listing('L1') == []


def test_media_recrop_revokes_approval_and_reuses_unique_asset(client, local_asset):
    row = local_asset
    headers = impersonate(client)
    assert approve(client, row, headers).status_code == 200
    old_derivative = media.derivative_path(row)
    assert old_derivative is not None
    old_bytes = old_derivative.read_bytes()
    response = client.post(review_url(row, 'regenerate'), json={'crop_bottom_pct': 35}, headers=headers)
    assert response.status_code == 200
    fresh = asset_row(row['asset_id'])
    assert fresh['asset_id'] == row['asset_id']
    assert fresh['safe_url'] != row['safe_url']
    assert fresh['fingerprint'] != row['fingerprint']
    assert old_derivative.read_bytes() == old_bytes
    assert fresh['visibility'] == 'needs_review'
    for field in ('reviewed_fingerprint','approved_source_fingerprint','approved_derivative_fingerprint','reviewed_by','reviewed_at'):
        assert fresh[field] is None
    assert approve(client, row, headers).status_code == 409
    assert server.app.test_client().get('/api/customer-safe-image/'+row['asset_id']).status_code == 404
    assert approve(client, fresh, headers).status_code == 200
    conn = db.get_db()
    assert conn.execute('SELECT count(*) FROM media_assets').fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize('uid', [None, 'client'])
def test_media_review_and_raw_inputs_are_not_customer_accessible(client, local_asset, uid):
    if uid:
        impersonate(client, uid)
    for path in ['/api/media-review/listing/L1', review_url(local_asset, 'original'), review_url(local_asset, 'derivative')]:
        assert client.get(path).status_code in (401,403)
    assert client.get('/uploads/source.jpg').status_code in (401,403,404)
    assert approve(client, local_asset, {'X-CSRF-Token': 'acceptance-csrf'}).status_code in (401,403)
    assert asset_row(local_asset['asset_id'])['visibility'] == 'needs_review'


@pytest.mark.parametrize('url', ['https://example.test/image.jpg', '/uploads/../outside.jpg', '/uploads/%2e%2e/outside.jpg', '/uploads/source.jpg?path=other', '/uploads/source.jpg#fragment', '/uploads/..\\outside.jpg'])
def test_media_canonical_source_rejects_external_and_path_tricks(local_asset, url):
    assert media.canonical_source(url) is None


def test_media_reject_revokes_previously_approved_asset(client, local_asset):
    headers = impersonate(client)
    assert approve(client, local_asset, headers).status_code == 200
    assert client.post(review_url(local_asset, 'reject'), json={}, headers=headers).status_code == 200
    row = asset_row(local_asset['asset_id'])
    assert row['visibility'] == 'internal_only'
    assert row['reviewed_fingerprint'] is None
    assert server.app.test_client().get('/api/customer-safe-image/'+row['asset_id']).status_code == 404


@pytest.mark.parametrize('field', ['reviewed_fingerprint', 'approved_source_fingerprint', 'approved_derivative_fingerprint'])
def test_media_customer_safe_label_alone_cannot_bypass_fingerprint_binding(client, local_asset, field):
    headers = impersonate(client)
    assert approve(client, local_asset, headers).status_code == 200
    conn = db.get_db()
    try:
        conn.execute('UPDATE media_assets SET '+field+'=NULL WHERE asset_id=?', (local_asset['asset_id'],))
        conn.commit()
    finally:
        conn.close()
    assert asset_row(local_asset['asset_id'])['visibility'] == 'customer_safe'
    assert server.app.test_client().get('/api/customer-safe-image/'+local_asset['asset_id']).status_code == 404
    assert media.safe_assets_for_listing('L1') == []


def test_migration_and_rollback_are_idempotent_preserve_legacy_rows(tmp_path):
    conn = sqlite3.connect(tmp_path / 'legacy.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
      CREATE TABLE listings(id TEXT PRIMARY KEY,address TEXT,price INTEGER,photos TEXT);
      CREATE TABLE clients(id TEXT PRIMARY KEY,name TEXT);
      CREATE TABLE client_shortlists(client_id TEXT,listing_id TEXT,note TEXT,
        FOREIGN KEY(client_id) REFERENCES clients(id),FOREIGN KEY(listing_id) REFERENCES listings(id));
      INSERT INTO listings VALUES ('legacy','Preserved address',8000,'[]');
      INSERT INTO clients VALUES ('legacy-client','Preserved client');
      INSERT INTO client_shortlists VALUES ('legacy-client','legacy','Preserved note');
    ''')
    tables = ('listings','clients','client_shortlists')
    columns = {table: [r[1] for r in conn.execute('PRAGMA table_info('+table+')')] for table in tables}
    def snapshot():
        return {table: [tuple(r) for r in conn.execute('SELECT '+','.join(columns[table])+' FROM '+table+' ORDER BY 1')] for table in tables}
    before = snapshot()
    rollback = (ROOT / 'migrations/commercial_20261107_rollback.sql').read_text()
    try:
        for _ in range(2):
            migrations.migrate(conn)
            schema = [tuple(r) for r in conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
            migrations.migrate(conn)
            assert [tuple(r) for r in conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")] == schema
            assert snapshot() == before
            assert list(conn.execute('PRAGMA foreign_key_check')) == []
            media_columns = {r[1] for r in conn.execute('PRAGMA table_info(media_assets)')}
            assert {'canonical_source','source_fingerprint','derivative_fingerprint','reviewed_fingerprint','approved_source_fingerprint','approved_derivative_fingerprint'} <= media_columns
            conn.executescript(rollback)
            conn.executescript(rollback)
            remaining = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert not {'users','audit_log','agent_client_assignments','media_assets'} & remaining
            assert snapshot() == before
            assert list(conn.execute('PRAGMA foreign_key_check')) == []
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        conn.close()
