# gift22.vip 日本不動產專案 Integration Report

生成時間：2026-08-31 09:23 UTC  
範圍：唯讀分析現有 `/home/ubuntu/ai-team/platform` 後端、前端、SQLite schema、登入驗證、API；沒有部署、沒有重啟正式服務、沒有輸出任何 secret。

## 1. 技術棧

### 後端

- 框架：Python Flask。
- 入口：`server.py`。
- 服務端口：`0.0.0.0:8900`。
- 對外域名：nginx `gift22.vip` / `www.gift22.vip` 反向代理到 `http://localhost:8900`。
- 資料庫：SQLite，路徑 `data/listings.db`。
- 主要後端模組：
  - `server.py`：Flask routes、頁面 serving、API、AI staging、路線、market intel。
  - `db.py`：SQLite schema、`get_db()`、`init_db()`、lightweight migrations。
  - `reins_client.py`：REINS login/session、搜尋、PDF download、PDF parse、REINS listing import/upsert。
  - `reins_import_jobs.py`：REINS background import job store，JSON persistence，single-worker thread。
  - `reins_pdf_parser.py`：REINS 概要 PDF coordinate parser。
  - `reins_pdf_render.py`：REINS 図面 PDF render 成圖片。
  - `nl_search.py` / `search_plan.py` / `nl_rule_parser.py` / `nl_llm_parser.py`：自然語言搜尋計劃與 REINS filter mapping。
  - `suumo_search.py` / `suumo_scraper.py`：SUUMO 搜尋/匯入相關。
  - `mlit_client.py` / `geocode_client.py`：國交省/GSI/地理資料。

### 前端

- 目前是 server-side static HTML + vanilla JavaScript + Tailwind CDN。
- 沒有 `package.json`、沒有前端 bundler（未見 React/Vite/Next app）。
- 主要頁面：
  - `index.html`：首頁/dashboard。
  - `listings.html`：全部 published listing 列表。
  - `listing.html`：listing 詳情頁。
  - `review.html`：draft review/edit/publish UI。
  - `collection.html`：REINS/SUUMO 搜尋與匯入 UI。
  - `upload.html` / `mysok_import.html`：手動/マイソク匯入。
  - `map.html`：地圖與路線規劃。

## 2. 專案主要目錄

```text
/home/ubuntu/ai-team/platform/
├── server.py                 # Flask app + all current routes
├── db.py                     # SQLite schema + migrations
├── data/
│   ├── listings.db           # production SQLite DB
│   ├── backup/               # DB backups after confirm/delete
│   ├── hourly_backup/        # hourly backup area
│   └── import_jobs/          # REINS import job JSON files
├── uploads/
│   ├── mysok/                # uploaded mysok / user images
│   ├── reins/                # REINS overview/drawing PDFs + rendered drawing images
│   ├── staged/               # AI virtual staging output images
│   └── thumbs/               # uploaded thumbnail images
├── tests/                    # pytest tests
└── verify_screenshots/       # screenshot evidence
```

## 3. 資料庫類型與 schema

資料庫是 SQLite。現有 tables：

- `listings`：主房源資料表。
- `agents`：agent/company metadata，目前實際 count 是 0。

### `listings` 欄位（與房源/圖片/審核最相關）

核心房源欄位：

- `id` TEXT PRIMARY KEY
- `agent_id` TEXT
- `address` TEXT
- `station` TEXT
- `walk_min` INTEGER
- `price` INTEGER
- `price_per_sqm` REAL
- `size_sqm` REAL
- `built_year` INTEGER
- `age` INTEGER
- `room_layout` TEXT
- `orientation` TEXT
- `floor` INTEGER
- `total_floors` INTEGER
- `floors_above` INTEGER
- `underground_floors` INTEGER
- `structure` TEXT
- `land_rights` TEXT
- `type` TEXT
- `source` TEXT
- `reins_id` TEXT
- `building_name` TEXT
- `management_company` TEXT
- `management_type` TEXT
- `registration_no` TEXT
- `current_status` TEXT
- `handover_timing` TEXT
- `transaction_type` TEXT
- `use_district` TEXT
- `balcony_sqm` REAL
- `total_units` INTEGER
- `mgmt_fee` TEXT
- `repair_reserve` TEXT
- `repair_fund` TEXT
- `other_costs` TEXT
- `renovation` TEXT
- `parking` TEXT
- `latitude` REAL
- `longitude` REAL
- `created_at` TEXT
- `updated_at` TEXT

圖片/媒體欄位：

- `photos` TEXT JSON array
- `floorplan_url` TEXT
- `floorplan_images` TEXT JSON array
- `interior_photos` TEXT JSON array
- `staged_photos` TEXT JSON array
- `reins_overview_pdf` TEXT
- `reins_drawing_pdf` TEXT

審核/狀態欄位：

- `status` TEXT DEFAULT `draft`，schema CHECK 只允許 `draft` / `published` / `archived`。
- 注意：程式現有 `leads_list()` 查 `status='lead'`，但 DB schema CHECK 未包含 `lead`；這是 schema 與程式邏輯不一致，若要正式支援 lead 狀態需要 migration。

目前實際資料量（唯讀查詢）：

- total listings：12
- `published`：12
- `draft`：0
- `archived`：0
- source：`reins` 10、`suumo` 2
- 有任一圖片欄位：11
- REINS 有概要 PDF：10
- REINS 有図面 PDF：9

## 4. 現有登入驗證方式

### Web app / API 本身

- 現有 Flask app 沒有 app-level login/session middleware。
- `app.secret_key` 未設定。
- 未見 `flask_login`、JWT、Bearer token、Basic auth、API key middleware。
- nginx `gift22.vip` site 未見 `auth_basic` 或 CORS/security header 設定。
- 結論：目前 `/api/*` 大部分是公開路由，另一個前端可以直接讀，但寫入/審核 endpoint 目前沒有權限保護。

### REINS 外部系統

- REINS login 由 `reins_client.py` 處理。
- 憑證從環境變數讀取：`REINS_LOGIN_URL`、`REINS_MEMBER_ID`、`REINS_PASSWORD`。
- 登入後 Playwright storage state 存在 `data/reins_storage_state.json`，並 chmod 0600。
- 本報告沒有讀取或輸出任何 `.env`、cookie、password 或 token。

## 5. CORS 設定

- Flask app 目前未使用 `flask_cors.CORS`。
- `server.py` 未見 `Access-Control-Allow-Origin` header 設定。
- nginx `gift22.vip` config 未見 CORS header 設定。
- 結論：現有 API 預設只適合同源前端使用。另一個 domain 的前端要直接呼叫 API，需要新增 CORS allowlist（建議只 allow 指定 frontend origin，不要 `*` 配合寫入 API）。

## 6. 現有 API 路徑

由 Flask `url_map` 讀到的現有 routes：

### 頁面

- `GET /`
- `GET /upload`
- `GET /review`
- `GET /mysok`
- `GET /map`
- `GET /collection`
- `GET /listings`
- `GET /listing/<listing_id>`
- `GET /<path:filename>` static file fallback
- `GET /static/<path:filename>`
- `GET /uploads/mysok/<path:filename>`
- `GET /uploads/staged/<path:filename>`

### 房源 / 審核 / 刪除

- `GET /api/drafts`：列出 `status='draft'` 房源。
- `POST /api/update/<listing_id>`：更新多個 editable fields。
- `POST /api/confirm/<listing_id>`：draft → published，帶 price/area consistency validation，成功後觸發 MLIT background check。
- `GET /api/listing/<listing_id>`：讀單個 listing。
- `DELETE /api/listing/<listing_id>`：刪除 listing。
- `GET /api/listings`：列出全部 `published` listings，無 pagination/filter。
- `GET /api/listings/leads`：列出 `status='lead'`，但 schema CHECK 未包含 lead。
- `GET /api/listings/geojson`：published listings GeoJSON。

### 搜尋 / 匯入

- `GET /api/search?q=`：簡單 published listings 搜尋，limit 8。
- `POST /api/collection/search`：REINS 或 SUUMO 搜尋。
- `GET /api/collection/cities`：搜尋表單 prefecture/city options。
- `POST /api/collection/import`：REINS background job 或 SUUMO sync import。
- `GET /api/collection/import-status/<job_id>`：查 REINS import job。
- `POST /api/collection/import-resume/<job_id>`：resume REINS import job。
- `POST /api/nl-search`：自然語言 → SearchPlan → REINS 搜尋。

### Upload / AI / map / market

- `POST /api/upload`
- `POST /api/upload-v2`
- `POST /api/mysok-upload`
- `POST /api/mysok-parse`
- `POST /api/mysok-parse-table`
- `POST /api/mysok-parse-floorplan`
- `POST /api/upload-photo/<listing_id>`
- `POST /api/delete-photo/<listing_id>`
- `POST /api/staging-generate`
- `POST /api/staging-accept/<listing_id>`
- `POST /api/staging-delete/<listing_id>`
- `GET /api/dashboard`
- `GET /api/market-intel`
- `GET /api/market-news`
- `GET /api/route-geocode?q=`
- `POST /api/route-plan`
- `GET /api/map-tile/<layer>/<z>/<x>/<y>`
- `GET/POST/PUT/PATCH/DELETE /api/agent/<subpath>`：proxy 到 agent backend。

## 7. 前端所需標準欄位 mapping

現有 DB/API 欄位可映射如下：

| 標準欄位 | 現有來源 | 備註 |
|---|---|---|
| `id` | `listings.id` | 已有 |
| `source` | `listings.source` | 已有 |
| `title` | `building_name` fallback `address + room_layout` | 需 API adapter 統一 |
| `prefecture` | 從 `address` parse | DB 未獨立存 |
| `city` | 從 `address` parse | DB 未獨立存 |
| `ward` | 從 `address` parse | 對東京 23 区可同 city |
| `address` | `listings.address` | 已有；樣本保留到町/丁目級別 |
| `station` | `listings.station` | 已有 |
| `walkMinutes` | `walk_min` | 需 camelCase adapter |
| `priceYen` | `price` | 注意：REINS/SUUMO 多數以万円存；upload 可能是 raw yen。adapter 要標準化成 yen。 |
| `managementFeeYen` | `mgmt_fee` | 現為 TEXT，需轉 integer/null |
| `areaSqm` | `size_sqm` | 已有 |
| `layout` | `room_layout` | 已有 |
| `floor` | `floor` | 已有 |
| `totalFloors` | `total_floors` fallback `floors_above` | REINS import 已補同步；舊資料已 backfill |
| `builtAt` | `built_date_full` fallback `built_year` | 已有 |
| `direction` | `orientation` | 已有 |
| `structure` | `structure` | 已有 |
| `status` | `status` | draft/published/archived；lead 需 schema 修正 |
| `completeness` | 需計算 | 現未儲存 |
| `missingFields` | 需計算 | 現未儲存 |
| `features` | `ai_keywords` + type/land/status/use_district 等 | 需 adapter 統一 |
| `images` | `photos` / `interior_photos` / `floorplan_images` / map URL | 需 adapter 統一成 `{url,type,order}` |
| `updatedAt` | `updated_at` | 已有 |

## 8. 已存在功能

- 房源 CRUD 基礎：讀取列表、讀單件、更新欄位、刪除。
- 審核流程：`/review` UI + `/api/drafts` + `/api/update/<id>` + `/api/confirm/<id>`。
- confirm 時有 price/area/price_per_sqm 5% consistency guard。
- 圖片 upload / delete。
- マイソク upload/parse/import。
- REINS 搜尋、自然語言搜尋、背景匯入、import status、resume。
- REINS PDF 儲存於 `/uploads/reins/...`，目前透過 static fallback 可讀。
- AI 虛擬裝修生成、accept、delete。
- published listings geojson / map tile proxy / route planning。
- Market intel / market news APIs。

## 9. 另一個前端需要新增 / 調整的功能

要讓外部前端穩定讀取及審核，建議新增版本化 API，不直接綁現有內部 UI routes：

1. `GET /api/v1/properties`
   - 需要 pagination、filter、sort。
   - 支援 query params：`q`、`area`、`min_price`、`max_price`、`layout`、`status`、`min_completeness`、`source`、`sort`、`page`、`page_size`。
   - 回傳標準欄位，不暴露 DB internal/raw fields。

2. `GET /api/v1/properties/{id}`
   - 回傳單個標準化 property。
   - 404 時標準 error envelope。

3. `GET /api/v1/property-stats`
   - 回傳 total/byStatus/bySource/completeness summary。
   - 可直接包裝現有 `/api/dashboard`，但要補更細 status/source 統計。

4. `PATCH /api/v1/properties/{id}/review`
   - 審核狀態更新 endpoint。
   - 建議 action：`publish`、`archive`、`reject`、`draft`。
   - 目前只有 `POST /api/confirm/<id>`（draft→published）和 `DELETE /api/listing/<id>`；沒有非破壞式 reject/archive endpoint。
   - 建議用 `archived` 表示 reject/hide，不要物理刪除。

5. Auth/CORS
   - 目前沒有 API auth；外部前端需要新增 Bearer/API key 或反向代理層保護。
   - 新增 CORS allowlist，例如只允許指定前端 domain。
   - 寫入/審核 endpoint 必須要求 auth；讀取 endpoint視產品需要可公開或 auth。

6. Schema consistency
   - 如果保留 `lead` 狀態，需要 migration 將 CHECK 改成 `draft/published/archived/lead`，或改用 `source='akiya_bank'` + `status='draft'` 表示 lead。
   - `mgmt_fee` / `repair_reserve` 是 TEXT；API adapter 應轉成 integer/null。

## 10. 驗證記錄

本次分析/產出前做過以下唯讀驗證（另有一個本地 DB backfill/前端文案修正已在上一輪收尾，不屬於本次 API spec 文件）：

- `GET http://127.0.0.1:8900/collection` → HTTP 200，24683 bytes。
- `GET http://127.0.0.1:8900/api/listings` → HTTP 200，39649 bytes。
- Flask route map 已用 `.venv/bin/python` 實際列出。
- SQLite schema 已用 `PRAGMA table_info(listings)` 實際讀取。
- 測試套件：`.venv/bin/python -m pytest tests -q` → `23 passed in 0.26s`。

## 11. 安全注意事項

- 本報告沒有輸出 `.env`、API key、cookie、password、SSH key、DB password 或 token。
- `sample-properties.json` 使用現有 DB 的實際結構與公開展示欄位；未包含 REINS PDF 原始文件連結、經紀公司聯絡資料、憑證或 session 資料。
- 外部前端接入前，強烈建議先補 auth + CORS allowlist，再開放 `PATCH /review`。