[HERMES_OK] PR #7 isolated staging public-transit planner fix

Scope kept to PR/staging worktree only (`/home/ubuntu/ai-team/viewing-route-planner-worktree`, branch `codex/viewing-route-planner`). No merge, no production deploy, no publish/review/import/AI generation, no secrets printed.

Root cause
- The failing real transit path is not a route mismatch or mocked-duration issue. The real provider call reaches Google Distance Matrix for transit and Google returns `REQUEST_DENIED` because the staging API key is not authorized for Distance Matrix API.
- Previous frontend handling treated non-OK / unexpected responses too generically, so Johnny saw only `伺服器回應唔係 JSON` instead of a provider/config remediation.

Fix
- Added planner JSON error envelope for validation/provider/auth/404/405/unexpected exception paths:
  `{"code":0,"error":"clear Traditional Chinese message","errorCode":"...","requestId":"..."}` with `Content-Type: application/json; charset=utf-8`.
- Added provider-specific mapping:
  - `REQUEST_DENIED` → `PROVIDER_CONFIG_ERROR` with remediation to enable/add `Distance Matrix API` in Google Cloud Console key restrictions.
  - quota and unsupported-route statuses have separate error codes/messages.
- Hardened frontend `fetch` handling to inspect HTTP status + content-type + text + request id. JSON provider/config errors now display specific remediation; true non-JSON responses include HTTP status/request fallback and no longer show only `伺服器回應唔係 JSON`.
- Shortlist cards now show distinguishing details without changing listing data: listing ID, price, layout, floor/total floors.

Commit
- Branch HEAD commit after this fix: see PR commits (`Fix planner transit provider error handling`).

Raw safe evidence (no secrets / no Google URLs)
- Real API request path: `POST /api/v1/viewing-plans/optimize`
- Real API status/content-type: `502`, `application/json; charset=utf-8`
- Real API request id: `PR7-REAL-TRANSIT`
- Real API safe JSON summary:
```json
{
  "code": 0,
  "errorCode": "PROVIDER_CONFIG_ERROR",
  "error": "Google Distance Matrix API 拒絕請求（REQUEST_DENIED）：staging 使用緊嘅 Google API key 未獲授權使用 Distance Matrix API。請在 Google Cloud Console 啟用對應 API，並在 API key restrictions 加入 Distance Matrix API；毋須改房源資料。",
  "requestId": "PR7-REAL-TRANSIT",
  "provider": null,
  "travelMode": null,
  "durations": [],
  "routeGeometryType": null
}
```
- Corresponding Flask access log after fix:
  `127.0.0.1 - - [01/Sep/2026 13:54:18] "POST /api/v1/viewing-plans/optimize HTTP/1.1" 502 -`
- Real UI network evidence: `status=502`, `content_type=application/json; charset=utf-8`, UI displayed:
  `優化失敗：Google Distance Matrix API 拒絕請求（REQUEST_DENIED）... [PROVIDER_CONFIG_ERROR] · request REQ-322C83FB1D`

Real transit staging result
- Because the configured staging Google key is not authorized for Distance Matrix API, the real public-transit smoke correctly returns an explicit JSON provider/config error. No durations/geometry were fabricated.
- Required remediation: enable Google Distance Matrix API for this project/key and include Distance Matrix API in the key's API restrictions (or use an unrestricted/server key scoped appropriately). Existing Driving Directions remains working.

Driving route preserved
```json
{
  "status": 200,
  "content_type": "application/json",
  "provider": "google_directions",
  "travelMode": "driving",
  "durations": [21, 16],
  "routeGeometryType": "LineString",
  "routeGeometryCoordCount": 342,
  "warnings": []
}
```

Tests / verification
- Unit/regression: `.venv/bin/python -m pytest -q` → `47 passed in 0.94s`
- Added regression coverage for:
  - transit provider `REQUEST_DENIED` → JSON `PROVIDER_CONFIG_ERROR`
  - unexpected planner exception → JSON envelope, `INTERNAL_ERROR`, request id
  - 404/405 planner JSON envelopes
- Real transit Playwright/API smoke: `python3 verify_pr7_transit_real.py`
  - API: 502 JSON provider/config error as above
  - Desktop UI: specific provider/config error, not generic non-JSON
  - Mobile UI: same specific provider/config error
- Desktop/mobile transit success UI path (staging-only mock durations, no fabricated real smoke claim): `python3 verify_pr7_transit_mock_ui.py`
  - desktop/mobile API `200 application/json`, `provider=mock_staging_matrix`, durations `[11,18]`, no console errors
- Additional planner envelope checks:
  - `PUT /api/v1/viewing-plans/optimize` → `405 application/json; charset=utf-8`, `METHOD_NOT_ALLOWED`
  - missing plan → `404 application/json; charset=utf-8`, `NOT_FOUND`

Screenshots
- Real transit desktop before optimize: `verify_screenshots/pr7_transit_desktop_before.png`
- Real transit desktop JSON provider/config error: `verify_screenshots/pr7_transit_desktop_after.png`
- Real transit mobile settings: `verify_screenshots/pr7_transit_mobile_settings.png`
- Real transit mobile JSON provider/config error: `verify_screenshots/pr7_transit_mobile_error.png`
- Mock-only transit desktop success UI regression: `verify_screenshots/pr7_transit_mock_desktop.png`
- Mock-only transit mobile success UI regression: `verify_screenshots/pr7_transit_mock_mobile.png`

Production unchanged
- Production PID before/after remained `1813102`, command `/home/ubuntu/ai-team/platform/.venv/bin/python server.py`, start `Tue Sep 1 11:25:23 2026`.
- Production health check unchanged: `prod_status=404 content_type=application/json`.

Note: I restarted only isolated staging 8901 around 13:54 UTC to load the fix. If Johnny saw a brief network error at that time, it was the staging restart window.
