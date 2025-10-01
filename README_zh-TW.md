# extended-mm-bot（繁體中文說明）

符合合規要求的 Extended 永續合約做市 MVP。此機器人串流訂單簿、以簡化 Avellaneda–Stoikov 模型計算庫存傾斜報價，並透過 X10 Python SDK 管理訂單。

## 系統概述
- `src/bot.py` 會讀取 `config.json`，依照啟用的市場啟動四個協程：
  - 公開 WebSocket → 本地訂單簿 (`stream_orderbook_to_local_book`)。
  - 報價迴圈 (`quote_loop`) 根據中價、波動度與庫存計算雙邊報價，並將 USD 名目限制轉為合約數量。
  - 帳戶事件串流 (`account_loop`) 監看成交、資產變動，作為庫存與風控更新的基礎。
  - PnL 監控 (`monitor_pnl`) 以 Rich 呈現利潤組成。
- 啟動時會以 REST 呼叫 Dead Man's Switch（`/user/deadmansswitch`），確保掉線時自動撤單。
- `MarketDataSource` 以指數退避機制維持 WS 連線，`OrderBook` 將快照轉為最佳價與 rolling σ，供報價與風控使用。
- 所有 REST/WS 請求都帶 `User-Agent` 與 `X-Api-Key`（若提供），符合 Extended API 的合規要求。

## 交易邏輯概述
- **報價邏輯**：
  - `QuoteEngine.compute_quote` 以 `fair_price = mid + inventory * k` 調整公平價。
  - 半點差為 `base_spread + alpha * σ + beta * (funding / 3)`，並轉換成 bid/ask。
  - 基礎下單量由名義上限除以中價計算，再以庫存傾斜 (`inventory_sensitivity`) 調整雙邊尺寸，並限制於最大下單量。
- **開倉 / 平倉**：
  - `ExecutionEngine` 追蹤雙邊掛單，當價格偏離門檻或尺寸改變時撤單重下。掛單被撮合即形成開倉，反向成交則減倉或平倉。
  - 風控 (`RiskManager`) 監控淨部位與掛單上限，超限時拒絕新報價並透過 size=0 促使撤單，以回復到允許區間。

## 功能特色
- 非同步 REST 與 WebSocket 連線，搭配 retry/backoff 與必要標頭。
- 本地訂單簿重建，並計算滾動波動度。
- 多市場報價：依 `config.json` 中啟用的市場啟動對應的訂單簿、風控與執行協程。
- 報價引擎支援資金費調整、庫存傾斜與最小下單量門檻；風控將 USD 名目限制轉為合約數量。
- 執行模組帶入 post-only 與自成交保護層級（STP），並以 Dead Man's Switch 做掉線防護。
- PnL 拆解骨架：價差、庫存、手續費、資金費。
- 單元測試：訂單簿回放、風控行為。

## config.json 快速概覽
- `stp`: 自成交保護層級（預設 `ACCOUNT`），會映射到 X10 `SelfTradeProtectionLevel`。
- `quote_loop_ms` / `replace_coalesce_ms`: 報價節奏與撤改節流（目前節流參數保留以利後續實作）。
- `dead_mans_switch_sec`: 啟動時呼叫 `/user/deadmansswitch` 的倒數秒數。
- `risk`: 以 USD 表示的淨部位與帳戶餘額限制；程式會依當前中價換算合約數量。
- `markets`: 可為多個市場設定 K / α / β、名目上限、最小下單量、post-only 與啟用開關。
- `fees_override`: 可覆寫 maker/taker 費率（填 `null` 時改用 API `CONFIG` 事件或 `/user/fees`）。
- `quote_loop_ms`: 做市循環（下新報價/檢查市場）的時間間隔（毫秒）。250ms = 每秒約 4 次更新。
- `replace_coalesce_ms`: 「報價合併間隔」：如果在這段時間內出現多次更新需求，就合併成一次改單，避免頻繁撤掛。400ms → 意思是 0.4 秒內重複觸發的更新會併成一次。
- `dead_mans_switch_sec`: 當你呼叫 API 設定 DMS 後，如果 bot 斷線或停止，交易所會在這個時間內自動撤掉所有掛單，防止風險。建議設 60–300 秒。
- `max_net_position_usd`: 允許的最大淨部位（以美元計）。假設設 200，代表 BTC-USD、ETH-USD 等加總的淨倉不能超過 ±200 美元。
- `max_open_orders`: 允許同時掛單的最大數量，防止程式出 bug 時掛成百上千張單。
- `min_balance_usd`: 最低可用資金門檻。如果資金 < 50 美元，bot 就不應該再開新單。
- `market.k`: Avellaneda-Stoikov 模型裡的“市場深度”參數，控制報價寬度。
  - K 越大 → 報價越保守，掛單離中間價更遠。
  - K 越小 → 報價更貼近中間價，但成交率高、風險大。
- `alpha`: 庫存敏感度（inventory aversion）。越高 → 你會更積極避開單邊部位（例如多了太多 BTC 就調整賣價靠近，趕快平倉）。
- `beta`: 波動度調整因子。越高 → 在高波動時報價會更寬（避險）。
- `quote_notional_cap_usd`: 單邊報價的最大名目金額上限。例如 50 → 同時 bid/ask 各最多掛 50 USD。
- `replace_threshold_bps`: 多少價差（basis points, 0.01%）才觸發報價更新。避免小波動就不停改單。
- `min_order_size`: 該市場最小下單數量（以標的單位計）。e.g. BTC 可能是 0.001 BTC。
- `post_only`: 是否只掛 maker 單（true = 報價必須是 maker）。maker 費率通常是 0 或更低，適合做市。
- `makerFeeRate / takerFeeRate`: 預設是 null，代表使用帳戶 WS CONFIG 事件回傳的實際費率。

## 安裝與啟動
1. 複製環境設定樣板並填入 Extended 測試網 API 與 Stark 金鑰：
   ```bash
   cp .env.example .env
   ```
2. 建立虛擬環境並安裝依賴：
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. 預設連線測試網，啟動機器人：
   ```bash
   python -m src.bot
   ```

## Docker
專案提供 `docker/Dockerfile`，可建立最小化執行環境。

## 測試
```bash
pytest
```

## 注意事項
- 先在 Extended 測試網驗證重連、撤單與風控流程，確認安全後再轉主網。
- 秘密金鑰與 API key 請存放於環境變數或安全金庫，切勿入庫。
- 上線前請補強：
  - 自成交防護與更精細的庫存追蹤。
  - 將 `/account` 串流的成交資訊寫回庫存管理，以反映實際部位。
  - 監控延遲與 429 錯誤，觸發熔斷 → mass cancel + dead man’s switch。
