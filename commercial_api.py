"""Commercial endpoints, deliberately no upstream/network calls."""
import os
import uuid
import sqlite3
from pathlib import Path
from flask import jsonify, request, session, send_from_directory, send_file, redirect, abort
import db
import authz as az

def register(app):
    @app.get('/privacy')
    def privacy_page():
        return send_from_directory(app.root_path, 'privacy.html')

    @app.get('/terms')
    def terms_page():
        return send_from_directory(app.root_path, 'terms.html')

    @app.get('/media-review')
    @app.get('/admin/media-review')
    @az.require_roles('admin', 'agent')
    def media_review_page():
        return send_from_directory(app.root_path, 'media_review.html')

    @app.get('/admin/accounts')
    @az.require_roles('admin')
    def admin_accounts_page():
        return send_from_directory(app.root_path, 'admin_users.html')

    @app.route('/api/auth/session')
    def auth_session():
        return jsonify(user=az.public_user(az.effective_user()),csrf_token=az.csrf_token())

    def login(kind):
        if request.method == 'GET':
            az.csrf_token()
            return send_from_directory(app.root_path, f'{kind}_login.html')
        if not az.csrf_valid():
            az.audit('csrf_denied',request.path); abort(403)
        data = request.get_json(silent=True) or request.form
        user, error = az.check_login(str(data.get('identifier') or '')[:200],str(data.get('password') or '')[:1024],{'admin','agent'} if kind == 'staff' else {'client'})
        if error:
            az.audit('login_failed',kind)
            return jsonify(code=0,error='登入資料不正確' if error != 'rate_limited' else '請求太頻密'),429 if error == 'rate_limited' else 401
        az.login_user(user); az.audit('login_success','user',user['id'],True)
        dest = '/workbench' if kind == 'staff' else '/client/portal'
        return jsonify(code=1,user=az.public_user(user),role=user['role'],csrf_token=az.csrf_token(),csrfToken=az.csrf_token(),redirect=dest)
    app.add_url_rule('/login/staff','login_staff',lambda:login('staff'),methods=['GET','POST'])
    app.add_url_rule('/login/client','login_client',lambda:login('client'),methods=['GET','POST'])

    @app.route('/logout',methods=['GET','POST'])
    def logout():
        if request.method == 'GET': abort(405)
        if not az.csrf_valid(): az.audit('csrf_denied',request.path); abort(403)
        az.audit('logout','user',session.get('user_id',''),True); session.clear()
        return jsonify(code=1)

    @app.route('/admin')
    @az.require_roles('admin','agent')
    def admin_page(): return redirect('/workbench')

    @app.route('/client/portal')
    @app.route('/portal')
    @az.require_roles('client')
    def portal_page(): return send_from_directory(app.root_path,'client_portal.html')

    @app.route('/api/client/portal')
    @app.route('/api/portal/shortlist')
    @az.require_roles('client')
    def portal_api():
        import server
        user = az.current_user(); cid = user.get('client_id')
        if not cid: abort(403)
        conn=db.get_db()
        rows=conn.execute('SELECT l.* FROM listings l JOIN client_shortlists cs ON cs.listing_id=l.id WHERE cs.client_id=?',(cid,)).fetchall()
        client=conn.execute('SELECT id,name FROM clients WHERE id=?',(cid,)).fetchone(); conn.close()
        return jsonify(code=1,clientId=cid,client=dict(client) if client else None,items=[server._public_listing_view(server._row_to_dict(r)) for r in rows])

    @app.route('/api/admin/users',methods=['GET','POST','PATCH'])
    @app.route('/admin/users',methods=['GET','POST','PATCH'])
    @az.require_roles('admin')
    def admin_users():
        conn=db.get_db()
        try:
            if request.method == 'GET':
                rows=conn.execute('SELECT id,username,email,role,agent_id,client_id,status FROM users').fetchall()
                return jsonify(code=1,users=[dict(r) for r in rows])
            d=request.get_json(silent=True) or {}
            if d.get('action') == 'disable':
                uid=str(d.get('id') or '')
                if uid == (az.effective_user() or {}).get('id'): return jsonify(error='cannot disable self'),400
                conn.execute("UPDATE users SET status='disabled',updated_at=datetime('now') WHERE id=?",(uid,)); conn.commit()
                az.audit('user_disable','user',uid,True); return jsonify(code=1)
            username=str(d.get('username') or '').strip().lower(); email=str(d.get('email') or username).strip().lower()
            role=d.get('role'); password=str(d.get('password') or '')
            cid=d.get('client_id') or None; aid=str(d.get('agent_id') or '')
            if role not in az.ROLES or not username or len(username)>120 or len(email)>200 or not 12<=len(password)<=1024:
                return jsonify(error='invalid user (password minimum 12 characters)'),400
            if role=='client' and (not cid or not conn.execute('SELECT 1 FROM clients WHERE id=?',(cid,)).fetchone()): return jsonify(error='valid client_id required'),400
            uid='U-'+uuid.uuid4().hex[:12]
            if role=='agent' and not aid: aid=uid
            conn.execute('INSERT INTO users(id,username,email,role,password_hash,agent_id,client_id) VALUES (?,?,?,?,?,?,?)',(uid,username,email,role,az.password_hash(password),aid,cid))
            conn.commit(); az.audit('user_create','user',uid,True)
            return jsonify(code=1,user={'id':uid,'username':username,'email':email,'role':role,'agent_id':aid,'client_id':cid}),201
        except sqlite3.IntegrityError:
            return jsonify(error='user conflict'),409
        finally: conn.close()

    @app.route('/api/admin/assignments',methods=['GET','POST','DELETE'])
    @app.route('/admin/assignments',methods=['GET','POST','DELETE'])
    @az.require_roles('admin')
    def assignments():
        conn=db.get_db()
        try:
            if request.method=='GET': return jsonify(code=1,assignments=[dict(r) for r in conn.execute('SELECT * FROM agent_client_assignments')])
            d=request.get_json(silent=True) or {}; aid=str(d.get('agent_id') or ''); cid=str(d.get('client_id') or '')
            if not conn.execute("SELECT 1 FROM users WHERE role='agent' AND status='active' AND agent_id=?",(aid,)).fetchone() or not conn.execute('SELECT 1 FROM clients WHERE id=?',(cid,)).fetchone(): return jsonify(error='invalid agent/client'),400
            if request.method=='DELETE': conn.execute('DELETE FROM agent_client_assignments WHERE agent_id=? AND client_id=?',(aid,cid))
            else: conn.execute('INSERT OR IGNORE INTO agent_client_assignments(agent_id,client_id) VALUES (?,?)',(aid,cid))
            conn.commit(); az.audit('assignment_'+request.method.lower(),'client',cid,True)
            return jsonify(code=1)
        finally: conn.close()

    @app.route('/api/public-contact')
    def contact():
        out={'brand':os.getenv('PUBLIC_BRAND_NAME','出雲ランドエステー'),'email':os.getenv('PUBLIC_CONTACT_EMAIL','akira@izumole.com'),'whatsapp_display':os.getenv('PUBLIC_WHATSAPP_DISPLAY','+81 80-9190-7999'),'whatsapp_url':os.getenv('PUBLIC_WHATSAPP_URL','https://wa.me/818091907999'),'label':os.getenv('PUBLIC_CONTACT_LABEL','聯絡出雲ランドエステー'),'whatsapp_qr_url':'/api/public-contact-qr/whatsapp','wechat_qr_url':'/api/public-contact-qr/wechat'}
        return jsonify(out)

    @app.route('/api/public-contact-qr/<kind>')
    def qr(kind):
        if kind not in {'whatsapp','wechat'}: abort(404)
        p=Path(os.getenv('PUBLIC_'+kind.upper()+'_QR_PATH',os.getenv('CONTACT_'+kind.upper()+'_QR_PATH',f'/home/ubuntu/ai-team/brand-assets/official/{kind}-qr.jpg')))
        if not p.is_file(): abort(404)
        r=send_file(p.resolve(),mimetype='image/jpeg',max_age=3600)
        r.headers['Cache-Control']='public, max-age=3600'; r.headers['X-Content-Type-Options']='nosniff'; return r
