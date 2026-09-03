from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
COLLECTION_HTML = ROOT / "collection.html"


def _open_collection_with_search_response(status=200, content_type="application/json", body='{"code":1,"found":1,"listings":[]}'):
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    def route_handler(route):
        url = route.request.url
        if url.endswith("/api/collection/cities"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"code":1,"prefectures":{"東京都":["港区"]}}',
            )
        elif url.endswith("/api/collection/search"):
            route.fulfill(status=status, content_type=content_type, body=body)
        elif url.endswith("/vendor/workbench-theme.css"):
            route.fulfill(status=200, content_type="text/css", body="")
        else:
            route.fulfill(status=200, content_type="text/html", body=COLLECTION_HTML.read_text(encoding="utf-8"))

    page.route("**/*", route_handler)
    page.goto("http://collection.test/collection")
    page.wait_for_selector("#searchBtn")
    return playwright, browser, page


def _click_search_and_status(page):
    page.click("#searchBtn")
    page.wait_for_function("document.querySelector('#searchBtn').disabled === false")
    return page.locator("#status").inner_text()


def test_fetch_page_200_json_displays_results():
    body = """{
      "code": 1,
      "found": 1,
      "page": 1,
      "page_size": 50,
      "total_pages": 1,
      "hit_limit": false,
      "listings": [{
        "source": "reins",
        "reins_id": "100140299379",
        "price": 5980,
        "walk_min": 6,
        "address": "東京都港区海岸１丁目",
        "building_name": "パークホームズ浜松町",
        "line": "山手線",
        "station": "有楽町"
      }]
    }"""
    playwright, browser, page = _open_collection_with_search_response(body=body)
    try:
        status = _click_search_and_status(page)
        assert "検索到 1 件" in status
        assert "パークホームズ浜松町" in page.locator("#resultList").inner_text()
    finally:
        browser.close()
        playwright.stop()


def test_fetch_page_422_json_displays_backend_message_without_zero_results():
    body = '{"code":0,"error":"REINS_WALK_REQUIRES_STATION","message":"REINS 步行條件必須同時指定沿線及車站"}'
    playwright, browser, page = _open_collection_with_search_response(status=422, body=body)
    try:
        page.evaluate("document.querySelector('#walk').disabled = false")
        page.select_option("#walk", "10")
        status = _click_search_and_status(page)
        assert status == "REINS 步行條件必須同時指定沿線及車站"
        assert "検索到 0 件" not in status
        assert "暫無結果" not in page.locator("#resultList").inner_text()
    finally:
        browser.close()
        playwright.stop()


def test_fetch_page_502_html_displays_service_unavailable_message():
    html = "<!DOCTYPE html><html><body><h1>502 Bad Gateway</h1></body></html>"
    playwright, browser, page = _open_collection_with_search_response(status=502, content_type="text/html", body=html)
    try:
        status = _click_search_and_status(page)
        assert status == "搜尋服務暫時不可用，請稍後再試。"
        assert page.locator("#walk").input_value() == "0"
    finally:
        browser.close()
        playwright.stop()


def test_fetch_page_504_html_displays_service_unavailable_message():
    html = "<!DOCTYPE html><html><body><h1>504 Gateway Time-out</h1></body></html>"
    playwright, browser, page = _open_collection_with_search_response(status=504, content_type="text/html", body=html)
    try:
        assert _click_search_and_status(page) == "搜尋服務暫時不可用，請稍後再試。"
    finally:
        browser.close()
        playwright.stop()


def test_fetch_page_200_non_json_displays_format_error():
    playwright, browser, page = _open_collection_with_search_response(status=200, content_type="text/plain", body="not json")
    try:
        assert _click_search_and_status(page) == "搜尋服務回傳格式異常，請稍後再試。"
    finally:
        browser.close()
        playwright.stop()


def test_suumo_keeps_walk_selector_enabled():
    playwright, browser, page = _open_collection_with_search_response()
    try:
        assert page.locator("#walk").is_disabled()
        page.click("#srcSuumo")
        page.wait_for_function("document.querySelector('#walk').disabled === false")
        assert not page.locator("#walk").is_disabled()
        assert "hidden" in (page.locator("#walkHint").get_attribute("class") or "")
    finally:
        browser.close()
        playwright.stop()
