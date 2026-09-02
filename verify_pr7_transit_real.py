import asyncio
import json
import re
from pathlib import Path

import requests
from playwright.async_api import async_playwright

BASE = Path(__file__).resolve().parent
BASE_URL = 'http://127.0.0.1:8901'
CLIENT_ID = 'CL-5240AD6D83C7'
LISTING_IDS = ['REINS20260901044118B1B6', 'REINS20260901044119F382']
SHOT_DIR = BASE / 'verify_screenshots'
SHOT_DIR.mkdir(exist_ok=True)
OUT = BASE / 'verify_pr7_transit_real_result.json'


def load_env():
    text = (BASE / '.env').read_text()
    def val(k, default=''):
        m = re.search(r'^' + re.escape(k) + r'=(.*)$', text, re.M)
        return (m.group(1).strip() if m else default)
    return val('WORKBENCH_USER', 'johnny'), val('WORKBENCH_PASSWORD', '')


def api_repro(user, pw):
    s = requests.Session(); s.auth = (user, pw)
    body = {
        'clientId': CLIENT_ID,
        'listingIds': LISTING_IDS,
        'viewingDate': '2026-09-03',
        'departureTime': '10:00',
        'start': {'label': '東京駅', 'lat': 35.681236, 'lon': 139.767125},
        'end': {'label': '東京駅', 'lat': 35.681236, 'lon': 139.767125},
        'viewingDurationMin': 45,
        'travelMode': 'transit',
    }
    r = s.post(BASE_URL + '/api/v1/viewing-plans/optimize', json=body, headers={'X-Request-ID':'PR7-REAL-TRANSIT'}, timeout=90)
    safe = {'path': '/api/v1/viewing-plans/optimize', 'status': r.status_code, 'content_type': r.headers.get('content-type'), 'request_id': r.headers.get('x-request-id')}
    try:
        d = r.json()
        safe['json'] = {
            'code': d.get('code'),
            'error': d.get('error'),
            'errorCode': d.get('errorCode'),
            'requestId': d.get('requestId'),
            'provider': d.get('provider'),
            'travelMode': d.get('travelMode'),
            'stopCount': len(d.get('stops', [])) if isinstance(d, dict) else None,
            'durations': [x.get('travelMinutes') for x in d.get('stops', [])] if isinstance(d, dict) else [],
            'routeGeometryType': (d.get('routeGeometry') or {}).get('type') if isinstance(d, dict) else None,
            'warnings': d.get('warnings') if isinstance(d, dict) else None,
        }
    except Exception:
        safe['text_prefix'] = r.text[:500]
    return safe


async def ui_run(user, pw):
    result = {'screenshots': {}, 'network': {}, 'console_errors': []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(http_credentials={'username': user, 'password': pw}, viewport={'width': 1440, 'height': 900})
        page = await ctx.new_page()
        page.on('console', lambda msg: result['console_errors'].append(msg.text) if msg.type == 'error' else None)
        async def capture(resp):
            if '/api/v1/viewing-plans/optimize' in resp.url:
                result['network'] = {'url_path': '/api/v1/viewing-plans/optimize', 'status': resp.status, 'content_type': resp.headers.get('content-type',''), 'request_id': resp.headers.get('x-request-id','')}
                try:
                    d = await resp.json()
                    result['network']['json'] = {'code': d.get('code'), 'error': d.get('error'), 'errorCode': d.get('errorCode'), 'requestId': d.get('requestId')}
                except Exception:
                    txt = await resp.text()
                    result['network']['text_prefix'] = txt[:500]
        page.on('response', capture)
        await page.goto(f'{BASE_URL}/workbench/viewing-planner?clientId={CLIENT_ID}', wait_until='networkidle')
        await page.wait_for_selector('#plannerMap canvas', timeout=30000)
        await page.wait_for_function("() => document.querySelectorAll('[data-pick]').length >= 2", timeout=30000)
        # Select exactly Johnny's two listings.
        await page.locator('[data-pick]').evaluate_all("els => els.forEach(e => e.checked && e.click())")
        for lid in LISTING_IDS:
            await page.locator(f'[data-pick="{lid}"]').check()
        await page.select_option('#routeMode', 'transit')
        result['screenshots']['desktop_before'] = str(SHOT_DIR / 'pr7_transit_desktop_before.png')
        await page.screenshot(path=result['screenshots']['desktop_before'], full_page=False)
        await page.click('#optimizeBtn')
        await page.wait_for_function("() => document.querySelector('#routeResult') && document.querySelector('#routeResult').innerText.includes('優化失敗')", timeout=90000)
        result['result_text'] = (await page.locator('#routeResult').inner_text())[:1500]
        result['shortlist_text'] = (await page.locator('#shortlist').inner_text())[:1500]
        result['diagnostics'] = await page.evaluate('window.__plannerDiagnostics && window.__plannerDiagnostics()')
        result['screenshots']['desktop_after'] = str(SHOT_DIR / 'pr7_transit_desktop_after.png')
        await page.screenshot(path=result['screenshots']['desktop_after'], full_page=False)
        mobile = await browser.new_context(http_credentials={'username': user, 'password': pw}, viewport={'width': 390, 'height': 844}, is_mobile=True)
        mp = await mobile.new_page()
        await mp.goto(f'{BASE_URL}/workbench/viewing-planner?clientId={CLIENT_ID}', wait_until='networkidle')
        await mp.wait_for_selector('#plannerMap canvas', timeout=30000)
        await mp.locator('[data-tab="settings"]').click()
        await mp.wait_for_function("() => document.querySelectorAll('[data-pick]').length >= 2", timeout=30000)
        await mp.locator('[data-pick]').evaluate_all("els => els.forEach(e => e.checked && e.click())")
        for lid in LISTING_IDS:
            await mp.locator(f'[data-pick="{lid}"]').check()
        await mp.select_option('#routeMode', 'transit')
        result['screenshots']['mobile_settings'] = str(SHOT_DIR / 'pr7_transit_mobile_settings.png')
        await mp.screenshot(path=result['screenshots']['mobile_settings'], full_page=False)
        await mp.click('#mobileOptimizeBtn')
        await mp.locator('[data-tab="result"]').click()
        await mp.wait_for_function("() => document.querySelector('#routeResult') && document.querySelector('#routeResult').innerText.includes('優化失敗')", timeout=90000)
        result['mobile_result_text'] = (await mp.locator('#routeResult').inner_text())[:1500]
        result['screenshots']['mobile_error'] = str(SHOT_DIR / 'pr7_transit_mobile_error.png')
        await mp.screenshot(path=result['screenshots']['mobile_error'], full_page=False)
        await browser.close()
    return result


async def main():
    user, pw = load_env()
    result = {'api': api_repro(user, pw), 'ui': await ui_run(user, pw)}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))

asyncio.run(main())
