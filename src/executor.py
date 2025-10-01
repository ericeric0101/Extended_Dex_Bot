"""Execution engine handling order placement and cancellation."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Protocol

from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.configuration import MAINNET_CONFIG, TESTNET_CONFIG
from x10.perpetual.orders import OrderSide, SelfTradeProtectionLevel, TimeInForce
from x10.perpetual.trading_client import PerpetualTradingClient

from .config import RuntimeSettings
from .schemas import QuoteDecision


class TradingClientProtocol(Protocol):
    async def place_order(
        self,
        market_name: str,
        amount_of_synthetic: Decimal,
        price: Decimal,
        side: OrderSide,
        post_only: bool,
        time_in_force: TimeInForce,
        self_trade_protection_level: SelfTradeProtectionLevel,
    ) -> object:
        ...

    async def cancel_order(self, order_id: int) -> None:
        ...


@dataclass
class LiveOrder:
    order_id: int
    price: Decimal
    size: Decimal
    side: OrderSide


class TradingClientAdapter:
    """Adapter around the X10 PerpetualTradingClient."""

    def __init__(self, client: PerpetualTradingClient) -> None:
        self._client = client

    async def place_order(
        self,
        market_name: str,
        amount_of_synthetic: Decimal,
        price: Decimal,
        side: OrderSide,
        post_only: bool,
        time_in_force: TimeInForce = TimeInForce.GTT,
        self_trade_protection_level: SelfTradeProtectionLevel = SelfTradeProtectionLevel.ACCOUNT,
    ) -> object:
        response = await self._client.place_order(
            market_name=market_name,
            amount_of_synthetic=amount_of_synthetic,
            price=price,
            side=side,
            post_only=post_only,
            time_in_force=time_in_force,
            self_trade_protection_level=self_trade_protection_level,
        )
        return getattr(response, "data", response)

    async def cancel_order(self, order_id: int) -> None:
        await self._client.orders.cancel_order(order_id=order_id)


async def build_trading_client(settings: RuntimeSettings) -> TradingClientAdapter:
    missing = [
        name
        for name, value in {
            "EXTENDED_API_KEY": settings.api_key,
            "EXTENDED_PUBLIC_KEY": settings.public_key,
            "EXTENDED_PRIVATE_KEY": settings.private_key,
            "EXTENDED_VAULT_ID": settings.vault_id,
        }.items()
        if value in (None, "")
    ]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"missing credentials: {joined}")
    config = TESTNET_CONFIG if settings.environment == "testnet" else MAINNET_CONFIG
    account = StarkPerpetualAccount(
        vault=settings.vault_id,
        private_key=settings.private_key,
        public_key=settings.public_key,
        api_key=settings.api_key,
    )
    client = PerpetualTradingClient(config, stark_account=account)
    return TradingClientAdapter(client)


class ExecutionEngine:
    """Maintains active quotes and handles replace/cancel logic."""

    def __init__(
        self,
        market: str,
        trading_client: TradingClientProtocol,
        replace_threshold_bps: Decimal,
        post_only: bool = True,
        stp_level: SelfTradeProtectionLevel = SelfTradeProtectionLevel.ACCOUNT,
    ) -> None:
        self._market = market
        self._client = trading_client
        self._post_only = post_only
        self._stp_level = stp_level
        self._threshold = replace_threshold_bps / Decimal("10000")
        self._orders: Dict[OrderSide, LiveOrder] = {}
        self._lock = asyncio.Lock()

    async def process_quote(self, decision: QuoteDecision) -> None:
        async with self._lock:
            await asyncio.gather(
                self._sync_side(OrderSide.BUY, decision.bid_price, decision.bid_size),
                self._sync_side(OrderSide.SELL, decision.ask_price, decision.ask_size),
            )

    async def _sync_side(self, side: OrderSide, target_price: Decimal, target_size: Decimal) -> None:
        live = self._orders.get(side)
        if target_size == 0:
            if live:
                await self._cancel(live)
            return
        if live is None:
            await self._place(side, target_price, target_size)
            return
        if live.price == 0:
            await self._cancel(live)
            await self._place(side, target_price, target_size)
            return
        price_diff = abs(live.price - target_price) / live.price
        if price_diff > self._threshold or live.size != target_size:
            await self._cancel(live)
            await self._place(side, target_price, target_size)

    async def _place(self, side: OrderSide, price: Decimal, size: Decimal) -> None:
        order = await self._client.place_order(
            market_name=self._market,
            amount_of_synthetic=size,
            price=price,
            side=side,
            post_only=self._post_only,
            time_in_force=TimeInForce.GTT,
            self_trade_protection_level=self._stp_level,
        )
        order_id = getattr(order, "id", None)
        if order_id is None:
            data = getattr(order, "data", None)
            if data is not None:
                order_id = getattr(data, "id", None)
        if order_id is None:
            return
        self._orders[side] = LiveOrder(order_id=order_id, price=price, size=size, side=side)

    async def _cancel(self, live: LiveOrder) -> None:
        await self._client.cancel_order(order_id=live.order_id)
        self._orders.pop(live.side, None)
