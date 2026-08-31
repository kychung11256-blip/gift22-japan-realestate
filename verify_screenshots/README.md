# UI 驗收截圖規則（固定規則，適用於所有未來任務）

## 規則內容

由 2026-08-29 起，凡係涉及 **UI/前端顯示** 嘅功能任務，驗收標準必須包括：

1. **實際截圖**：唔准只用文字描述「已確認」或「應該 work」，必須附上實際截圖
2. **固定目錄**：所有截圖必須存落 `/home/ubuntu/ai-team/platform/verify_screenshots/`
3. **命名規範**：檔名格式為 `{功能}_{動作}_{時間}.png`，例如：
   - `map_initial_20260829_0558.png`（初始畫面）
   - `map_zoning_on_20260829_0558.png`（開啟用途地域後）
   - `map_marker_popup_20260829_0604.png`（點擊 marker 彈出 popup）

## 適用範圍

呢個規則適用於所有涉及以下情況嘅任務：
- 地圖顯示（marker、圖層、popup）
- 表格、列表、卡片等 UI 元件顯示
- 按鈕點擊效果
- 表單提交後嘅畫面變化
- 任何需要視覺確認嘅前端功能

## 工具

使用 `/home/ubuntu/ai-team/platform/screenshot_tool.py` 進行自動化截圖：
```bash
python3 screenshot_tool.py <url> [--selector <css>] [--wait <seconds>] [--output <path>] [--click <selector>]
```

---

**建立日期**：2026-08-29  
**建立原因**：確保 UI 功能驗收有實際證據，避免「講就話 work 但實際睇唔到」嘅情況
