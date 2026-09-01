import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path
from playwright.async_api import async_playwright

BASE = Path(__file__).resolve().parent
BASE_URL = 'http://127.0.0.1:8901'
CLIENT_ID = 'CL-5240AD6D83C7'
SHOT_DIR = BASE / 'verify_screenshots'
SHOT_DIR.mkdir(exist_ok=True)
OUT = BASE / 'verify_viewing_realmaps_result.json'
DB = BASE / 'data' / 'listings.db'

def load_env():
    env = {}
    for line in (BASE / '.env').read_text().splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k,v=line.split('=',1); env[k.strip()]=v.strip()
    return env

def counts():
    con=sqlite3.connect(DB); con.row_factory=sqlite3.Row
    out={
      'listings': con.execute('select count(*) c from listings').fetchone()['c'],
      'clients': con.execute('select count(*) c from clients').fetchone()['c'],
      'shortlists': con.execute('select count(*) c from client_shortlists').fetchone()['c'],
      'viewing_plans': con.execute('select count(*) c from viewing_plans').fetchone()['c'],
      'viewing_plan_stops': con.execute('select count(*) c from viewing_plan_stops').fetchone()['c'],
    }
    con.close(); return out

async def wait_map(page, diag_name='__plannerDiagnostics'):
    await page.wait_for_function(f"() => window.{diag_name} && window.{diag_name}().map && window.{diag_name}().map.canvasWidth > 0 && window.{diag_name}().map.canvasHeight > 0", timeout=30000)
    return await page.evaluate(f"window.{diag_name}()")

async def main():
    env=load_env(); user=env.get('WORKBENCH_USER','johnny'); pw=env.get('WORKBENCH_PASSWORD','')
    result={'base_url':BASE_URL,'client_id':CLIENT_ID,'before':counts(),'console_errors':[],'screenshots':{},'noted_non_blocking_404s':[]}
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        ctx=await browser.new_context(http_credentials={'username':user,'password':pw}, viewport={'width':1440,'height':900})
        page=await ctx.new_page()
        def record_console(prefix=''):
            def inner(msg):
                text=prefix+msg.text
                if msg.type == 'error':
                    if 'Failed to load resource' in text and '404' in text:
                        result['noted_non_blocking_404s'].append(text)
                    else:
                        result['console_errors'].append(text)
            return inner
        page.on('console', record_console())
        await page.goto(BASE_URL+'/workbench', wait_until='networkidle')
        result['screenshots']['before_workbench']=str(SHOT_DIR/'viewing_before_workbench.png')
        await page.screenshot(path=result['screenshots']['before_workbench'], full_page=False)
        await page.select_option('#clientFilter', CLIENT_ID)
        await page.click('#arrangeViewing')
        await page.wait_for_url('**/workbench/viewing-planner**')
        await page.wait_for_selector('#plannerMap canvas')
        diag_initial=await wait_map(page)
        result['screenshots']['desktop_planner_initial']=str(SHOT_DIR/'viewing_planner_fullscreen_desktop.png')
        await page.screenshot(path=result['screenshots']['desktop_planner_initial'], full_page=False)
        await page.wait_for_function("() => document.querySelectorAll('[data-pick]:checked').length >= 2", timeout=30000)
        checked=await page.locator('[data-pick]:checked').count()
        result['selected_initial']=checked
        if checked < 2:
            await page.locator('[data-pick]:not(:disabled)').nth(0).check()
            await page.locator('[data-pick]:not(:disabled)').nth(1).check()
        await page.locator('#optimizeBtn').click()
        await page.wait_for_function("() => window.__plannerDiagnostics && window.__plannerDiagnostics().plan && window.__plannerDiagnostics().plan.hasGeometry", timeout=60000)
        try:
            await page.wait_for_function("() => window.__plannerDiagnostics && window.__plannerDiagnostics().map && window.__plannerDiagnostics().map.hasRouteLayer", timeout=10000)
        except Exception:
            # If MapLibre load and optimize complete in an unlucky order, explicitly re-render the real provider geometry.
            await page.evaluate("() => { if (typeof state !== 'undefined' && state.map && (state.optimized || state.plan)) state.map.render({plan: state.optimized || state.plan}); }")
        await page.wait_for_function("() => window.__plannerDiagnostics && window.__plannerDiagnostics().map && window.__plannerDiagnostics().map.hasRouteLayer", timeout=60000)
        diag_opt=await page.evaluate('window.__plannerDiagnostics()')
        result['screenshots']['desktop_optimized']=str(SHOT_DIR/'viewing_route_real_map_optimized_desktop.png')
        await page.screenshot(path=result['screenshots']['desktop_optimized'], full_page=False)
        await page.click('#savePlan')
        await page.wait_for_selector('.share-box a')
        share=await page.locator('.share-box a').get_attribute('href')
        result['share_url']=share
        # Reopen stable URL
        await page.goto(BASE_URL + '/workbench/viewing-planner?clientId='+CLIENT_ID, wait_until='networkidle')
        await page.wait_for_selector('[data-open-plan]', state='attached')
        await page.evaluate("() => document.querySelector('#savedList')?.classList.add('open')")
        await page.locator('[data-open-plan]').first.click()
        await page.wait_for_function("() => window.__plannerDiagnostics && window.__plannerDiagnostics().plan && window.__plannerDiagnostics().map.hasRouteLayer", timeout=30000)
        diag_reopen=await page.evaluate('window.__plannerDiagnostics()')
        result['screenshots']['desktop_saved_reopen']=str(SHOT_DIR/'viewing_route_saved_reopen_desktop.png')
        await page.screenshot(path=result['screenshots']['desktop_saved_reopen'], full_page=False)
        sp=await ctx.new_page()
        sp.on('console', record_console('share:'))
        await sp.goto(BASE_URL+share, wait_until='networkidle')
        await sp.wait_for_selector('#shareMap canvas')
        await sp.wait_for_function("() => window.__shareDiagnostics && window.__shareDiagnostics().map && window.__shareDiagnostics().map.hasRouteLayer", timeout=30000)
        diag_share=await sp.evaluate('window.__shareDiagnostics()')
        result['screenshots']['share_page_map']=str(SHOT_DIR/'viewing_share_real_map_desktop.png')
        await sp.screenshot(path=result['screenshots']['share_page_map'], full_page=False)
        mobile=await browser.new_context(http_credentials={'username':user,'password':pw}, viewport={'width':390,'height':844}, is_mobile=True)
        mp=await mobile.new_page()
        mp.on('console', record_console('mobile:'))
        await mp.goto(BASE_URL + '/workbench/viewing-planner?clientId='+CLIENT_ID, wait_until='networkidle')
        await mp.wait_for_selector('#plannerMap canvas')
        await wait_map(mp)
        await mp.locator('[data-tab="map"]').click()
        await mp.wait_for_timeout(800)
        result['screenshots']['mobile_planner_map']=str(SHOT_DIR/'viewing_planner_fullscreen_mobile.png')
        await mp.screenshot(path=result['screenshots']['mobile_planner_map'], full_page=False)
        result['diagnostics']={'initial':diag_initial,'optimized':diag_opt,'reopen':diag_reopen,'share':diag_share}
        await browser.close()
    result['after']=counts()
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))

asyncio.run(main())
