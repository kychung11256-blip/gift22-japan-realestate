# REINS 交接文件 — 2026-08-30

> 目的：聽日開新 Hermes 對話可以直接接手，唔使重新調查。
> 本文件只根據 2026-08-30 實際完成內容，唔含猜測。
> **credentials / password / cookie 實際值唔會寫入本文件，只寫來源位置。**
> 注意：REINS 每日日本時間 23:00 關站，實站測試要避開停站時間。

---

# 1. Project Context

- **Project root**: `/home/ubuntu/ai-team/platform/`
- **Domain**: `gift22.vip`（BytePlus 機 101.47.152.49）
- **Flask port**: `8900`
- **Service**: `platform.service`（systemd 管理）
- **DB path**: `/home/ubuntu/ai-team/platform/data/listings.db`（SQLite, WAL mode）
- **主要 backend files**:
  - `server.py` — Flask routes（search / import / listing / collection）
  - `db.py` — DB init + lightweight migrations
  - `reins_client.py` — REINS session / search / PDF download / import orchestration
  - `reins_pdf_parser.py` — coordinate-based overview PDF parser（production）
  - `reins_pdf_render.py` — drawing PDF → image renderer
  - `reins_import_jobs.py` — multi-item background import job store + worker
  - `geocode_client.py` — GSI geocoding
  - `suumo_scraper.py` / `suumo_search.py` — SUUMO source（保留）
- **主要 frontend files**:
  - `collection.html` — 搜尋 + 多選 + 匯入（REINS/SUUMO）
  - `listings.html` — 全部物件 card 列表
  - `listing.html` — 物件詳情頁
  - `map.html` — 地圖
- **REINS login/session 相關檔案**:
  - `reins_client.py` — `login_and_verify()` / `_has_storage_state()` / `_save_storage_state()`
  - Storage state: `/home/ubuntu/ai-team/platform/data/reins_storage_state.json`
  - Credentials 由環境變數讀取：`REINS_LOGIN_URL` / `REINS_MEMBER_ID` / `REINS_PASSWORD`（實際值喺 `.env`，**唔喺呢份文件**）

---

# 2. Johnny AI 目前產品定位

**最新決策（2026-08-30）：Johnny AI 唔係要預先匯入全部 REINS 房源。**

核心定位係：**日本不動產智能搜尋引擎 / REINS Intelligent Search Layer**。

流程：

```
Natural language user query
→ Johnny requirement understanding
→ Search Plan JSON
→ REINS hard filters
→ lightweight search results
→ post-filter / ranking
→ shortlist
→ 用戶選中先 full import
→ overview PDF + drawing PDF
→ Johnny listing page
```

要點：

- **Transient search results 唔需要全部入 DB**——search result 只係即時資料。
- **用戶選中/收藏後先做完整 import**（overview + drawing + DB）。
- **REINS 係第一個核心 source**。
- Yahoo!不動産等其他來源之後先接，**暫時唔做**。

---

# 3. REINS Login / Session

目前狀態：

- **Login URL / landing URL**: `https://system.reins.jp/`（主選單 `https://system.reins.jp/main/KG/GKG003100`）
- **Login bootstrap 已成功**（Playwright persistent session）。
- **Storage state path**: `/home/ubuntu/ai-team/platform/data/reins_storage_state.json`
  - Cookies: `REMEMBER-ME`（長期）+ `ACCESS-TOKEN`（session）
- **Session reuse work**：所有 search / download 都用 `storage_state=STORAGE_STATE_PATH` 重用。
- **2FA / captcha**：目前**唔需要**；如果遇到，`reins_client.py` 會 raise `ReinsManualInterventionRequired` 並停止（唔會自己繞過）。
- **`reins_client.py` 相關 function**：
  - `login_and_verify()` — 首次登入 + 存 storage state
  - `_has_storage_state()` — 檢查 session 存在
  - `_looks_like_login_page(pg)` — 偵測 session 失效
  - `search_properties(filters, headless)` — 搜尋（含 pagination）
  - `download_overview_pdf(reins_id)` / `download_drawing_pdf(reins_id)`
  - `import_reins_listing(reins_id, drawing_available)` — 單件完整 import
  - `parse_overview_pdf(path)` — production wrapper（→ reins_pdf_parser）
- **停站時間**：REINS 每日**日本時間 23:00 關站**。下一輪實站測試要避開停站時間。

（帳號密碼唔喺呢度；喺 platform `.env`。）

---

# 4. REINS Search 已完成內容

- **Search page URL**: `https://system.reins.jp/main/BK/GBK001210`（売買物件検索）
- **Result page**: 同上頁 AJAX 載入結果
- **每頁 50 件**
- **REINS 500 件上限**：found=500 代表實際可能 >500；UI 已顯示「検索結果が500件上限に達しています。条件を絞り込んでください。」（唔聲稱總數係 500）
- **Pagination 已支援**：用真實 pagination DOM（`.p-pagination button[aria-label="Go to page N"]`），唔係 URL 猜。目標頁收埋喺「…」後面時逐頁撳「Go to next page」。
- **page 1/2/3 已驗證**：各 50 件、reins_id 唔同、range 正確（1–50 / 51–100 / 101–150）。
- **Cross-page selection 已驗證**：`selected` 係 `Map` keyed by `reins_id`，換頁唔清空；page1 揀 3 + page2 揀 2 = 已選 5；返回 page1 仍 selected。
- **already_imported 標記**：search 結果對照 DB `source='reins' AND reins_id`，跨頁正確。
- **REINS mode import 上限目前 20 件**（`IMPORT_MAX = 20`）。
- **SUUMO 舊 source 仍保留**（同步 import，上限 3 件，唔受影響）。

目前 production API：

- `POST /api/collection/search` — body: `{source, pref, city, priceMin, priceMax, walkMin, page}` → 回 `{code, found, page, page_size, total_pages, hit_limit, listings[]}`
- `POST /api/collection/import` — REINS: `{source:'reins', items:[{reins_id, drawing_available}]}` → 立即回 `{code, job_id, total}`（background job，唔阻塞 HTTP）。SUUMO: `{urls:[...]}` 同步。
- `GET /api/collection/import-status/<job_id>` — 回 job 狀態 + 每件 status。
- `POST /api/collection/import-resume/<job_id>` — resume session_expired / 未完成 job。

---

# 5. REINS Search Capability Audit

完整記錄喺：`/home/ubuntu/ai-team/platform/reins_search_capabilities.json`

- **26 個 hard filter 群組 / 50+ individual conditions**
- **location 最多 3 組**（所在地１/２/３）
- **station/line 最多 3 組**（沿線１/２/３）
- selector 係 REINS Vue BVID（動態生成但喺 GBK001210 頁面穩定，已實測存在）

Capability 摘要（全部 hard_filter，除非另註）：

| Johnny key | REINS label | type | values/unit |
|---|---|---|---|
| target | 対象区分 | radio | 在庫 / 成約 |
| sort | 並び順 | select+方向 | 最新順/価格/面積/㎡単価/沿線駅 + ▲▼ |
| property_type | 物件種別１ | select | 売土地/売一戸建/売マンション/売外全/売外一 |
| new_or_used | 新築・中古区分 | radio | 指定なし/新築/中古 |
| land_rights | 土地権利/借地権種類 | radio | 指定なし/所有権のみ/借地権のみ |
| has_drawing | その他の条件 | checkbox | 図面ありのみ |
| has_image | その他の条件 | checkbox | 物件画像ありのみ |
| auction_only | その他の条件 | checkbox | オークションのみ |
| transaction_status | 取引状況 | radio | 指定なし/公開中のみ/購入申込あり/一時停止中 |
| pref | 都道府県名 | text | 完全一致 |
| city | 所在地名１ | text | 前方/部分；「23区」= 東京23区全域 |
| building_name | 建物名 | text | 部分一致 |
| line | 沿線名 | text | 完全一致 |
| station | 駅名 | text | 完全一致（連絡駅都搜） |
| walk | 駅から徒歩 | text+select | 分 or ｍ |
| price | 価格 | range | 万円（在庫のみ有効） |
| sold_price | 成約価格 | range | 万円（成約のみ有効） |
| tsubo_unit_price | 坪単価 | range | 万円/坪 |
| exclusive_area | 専有面積 | range | ㎡（マンション/外一） |
| land_area | 土地面積 | range | ㎡（土地/戸建/外全） |
| building_area | 建物面積 | range | ㎡（戸建/外全） |
| room_count | 間取部屋数 | range | 室 |
| layout_type | 間取タイプ | checkbox multi | ワンルーム/Ｋ/ＤＫ/ＬＫ/ＬＤＫ/ＳＫ/ＳＤＫ/ＳＬＫ/ＳＬＤＫ |
| corner_room | 部屋位置 | checkbox | 角部屋 |
| floor | 所在階 | range | 階（マンション/外一） |
| orientation | バルコニー方向/採光面方向 | select | 北/北東/東/南東/南/南西/西/北西 |
| road | 接道状況/幅員 | select+text | 一方/角地/三方/四方/二方；m以上 |
| city_planning | 都市計画 | select | 市街/調整/非線引き/域外/準都市 |
| use_district | 用途地域 | select | 一低/二中/二住/近商/商業/準工/工業/工専/... |
| best_use | 最適用途 | select | 住宅用地/マンション用地/... |
| owner_change | オーナーチェンジ | select | のみ/を除く |
| parking | 駐車場の有無 | select | 有／空有/無／空無/近隣確保 |
| building_age | 築年月 | range（年+月） | 1926（昭和1)～2028（令和10) |
| equipment | 設備・条件・住宅性能等 | textarea | 部分一致 |
| notes | 備考１ | text | 部分一致 |
| neighborhood | 周辺環境 | text | 部分一致 |
| registered/changed/sold/sold_registered date | 登録年月日/変更年月日/成約年月日/成約登録年月日 | radio+和暦 | 全期間/3日/1週/1月/1年/指定 |

分三類：

**A. hard_filter**（REINS 表單直接搜尋）：上表全部。

**B. post_filter**（REINS search result / overview PDF 有資料，但表單冇直接 filter，由 Johnny script 二次過濾）：
- 管理費（mgmt_fee）
- 修繕積立金（repair_reserve）
- total_units（棟総戸数）
- total floors（地上階層 floors_above / 地下階層 underground_floors）
- balcony（バルコニー面積 balcony_sqm）
- structure（建物構造）
- management company / type（管理会社名 / 管理形態）
- yield（利回り，Johnny 用 price + 預估租金自算）

**C. semantic_preference**（REINS 無直接結構化 filter，之後由 Johnny ranking / LLM 處理）：
- 景觀好 / 開揚
- 安靜 / 閑静
- 高級感 / ブランド
- 管理質素好
- 採光好（方向可 hard filter，但「採光好」係評價）
- 生活便利（距離可搜，「便利」係評價）
- 家庭/育兒適合度

---

# 6. PDF Parser

Production parser 架構：

- **檔案**: `reins_pdf_parser.py`
- **技術**: PyMuPDF（`page.get_text('dict')`）coordinate-based
- **唔用 OCR / AI**
- **固定雙欄 coordinate mapping**：
  - 左欄 label x≈25.4，左欄 value x≈100–305
  - 右欄 label x≈306.0，右欄 value x≈380–520
  - 每欄位用「label 文字 → 佢嘅 y 行 → 同一行（±2pt）且 x 落喺該欄 value 區嘅 span」對位
- **regex 只做 normalization**（數字/金額去逗號/去「万円/円/㎡/階/戸」/和暦換算），唔用 regex 判斷版面欄位關係
- **日本橋（100140509385）+ HARUMI（100140505492）cross-check 100%**
- **Tests**: `tests/test_reins_pdf_parser.py` — **6 passed**（known-value × 2 PDF + empty-blank × 2 + schema-compat × 2）
- **Production**: `reins_client.parse_overview_pdf()` 已改成薄 wrapper → `reins_pdf_parser.parse_overview_pdf(path)`
- **Legacy parser**: 保留做 `reins_client._legacy_parse_overview_pdf()`（rollback 用，production 唔會行；舊版 runtime 會 `NameError: size_sqm` crash）

Parser schema keys（33 個）：

```
reins_id, price, property_type, address, building_name, line, station, walk_min,
room_layout, size_sqm, built_date, built_date_full, structure, floor, floors_above,
underground_floors, orientation, balcony_sqm, total_units, land_rights, use_district,
current_status, handover_timing, transaction_type, mgmt_fee, repair_reserve,
management_company, management_type, parking, registration_date, latest_update_date,
notes_freetext, registration_no
```

特別處理：

- **registration_no / notes_freetext**：REINS 習慣——冇其他備考時備考欄會填登録No.（全形英數）。Parser 如實讀取備考欄做 `notes_freetext`；如果成段係全形英數 6–10 位，另外抽出做 `registration_no`（半形正規化）。**唔改 REINS 原意**。
- **Full-width Japanese raw value 保留**：parser 回傳 REINS 原始全形值（`３ＬＤＫ`/`ＲＣ`/`令和 7年 8月`）。
- **Presentation layer 先 deterministic normalize**（`listing.html` 嘅 `z2h`/`normBuiltDate`/`normMoney`/`normArea`）：`３ＬＤＫ`→`3LDK`、`ＲＣ`→`RC`、`令和 7年 8月`→`令和 7年 8月（2025年8月）`、`13990`→`13,990万円`。**唔覆蓋 DB raw value**。

---

# 7. PDF / Drawing Pipeline

**Overview PDF**：
- 必有
- 下載到 server：`uploads/reins/<reins_id>/overview.pdf`（atomic replace，冇 duplicate）
- 經 `reins_pdf_parser.py` parse → 欄位入 DB

**Drawing PDF**：
- Optional（`drawing_available` 先有）
- PDF 保存：`uploads/reins/<reins_id>/drawing.pdf`
- **Render**: `reins_pdf_render.py` — `render_drawing_pdf(pdf_path, output_dir, dpi=180)`
  - PyMuPDF `page.get_pixmap`，180 DPI，保留原比例
  - 唔用 AI / OCR
  - output：`drawing_page_N.jpg`（JPEG quality 85）
  - 重新 render 前清理舊 `drawing_page_*.jpg`（唔留 `page_1(1).jpg` duplicate）
- **DB**: rendered image paths 存 `floorplan_images`（reuse 現有欄位，唔開新 table）：`[{"url": "/uploads/reins/<id>/drawing_page_1.jpg", "label": "REINS 図面 1"}]`
- **Listing card / gallery 用 rendered drawing image**；原始 drawing PDF button 保留（`図面を見る`）

HARUMI 實例：
- `uploads/reins/100140505492/overview.pdf`（86884 bytes）
- `uploads/reins/100140505492/drawing.pdf`（971183 bytes）
- `uploads/reins/100140505492/drawing_page_1.jpg`（4784×3382, 1.82MB）

---

# 8. DB / Import Pipeline

- **DB**: SQLite `data/listings.db`（WAL mode）
- **REINS listing 識別**: `source='reins'` + `reins_id`
- **Upsert 規則**: `SELECT id FROM listings WHERE source='reins' AND reins_id=?`
  - 存在 → UPDATE
  - 唔存在 → INSERT（id = `REINS<timestamp><4hex>`，status='published'）
- **Duplicate re-import = update**（唔增加 duplicate row，已驗證）
- **Geocode**: `geocode_client.geocode(address)`（GSI API）→ `latitude`/`longitude`
- **PDF paths**: `reins_overview_pdf` / `reins_drawing_pdf`
- **Migration（2026-08-30 新增 column）**: `building_name`, `management_company`, `management_type`, `registration_no`, `underground_floors`
- **注意**: `mgmt_fee` / `repair_reserve` 目前仍係 **TEXT**（存 `'27540'` 字串）。值正確但唔係 INTEGER。顯示層 `normMoney` 已處理；如要數字運算先需要改 schema（**暫時唔好改**）。

**多件 import**（`reins_import_jobs.py`）：
- Background thread（唔用 Redis/Celery）
- JSON job store：`data/import_jobs/<job_id>.json`（持久化，可 resume）
- **concurrency=1**，逐件處理，唔 parallel
- 每件狀態：`pending` / `processing` / `success` / `partial` / `failed`
- **drawing fail 唔 rollback overview+DB**——標 `partial`，listing 保留
- **Session expired**：停止後續，未完成 item 保持 `pending`，job status=`session_expired`；前端顯示 +「繼續」掣（resume）。唔會自己不停 retry login。

---

# 9. Listing UI

目前 REINS listing 顯示：

- **`/listings` card 主圖**：用 `drawing_page_1.jpg`（floorplan_images 優先）→ photos → 写真なし
- **`/listing/<id>` gallery**：用 rendered drawing images，可 click lightbox 放大
- **PDF buttons**：`📄 REINS 概要`（→ overview.pdf）+ `📐 図面を見る`（→ drawing.pdf），browser 原生 viewer 開
- **完整概要 fields**：分 section（物件概要 / 交通 / 土地権利 / 管理 / 取引状況），有值先顯示，空值唔顯示「-」
- **Long text wrap**：label 欄固定 200px，value 欄 `white-space:normal; word-break:break-word`，長地址/建物名自動 wrap，冇 ellipsis 截斷
- **Deterministic normalizer**：見第 6 節
- **registration_no 同 notes duplicate suppression**：`normalizeCompareText()` 比較（全形→半形/trim/去空格/uppercase），如果 `normalize(notes_freetext) === normalize(registration_no)` 就顯示「登録No.」、隱藏「備考」。**唔改 parser、唔改 DB raw value**，純 presentation layer。
- **修咗嘅 bug**: gallery lightbox 原本 `onclick="openLightbox(${JSON.stringify(...)})"` 雙引號 break HTML attribute，改用 `openGalleryLightbox(i)` 行 `window._galleryImages`。

---

# 10. 已實測過嘅重要物件

**Parser test**（coordinate parser cross-check）：
- HARUMI FLAG PARK VILLAGE T棟 — `100140505492`
- 日本橋三越前アムフラット — `100140509385`

**Import test**（Phase 2B 單件 + Phase 2C 跨頁 5 件）：
- `100140505492`（HARUMI，多次 update 驗證 duplicate upsert）
- `100139596107`（エクセレント三越前，insert + update）
- `100139974823`（グリーンパーク日本橋浜町，insert）
- `100139612949`（レックス日本橋水天宮プレミアタワー，insert）
- `100140535343`（HARUMI 另一戶，insert）
- Phase 2C 跨頁：`100140535711`, `100140535704`, `100140531702`（drawing=False 樣本）, `100140495118`

---

# 11. Current Known Limitations

- **REINS search/pagination 本質仍然係 browser UI automation**（Playwright + storage state），**唔係正式 API**。REINS 利用規約禁止未授權第三方自動化存取；目前係用會員自己嘅合法 session。要留意合規風險。
- **搜尋速度偏慢**（每次 search / 換頁要開 headless browser + networkidle wait，約 10–20 秒）。
- **Pagination 曾經用固定 wait**（`wait_for_timeout`），**下一輪應避免 brittle fixed sleep**，改做 event-based / selector-based wait。
- **drawing_available search hint 可能同實際 drawing availability 不完全一致**（Phase 2C 嘅 `100140531702` search 報有圖但實際冇 → 已正確標 `図面なし`，唔會 rollback）。
- **Job store 目前單 process**（in-memory + JSON 檔案）。Flask 單 process 下 work；如改用 multi-worker（gunicorn）要搬去共享儲存。
- **`mgmt_fee` / `repair_reserve` 係 TEXT**（見第 8 節）。
- **REINS 23:00 JST 關站**，實站操作要避開。
- **暫未做 Yahoo!不動産**（等其他來源）。
- **暫未做 bulk 図面一括取得**。
- **暫未做 Redis/Celery**。

---

# 12. 明日第一優先級

**不要先繼續優化 pagination。**

下一階段先做：

## Natural Language → REINS Search Plan

目標：**首頁自然語言搜尋框成為 Johnny AI 核心入口**。

架構：

```
User query
→ LLM parse intent
→ strict SearchPlan JSON
→ deterministic validator
→ map 到 reins_search_capabilities.json
→ REINS hard filter search
→ lightweight result
→ post-filter
→ ranking
→ explanation
```

**LLM 唔可以**：
- 自己直接操作 browser
- 自己決定不存在嘅 filter（只可用 `reins_search_capabilities.json` 入面嘅 hard_filter）
- 擅自將 vague preference 變成 hard cutoff

例如：「新啲」應該先係 **preference**，唔可以擅自變「築20年內」hard filter。

**聽日應先設計**：

- **SearchPlan JSON schema**：
  - `hard_filters` — 直接 map 到 REINS 表單（price range / location / layout_type / ...）
  - `soft_preferences` — 唔係 hard cutoff（例如「新啲」「景觀好」）
  - `post_filters` — REINS 冇表單但 result/PDF 有資料（mgmt_fee / total_units / ...）
  - `unsupported_preferences` — REINS 完全做唔到嘅（要 semantic ranking）
  - `clarification_needed` — LLM 唔確定時要問返用戶
  - `ranking weights` — post-filter 後點排

---

# 13. Do Not Redo

以下**已完成**，明日新 session **唔好重新做**：

- REINS login bootstrap
- session reuse
- search first page
- pagination inspect（selector 已知：`.p-pagination button[aria-label="Go to page N"]`，total_pages 用 `aria-setsize`）
- cross-page selection（`selected` 係 `Map` keyed by `reins_id`）
- overview PDF parser（`reins_pdf_parser.py` coordinate-based，6 tests pass）
- drawing PDF download（`download_drawing_pdf`）
- PDF render（`reins_pdf_render.py`，180 DPI）
- listing display（card 主圖 / gallery / PDF buttons / 完整欄位）
- duplicate upsert（`source='reins' AND reins_id`）
- multi-item import queue（`reins_import_jobs.py`，concurrency=1，background job）
- capability audit（`reins_search_capabilities.json`）

---

*文件生成：2026-08-30。只含今日實際完成內容。*
