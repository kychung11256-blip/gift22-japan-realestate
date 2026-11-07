"""Fail-closed request scopes for legacy routes plus collection response scoping."""
from flask import request, abort
import authz as az
import db

def register(app):
    @app.before_request
    def row_scope():
        u=az.effective_user()
        if request.path.startswith('/uploads/') and not u: abort(401)
        login_write = request.path.startswith('/login/') and request.method not in {'GET','HEAD','OPTIONS'}
        if login_write or (u and u['role'] in {'admin','agent'} and request.method not in {'GET','HEAD','OPTIONS'} and request.path!='/logout'):
            if not az.csrf_valid(): az.audit('csrf_denied',request.path); abort(403)
        data = request.get_json() if request.is_json and (u or login_write) else {}
        if not isinstance(data,dict): abort(400)
        if not u: return
        args=request.view_args or {}; p=request.path
        listing_ids=[]; client_ids=[]
        if args.get('listing_id'): listing_ids.append(args['listing_id'])
        if args.get('client_id'): client_ids.append(args['client_id'])
        if p.startswith(('/api/v1/','/api/client-shortlists/','/api/update/','/api/confirm/','/api/upload-photo/','/api/delete-photo/','/api/staging-')):
            for key in ('listingId','listing_id','clientId','client_id'):
                if key in data and not isinstance(data[key],str): abort(400)
            for key in ('listingIds','listing_ids','client_ids'):
                if key in data and (not isinstance(data[key],list) or not all(isinstance(x,str) for x in data[key])): abort(400)
            listing_ids += [x for x in [data.get('listingId'),data.get('listing_id')] if x]
            listing_ids += data.get('listingIds') or data.get('listing_ids') or []
            client_ids += [x for x in [data.get('clientId'),data.get('client_id'),request.args.get('clientId')] if x]
            client_ids += data.get('client_ids') or []
            optimized=data.get('optimized')
            if 'optimized' in data:
                if not isinstance(optimized,dict): abort(400)
                stops=optimized.get('stops',[])
                if not isinstance(stops,list) or not all(isinstance(x,dict) for x in stops): abort(400)
                for stop in stops:
                    if 'listingId' in stop and not isinstance(stop['listingId'],str): abort(400)
                listing_ids += [x['listingId'] for x in stops if x.get('listingId')]
        if 'planId' in data and not isinstance(data['planId'],str): abort(400)
        plan_id=args.get('plan_id') or data.get('planId')
        if plan_id:
            conn=db.get_db(); r=conn.execute('SELECT client_id FROM viewing_plans WHERE id=?',(plan_id,)).fetchone();conn.close()
            if r: client_ids.append(r['client_id'])
        for cid in client_ids:
            if not isinstance(cid,str) or not az.client_allowed(u,cid): az.deny_row('client',str(cid))
        for lid in listing_ids:
            if not isinstance(lid,str) or not az.listing_allowed(u,lid): az.deny_row('listing',str(lid))
        if u['role']=='client':
            # Clients exclusively read own portal, own detail and approved media.
            allowed=(p.startswith(('/api/auth/','/api/listings','/api/search','/api/client/portal','/api/portal/shortlist','/api/customer-safe-image/','/api/public-contact','/login/')) or p=='/logout' or (p.startswith('/api/listing/') and request.method=='GET') or p in {'/client/portal','/portal','/','/listings','/map'} or p.startswith('/listing/'))
            if p.startswith('/api/') and not allowed: az.deny_row('path',p)
        if u['role']=='agent':
            # Legacy global DB/credential operations have no row ownership contract.
            admin_only=('/api/agent/','/api/scrape','/api/dashboard','/api/ai-generate','/api/upload','/api/mysok-','/api/collection/import','/api/collection/search','/api/reins-documents/','/uploads/reins-cache/','/uploads/reins-extracted/_preview/')
            if p.startswith(admin_only): az.deny_row('path',p)
        if p.startswith('/uploads/'):
            # Raw media requires staff plus a positively identified assigned listing.
            if u['role']=='client': az.deny_row('media',p)
            if u['role']=='agent' and not listing_ids:
                conn=db.get_db(); rows=conn.execute('SELECT listing_id FROM media_assets WHERE original_url=?',(p,)).fetchall(); conn.close()
                if not rows or not all(az.listing_allowed(u,r['listing_id']) for r in rows): az.deny_row('media',p)

    @app.after_request
    def scoped_response(resp):
        u=az.effective_user()
        if not resp.is_json or resp.status_code>=400: return resp
        if not u:
            if request.path=='/api/search':
                import server
                d=resp.get_json();d['listings']=[server._public_listing_view(x) for x in d.get('listings',[])];resp.set_data(app.json.dumps(d))
            return resp
        if u['role']=='admin': return resp
        d=resp.get_json()
        if not isinstance(d,dict): return resp
        for key in ('listings','items','properties'):
            if isinstance(d.get(key),list):
                d[key]=[x for x in d[key] if not isinstance(x,dict) or not x.get('id') or az.listing_allowed(u,x['id'])]
                if 'count' in d: d['count']=len(d[key])
        if isinstance(d.get('clients'),list): d['clients']=[x for x in d['clients'] if az.client_allowed(u,x['id'])]
        if isinstance(d.get('plans'),list): d['plans']=[x for x in d['plans'] if x and az.client_allowed(u,x['clientId'])]
        # Public-style legacy search/list projections must never trust raw prefixes.
        if u['role']=='client':
            import server
            for key in ('listings','items','properties','results'):
                if isinstance(d.get(key),list): d[key]=[server._public_listing_view(x) for x in d[key] if isinstance(x,dict) and az.listing_allowed(u,x.get('id'))]
        resp.set_data(app.json.dumps(d)); return resp
