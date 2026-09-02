import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

BASE = Path(__file__).resolve().parent
BASE_URL = 'http://127.0.0.1:8901'
CLIENT_ID = 'CL-5240AD6D83C7'
LISTING_IDS = ['REINS20260901044118B1B6', 'REINS20260901044119F382']
SHOT_DIR = BASE / 'verify_screenshots'
SHOT_DIR.mkdir(exist_ok=True)
OUT = BASE / 'verify_pr7_transit_mock_ui_result.json'


def load_env():
    text = (BASE / '.env').read_text()
    def val(k, default=''):
        m = re.search(r'^' + re.escape(k) + r'=(.*)$', text, re.M)
        return (m.group(1).strip() if m else default)
    return val('WORKBENCH_USER', 'johnny'), val('WORKBENCH_PASSWORD', '')


async def run():
    user, pw = load_env()
    result = {'screenshots': {}, 'network': {}, 'console_errors': []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for name, viewport, mobile in [('desktop', {'width':1440,'height':900}, False), ('mobile', {'width':390,'height':844}, True)]:
            ctx = await browser.new_context(http_credentials={'username': user, 'password': pw}, viewport=viewport, is_mobile=mobile)
            page = await ctx.new_page()
            page.on('console', lambda msg: result['console_errors'].append(f'{name}:{msg.text}') if msg.type == 'error' else None)
            async def capture(resp):
                if '/api/v1/viewing-plans/optimize' in resp.url:
                    result['network'][name] = {'status': resp.status, 'content_type': resp.headers.get('content-type','')}
                    try:
                        d = await resp.json()
                        result['network'][name]['json'] = {'code': d.get('code'), 'provider': d.get('provider'), 'travelMode': d.get('travelMode'), 'durations': [s.get('travelMinutes') for s in d.get('stops', [])], 'routeGeometryType': (d.get('routeGeometry') or {}).get('type') if isinstance(d, dict) else None}
                    except Exception as e:
                        result['network'][name]['json_error'] = repr(e)
            page.on('response', capture)
            await page.goto(f'{BASE_URL}/workbench/viewing-planner?clientId={CLIENT_ID}', wait_until='networkidle')
            await page.wait_for_selector('#plannerMap canvas', timeout=30000)
            await page.wait_for_function("() => document.querySelectorAll('[data-pick]').length >= 2", timeout=30000)
            await page.evaluate("""(ids) => {
              const oldFetch = window.fetch.bind(window);
              window.fetch = (url, options={}) => {
                if (String(url).includes('/api/v1/viewing-plans/optimize')) {
                  const body = JSON.parse(options.body || '{}');
                  body.listingIds = ids;
                  body.mockDurations = Array.from({length: ids.length}, (_, i) => 11 + i * 7);
                  options = {...options, body: JSON.stringify(body)};
                }
                return oldFetch(url, options);
              };
            }""", LISTING_IDS)
            await page.locator('[data-pick]').evaluate_all("els => els.forEach(e => e.checked && e.click())")
            for lid in LISTING_IDS:
                await page.locator(f'[data-pick="{lid}"]').check()
            await page.select_option('#routeMode', 'transit')
            if mobile:
                await page.locator('[data-tab="settings"]').click()
                await page.click('#mobileOptimizeBtn')
                await page.locator('[data-tab="result"]').click()
            else:
                await page.click('#optimizeBtn')
            await page.wait_for_function("() => window.__plannerDiagnostics && window.__plannerDiagnostics().plan && window.__plannerDiagnostics().plan.stops === 2", timeout=30000)
            result[f'{name}_result_text'] = (await page.locator('#routeResult').inner_text())[:1000]
            result['screenshots'][name] = str(SHOT_DIR / f'pr7_transit_mock_{name}.png')
            await page.screenshot(path=result['screenshots'][name], full_page=False)
            await ctx.close()
        await browser.close()
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))

asyncio.run(run())
