"""SEC01/02/03 convergence: tmpDB only, no server/network/live mutations."""
from pathlib import Path
import json
import secrets
import shutil

import pytest

import authz
import db
import commercial_media as media
import reins_photo_extractor as extractor
import server

ROOT = Path(__file__).resolve().parents[1]


def hook_status(client, path, payload, headers):
    # Exercise real before_request + HTTP error handler, never mutation handler.
    with client.session_transaction() as session:
        saved_session = dict(session)
    with server.app.test_request_context(path, method='POST', data=json.dumps(payload),
                                         content_type='application/json', headers=headers):
        from flask import session
        session.update(saved_session)
        try:
            result = server.app.preprocess_request()
        except Exception as error:
            result = server.app.handle_user_exception(error)
        if result is None:
            return None
        response = server.app.make_response(result)
        assert response.is_json
        assert response.get_json()['code'] == 0
        return response.status_code


MALFORMED_SCOPE = [
    [], [1], 'text', 1, True, None,
    {'listingIds': 1}, {'listing_ids': 'L1'}, {'listingIds': {}},
    {'listingIds': [1]}, {'listingIds': ['L1', {}]},
    {'client_ids': 'C1'}, {'client_ids': [False]},
    {'listingId': {}}, {'clientId': []}, {'planId': {}},
    {'optimized': 1}, {'optimized': []}, {'optimized': False},
    {'optimized': {'stops': 1}}, {'optimized': {'stops': {}}},
    {'optimized': {'stops': [1]}}, {'optimized': {'stops': [{'listingId': []}]}},
]


@pytest.mark.parametrize('uid', ['admin', 'agent', 'client'])
@pytest.mark.parametrize('payload', MALFORMED_SCOPE)
def test_malformed_json_scope_hook_returns_400(client, uid, payload):
    headers = impersonate(client, uid)
    assert hook_status(client, '/api/v1/shortlists', payload, headers) == 400


@pytest.mark.parametrize('payload', [[], [1], 'text', {'optimized': {'stops': 1}}])
@pytest.mark.parametrize('uid', ['admin', 'agent'])
def test_csrf_rejection_precedes_malformed_body(client, uid, payload):
    impersonate(client, uid)
    assert hook_status(client, '/api/v1/shortlists', payload, {}) == 403
    assert hook_status(client, '/api/v1/shortlists', payload, {'X-CSRF-Token': 'wrong'}) == 403


@pytest.mark.parametrize('uid', ['admin', 'agent'])
def test_valid_scope_shapes_continue_and_unassigned_rows_still_deny(client, uid):
    headers = impersonate(client, uid)
    assert hook_status(client, '/api/v1/shortlists', {
        'listingIds': ['L1', 'L3'], 'client_ids': ['C1'],
        'optimized': {'stops': [{'listingId': 'L1'}, {'listingId': 'L3'}]},
    }, headers) is None
    assert hook_status(client, '/api/v1/shortlists', {'listingIds': ['L2']}, headers) == (403 if uid == 'agent' else None)


@pytest.mark.parametrize('uid', [None, 'client', 'agent'])
@pytest.mark.parametrize('payload', [[], [1], 'text'])
def test_malformed_login_top_level_preserves_csrf_priority(client, uid, payload):
    headers = impersonate(client, uid)
    assert hook_status(client, '/login/staff', payload, {}) == 403
    assert hook_status(client, '/login/staff', payload, headers) == 400


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'scope.db'))
    monkeypatch.setenv('BASIC_AUTH_BOOTSTRAP_ENABLED', '0')
    monkeypatch.setitem(server.app.config, 'TESTING', True)
    monkeypatch.setitem(server.app.config, 'SECRET_KEY', secrets.token_hex(32))
    db.init_db()
    conn = db.get_db()
    try:
        for lid, owner, status, lat in [('L1', 'A1', 'published', 35),
                                        ('L2', 'A2', 'published', 36),
                                        ('L3', 'A2', 'published', 37),
                                        ('DRAFT', 'A1', 'draft', 38),
                                        ('NOCOORD', 'A1', 'published', 0)]:
            conn.execute('INSERT INTO listings(id,address,price,status,agent_id,latitude,longitude,source) VALUES (?,?,?,?,?,?,?,?)',
                         (lid, 'Scope unit fixture '+lid, 80000000, status, owner, lat, 139, 'reins'))
        conn.execute("INSERT INTO clients(id,name,agent_id) VALUES ('C1','Scope unit fixture','A1')")
        conn.execute("INSERT INTO clients(id,name,agent_id) VALUES ('C2','Unassigned unit fixture','A2')")
        conn.execute("INSERT INTO client_shortlists(client_id,listing_id) VALUES ('C2','L2')")
        conn.execute("INSERT INTO agent_client_assignments(agent_id,client_id) VALUES ('A1','C1')")
        # L3 is intentionally accessible via assignment, not direct ownership.
        for lid in ('L1', 'L3'):
            conn.execute('INSERT INTO client_shortlists(client_id,listing_id) VALUES (?,?)', ('C1', lid))
        for uid, role, aid, cid in [('admin', 'admin', '', None), ('agent', 'agent', 'A1', None), ('client', 'client', '', 'C1'), ('clientB', 'client', '', 'C2')]:
            conn.execute('INSERT INTO users(id,username,email,role,password_hash,agent_id,client_id) VALUES (?,?,?,?,?,?,?)',
                         (uid, uid, uid+'@example.test', role, 'unused-session-only', aid, cid))
        conn.commit()
    finally:
        conn.close()
    return server.app.test_client()


def impersonate(client, uid):
    with client.session_transaction() as session:
        session.clear()
        if uid:
            session['user_id'] = uid
        session['csrf_token'] = 'scope-unit-csrf'
    return {'X-CSRF-Token': 'scope-unit-csrf'}


@pytest.fixture
def preview_assets(client, tmp_path, monkeypatch):
    # Existing local input only: never synthesize preview pixels or read live DB.
    source = next(iter(sorted((ROOT / 'uploads').rglob('*.jpg'))), None)
    if source is None:
        pytest.skip('existing local JPG input unavailable')
    preview_root = tmp_path / 'preview'
    monkeypatch.setattr(extractor, 'CONTROLLED_TEMP_ROOT', preview_root)
    monkeypatch.setattr(server, 'CONTROLLED_TEMP_ROOT', preview_root)
    monkeypatch.setenv('WORKBENCH_PASSWORD', secrets.token_urlsafe(32))
    urls = {}
    for lid in ('L1', 'L2', 'L3'):
        path = preview_root / lid / 'asset.jpg'
        path.parent.mkdir(parents=True)
        shutil.copyfile(source, path)
        manifest = {'listing_id': lid, 'session_id': 'tmp-only', 'source_pages': [],
                    'candidates': [{'id': 'asset', 'temp_path': str(path)}]}
        (path.parent / 'manifest.json').write_text(json.dumps(manifest))
        token = server._sign_preview_asset(lid, 'asset', manifest)
        urls[lid] = ('/api/reins-photo-preview-file/'+lid+'/asset', token, path.read_bytes())
    return urls


@pytest.mark.parametrize('uid,allowed', [(None, set()), ('client', set()), ('clientB', set()),
                                         ('agent', {'L1', 'L3'}), ('admin', {'L1', 'L2', 'L3'})])
def test_raw_preview_requires_active_staff_and_row_scope(client, preview_assets, uid, allowed):
    impersonate(client, uid)
    for lid, (path, token, content) in preview_assets.items():
        response = client.get(path, query_string={'token': token})
        if lid in allowed:
            assert response.status_code == 200
            assert response.data == content
            assert response.headers['Cache-Control'] == 'private, no-store'
        else:
            assert response.status_code in ({401, 404} if uid is None else {403, 404})
            assert response.data != content


def test_raw_preview_valid_token_does_not_revive_disabled_staff(client, preview_assets):
    impersonate(client, 'admin')
    conn = db.get_db()
    conn.execute("UPDATE users SET status='disabled' WHERE id='admin'")
    conn.commit()
    conn.close()
    path, token, content = preview_assets['L1']
    response = client.get(path, query_string={'token': token})
    assert response.status_code in {401, 404}
    assert response.data != content


@pytest.mark.parametrize('uid', ['admin', 'agent'])
def test_raw_preview_staff_still_requires_valid_token(client, preview_assets, uid):
    impersonate(client, uid)
    path, _, _ = preview_assets['L1']
    assert client.get(path).status_code == 403
    assert client.get(path, query_string={'token': 'invalid'}).status_code == 403


@pytest.mark.parametrize('uid,allowed', [(None, {'L1', 'L2', 'L3'}),
                                         ('client', {'L1', 'L3'}), ('clientB', {'L2'}),
                                         ('agent', {'L1', 'L3'}), ('admin', {'L1', 'L2', 'L3'})])
def test_listing_detail_cannot_cross_client_assignment(client, uid, allowed):
    impersonate(client, uid)
    for lid in ('L1', 'L2', 'L3'):
        response = client.get('/api/listing/'+lid)
        assert response.status_code == (200 if lid in allowed else 403)


@pytest.mark.parametrize('uid,allowed', [(None, set()), ('client', set()), ('clientB', set()),
                                         ('agent', {'L1', 'L3'}), ('admin', {'L1', 'L2', 'L3'})])
def test_original_media_cannot_cross_assignment(client, preview_assets, tmp_path, monkeypatch, uid, allowed):
    root = tmp_path / 'media'
    monkeypatch.setattr(media, 'ROOT', root)
    monkeypatch.setattr(media, 'UPLOADS', root / 'uploads')
    conn = db.get_db()
    for lid, (_, _, _) in preview_assets.items():
        source = tmp_path / 'preview' / lid / 'asset.jpg'
        path = root / 'uploads' / (lid+'.jpg')
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        conn.execute('INSERT INTO media_assets(asset_id,listing_id,original_url,canonical_source,category) VALUES (?,?,?,?,?)',
                     (lid, lid, '/uploads/'+lid+'.jpg', str(path), 'photo'))
    conn.commit()
    conn.close()
    impersonate(client, uid)
    for lid in ('L1', 'L2', 'L3'):
        response = client.get('/api/media-review/listing/'+lid+'/asset/'+lid+'/original')
        assert response.status_code == (200 if lid in allowed else 401 if uid is None else 403)


@pytest.mark.parametrize('uid,allowed_clients', [('agent', {'C1'}), ('client', {'C1'}),
                                                ('clientB', {'C2'}), ('admin', {'C1', 'C2'})])
def test_client_assignment_policy_is_c1_only_for_agent(client, uid, allowed_clients):
    impersonate(client, uid)
    with server.app.test_request_context('/'):
        user = {'id': uid, 'role': 'client' if uid.startswith('client') else uid,
                'agent_id': 'A1', 'client_id': 'C2' if uid == 'clientB' else 'C1'}
        assert {cid for cid in ('C1', 'C2') if authz.client_allowed(user, cid)} == allowed_clients
    if uid in {'client', 'clientB'}:
        assert hook_status(client, '/api/v1/shortlists', {'clientId': next(iter({'C1', 'C2'} - allowed_clients))}, {}) == 403


@pytest.mark.parametrize('uid,expected_ids,has_source', [
    (None, {'L1', 'L2', 'L3'}, False),
    ('client', {'L1', 'L3'}, False),
    ('clientB', {'L2'}, False),
    ('agent', {'L1', 'L3'}, True),
    ('admin', {'L1', 'L2', 'L3'}, True),
])
def test_geojson_role_scope_and_source_projection(client, uid, expected_ids, has_source):
    impersonate(client, uid)
    response = client.get('/api/listings/geojson')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['type'] == 'FeatureCollection'
    assert {f['properties']['id'] for f in payload['features']} == expected_ids
    for feature in payload['features']:
        assert feature['type'] == 'Feature'
        assert feature['geometry']['type'] == 'Point'
        assert len(feature['geometry']['coordinates']) == 2
        assert feature['properties']['price'] == 8000
        assert ('source' in feature['properties']) is has_source
        if has_source:
            assert feature['properties']['source'] == 'reins'
    if uid in {'agent', 'client', 'clientB'}:
        listing_response = client.get('/api/listings')
        assert listing_response.status_code == 200
        # Staff lists also contain assigned drafts/unlocated rows; GeoJSON is
        # deliberately limited to the published, geocoded listing subset.
        map_eligible = {'L1', 'L2', 'L3'}
        assert {row['id'] for row in listing_response.get_json()['listings'] if row['id'] in map_eligible} == expected_ids
