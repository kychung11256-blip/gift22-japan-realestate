#!/usr/bin/env python3
"""
Screenshot helper for Hermes browser automation.
Usage: python3 screenshot_tool.py <url> [--selector <css>] [--wait <seconds>] [--output <path>]
"""

import asyncio
import argparse
import os
import sys
from datetime import datetime
from playwright.async_api import async_playwright

SCREENSHOT_DIR = "/home/ubuntu/ai-team/platform/verify_screenshots"

async def take_screenshot(url, selector=None, wait=3, output=None, click_selector=None, viewport_width=1280, viewport_height=800):
    """
    Open URL, wait for load, optionally click element, take screenshot.
    Returns screenshot path.
    """
    if not output:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_url = url.replace("://", "_").replace("/", "_").replace("?", "_").replace("&", "_")
        output = os.path.join(SCREENSHOT_DIR, f"{timestamp}_{safe_url}.png")

    os.makedirs(os.path.dirname(output), exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": viewport_width, "height": viewport_height})

        try:
            # Navigate to URL
            await page.goto(url, wait_until="networkidle", timeout=30000)

            # Wait for JS render (especially important for MapLibre)
            if wait > 0:
                await asyncio.sleep(wait)

            # Wait for specific selector if provided
            if selector:
                try:
                    await page.wait_for_selector(selector, timeout=10000)
                except Exception as e:
                    print(f"Warning: selector '{selector}' not found: {e}", file=sys.stderr)

            # Click element if specified
            if click_selector:
                try:
                    await page.click(click_selector, timeout=5000)
                    await asyncio.sleep(1)  # Wait for click effect
                except Exception as e:
                    print(f"Warning: click on '{click_selector}' failed: {e}", file=sys.stderr)

            # Take screenshot
            await page.screenshot(path=output, full_page=False)
            print(output)
            return output

        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            # Still try to take screenshot even on error
            try:
                await page.screenshot(path=output, full_page=False)
                print(output)
                return output
            except:
                return None
        finally:
            await browser.close()

def main():
    parser = argparse.ArgumentParser(description="Take screenshot of webpage")
    parser.add_argument("url", help="URL to open")
    parser.add_argument("--selector", help="CSS selector to wait for")
    parser.add_argument("--wait", type=int, default=3, help="Seconds to wait after load")
    parser.add_argument("--output", help="Output file path")
    parser.add_argument("--click", dest="click_selector", help="CSS selector to click before screenshot")
    parser.add_argument("--width", type=int, default=1280, help="Viewport width")
    parser.add_argument("--height", type=int, default=800, help="Viewport height")

    args = parser.parse_args()

    result = asyncio.run(take_screenshot(
        args.url,
        selector=args.selector,
        wait=args.wait,
        output=args.output,
        click_selector=args.click_selector,
        viewport_width=args.width,
        viewport_height=args.height
    ))

    if result:
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
