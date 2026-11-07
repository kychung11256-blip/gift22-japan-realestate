#!/usr/bin/env python3
"""Capture UI without server startup. Storage-state secrets are never printed.
--storage-state accepts Playwright state saved by an API login/browser smoke.
"""
import argparse
import asyncio
import hashlib
import os
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

SCREENSHOT_DIR = str(Path(__file__).resolve().parent.parent / 'outputs' /
                     'commercial_phase_1_4_20261107' / 'screenshots')


async def take_screenshot(url, selector=None, wait=3, output=None,
                          click_selector=None, viewport_width=1280,
                          viewport_height=800, storage_state=None, local_root=None):
    if not output:
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        output = os.path.join(SCREENSHOT_DIR, f'{datetime.now():%Y%m%d_%H%M%S}_{digest}.png')
    Path(output).resolve().parent.mkdir(parents=True, exist_ok=True)
    state = storage_state or os.environ.get('SCREENSHOT_STORAGE_STATE')
    if state and not Path(state).is_file():
        raise ValueError('Storage state file does not exist')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={'width': viewport_width, 'height': viewport_height},
                storage_state=state if state else None)
            if local_root:
                root = Path(local_root).resolve()
                async def local_asset(route):
                    from urllib.parse import urlparse, unquote
                    relative = unquote(urlparse(route.request.url).path).lstrip('/')
                    candidate = (root / relative).resolve()
                    if candidate.is_relative_to(root) and candidate.is_file() and candidate.suffix in {'.html', '.css', '.js', '.woff2'}:
                        await route.fulfill(path=str(candidate))
                    else:
                        await route.abort()
                await context.route('**/*', local_asset)
            page = await context.new_page()
            await page.goto(url, wait_until='networkidle', timeout=30000)
            if wait:
                await asyncio.sleep(wait)
            if selector:
                await page.wait_for_selector(selector, timeout=10000)
            if click_selector:
                await page.click(click_selector, timeout=5000)
                await asyncio.sleep(0.3)
            await page.screenshot(path=output, full_page=False)
            os.chmod(output, 0o600)
            print(output)
            return output
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--selector')
    parser.add_argument('--wait', type=float, default=3)
    parser.add_argument('--output')
    parser.add_argument('--click', dest='click_selector')
    parser.add_argument('--width', dest='viewport_width', type=int, default=1280)
    parser.add_argument('--height', dest='viewport_height', type=int, default=800)
    parser.add_argument('--local-root', help='Capture real static repo files without a server; API requests blocked')
    parser.add_argument('--storage-state', help='Private Playwright JSON state from API/browser login')
    args = vars(parser.parse_args())
    asyncio.run(take_screenshot(**args))


if __name__ == '__main__':
    main()
