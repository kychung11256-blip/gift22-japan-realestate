# AGENTS.md — Johnny AI Platform 執行規則

呢個檔案係累積落嚟嘅教訓同規則，每次執行任務前必讀。

## 執行紀律

1. **防 loop 規則**：如果 5 分鐘內重複同一個 tool call / 同一個動作 3 次以上，結果冇實質變化，即刻停低。喺報告開頭老實講「呢一步我卡咗，原因係 XXX，已完成部分係 YYY，未完成係 ZZZ」，唔准靜雞雞繼續試。
2. **證據規則**：唔准用「已完成」「已確認」代替實際證據。任何驗收都要附：實際 query 結果 / 實際 API response / 實際截圖，三選一至少一樣。
3. **UI 驗證規則**：涉及 UI / 前端顯示嘅任務，必須用 screenshot_tool.py 實際截圖驗證，唔可以淨係 code review 就話 work。
4. **用戶專屬動作**：如果做緊嘅嘢原本要求「等用戶 confirm 先」（例如 listing publish），唔准自己代為 confirm，呢個係用戶專屬嘅動作。
5. **合成資料禁令**：如果冇真實輸入資料（例如冇圖檔、冇 API key），唔准自己生成合成資料嚟頂替測試，要老實回報「冇資料，無法測試」。
6. **批量驗證規則**：批量處理任務（例如 20 件全選匯入），完成後要驗證「實際處理咗幾多件」，唔可以假設 loop 跑晒就當全部成功。
7. **Server 重啟規則**：開發/測試期間盡量避免頻繁重啟 server（Johnny 可能同一時間喺度用緊個網，重啟前後幾秒空窗期會令佢撞到 network error，例如「Failed to fetch」）。必須重啟時：(a) 如可行用兩個 process 做 rolling restart（新 process 起好先 kill 舊嗰個），(b) 無論用咩方法，報告入面必須主動提醒：「呢段時間我啱啱重啟過 server（約 HH:MM），如果你撞到 network error，可能係嗰個窗口期」。

## 呢個 project 嘅已知事實 / 踩過嘅坑

1. **資料來源合法性**：REINS 唔可以自動化存取（利用規約禁止，包括借用會員 login 畀第三方軟件），SUUMO / athome / homes.co.jp 呢類商業 portal 都明文禁止 scraping。合法資料來源：國交省 reinfolib API（REINFOLIB_API_KEY，已設定）、GSI geocoding、空き家バンク（有限規模，主要郊外）。
2. **マイソク圖片解析**：一定要分開問（資料表 / 平面圖 / 相片分開 call vision model），唔可以一次過讀成張圖，準確度會跌。
3. **價格防呆機制**：価格數字有數學一致性防呆機制（price/size_sqm vs price_per_sqm，容許 5% 誤差），confirm 前會擋，呢個唔可以移除。DB 存 raw yen，API 回應顯示用萬円（price_raw 欄位保留 raw 值做計算）。
4. **XKT002 用途地域查詢**：已經修正做真正 point-in-polygon（ray casting），一個座標只應該得一個用途地域結果，唔可以將個 tile 全部 polygon 夾埋輸出。
5. **DB backup 同搬機風險**：DB 有定期 backup 機制（backup_db.sh，cron 每小時），但之前搬機試過資料流失（42 筆變 14 筆，包括 13 筆 mysok 全部冧咗），搬機 / 大改動前後都要驗證資料筆數。
6. **用戶係中介（B2B）**：唔係消費者。優先順序：中介日常工作流程（路線規劃、客戶配對、上傳效率）> 視覺化功能（色塊圖層呢類）。
