from pathlib import Path
import json

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
LISTINGS_HTML = ROOT / "listings.html"
COLLECTION_HTML = ROOT / "collection.html"


def _open_listings(can_manage=True):
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    listings = [
        {"id": "L1", "address": "東京都港区赤坂", "price": 3000, "source": "reins", "status": "draft", "clientAssignments": [{"id": "C1", "name": "客人 A"}]},
        {"id": "L2", "address": "東京都新宿区", "price": 4000, "source": "suumo", "status": "draft", "clientAssignments": []},
    ]
    clients = [
        {"id": "C1", "name": "客人 A", "requirement_text": "港区", "shortlist_count": 1},
        {"id": "C2", "name": "客人 B", "requirement_text": "新宿区", "shortlist_count": 0},
    ]
    state = {"bulk_requests": [], "client_requests": 0}

    def route_handler(route):
        url = route.request.url
        if url.endswith("/api/listings"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"count": len(listings), "listings": listings, "canManage": can_manage}))
        elif url.endswith("/api/v1/clients"):
            state["client_requests"] += 1
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"code": 1, "clients": clients}))
        elif url.endswith("/api/client-shortlists/bulk-add"):
            state["bulk_requests"].append(route.request.post_data_json)
            route.fulfill(status=200, content_type="application/json", body='{"code":1,"created":3,"already_exists":1,"failed":[],"client_count":2,"listing_count":2}')
        elif url.endswith("/vendor/workbench-theme.css"):
            route.fulfill(status=200, content_type="text/css", body="")
        else:
            route.fulfill(status=200, content_type="text/html", body=LISTINGS_HTML.read_text(encoding="utf-8"))

    page.route("**/*", route_handler)
    page.goto("http://listings.test/listings")
    if can_manage:
        page.wait_for_selector(".card-check")
    else:
        page.wait_for_function("document.querySelectorAll('.reveal').length > 0")
    page.evaluate("window.__lastAlert = null; window.alert = (msg) => { window.__lastAlert = String(msg); }")
    return playwright, browser, page, state


def test_listings_multiselect_client_modal_cancel_does_not_write_db():
    playwright, browser, page, state = _open_listings()
    try:
        page.locator(".card-check").nth(0).click()
        assert "已選 1 件" in page.locator("#bulkCount").inner_text()
        page.click("text=加入客人私庫")
        page.wait_for_selector("#clientAssignModal")
        assert "客人 A" in page.locator("#clientAssignModal").inner_text()
        assert "已選 1 件物件" in page.locator("#clientAssignModal").inner_text()
        page.locator("#clientAssignModal button:has-text('取消')").click()
        page.wait_for_function("!document.querySelector('#clientAssignModal')")
        assert state["bulk_requests"] == []
    finally:
        browser.close(); playwright.stop()


def test_listings_bulk_add_uses_listing_ids_not_indexes_and_is_idempotency_aware():
    playwright, browser, page, state = _open_listings()
    try:
        page.click("text=選取本頁")
        assert "已選 2 件" in page.locator("#bulkCount").inner_text()
        page.click("text=加入客人私庫")
        page.locator('[data-client-pick="C1"]').check()
        page.locator('[data-client-pick="C2"]').check()
        assert "預計新增關聯 3" in page.locator("#clientAssignModal").inner_text()
        assert "已存在 1" in page.locator("#clientAssignModal").inner_text()
        page.click("text=確認加入客人私庫")
        page.wait_for_function("window.__lastAlert && window.__lastAlert.includes('新增3個關聯')")
        assert state["bulk_requests"] == [{"client_ids": ["C1", "C2"], "listing_ids": ["L1", "L2"]}]
    finally:
        browser.close(); playwright.stop()


def test_listings_public_mode_hides_private_client_controls():
    playwright, browser, page, state = _open_listings(can_manage=False)
    try:
        assert page.locator(".card-check").count() == 0
        assert page.locator("text=加入客人私庫").count() == 1
        assert page.locator("#bulkBar").evaluate("el => el.classList.contains('hidden')") is True
        assert state["client_requests"] == 0
    finally:
        browser.close(); playwright.stop()


def test_collection_import_button_copy_and_completion_link():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    state = {"imported": False}

    def route_handler(route):
        url = route.request.url
        if url.endswith("/api/collection/cities"):
            route.fulfill(status=200, content_type="application/json", body='{"code":1,"prefectures":{"東京都":["港区"]}}')
        elif url.endswith("/api/collection/search"):
            route.fulfill(status=200, content_type="application/json", body='{"code":1,"found":1,"source":"suumo","listings":[{"url":"https://suumo.jp/ms/chuko/tokyo/sc_minato/nc_1/","price":5000,"address":"東京都港区","layout":"2LDK"}]}')
        elif url.endswith("/api/collection/import"):
            state["imported"] = True
            route.fulfill(status=200, content_type="application/json", body='{"code":1,"imported":1,"existing":0,"failed":0,"total":1,"results":[{"code":1,"id":"SU1","action":"inserted","address":"東京都港区","price":5000}]}')
        elif url.endswith("/vendor/workbench-theme.css"):
            route.fulfill(status=200, content_type="text/css", body="")
        else:
            route.fulfill(status=200, content_type="text/html", body=COLLECTION_HTML.read_text(encoding="utf-8"))

    page.route("**/*", route_handler)
    try:
        page.goto("http://collection.test/collection")
        page.click("#srcSuumo")
        page.click("#searchBtn")
        page.wait_for_selector("text=加入全部物件")
        page.locator(".result-card").first.click()
        page.click("#importBtn")
        page.wait_for_selector("text=前往全部物件")
        assert state["imported"] is True
        assert "新增1件，已存在0件，失敗0件" in page.locator("#status").inner_text()
    finally:
        browser.close(); playwright.stop()
