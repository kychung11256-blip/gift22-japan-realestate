# Frontend integration contract — commercial 20261107

Frontend files use backend_contract.json, read on 2026-11-07. Please serve:
- /login/staff → staff_login.html; /login/client → client_login.html
- /client/portal → client_portal.html (client only)
- /admin/users → admin_users.html (admin only)
- /admin/media-review → media_review.html (admin/agent only)
- /privacy → privacy.html; /terms → terms.html. Replace current inline HTML (including inline lang/brand/CTA), or explicitly include /config/commercial.js and lang=zh-HK in any retained inline page.
- /config/commercial.js and /config/commercial.css must be GET-accessible anonymously alongside /config/api.js; do not expose arbitrary files.

Transport: /api/auth/session returns user and csrf_token for anonymous login and signed-in mutations. Shared Commercial wraps all same-origin unsafe fetch requests with X-CSRF-Token; login uses the same token plus JSON. API errors need error/message. Session transport never caches a password.

Admin users: GET shape {users:[...]}; POST shape matches backend contract. GET assignments may return {assignments:[...]} or a list. DELETE sends JSON {agent_id,client_id}. UI never logs or persists created passwords.

Media UI: exactly scoped image/derivative endpoints. Listing GET must include original_preview_url, derivative_preview_url, derivative_fingerprint and crop_bottom_pct in manifest. Approve requires loaded original+derivative and latest fingerprint + confirmed:true, sent only by user click. Reject/regenerate require CSRF. Please ensure agent listing groups use assignment scope (not all listings).

Public safeMedia allowlist accepts only relative same-origin /api/customer-safe-image/<listing_id>/<asset_id>, never remote or raw. Do not return original preview URLs in public/client projection. Public detail strips legacy raw-media/admin fields when canManage=false as defense in depth. Backend remains the actual access-control boundary.

Dates: jp_date.py exports parse_japanese_date, iso_z, display_jst. Preserve original DB/raw inputs and supply *_raw only to staff projections. YYYY-MM never gets a day. Public can pass normalized or era raw into Commercial.date (raw never displayed). Admin detail supports built_date_full_raw as subordinate text. Workbench comparison uses Commercial.date(p.builtAt).

Screenshots: screenshot_tool.py default outputs outside repo under task outputs/screenshots. --storage-state PATH or SCREENSHOT_STORAGE_STATE supports Playwright state saved by API login/browser smoke; state never printed. No server has been started by frontend worker. Parent should run screenshot_tool.py against real 8901 after backend health, for authenticated and public desktop/mobile.
