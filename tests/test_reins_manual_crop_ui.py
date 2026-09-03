import io
import json
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
LISTING_HTML = ROOT / "listing.html"


def _png_bytes(width=1000, height=800):
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle((80, 100, 360, 330), fill=(160, 120, 90))
    draw.rectangle((430, 120, 760, 360), fill=(80, 120, 150))
    draw.rectangle((120, 460, 500, 720), outline=(30, 30, 30), width=8)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _open_listing():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    image = _png_bytes()
    state = {"manual_payloads": [], "confirm_payloads": []}

    def route_handler(route):
        url = route.request.url
        if url.endswith("/api/listing/REINS-TEST-1"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "id": "REINS-TEST-1",
                        "source": "reins",
                        "price": 30000,
                        "address": "東京都港区",
                        "photos": [],
                        "interior_photos": [],
                        "staged_photos": [],
                        "floorplan_images": [],
                        "reins_drawing_pdf": "/uploads/reins/TESTREINS/drawing.pdf",
                    }
                ),
            )
        elif url.endswith("/api/reins-photo-preview/REINS-TEST-1"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "code": 1,
                        "source_pages": [
                            {"id": "page_1", "page": 1, "url": "/api/reins-photo-preview-file/REINS-TEST-1/page_1?token=source-token", "width": 1000, "height": 800}
                        ],
                        "candidates": [
                            {
                                "id": "auto_1",
                                "url": "/api/reins-photo-preview-file/REINS-TEST-1/auto_1?token=auto-token",
                                "page": 1,
                                "width": 220,
                                "height": 160,
                                "quality": 80,
                                "classification": "interior_photo",
                                "excluded": False,
                                "reason": "室內相片候選",
                                "method": "embedded_xobject",
                                "source_image_id": "page_1",
                                "normalized_crop": {"x": 0.08, "y": 0.12, "width": 0.28, "height": 0.25},
                            }
                        ],
                    }
                ),
            )
        elif url.endswith("/api/reins-photo-manual-crop/REINS-TEST-1"):
            payload = route.request.post_data_json
            state["manual_payloads"].append(payload)
            crop = payload["crop"]
            w = round(crop["width"] * 1000)
            h = round(crop["height"] * 800)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "code": 1,
                        "candidate": {
                            "id": payload["temp_id"],
                            "url": "/api/reins-photo-preview-file/REINS-TEST-1/" + payload["temp_id"] + "?token=manual-token",
                            "page": 1,
                            "width": w,
                            "height": h,
                            "classification": payload["category"],
                            "method": "manual_crop",
                            "source_image_id": payload["source_image_id"],
                            "normalized_crop": crop,
                            "crop_pixels": {"x": round(crop["x"] * 1000), "y": round(crop["y"] * 800), "width": w, "height": h},
                        },
                    }
                ),
            )
        elif url.endswith("/api/reins-photo-confirm/REINS-TEST-1"):
            state["confirm_payloads"].append(route.request.post_data_json)
            route.fulfill(status=200, content_type="application/json", body='{"code":1,"confirmed":0}')
        elif "/api/reins-photo-preview-file/" in url:
            route.fulfill(status=200, content_type="image/png", body=image)
        elif url.endswith("/vendor/workbench-theme.css"):
            route.fulfill(status=200, content_type="text/css", body="")
        else:
            route.fulfill(status=200, content_type="text/html", body=LISTING_HTML.read_text(encoding="utf-8"))

    page.route("**/*", route_handler)
    page.goto("http://listing.test/listing/REINS-TEST-1")
    page.wait_for_selector("button:has-text('從 REINS 圖面提取相片')")
    return playwright, browser, page, state


def _drag_on_stage(page, start=(0.1, 0.12), end=(0.35, 0.36)):
    box = page.locator("#reinsSourceImage").bounding_box()
    assert box
    x1 = box["x"] + box["width"] * start[0]
    y1 = box["y"] + box["height"] * start[1]
    x2 = box["x"] + box["width"] * end[0]
    y2 = box["y"] + box["height"] * end[1]
    page.mouse.move(x1, y1)
    page.mouse.down()
    page.mouse.move(x2, y2)
    page.mouse.up()


def test_manual_drag_preview_category_delete_clear_undo_and_no_confirm_on_close():
    playwright, browser, page, state = _open_listing()
    try:
        page.click("button:has-text('從 REINS 圖面提取相片')")
        page.wait_for_selector("#manualModeBtn")
        page.click("#manualModeBtn")
        _drag_on_stage(page)
        page.wait_for_selector("[data-manual-box]")
        assert len(state["manual_payloads"]) >= 1
        crop = state["manual_payloads"][-1]["crop"]
        assert abs(crop["x"] - 0.1) < 0.02
        assert abs(crop["y"] - 0.12) < 0.02
        assert abs(crop["width"] - 0.25) < 0.02
        assert "手動裁切 #1" in page.locator("#manualCards").inner_text()

        page.select_option("[data-manual-category]", "外觀")
        page.wait_for_function("window.reinsExtractState && window.reinsExtractState.manual[0].category === '外觀'")
        assert state["manual_payloads"][-1]["category"] == "外觀"

        page.click("[data-delete-manual]")
        assert page.locator("[data-manual-box]").count() == 0
        _drag_on_stage(page, (0.4, 0.15), (0.65, 0.4))
        page.wait_for_selector("[data-manual-box]")
        page.click("#undoManualCrop")
        assert page.locator("[data-manual-box]").count() == 0
        _drag_on_stage(page, (0.2, 0.2), (0.5, 0.5))
        page.wait_for_selector("[data-manual-box]")
        page.click("#clearManualCrops")
        assert page.locator("[data-manual-box]").count() == 0
        page.click("button:has-text('取消')")
        assert state["confirm_payloads"] == []
    finally:
        browser.close()
        playwright.stop()


def test_manual_move_resize_and_viewport_resize_keep_normalized_coordinates():
    playwright, browser, page, state = _open_listing()
    try:
        page.click("button:has-text('從 REINS 圖面提取相片')")
        page.click("#manualModeBtn")
        _drag_on_stage(page, (0.1, 0.1), (0.3, 0.3))
        page.wait_for_selector("[data-manual-box]")
        first = state["manual_payloads"][-1]["crop"]
        page.set_viewport_size({"width": 900, "height": 700})
        after_resize = page.evaluate("window.reinsExtractState.manual[0].crop")
        assert after_resize == first

        page.locator("[data-manual-box]").first.scroll_into_view_if_needed()
        box = page.locator("[data-manual-box]").first.bounding_box()
        assert box
        page.mouse.move(box["x"] + 20, box["y"] + 20)
        page.mouse.down()
        page.mouse.move(box["x"] + 70, box["y"] + 60)
        page.mouse.up()
        moved = page.evaluate("window.reinsExtractState.manual[0].crop")
        assert moved["x"] > first["x"]
        assert moved["y"] > first["y"]

        page.wait_for_function("document.querySelectorAll('[data-resize-handle]').length > 0")
        handle = page.locator("[data-resize-handle]").first.bounding_box()
        assert handle
        before_w = moved["width"]
        page.mouse.move(handle["x"] + 4, handle["y"] + 4)
        page.mouse.down()
        page.mouse.move(handle["x"] + 60, handle["y"] + 50)
        page.mouse.up()
        resized = page.evaluate("window.reinsExtractState.manual[0].crop")
        assert resized["width"] > before_w
    finally:
        browser.close()
        playwright.stop()


def test_touch_pointer_event_can_create_manual_crop_and_auto_candidate_highlights_source():
    playwright, browser, page, state = _open_listing()
    try:
        page.click("button:has-text('從 REINS 圖面提取相片')")
        page.wait_for_selector("#reinsCropOverlay")
        assert page.locator("#reinsCropOverlay div").count() >= 1
        page.click("#manualModeBtn")
        box = page.locator("#reinsSourceImage").bounding_box()
        assert box
        page.dispatch_event("#reinsCropStage", "pointerdown", {"pointerId": 7, "pointerType": "touch", "clientX": box["x"] + box["width"] * 0.15, "clientY": box["y"] + box["height"] * 0.15})
        page.dispatch_event("#reinsCropStage", "pointermove", {"pointerId": 7, "pointerType": "touch", "clientX": box["x"] + box["width"] * 0.38, "clientY": box["y"] + box["height"] * 0.38})
        page.dispatch_event("#reinsCropStage", "pointerup", {"pointerId": 7, "pointerType": "touch", "clientX": box["x"] + box["width"] * 0.38, "clientY": box["y"] + box["height"] * 0.38})
        page.wait_for_selector("[data-manual-box]")
        assert len(state["manual_payloads"]) >= 1
    finally:
        browser.close()
        playwright.stop()


def test_preview_images_use_signed_urls_and_image_failures_do_not_stay_loading():
    playwright, browser, page, state = _open_listing()
    try:
        page.click("button:has-text('從 REINS 圖面提取相片')")
        page.wait_for_selector("#reinsSourceImage")
        urls = page.evaluate("Array.from(document.querySelectorAll('#reinsExtractModal img[data-preview-img]')).map(img => img.getAttribute('src'))")
        assert urls
        assert all('/api/reins-photo-preview-file/REINS-TEST-1/' in u and 'token=' in u for u in urls)
    finally:
        browser.close()
        playwright.stop()

    playwright, browser, page, state = _open_listing()
    try:
        page.route("**/api/reins-photo-preview-file/**", lambda route: route.fulfill(status=401, content_type="text/plain", body="unauthorized"))
        page.click("button:has-text('從 REINS 圖面提取相片')")
        page.wait_for_selector("text=預覽圖片載入失敗，請重新開啟。")
        assert page.locator("button:has-text('重新載入預覽')").count() == 1
        assert state["confirm_payloads"] == []
    finally:
        browser.close()
        playwright.stop()

    playwright, browser, page, state = _open_listing()
    try:
        page.route("**/api/reins-photo-preview-file/**", lambda route: route.fulfill(status=404, content_type="text/plain", body="missing"))
        page.click("button:has-text('從 REINS 圖面提取相片')")
        page.wait_for_selector("text=預覽圖片載入失敗，請重新開啟。")
        assert page.locator("button:has-text('重新載入預覽')").count() == 1
        assert state["confirm_payloads"] == []
    finally:
        browser.close()
        playwright.stop()


def test_preview_image_timeout_shows_reload_without_infinite_retry():
    playwright, browser, page, state = _open_listing()
    try:
        page.click("button:has-text('從 REINS 圖面提取相片')")
        page.wait_for_selector("#reinsSourceImage")
        page.evaluate("""() => {
            const modal = document.getElementById('reinsExtractModal');
            const img = document.createElement('img');
            img.setAttribute('data-preview-img', '1');
            img.src = 'http://preview.invalid/never-loads.jpg';
            modal.appendChild(img);
            const originalSetTimeout = window.setTimeout;
            window.__timeouts = [];
            window.setTimeout = (fn, ms) => {
                window.__timeouts.push(ms);
                if (typeof fn === 'function') fn();
                return 1;
            };
            window.reinsStartImageLoadGuard();
            window.setTimeout = originalSetTimeout;
        }""")
        page.wait_for_selector("text=預覽圖片載入失敗，請重新開啟。")
        assert page.evaluate("window.__timeouts") == [10000]
        assert state["confirm_payloads"] == []
    finally:
        browser.close()
        playwright.stop()
