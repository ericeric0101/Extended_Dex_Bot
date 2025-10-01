"""Entry point wiring all components together."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import List

from rich.console import Console
from rich.table import Table
from x10.perpetual.orders import SelfTradeProtectionLevel

from .account_ws import AccountStream
from .config import BotConfig, MarketConfig, get_endpoints, get_settings, load_bot_config
from .executor import ExecutionEngine, build_trading_client
from .md_source import MarketDataSource, stream_orderbook_to_local_book
from .orderbook import OrderBook
from .pnl import PnLTracker
from .quoting import MarketQuotingConfig, QuoteEngine
from .rest import RestClient
from .risk import RiskConfig, RiskManager

console = Console()


async def quote_loop(
    market_cfg: MarketConfig,
    bot_cfg: BotConfig,
    orderbook: OrderBook,
    quote_engine: QuoteEngine,
    execution: ExecutionEngine,
    risk_manager: RiskManager,
    pnl: PnLTracker,
) -> None:
    inventory = Decimal("0")
    funding_rate = Decimal("0")
    quote_interval = max(bot_cfg.quote_loop_ms, 50) / 1000.0
    max_net_usd = Decimal(str(bot_cfg.risk.max_net_position_usd))
    cap_usd = Decimal(str(market_cfg.quote_notional_cap_usd))

    while True:
        mid = orderbook.mid_price()
        sigma = orderbook.sigma()
        if mid is None or mid <= 0:
            await asyncio.sleep(quote_interval)
            continue

        max_net_units = Decimal("0") if max_net_usd <= 0 else max_net_usd / mid
        max_order_units = Decimal("0") if cap_usd <= 0 else cap_usd / mid
        risk_manager.update_limits(max_net_units, max_order_units)

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
        await asyncio.sleep(quote_interval)


async def account_loop(stream: AccountStream) -> None:
    async for event in stream.updates():
        console.log({"account_event": event})


async def monitor_pnl(pnl: PnLTracker, interval: float = 1.0) -> None:
    while True:
        snapshot = pnl.snapshot()
        table = Table("Component", "Value")
        for key, value in snapshot.items():
            table.add_row(key, f"{value:.6f}")
        console.print(table)
        await asyncio.sleep(interval)


def _build_quote_engine(market_cfg: MarketConfig) -> QuoteEngine:
    quoting_cfg = MarketQuotingConfig(
        market=market_cfg.name,
        k=Decimal(str(market_cfg.K)),
        base_spread=Decimal(str(market_cfg.base_spread)),
        alpha=Decimal(str(market_cfg.alpha)),
        beta=Decimal(str(market_cfg.beta)),
        quote_notional_cap=Decimal(str(market_cfg.quote_notional_cap_usd)),
        max_order_size=None,
        min_order_size=Decimal(str(market_cfg.min_order_size)),
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
        await rest_client.post(
            "/user/deadmansswitch",
            params={"countdownTime": bot_cfg.dead_mans_switch_sec},
        )
        console.log(
            {
                "dead_mans_switch": {
                    "countdown": bot_cfg.dead_mans_switch_sec,
                    "status": "armed",
                }
            }
        )
    except Exception as exc:  # pragma: no cover - network dependent
        console.log({"dead_mans_switch": {"status": "failed", "error": str(exc)}})
    finally:
        await rest_client.aclose()


async def run() -> None:
    settings = get_settings()
    endpoints = get_endpoints()
    bot_cfg = load_bot_config()

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

    tasks = [
        asyncio.create_task(account_loop(account_stream)),
        asyncio.create_task(monitor_pnl(pnl, interval=5.0)),
    ]

    for market_cfg in enabled_markets:
        orderbook = OrderBook(market=market_cfg.name)
        quote_engine = _build_quote_engine(market_cfg)
        risk_manager = _build_risk_manager(bot_cfg)
        execution = ExecutionEngine(
            market=market_cfg.name,
            trading_client=trading_client,
            replace_threshold_bps=Decimal(str(market_cfg.replace_threshold_bps)),
            post_only=market_cfg.post_only,
            stp_level=stp_level,
        )
        tasks.append(asyncio.create_task(stream_orderbook_to_local_book(orderbook, market_data)))
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
