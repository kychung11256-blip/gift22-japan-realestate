# Frontend verification — 20261107

- Added precision-preserving Python + browser Japanese-era parser (令和/平成/昭和, 元年, fullwidth, calendar + era boundary validation). YYYY-MM never gains -01; timestamp UI explicitly Asia/Tokyo.
- Shared same-origin CSRF wrapper covers old fetch mutation paths plus new logins/logout/users/assignments/media review. Public image rendering accepts only scoped customer-safe URLs.
- Added staff/client login, client portal, admin users/assignments, per-listing media review with original/derivative parallel display, previous/next, crop-bottom %, regenerate, explicit human confirmation + current fingerprint approve, reject/progress.
- Public contact shell uses fixed endpoint QR URLs (no QR copy), WhatsApp/Email/WeChat modal, responsive focus management and source-safe no-image placeholder.
- Existing manual crop retained; fixture now serves commercial.js, real staff projection and checks CSRF header. Collection browser fixture updated likewise with Traditional Chinese search text.

## Real execution

`tests/test_commercial_dates_ui_20261107.py`, `tests/test_reins_manual_crop_ui.py`, `tests/test_collection_api_error_handling.py`: **28 passed**, Chromium exercised, zero skips (latest run before final label-only cleanup 7.62s).

Screenshots actually produced with screenshot_tool.py using Playwright route-based local file serving (NO server started, no invented listing/API data):
- repo-external screenshots/frontend_staff_static_desktop.png
- repo-external screenshots/frontend_privacy_static_mobile.png
Visual inspection confirmed clean non-blank desktop form and mobile privacy/contact CTA without overflow. Login correctly shows security-unavailable fail-closed state because route-based capture intentionally cannot reach a backend.

## Integration outstanding

At check time 127.0.0.1:8901 connection returned 000 and no saved browser storage state was yet supplied. Real API role-page and actual media preview screenshot acceptance therefore remains mainline smoke responsibility; do not treat static captures as authenticated integration passes. `docs/frontend_contract_20261107.md` records all server route/inline HTML/config serving requirements. No server/backend/DB/media Python touched; no commit/push/QR copy/real mutation performed.
