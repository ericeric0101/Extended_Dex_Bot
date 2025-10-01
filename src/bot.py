"""Entry point wiring all components together."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List

from rich.console import Console
from rich.table import Table
from x10.perpetual.orders import SelfTradeProtectionLevel

from .account_ws import AccountStream
from .config import BotConfig, MarketConfig, get_endpoints, get_settings, load_bot_config
from .executor import ExecutionEngine, build_trading_client
from .md_source import MarketDataSource
from .orderbook import OrderBook
from .pnl import PnLTracker
from .quoting import MarketQuotingConfig, QuoteEngine
from .rest import RestClient
from .risk import RiskConfig, RiskManager

console = Console()
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

        risk_manager.update_limits(max_net_units, max_order_units)

        inventory = state.inventory

        decision = quote_engine.compute_quote(mid, inventory, sigma or Decimal("0"), funding_rate)
        bid_allowed = risk_manager.can_place_order(inventory, decision.bid_size)
        ask_allowed = risk_manager.can_place_order(-inventory, decision.ask_size)
        bid_size = decision.bid_size if bid_allowed else Decimal("0")
        ask_size = decision.ask_size if ask_allowed else Decimal("0")

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
            console.log({"account_event": event})

        if event_type == "POSITION":
            positions = data.get("positions") or []
            is_snapshot = data.get("isSnapshot", False)
            if is_snapshot:
                for state in states.values():
                    state.inventory = Decimal("0")
                    state.entry_price = Decimal("0")

            for pos in positions:
                market = pos.get("market")
                state = states.setdefault(market, MarketState())
                size = Decimal(str(pos.get("size", "0")))
                side = (pos.get("side") or "").upper()
                signed_size = size if side != "SHORT" else -size
                state.inventory = signed_size
                entry_price = pos.get("open_price") or pos.get("entry_price") or "0"
                entry_price_value = pos.get("open_price") or pos.get("openPrice") or pos.get("entry_price")
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
        table = Table("Component", "Value")
        for key, value in snapshot.items():
            table.add_row(key, f"{value:.6f}")
        console.print(table)
        await asyncio.sleep(interval)


async def _orderbook_consumer(
    market: str,
    orderbook: OrderBook,
    source: MarketDataSource,
    state: MarketState,
) -> None:
    async for snapshot in source.orderbook_snapshots(market):
        orderbook.ingest_snapshot(snapshot)
        state.mid_price = orderbook.mid_price()


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


async def _arm_dead_man_switch(bot_cfg: BotConfig, settings, endpoints) -> None:
    if bot_cfg.dead_mans_switch_sec <= 0:
        return
    rest_client = RestClient(settings, endpoints)
    try:
        payload = {"countdownTime": bot_cfg.dead_mans_switch_sec}
        await rest_client.post(
            "/user/deadmansswitch",
            json=payload,
            params={"countdownTime": bot_cfg.dead_mans_switch_sec},
        )
        # console.log(
        #     {
        #         "dead_mans_switch": {
        #             "countdown": bot_cfg.dead_mans_switch_sec,
        #             "status": "armed",
        #         }
        #     }
        # )
    except Exception as exc:  # pragma: no cover - network dependent
        console.log({"dead_mans_switch": {"status": "failed", "error": str(exc)}})
    finally:
        await rest_client.aclose()


async def _hydrate_market_trading_rules(bot_cfg: BotConfig, settings, endpoints) -> None:
    rest_client = RestClient(settings, endpoints)
    try:
        for market_cfg in bot_cfg.markets:
            try:
                response = await rest_client.get("/info/markets", params={"market": market_cfg.name})
            except Exception as exc:  # pragma: no cover - network dependent
                console.log(
                    {"market_rules": {"market": market_cfg.name, "status": "failed", "error": str(exc)}}
                )
                continue

            data = response.get("data") or []
            if not data:
                console.log({"market_rules": {"market": market_cfg.name, "status": "missing"}})
                continue

            trading_cfg = data[0].get("tradingConfig", {})
            min_order = trading_cfg.get("minOrderSize")
            price_tick = trading_cfg.get("minPriceChange")
            if min_order is not None:
                market_cfg.min_order_size = float(min_order)
            if price_tick is not None:
                market_cfg.price_tick = float(price_tick)

            console.log(
                {
                    "market_rules": {
                        "market": market_cfg.name,
                        "min_order_size": market_cfg.min_order_size,
                        "price_tick": market_cfg.price_tick,
                    }
                }
            )
    finally:
        await rest_client.aclose()


async def run() -> None:
    settings = get_settings()
    endpoints = get_endpoints()
    bot_cfg = load_bot_config()

    await _hydrate_market_trading_rules(bot_cfg, settings, endpoints)
    await _arm_dead_man_switch(bot_cfg, settings, endpoints)

    market_data = MarketDataSource(settings=settings, endpoints=endpoints)
    trading_client = await build_trading_client(settings)
    try:
        stp_level = SelfTradeProtectionLevel[bot_cfg.stp.upper()]
    except KeyError:
        console.log({"stp": {"status": "unsupported", "value": bot_cfg.stp}})
        stp_level = SelfTradeProtectionLevel.ACCOUNT
    account_stream = AccountStream(settings=settings, endpoints=endpoints)
    pnl = PnLTracker()

    enabled_markets: List[MarketConfig] = [m for m in bot_cfg.markets if m.enabled]
    if not enabled_markets:
        console.log("No markets enabled in config.json; exiting.")
        return

    market_states: Dict[str, MarketState] = {cfg.name: MarketState() for cfg in enabled_markets}

    tasks = [
        asyncio.create_task(account_loop(account_stream, market_states, pnl)),
        asyncio.create_task(monitor_pnl(pnl, interval=5.0)),
    ]

    for market_cfg in enabled_markets:
        state = market_states[market_cfg.name]
        orderbook = OrderBook(market=market_cfg.name)
        quote_engine = _build_quote_engine(market_cfg)
        risk_manager = _build_risk_manager(bot_cfg)
        execution = ExecutionEngine(
            market=market_cfg.name,
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
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.log("shutdown requested")


if __name__ == "__main__":
    main()
