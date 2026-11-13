import secrets
import pytest
import db
import server

@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'commercial.db'))
    monkeypatch.setenv('BASIC_AUTH_BOOTSTRAP_ENABLED', '0')
    server.app.config.update(TESTING=True, SECRET_KEY=secrets.token_hex(32))
    db.init_db()
    return server.app.test_client()

def seed_users():
    import authz
    conn = db.get_db()
    for lid,aid in [('L1','A1'),('L2','A2')]:
        conn.execute("INSERT INTO listings(id,address,price,status,agent_id,photos) VALUES (?,?,8000,'published',?,?)",(lid,'東京都',aid,'["https://upstream/raw.jpg","/uploads/staged/raw.jpg"]'))
    for cid,aid in [('C1','A1'),('C2','A2')]:
        conn.execute('INSERT INTO clients(id,name,agent_id) VALUES (?,?,?)',(cid,cid,aid))
        conn.execute('INSERT INTO agent_client_assignments(agent_id,client_id) VALUES (?,?)',(aid,cid))
    conn.execute("INSERT INTO client_shortlists(client_id,listing_id,note) VALUES ('C1','L1','internal secret')")
    conn.execute("INSERT INTO client_shortlists(client_id,listing_id) VALUES ('C2','L2')")
    for uid,role,aid,cid in [('admin','admin','',None),('agent','agent','A1',None),('client','client','','C1')]:
        conn.execute('INSERT INTO users(id,username,email,role,password_hash,agent_id,client_id) VALUES (?,?,?,?,?,?,?)',(uid,uid,uid+'@example.test',role,authz.password_hash(secrets.token_urlsafe(24)),aid,cid))
    conn.commit(); conn.close()

def as_user(client, uid):
    with client.session_transaction() as s:
        s['user_id']=uid; s['csrf_token']='test-token'
    return {'X-CSRF-Token':'test-token'}

def test_agent_row_scope_and_client_projection(app_client):
    seed_users()
    h=as_user(app_client,'agent')
    assert [x['id'] for x in app_client.get('/api/v1/properties').json['items']] == ['L1']
    assert [x['id'] for x in app_client.get('/api/v1/clients').json['clients']] == ['C1']
    for path in ['/api/v1/properties/L2','/api/v1/shortlists/C2','/api/listing/L2']:
        assert app_client.get(path).status_code == 403, path
    assert app_client.post('/api/client-shortlists/bulk-add',json={'client_ids':['C1','C2'],'listing_ids':['L1']},headers=h).status_code == 403
    assert app_client.post('/api/v1/shortlists',json={'clientId':'C1','listingId':'L2'},headers=h).status_code == 403
    assert app_client.get('/api/admin/users').status_code == 403
    as_user(app_client,'client')
    assert app_client.get('/workbench').status_code == 403
    assert app_client.get('/api/v1/properties').status_code == 403
    assert app_client.get('/api/listing/L2').status_code == 403
    data=app_client.get('/api/client/portal').json
    assert [x['id'] for x in data['items']] == ['L1']
    assert data['items'][0]['photos'] == []
    assert 'agent_id' not in data['items'][0] and 'source' not in data['items'][0]
    assert app_client.post('/api/update/L1',json={'price':1},headers=h).status_code == 403

def test_auth_session_and_login_csrf_fail_closed(app_client):
    r = app_client.get('/api/auth/session')
    assert r.status_code == 200
    assert r.json['user'] is None and r.json['csrf_token']
    assert app_client.post('/login/staff', json={'identifier':'unknown','password':secrets.token_urlsafe(24)}).status_code == 403
    assert app_client.get('/logout').status_code == 405
    assert app_client.post('/logout').status_code == 403
