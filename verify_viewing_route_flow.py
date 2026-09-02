import asyncio
import base64
import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright

BASE = Path(__file__).resolve().parent
DB = BASE / 'data' / 'listings.db'
BASE_URL = 'http://127.0.0.1:8901'
CLIENT_ID = 'CL-5240AD6D83C7'
OUT = BASE / 'verify_viewing_route_result.json'
SHOT_DIR = BASE / 'verify_screenshots'
SHOT_DIR.mkdir(exist_ok=True)


def creds():
    env = (BASE / '.env').read_text()
    user = re.search(r'^WORKBENCH_USER=(.*)$', env, re.M).group(1).strip() or 'johnny'
    pw = re.search(r'^WORKBENCH_PASSWORD=(.*)$', env, re.M).group(1).strip()
    return user, pw


def counts():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    out = {
        'listings_total': conn.execute('select count(*) c from listings').fetchone()['c'],
        'listings_by_status_source': [dict(r) for r in conn.execute('select status, source, count(*) count from listings group by status, source order by status, source')],
        'clients': conn.execute('select count(*) c from clients').fetchone()['c'],
        'shortlists': conn.execute('select count(*) c from client_shortlists').fetchone()['c'],
        'viewing_plans': conn.execute('select count(*) c from viewing_plans').fetchone()['c'],
        'viewing_plan_stops': conn.execute('select count(*) c from viewing_plan_stops').fetchone()['c'],
        'statuses': [dict(r) for r in conn.execute('select id,status from listings order by id')],
    }
    conn.close()
    return out


async def main():
    user, pw = creds()
    auth = base64.b64encode(f'{user}:{pw}'.encode()).decode()
    result = {'base_url': BASE_URL, 'client_id': CLIENT_ID, 'before': counts(), 'screenshots': {}, 'requests': {'forbidden': []}}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(http_credentials={'username': user, 'password': pw}, viewport={'width': 1440, 'height': 980})
        page = await ctx.new_page()
        page.on('request', lambda req: result['requests']['forbidden'].append(req.url) if any(x in req.url for x in ['/api/staging-generate','/api/review','/api/import','/api/publish']) else None)
        await page.goto(BASE_URL + '/workbench', wait_until='networkidle')
        await page.select_option('#clientFilter', CLIENT_ID)
        await page.click('#arrangeViewing')
        await page.wait_for_selector('#viewingPlannerModal .route-list')
        result['screenshots']['planner'] = str(SHOT_DIR / 'viewing_planner_desktop.png')
        await page.screenshot(path=result['screenshots']['planner'], full_page=False)

        selected = await page.locator('[data-route-pick]:checked').count()
        route_eligible = await page.locator('[data-route-pick]:not(:disabled)').count()
        result['selected_count'] = selected
        result['route_eligible_count'] = route_eligible
        result['client_shortlist_titles'] = await page.locator('.route-item strong').all_inner_texts()

        # Use staging-only mocked durations to avoid external provider dependency while exercising UI/API save/share.
        await page.evaluate("""
        () => {
          const oldFetch = window.fetch.bind(window);
          window.fetch = (url, options={}) => {
            if (String(url).includes('/api/v1/viewing-plans/optimize')) {
              const body = JSON.parse(options.body || '{}');
              body.mockDurations = Array.from({length: (body.listingIds || []).length}, (_, i) => 12 + i * 8);
              options = {...options, body: JSON.stringify(body)};
            }
            return oldFetch(url, options);
          };
        }
        """)
        await page.click('#routeOptimize')
        try:
            await page.wait_for_selector('.route-marker', timeout=10000)
        except Exception:
            result['optimize_debug_text'] = await page.locator('body').inner_text()
            try:
                result['optimize_debug_html'] = await page.locator('#viewingPlannerModal').evaluate('el => el.innerHTML')
            except Exception as dbg_e:
                result['optimize_debug_html'] = str(dbg_e)
            OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            raise
        result['screenshots']['optimized'] = str(SHOT_DIR / 'viewing_route_optimized_timeline.png')
        await page.screenshot(path=result['screenshots']['optimized'], full_page=False)
        result['optimized_markers'] = await page.locator('.route-marker').count()
        result['timeline_text'] = (await page.locator('#routeResult').inner_text())[:1200]
        await page.click('#routeReorder')
        await page.wait_for_timeout(500)
        result['after_reorder_text'] = (await page.locator('#routeResult').inner_text())[:800]
        await page.click('#routeSave')
        await page.wait_for_selector('.share-box a')
        share_href = await page.locator('.share-box a').get_attribute('href')
        result['share_href'] = share_href
        result['saved_plan_text'] = (await page.locator('.share-box').inner_text())[:500]

        # Reopen saved itinerary in Workbench.
        await page.click('[data-close]')
        await page.click('#arrangeViewing')
        await page.wait_for_selector('[data-open-plan]')
        await page.click('[data-open-plan]')
        await page.wait_for_selector('.route-marker')
        result['reopened_text'] = (await page.locator('#routeResult').inner_text())[:800]

        share_url = share_href if share_href.startswith('http') else urljoin(BASE_URL, share_href)
        share_page = await ctx.new_page()
        await share_page.goto(share_url, wait_until='networkidle')
        result['screenshots']['share'] = str(SHOT_DIR / 'viewing_client_share_page.png')
        await share_page.screenshot(path=result['screenshots']['share'], full_page=False)
        result['share_title'] = await share_page.locator('h1').inner_text()
        result['share_has_workbench_controls'] = await share_page.locator('text=撤回分享').count() + await share_page.locator('text=重新產生分享').count()
        result['share_meta_noindex'] = await share_page.locator('meta[name="robots"]').get_attribute('content')
        await share_page.close()

        await page.click('#routeRevoke')
        await page.wait_for_timeout(500)
        revoked_api = await ctx.request.get(urljoin(BASE_URL, '/api/share/viewing/' + share_url.rstrip('/').split('/')[-1]))
        revoked_page = await ctx.request.get(share_url)
        result['revoked_share_api_http'] = revoked_api.status
        result['revoked_share_page_http'] = revoked_page.status

        mobile = await browser.new_context(http_credentials={'username': user, 'password': pw}, viewport={'width': 390, 'height': 844}, is_mobile=True)
        mp = await mobile.new_page()
        await mp.goto(BASE_URL + '/workbench', wait_until='networkidle')
        await mp.select_option('#clientFilter', CLIENT_ID)
        await mp.click('#arrangeViewing')
        await mp.wait_for_selector('#viewingPlannerModal .route-list')
        result['screenshots']['mobile'] = str(SHOT_DIR / 'viewing_planner_mobile.png')
        await mp.screenshot(path=result['screenshots']['mobile'], full_page=False)
        await mobile.close()
        await browser.close()

    result['after'] = counts()
    result['status_mutations'] = [a for a,b in zip(result['before']['statuses'], result['after']['statuses']) if a != b]
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))

asyncio.run(main())
