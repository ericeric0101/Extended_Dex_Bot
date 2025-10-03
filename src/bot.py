"""Entry point wiring all components together."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List

import httpx
from x10.perpetual.orders import SelfTradeProtectionLevel

from .account_ws import AccountStream
from .config import BotConfig, EndpointConfig, MarketConfig, RuntimeSettings, get_endpoints, get_settings, load_bot_config
from .executor import ExecutionEngine, build_trading_client
from .logging_setup import setup_logging
from .md_source import MarketDataSource
from .orderbook import OrderBook
from .pnl import PnLTracker
from .quoting import MarketQuotingConfig, QuoteEngine
from .rest import RestClient
from .risk import RiskConfig, RiskManager

DEBUG_ACCOUNT_EVENTS = os.getenv("EXTENDED_DEBUG_ACCOUNT_EVENTS", "0") == "1"


@dataclass
class MarketState:
    """Mutable per-market state shared across tasks."""

    inventory: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    mid_price: Decimal | None = None


async def quote_loop(
    market_cfg: MarketConfig,
    bot_cfg: BotConfig,
    orderbook: OrderBook,
    quote_engine: QuoteEngine,
    execution: ExecutionEngine,
    risk_manager: RiskManager,
    pnl: PnLTracker,
    state: MarketState,
) -> None:
    funding_rate = Decimal("0")
    quote_interval = max(bot_cfg.quote_loop_ms, 50) / 1000.0
    max_net_usd = Decimal(str(bot_cfg.risk.max_net_position_usd))
    cap_usd = Decimal(str(market_cfg.quote_notional_cap_usd))
    min_units_cfg = Decimal(str(market_cfg.min_order_size))

    # 添加日誌計數器 - 每60秒（1分鐘）記錄一次
    log_counter = 0
    log_interval_loops = int(60 / quote_interval)  # 1分鐘對應的循環次數

    while True:
        mid = orderbook.mid_price()
        sigma = orderbook.sigma()
        if mid is None or mid <= 0:
            await asyncio.sleep(quote_interval)
            continue

        state.mid_price = mid

        max_net_units = Decimal("0") if max_net_usd <= 0 else max_net_usd / mid
        max_order_units = Decimal("0") if cap_usd <= 0 else cap_usd / mid

        if max_order_units <= Decimal("0"):
            max_order_units = min_units_cfg
        else:
            max_order_units = max(max_order_units, min_units_cfg)

        if max_net_units <= Decimal("0"):
            max_net_units = min_units_cfg
        else:
            max_net_units = max(max_net_units, min_units_cfg)

        inventory = state.inventory
        max_order_units = max(max_order_units, abs(inventory))

        risk_manager.update_limits(max_net_units, max_order_units)

        decision = quote_engine.compute_quote(mid, inventory, sigma or Decimal("0"), funding_rate)

        # 格式化日誌輸出 - 只顯示關鍵信息到小數點後三位
        bid_bps = float((mid - decision.bid_price) / mid * 10000)
        ask_bps = float((decision.ask_price - mid) / mid * 10000)

        # 只在計數器到達時記錄日誌
        if log_counter % log_interval_loops == 0:
            bid_bps = float((mid - decision.bid_price) / mid * 10000)
            ask_bps = float((decision.ask_price - mid) / mid * 10000)
            logging.info(
                f"[{market_cfg.name}] "
                f"mid={float(mid):.2f} | "
                f"inventory={float(inventory):.4f} | "
                f"fair_price={float(decision.fair_price):.2f} | "
                f"σ={float(sigma or 0):.5f} | "
                f"inv={float(inventory):.3f} | "
                f"bid={float(decision.bid_price):.2f} (-{bid_bps:.2f}bps) | "
                f"ask={float(decision.ask_price):.2f} (+{ask_bps:.2f}bps) | "
                f"spread={float(decision.half_spread * 10000):.2f}bps"
            )
        
        log_counter += 1
        
        capped_bid = min(decision.bid_size, max_order_units)
        capped_ask = min(decision.ask_size, max_order_units)

        if capped_bid < min_units_cfg:
            capped_bid = Decimal("0")
        if capped_ask < min_units_cfg:
            capped_ask = Decimal("0")

        bid_allowed = risk_manager.can_place_order(inventory, capped_bid)
        ask_allowed = risk_manager.can_place_order(-inventory, capped_ask)
        bid_size = capped_bid if bid_allowed else Decimal("0")
        ask_size = capped_ask if ask_allowed else Decimal("0")

        if bid_size == Decimal("0") and ask_size == Decimal("0"):
            await asyncio.sleep(quote_interval)
            continue

        adjusted = decision.model_copy(update={"bid_size": bid_size, "ask_size": ask_size})
        await execution.process_quote(adjusted)

        if state.inventory != Decimal("0") and state.entry_price != Decimal("0"):
            pnl.mark_to_market(
                inventory=state.inventory,
                current_mid=mid,
                entry_price=state.entry_price,
            )

        await asyncio.sleep(quote_interval)


async def account_loop(
    stream: AccountStream,
    states: Dict[str, MarketState],
    pnl: PnLTracker,
) -> None:
    async for event in stream.updates():
        event_type = event.get("type")
        data = event.get("data") or {}

        if DEBUG_ACCOUNT_EVENTS:
            logging.info(f"Account Event: {json.dumps(event)}")

        if event_type == "POSITION":
            positions = data.get("positions") or []
            is_snapshot = data.get("isSnapshot", False)
            seen_markets = set()

            for pos in positions:
                market = pos.get("market")
                if not market:
                    continue
                seen_markets.add(market)
                state = states.setdefault(market, MarketState())
                size = Decimal(str(pos.get("size", "0")))
                side = (pos.get("side") or "").upper()
                signed_size = size if side != "SHORT" else -size
                state.inventory = signed_size
                entry_price_value = (
                    pos.get("open_price")
                    or pos.get("openPrice")
                    or pos.get("entry_price")
                    or pos.get("entryPrice")
                )
                state.entry_price = Decimal(str(entry_price_value)) if entry_price_value is not None else Decimal("0")
                mark_price_value = pos.get("mark_price") or pos.get("markPrice")
                if mark_price_value is not None:
                    state.mid_price = Decimal(str(mark_price_value))
                if state.mid_price is not None and state.entry_price != Decimal("0"):
                    pnl.mark_to_market(
                        inventory=state.inventory,
                        current_mid=state.mid_price,
                        entry_price=state.entry_price,
                    )

            if is_snapshot:
                markets_to_clear = set(states.keys()) - seen_markets
            elif not positions:
                markets_to_clear = set(states.keys())
            else:
                markets_to_clear = {market for market in states.keys() if market not in seen_markets}

            for market in markets_to_clear:
                state = states.setdefault(market, MarketState())
                state.inventory = Decimal("0")
                state.entry_price = Decimal("0")
                state.mid_price = None

        elif event_type == "TRADE":
            trades = data.get("trades") or []
            for trade in trades:
                market = trade.get("market")
                state = states.setdefault(market, MarketState())
                price = Decimal(str(trade.get("price", "0")))
                size = Decimal(str(trade.get("size") or trade.get("qty") or "0"))
                side = (trade.get("side") or "").upper()
                direction = Decimal("1") if side in {"BUY", "LONG"} else Decimal("-1")
                if size == Decimal("0"):
                    continue

                prev_inventory = state.inventory
                updated_inventory = prev_inventory + size * direction
                if updated_inventory == Decimal("0"):
                    state.entry_price = Decimal("0")
                else:
                    same_direction = (prev_inventory > 0 and updated_inventory > 0) or (
                        prev_inventory < 0 and updated_inventory < 0
                    )
                    if prev_inventory == Decimal("0") or not same_direction:
                        state.entry_price = price
                    else:
                        weighted = (abs(prev_inventory) * state.entry_price) + (size * price)
                        state.entry_price = weighted / (abs(prev_inventory) + size)
                state.inventory = updated_inventory

                mid = state.mid_price or price
                pnl.record_fill(price=price, size=size, side=side, mid_at_fill=mid)

                fee_value = trade.get("fee")
                if fee_value is not None:
                    pnl.record_fee(Decimal(str(fee_value)) * Decimal("-1"))

                if state.entry_price != Decimal("0") and state.mid_price is not None:
                    pnl.mark_to_market(
                        inventory=state.inventory,
                        current_mid=state.mid_price,
                        entry_price=state.entry_price,
                    )


async def monitor_pnl(pnl: PnLTracker, interval: float = 1.0) -> None:
    while True:
        snapshot = pnl.snapshot()
        # Convert Decimal objects to strings for JSON serialization
        log_snapshot = {k: str(v) for k, v in snapshot.items()}
        logging.info(f"PnL Snapshot: {json.dumps(log_snapshot)}")
        await asyncio.sleep(interval)


async def _orderbook_consumer(
    market: str,
    orderbook: OrderBook,
    source: MarketDataSource,
    state: MarketState,
) -> None:
    i = 0
    async for snapshot in source.orderbook_snapshots(market):
        orderbook.ingest_snapshot(snapshot)
        state.mid_price = orderbook.mid_price()
        if i % 100 == 0:  # Log every 100 snapshots to avoid spamming
            logging.info(f"[{market}] Received snapshot #{i}, new mid_price: {state.mid_price}")
        i += 1


def _build_quote_engine(market_cfg: MarketConfig) -> QuoteEngine:
    quoting_cfg = MarketQuotingConfig(
        market=market_cfg.name,
        k=Decimal(str(market_cfg.K)),
        base_spread=Decimal(str(market_cfg.base_spread)),
        alpha=Decimal(str(market_cfg.alpha)),
        beta=Decimal(str(market_cfg.beta)),
        quote_notional_cap=Decimal(str(market_cfg.quote_notional_cap_usd)),
        min_order_size=Decimal(str(market_cfg.min_order_size)),
        price_tick=Decimal(str(market_cfg.price_tick)),
    )
    return QuoteEngine(config=quoting_cfg)


def _build_risk_manager(bot_cfg: BotConfig) -> RiskManager:
    risk_config = RiskConfig(
        max_net_position=Decimal("0"),
        max_order_size=Decimal("0"),
        max_open_orders=bot_cfg.risk.max_open_orders,
    )
    return RiskManager(config=risk_config)


async def _arm_dead_mans_switch(bot_cfg: BotConfig, settings: RuntimeSettings, endpoints: EndpointConfig) -> None:
    if bot_cfg.dead_mans_switch_sec <= 0:
        return

    headers = {"User-Agent": settings.user_agent}
    if settings.api_key:
        headers["X-Api-Key"] = settings.api_key

    url = f"{str(endpoints.rest_base).rstrip('/')}/user/deadmanswitch"
    params = {"countdownTime": bot_cfg.dead_mans_switch_sec}

    try:
        async with httpx.AsyncClient(headers=headers) as client:
            response = await client.post(url, params=params)
            response.raise_for_status()

        if response.status_code == 200:
            logging.info(f"Dead man's switch armed for {bot_cfg.dead_mans_switch_sec} seconds.")
        else:
            logging.error(
                f"Failed to arm dead man's switch. Status: {response.status_code}, Response: {response.text}"
            )

    except Exception as exc:
        logging.error(f"Failed to arm dead man's switch: {exc}")


async def _hydrate_market_trading_rules(bot_cfg: BotConfig, settings, endpoints) -> None:
    rest_client = RestClient(settings, endpoints)
    try:
        for market_cfg in bot_cfg.markets:
            try:
                response = await rest_client.get("/info/markets", params={"market": market_cfg.name})
            except Exception as exc:  # pragma: no cover - network dependent
                logging.error(f"Failed to get market rules for {market_cfg.name}: {exc}")
                continue

            data = response.get("data") or []
            if not data:
                logging.warning(f"Market rules for {market_cfg.name} are missing in API response.")
                continue

            trading_cfg = data[0].get("tradingConfig", {})
            min_order = trading_cfg.get("minOrderSize")
            min_order_change = trading_cfg.get("minOrderSizeChange")
            price_tick = trading_cfg.get("minPriceChange")

            if min_order is not None:
                market_cfg.min_order_size = float(min_order)
            if min_order_change is not None:
                market_cfg.min_order_size_change = float(min_order_change)
            if price_tick is not None:
                market_cfg.min_price_change = float(price_tick)

            logging.info(
                f"Hydrated market rules for {market_cfg.name}: "
                f"min_order_size={market_cfg.min_order_size}, "
                f"min_order_size_change={market_cfg.min_order_size_change}, "
                f"price_tick={market_cfg.min_price_change}"
            )
    finally:
        await rest_client.aclose()


async def run() -> None:
    settings = get_settings()
    endpoints = get_endpoints()
    bot_cfg = load_bot_config()

    # First, hydrate the market rules to get the correct precision
    await _hydrate_market_trading_rules(bot_cfg, settings, endpoints)
    
    # Then, arm the dead man's switch
    await _arm_dead_mans_switch(bot_cfg, settings, endpoints)

    market_data = MarketDataSource(settings=settings, endpoints=endpoints)
    trading_client = await build_trading_client(settings)

    try:
        logging.info("--- Checking Account Info ---")
        balance = await trading_client.account.get_balance()
        logging.info(f"Balance: {balance.to_pretty_json()}")
        positions = await trading_client.account.get_positions()
        logging.info(f"Positions: {positions.to_pretty_json()}")
        logging.info("--- Account Info OK ---")
    except Exception as e:
        logging.error(f"--- FAILED to get account info: {e} ---")
        return

    try:
        stp_level = SelfTradeProtectionLevel[bot_cfg.stp.upper()]
    except KeyError:
        logging.warning(f"Unsupported STP level '{bot_cfg.stp}', defaulting to ACCOUNT.")
        stp_level = SelfTradeProtectionLevel.ACCOUNT
    account_stream = AccountStream(settings=settings, endpoints=endpoints)
    pnl = PnLTracker()

    enabled_markets: List[MarketConfig] = [m for m in bot_cfg.markets if m.enabled]
    if not enabled_markets:
        logging.warning("No markets enabled in config.json; exiting.")
        return

    market_states: Dict[str, MarketState] = {cfg.name: MarketState() for cfg in enabled_markets}

    tasks = [
        asyncio.create_task(account_loop(account_stream, market_states, pnl)),
        asyncio.create_task(monitor_pnl(pnl, interval=600.0)),  # 10 minutes
    ]

    # Now, with hydrated rules, create the market-specific components
    for market_cfg in enabled_markets:
        state = market_states[market_cfg.name]
        orderbook = OrderBook(market=market_cfg.name)
        quote_engine = _build_quote_engine(market_cfg)
        risk_manager = _build_risk_manager(bot_cfg)
        execution = ExecutionEngine(
            market_cfg=market_cfg,
            trading_client=trading_client,
            replace_threshold_bps=Decimal(str(market_cfg.replace_threshold_bps)),
            post_only=market_cfg.post_only,
            stp_level=stp_level,
            risk_manager=risk_manager,
        )
        tasks.append(
            asyncio.create_task(
                _orderbook_consumer(
                    market=market_cfg.name,
                    orderbook=orderbook,
                    source=market_data,
                    state=state,
                )
            )
        )
        tasks.append(
            asyncio.create_task(
                quote_loop(
                    market_cfg=market_cfg,
                    bot_cfg=bot_cfg,
                    orderbook=orderbook,
                    quote_engine=quote_engine,
                    execution=execution,
                    risk_manager=risk_manager,
                    pnl=pnl,
                    state=state,
                )
            )
        )

    await asyncio.gather(*tasks)


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.info("Shutdown requested by user.")


if __name__ == "__main__":
    main()
