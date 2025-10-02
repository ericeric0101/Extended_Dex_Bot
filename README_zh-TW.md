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
- `dead_mans_switch_sec`: 啟動時呼叫 `/user/deadmanswitch` 的倒數秒數。
- `risk`: 以 USD 表示的淨部位與帳戶餘額限制；程式會依當前中價換算合約數量。
- `markets`: 可為多個市場設定 K / α / β、名目上限、最小下單量、post-only 與啟用開關。
- `fees_override`: 可覆寫 maker/taker 費率（填 `null` 時改用 API `CONFIG` 事件或 `/user/fees`）。
- `quote_loop_ms`: 做市循環（下新報價/檢查市場）的時間間隔（毫秒）。250ms = 每秒約 4 次更新。
- `replace_coalesce_ms`: 「報價合併間隔」：如果在這段時間內出現多次更新需求，就合併成一次改單，避免頻繁撤掛。400ms → 意思是 0.4 秒內重複觸發的更新會併成一次。
- `dead_mans_switch_sec`: 當你呼叫 API 設定 DMS 後，如果 bot 斷線或停止，交易所會在這個時間內自動撤掉所有掛單，防止風險。建議設 60–300 秒。

- `max_net_position_usd`: 允許的最大淨部位。假設設 200，代表 BTC-USD、ETH-USD 等加總的淨倉 (淨曝險) 不能超過 ±200 美元。
- `max_open_orders`: 允許同時掛單的最大數量，防止程式出 bug 時掛成百上千張單。
- `min_balance_usd`: 最低可用資金門檻。如果資金 < 50 美元，bot 就不應該再開新單。
**舉例：**
1. 剛啟動：
- 帳戶餘額 1000 USD，ETH 市價 2000 USD。設定max_net_position_usd: 100
- bot 可能會同時掛 0.005 ETH 的 buy order 在 1998，0.005 ETH 的 sell order 在 2002。

2. 假設先成交 buy：
- 你買到 0.005 ETH ≈ 10 USD → 現在帳戶淨倉位是 +10 USD long。
- 還沒到 100 USD 限制，所以 bot 繼續正常掛單。

3. 如果連續吃掉你 0.05 ETH（≈100 USD）的 buy：
- 現在倉位是 +100 USD long。
- bot 會停止再掛新的 buy（避免倉位更大），只留 sell 來幫你慢慢對沖掉這 +100 USD。

4. 再多吃一點（超過 100 USD）：
- bot 邏輯會拒絕這些訂單，不讓淨倉再放大。
- 所以最大淨曝險永遠是 100 USD。

5. 如果 ETH 漲到 4000 USD：
- 那最大淨倉就會變成 0.025 ETH（因為 0.025 × 4000 ≈ 100 USD）。
- 限制永遠是名目價值，而不是固定幾顆幣。

6. max_open_orders: 20 是每個市場的限制。我們為每個 enabled 市場各建一個 RiskManager，各自統計 open order 數量，所以多個交易對都會在自己的限額內運作，不會互相占用那 20 張 quota。

7. Bot 會依 `config.json` 中啟用的市場，分別掛雙邊報價；哪個市場的行情觸發、先被成交，就在哪個市場出現部位。

8. 程式不會主動把所有資金灌到單一倉位；只能在你設定的 max_net_position_usd 範圍內累積淨曝險，而且是各市場各自計算。帳戶的實際可用資金由你和交易所決定，風控只是確保單一市場不超過你給的名目門檻。

- `market.k`: Avellaneda-Stoikov 模型裡的“市場深度”參數，控制報價寬度。
  - K 越大 → 報價越保守，掛單離中間價更遠。
  - K 越小 → 報價更貼近中間價，但成交率高、風險大。
- `alpha`: 庫存敏感度（inventory aversion）。越高 → 你會更積極避開單邊部位（例如多了太多 BTC 就調整賣價靠近，趕快平倉）。
- `beta`: 波動度調整因子。越高 → 在高波動時報價會更寬（避險）。
- `base_spread`: 這是「基礎利潤 (價差)」。它決定了您的買單和賣單分別比市場中間價低多少和高多少。這是控制您交易頻率最直接的工具。這個值越小，您的報價就越接近市場中心價，也就越容易成交。如果您的報價從未被觸發，請嘗試逐步調小 `base_spread`。例如，從預設的 0.001 (代表 0.1%) 開始，嘗試 0.0008、0.0005 等。
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

## PnL 面板每次刷新時的分解結果，代表 bot 在該市場目前為止的獲利構成：

- spread_pnl：價差收益。每當掛單被成交時，record_fill() 會用「成交價 − 當下 mid 價」乘上成交量（依買/賣方向帶正負）來計算；理想情況是 Maker 單被打掉產生正的 spread PnL。
- inventory_pnl：庫存盈虧（也就是 MtM，mark-to-market）。根據目前持有的淨部位 inventory 與 entry_price、當前 mid_price 的差額計算 (mid – entry) * inventory；你看到 0.145626，表示現價比成本高出 0.145626 USD（或合約的名義單位）。
- fees：手續費累積。record_fee() 會把每筆成交後平台收取的費用（Maker 通常 0，Taker 為負）加總；目前顯示 0，代表尚未發生需付費的成交。
- funding：資金費（Funding Rate）累積，目前尚未實作填值，所以保持 0。
- net_pnl：總盈虧，等於前面四項的合計。當前只有庫存盈虧，所以 net_pnl 也為 0.145626。


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

## 做市邏輯
此做市 bot 會同時掛出買單與賣單（即多空兩側的限價單），目標是吃回報價差並保持淨部位接近 0。
當其中一側被成交時，bot就會短暫持有單邊部位，但下一次迴圈會調整反向掛單的價格與數量，把庫存「賣掉／買回」以回到 delta-neutral。
除非刻意改風控或報價邏輯，程式不會主動偏向單邊方向長時間持倉。