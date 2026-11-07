"""Final UI contract regression; all media mutations isolated to tmp_path."""
import pytest
from test_commercial_acceptance_security import client, credentials, local_asset, impersonate

@pytest.mark.parametrize('uid,status', [(None,401),('admin',200),('agent',200),('client',403)])
def test_canonical_admin_media_review_page(client, uid, status):
    if uid: impersonate(client,uid)
    r=client.get('/admin/media-review')
    assert r.status_code==status
    if status==200:
        assert r.mimetype=='text/html'
        assert b'originalImage' in r.data and b'derivativeImage' in r.data

@pytest.mark.parametrize('uid', ['admin','agent'])
def test_frontend_contract_and_both_preview_bytes(client, local_asset, uid):
    headers=impersonate(client,uid)
    groups=client.get('/api/admin/media-review/listings').json['listings']
    assert len(groups)==1 and groups[0]['needs_review']==1 and groups[0]['customer_safe']==0
    d=client.get('/api/admin/media-review/listings/L1').json
    assert d['listing']['id']=='L1'
    a=d['assets'][0]
    assert a['visibility']=='needs_review' and a['manifest']['crop_bottom_pct']==15
    for key in ('original_preview_url','derivative_preview_url'):
        r=client.get(a[key]); assert r.status_code==200 and r.mimetype.startswith('image/') and r.data
    # No real review action: missing CSRF must deny before regeneration.
    r=client.post('/api/admin/media-review/regenerate/L1/'+a['asset_id'],json={'crop_bottom_pct':15})
    assert r.status_code==403
    r=client.post('/api/admin/media-review/approve/L1/'+a['asset_id'],json={'derivative_fingerprint':a['derivative_fingerprint']},headers=headers)
    assert r.status_code==400
    assert client.get('/api/admin/media-review/listings/L1').json['assets'][0]['visibility']=='needs_review'
