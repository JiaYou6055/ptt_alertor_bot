# 🤖 PTT Alert Bot (樹莓派爬蟲 & Telegram 即時通知機器人)

輕量級的 PTT 看板文章與推文追蹤機器人，完整支援 **Ptt Alertor 官方自然語言指令語法**，專門設計運行於 **樹莓派 (Raspberry Pi)** 等低功耗設備上。

---

## 🌟 特色

- 🚀 **極致輕量**：採用 Python 3 + SQLite + HTML 網頁解析，記憶體與 CPU 佔用極低。
- 📱 **完整支援 Ptt Alertor 語法**：支援中文自然語言指令（`新增 看板 關鍵字`、`新增作者`、`新增推文數`、`新增推文` 等）。
- 👥 **多看板與多關鍵字批量訂閱**：支援逗號分隔（例如 `新增 gossiping,movie 金城武,結衣`）。
- 💬 **特定文章推文即時追蹤**：支援訂閱指定 PTT 文章網址，有新推文或留言時自動推送。
- 🏆 **排行榜查詢**：支援 `排行` 指令查看目前熱門前 5 名追蹤關鍵字與作者。
- 🌙 **夜間模式防打擾 (靜音 + 拉長頻率)**：預設凌晨 01:00 ~ 07:00 啟用，輪詢時間自動拉長為 30 分鐘，且 Telegram 改為 **無聲靜音推播 (`disable_notification`)**，手機不響鈴不震動，不打擾睡眠。
- ⏰ **自動背景定期排程與熔斷冷卻**：內建 `JobQueue` 排程器（日間預設每 5 分鐘輪詢一次），當遇到 PTT 維護或網路停機時自動觸發 **Circuit Breaker 階梯式冷卻機制**。為避免單次網路抖動就凍結抓取，需**連續失敗達 3 次**才會進入冷卻（前 2 次僅記錄並於下一輪重試）：連續第 3 次失敗冷卻 15 分鐘 ➔ 第 4 次冷卻 30 分鐘 ➔ 第 5 次以上冷卻 1 小時，避免浪費樹莓派資源與無效連線。
- 🧹 **去重表每日自動清理**：內建每日凌晨 `04:00` 排程，自動清除 `pushed_articles` 去重表中 **7 天前**的記錄。過期文章早已滑出看板首頁不會再被比對，清理零風險，可避免資料庫無限增長、維持樹莓派長期運作精簡。
- 🔒 **使用者白名單防護**：支援 `ALLOWED_USER_IDS` 設定，可防止陌生人點擊使用您的機器人與樹莓派資源。
- 🔒 **自動處理 18 歲驗證**：支援 Gossiping 等限制級看板。
- 🔄 **樹莓派系統服務整合**：提供 `systemd` 設定檔，開機自動背景執行，崩潰自動重啟。

---

## 📁 專案結構

- [main.py](file:///home/kevin/ptt-alert/main.py) - 主程式入口點
- [bot.py](file:///home/kevin/ptt-alert/bot.py) - Telegram Bot 指令處理器與背景監控任務
- [crawler.py](file:///home/kevin/ptt-alert/crawler.py) - PTT 網頁爬蟲與文章/推文解析模組
- [database.py](file:///home/kevin/ptt-alert/database.py) - SQLite 資料庫模組
- [config.py](file:///home/kevin/ptt-alert/config.py) - 環境變數與設定讀取
- [systemd/ptt-alert.service](file:///home/kevin/ptt-alert/systemd/ptt-alert.service) - 樹莓派 systemd 開機自啟動服務設定

---

## 🛠️ 全新環境建置與背景自動執行步驟

### 步驟 1：建立 Python 虛擬環境 (`venv`)

1. 複製或 Clone 專案至新環境：
   ```bash
   cd ptt-alert
   ```
2. 建立獨立虛擬環境：
   ```bash
   python3 -m venv venv
   ```
3. 啟用虛擬環境並安裝依賴套件：
   ```bash
   source venv/bin/activate
   pip install -r requirements.txt
   ```

---

### 步驟 2：設定環境變數 (`.env`)

1. 在 Telegram 搜尋 `@BotFather` 建立機器人，取得 API Token（例如 `123456789:ABCdef...`）。
2. 傳送 `/myid` 給機器人以取得您的 Telegram Chat ID。
3. 複製範本檔並編輯 `.env`：
   ```bash
   cp .env.example .env
   nano .env
   ```
4. 在 `.env` 中填入 Token、超級管理員 ID 及相關設定：
   ```ini
   TELEGRAM_BOT_TOKEN=您的_TELEGRAM_BOT_TOKEN
   ADMIN_USER_ID=您的_TELEGRAM_CHAT_ID
   CHECK_INTERVAL_SECONDS=300
   COMPACT_NOTIFICATION=true
   DB_PATH=ptt_alert.db
   ```

---

### 步驟 3：手動測試執行

執行手動測試確認能順利連接 Telegram API：
```bash
python3 main.py
```
若看到以下輸出即代表成功：
```text
Database initialized successfully.
Registered PTT board & article comment check jobs.
PTT Alert Bot starting polling...
```
*(在 Telegram 開啟機器人傳送 `指令` 或 `清單` 測試，確認無誤後按 `Ctrl + C` 結束手動測試)*

---

### 步驟 4：設定樹莓派背景自動啟動 (`systemd`)

為了讓爬蟲與 Telegram 機器人能夠**在背景自動定期執行**，且在開機或網路斷線重連後持續運作，建議將其部署為 `systemd` 服務：

1. **複製服務設定檔至系統服務目錄**：
   ```bash
   sudo cp systemd/ptt-alert.service /etc/systemd/system/
   ```

2. **確認服務檔內的路徑與使用者**：
   *(預設 WorkingDirectory 為 `/home/pi/ptt-alert`，若使用 `venv`，請確保 ExecStart 指向 `venv/bin/python3`，可透過 `sudo nano /etc/systemd/system/ptt-alert.service` 調整)*

3. **載入服務並設定開機自啟動**：
   ```bash
   # 重新載入系統服務
   sudo systemctl daemon-reload
   
   # 設定開機自動啟動
   sudo systemctl enable ptt-alert
   
   # 立即在背景啟動服務
   sudo systemctl start ptt-alert
   ```

---

## 🔍 背景服務管理與 Log 監控指令

當機器人在背景執行時，您可以使用以下指令管理服務：

- **查看服務運行狀態**：
  ```bash
  sudo systemctl status ptt-alert
  ```
- **即時查看背景抓取紀錄與 Log**：
  ```bash
  journalctl -u ptt-alert -f
  ```
- **重啟背景服務**：
  ```bash
  sudo systemctl restart ptt-alert
  ```
- **停止背景服務**：
  ```bash
  sudo systemctl stop ptt-alert
  ```

---

## 📱 指令對照表 (與 Ptt Alertor 完全相容)

### 🔑 關鍵字相關
- `新增 看板 關鍵字`：新增看板關鍵字 (例如：`新增 gossiping,movie 金城武,結衣`)
- `刪除 看板 關鍵字`：刪除看板關鍵字 (例如：`刪除 gossiping 金城武`)

### 👤 作者相關
- `新增作者 看板 作者`：新增看板作者 (例如：`新增作者 gossiping ffaarr,obov`)
- `刪除作者 看板 作者`：刪除看板作者 (例如：`刪除作者 gossiping ffaarr`)

### 🔥 推噓文數門檻
- `新增推文數 看板 總數`：文章推文數達到門檻時通知 (例如：`新增推文數 beauty,joke 10`)
- `新增噓文數 看板 總數`：文章噓文數達到門檻時通知 (例如：`新增噓文數 gossiping 20`)

### 💬 特定文章推文追蹤
- `新增推文 PTT文章網址`：追蹤特定文章的最新留言 (例如：`新增推文 https://www.ptt.cc/bbs/EZsoft/M.1708247900.A.27C.html`)
- `刪除推文 PTT文章網址`：停止追蹤特定文章推文

### 📊 一般查詢與操作
- `立即檢查` (或 `/check`)：不需等待 5 分鐘輪詢，立即強制連線 PTT 抓取最新文章與推文。**兼具推播管道自我測試**：若本輪沒有新文章可推（皆已推送過），會自動重送一篇最新且符合訂閱門檻的文章（標注 🧪【推播管道測試】），讓您確認 Telegram 推播是否正常。
- `清單` (或 `/list`)：顯示設定的看板、關鍵字、作者、推噓文與追蹤文章
- `排行` (或 `/top`)：顯示前五名最受歡迎的追蹤關鍵字與作者
- `/night`：查看夜間靜音模式運算狀態與頻率
- `/myid`：查詢自己的 Telegram Chat ID 與帳號名稱
- `授權 <Chat_ID> [備註/姓名]` (或 `/allow <ID> [備註/姓名]`)：動態授權 Chat ID 並可附帶備註姓名（免重啟，如 `授權 123456789 小明`）
- `取消授權 <Chat_ID>` (或 `/deny <ID>`)：動態撤銷某 Chat ID 的存取權限（免重啟）
- `白名單` (或 `/whitelist`)：查看目前所有獲授權的 Chat ID 列表與自動紀錄的人名網名

---

## 🧪 執行單元測試
```bash
python3 -m unittest discover tests
```
