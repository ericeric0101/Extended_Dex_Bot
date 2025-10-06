[中文說明](README_zh-TW.md)

# extended-mm-bot

Compliance-first market-making MVP for Extended perpetuals. The bot streams order books, computes simplified Avellaneda–Stoikov quotes with inventory skew, and manages orders through the X10 Python SDK.

## System Overview
- `src/bot.py` reads `config.json` and spins up four core coroutines for every enabled market:
  - Public WebSocket → local order book (`stream_orderbook_to_local_book`).
  - Quoting loop (`quote_loop`) that derives bid/ask from mid price, volatility, and inventory, converting USD notionals into contract size.
  - Account stream (`account_loop`) that ingests private events (fills, balances, positions) and updates inventory/risk state.
  - PnL monitor (`monitor_pnl`) that periodically logs spread, inventory, fee, and funding components.
- A REST call arms the Dead Man’s Switch (`/user/deadmanswitch`) during startup so stale orders are cancelled on disconnect.
- `MarketDataSource` maintains WS connections with exponential backoff; `OrderBook` rebuilds the book and produces best bid/ask plus rolling σ for quoting and risk.
- All REST/WS calls include the required `User-Agent` and `X-Api-Key` headers to satisfy Extended’s compliance requirements.

## Trading Logic Overview
- **Quoting**
  - `QuoteEngine.compute_quote` shifts the reservation price with `fair_price = mid + inventory * k_relative`.
  - Half spread = `base_spread + alpha * σ + beta * (funding / 3)` and is applied symmetrically to form bid/ask.
  - Base order size comes from the USD notional cap divided by mid price; inventory skew (`inventory_sensitivity`) reshapes each side and enforces max order size.
- **Execution / Hedging**
  - `ExecutionEngine` tracks live quotes; when prices move beyond replace thresholds or size changes, it cancels and re-places. Incoming fills create positions; opposite-side fills unwind them.
  - `RiskManager` enforces net-position and open-order limits. When limits are breached it zeroes target size to force cancels and bring exposure back inside the envelope.

## Features
- Asynchronous REST and WebSocket clients with retry/backoff and mandatory headers.
- Local order book reconstruction with rolling volatility estimate.
- Multi-market quoting based on the `config.json` enable flags.
- Quoting engine supports funding adjustments, inventory skew, minimum order size, and converts USD caps to contract size.
- Execution module honours replace thresholds, post-only, and self-trade protection; Dead Man’s Switch handles disconnect fail-safes.
- PnL scaffold decomposes spread, inventory, fees, and funding.
- Unit-test skeletons for order book replay and risk behaviour.

## `config.json` at a Glance
- `stp`: self-trade protection level (default `ACCOUNT` → mapped to X10 `SelfTradeProtectionLevel`).
- `quote_loop_ms` / `replace_coalesce_ms`: quoting cadence and optional throttling (coalescing currently reserved for future use).
- `dead_mans_switch_sec`: countdown used when calling `/user/deadmansswitch` on startup.
- `risk`: USD-based net-position / open-order / balance safeguards; the bot converts limits to contract units using the current mid price.
- `markets`: per-market settings (K/α/β, USD caps, min size change, post-only flag, enable switch, etc.).
- `fees_override`: optional maker/taker overrides (`null` = use `/user/fees` or WS CONFIG payload).
- `quote_loop_ms`: quoting loop interval in milliseconds (250 ms ≈ four updates per second).
- `replace_coalesce_ms`: window for batching multiple quote updates (e.g. 400 ms merges repeats into one replace).
- `dead_mans_switch_sec`: when armed, the exchange auto-cancels orders if the bot stops before the countdown expires (recommend 60–300 s).

### Core Parameters
- `max_net_position_usd`: maximum allowed net notional. Example: if set to 100 USD, the bot stops adding new longs once the net exposure reaches +100 USD and only quotes the opposite side to unwind.
- `max_open_orders`: per-market limit of concurrent working orders (each enabled market has its own `RiskManager`).
- `min_balance_usd`: minimum equity to keep quoting; if equity falls below the threshold the bot sits out.
- `K` (relative K): controls how aggressively reservation price shifts with inventory; higher values push the opposite side closer when exposure grows.
- `alpha`: inventory aversion. Larger values avoid one-sided positions faster.
- `beta`: volatility sensitivity. Larger values widen spread during high σ periods.
- `base_spread`: baseline edge. Smaller values hug the mid and increase fill rate; if orders never trade, tighten `base_spread` gradually.
- `quote_notional_cap_usd`: USD cap per side. Example: 50 → each side is limited to 50 USD notionals.
- `replace_threshold_bps`: minimum price drift (in basis points) before refreshing quotes, reducing churn from tiny moves.
- `min_order_size`: exchange minimum size per order.
- `post_only`: whether to enforce maker-only orders.
- `makerFeeRate` / `takerFeeRate`: overrides for fees; `null` uses the account defaults from the CONFIG event.

### How to Tune a New Market
1. **Observe Natural Spread** – watch the live order book (e.g., ASTER-USD). If bid is 1.89 and ask 1.91, natural spread ≈ 0.02 (≈1.05%).
2. **Set `base_spread`** – start slightly tighter than the natural spread to remain competitive (e.g., 0.8% if natural is 1.05%).
3. **Choose `K` / `alpha`** – higher volatility markets often need higher K to penalise exposure; adjust α upward if you want faster inventory rebalancing.
4. **Set `quote_notional_cap_usd`** – pick a sensible per-side order size (70 USD is a common baseline).
5. **Set `replace_threshold_bps`** – faster markets may need larger thresholds (e.g., 20–25 bps) if you want orders to rest longer.

Balancing fill-rate vs. risk is iterative: if inventory drifts too much, raise `K` and/or `alpha` so the bot becomes more eager to unload.

## Installation & Startup
1. Copy the environment template and populate Extended testnet credentials:
   ```bash
   cp .env.example .env
   ```
2. Create a virtualenv and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Launch the bot (testnet by default):
   ```bash
   python -m src.bot
   ```

## Configuration Tips (Scaling to 1,000 USDC)
- `risk.max_net_position_usd`: keep leverage within ~2× by raising the cap to 1,500–2,000 USD (e.g., 1,800) so the bot can rebalance without running into limits.
- `risk.max_open_orders`: raise to 30–40 if you intend to quote multiple price levels; otherwise 20 remains fine.
- `risk.min_balance_usd`: consider 100–200 USD so the bot stops when equity gets too low.
- `quote_notional_cap_usd`: increase to 150–200 USD (≈15–20% of equity per side) when adding capital to speed up rebalancing.
- Other knobs (`K`, `base_spread`, `inventory_sensitivity`…) should be tuned in simulation or with small size to ensure you stay inside `max_net_position_usd` during volatile periods.

### Parameter Scaling Rules When Increasing Capital
- **Keep constant (ratio-based):** `base_spread`, `inventory_sensitivity`, `inventory_spread_multiplier`, `replace_threshold_bps`.
- **Scale linearly (absolute limits):** `quote_notional_cap_usd`, `max_net_position_usd`.
- **Recalibrate as needed:** `K` should reflect inventory *percentage* of total capital rather than absolute units (the quoting engine already uses a relative-K formulation).

## Exporting Trades / Positions / Funding History
- `scripts/export_history.py` pulls private REST data.
- Set `EXTENDED_API_KEY` and other credentials in `.env` (shared with the main bot).
- Example: export only `ETH-USD` data to `history_export/`:
  ```bash
  python scripts/export_history.py --market ETH-USD --funding-from 0 --output history_export
  ```
- Omitting `--market` fetches all markets; `--funding-from` sets the starting timestamp (ms) for funding entries.
- The script writes three CSVs:
  - `trades.csv`: fills with quantity, notional value, fees, maker/taker flag, and timestamp.
  - `positions.csv`: open/close prices, realised PnL, and timestamps.
  - `funding.csv`: funding payments alongside the applicable rate.

## Docker
A minimal runtime image is available in `docker/Dockerfile`.

## Testing
Run unit tests with:
```bash
pytest
```

## Notes / Operational Checklist
- Start on Extended testnet to validate reconnect, cancel, and risk flows before going live.
- Keep secrets (API key, Stark keys) outside version control—environment variables or a vault.
- Before production, strengthen:
  - Self-trade protection and granular inventory accounting.
  - Feeding `/account` stream fills back into inventory management in real time.
  - Monitoring latency and HTTP 429 responses, with circuit breakers (mass cancel + Dead Man’s Switch) on threshold breaches.

## Market-Making Logic Summary
The bot continuously posts both bid and ask quotes aiming to capture spread while keeping inventory near zero. A fill on one side creates a momentary position; the next cycle skews pricing and size to favour trading the opposite side, bringing inventory back to delta-neutral. Unless you explicitly change the logic, the bot does not intentionally directional-trade.
