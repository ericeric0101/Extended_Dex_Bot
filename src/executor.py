"""Execution engine handling order placement and cancellation."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_CEILING, Decimal
from typing import Dict, Optional, Protocol

from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.configuration import STARKNET_MAINNET_CONFIG, TESTNET_CONFIG
from x10.perpetual.orders import OrderSide, SelfTradeProtectionLevel, TimeInForce
from x10.perpetual.trading_client import PerpetualTradingClient

from .config import MarketConfig, RuntimeSettings
from .risk import RiskManager
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

    @property
    def account(self):
        return self._client.account

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
    config = TESTNET_CONFIG if settings.environment == "testnet" else STARKNET_MAINNET_CONFIG
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
        market_cfg: MarketConfig,
        trading_client: TradingClientProtocol,
        replace_threshold_bps: Decimal,
        post_only: bool = True,
        stp_level: SelfTradeProtectionLevel = SelfTradeProtectionLevel.ACCOUNT,
        risk_manager: Optional[RiskManager] = None,
    ) -> None:
        self._market_cfg = market_cfg
        self._market = market_cfg.name
        self._client = trading_client
        self._post_only = post_only
        self._stp_level = stp_level
        self._threshold = replace_threshold_bps / Decimal("10000")
        self._orders: Dict[OrderSide, LiveOrder] = {}
        self._lock = asyncio.Lock()
        self._risk_manager = risk_manager

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
        """Place a rounded order that respects exchange precision:
        - quantity rounded by `min_order_size_change` (Minimum Change in Trade Size)
        - price rounded by `min_price_change` (or legacy `price_tick`)
        """
        # --- 1) 基本規則（全部轉成 Decimal） ---
        min_order_size = Decimal(str(self._market_cfg.min_order_size))

        # 數量步進：優先用交易所的 min_order_size_change；沒有就整數步進 1
        quantity_step = None
        msc = getattr(self._market_cfg, "min_order_size_change", None)
        if msc is not None:
            try:
                msc_dec = Decimal(str(msc))
                if msc_dec > 0:
                    quantity_step = msc_dec
            except Exception:
                pass
        if quantity_step is None or quantity_step <= 0:
            quantity_step = Decimal("1")

        # 價格步進：min_price_change > price_tick > fallback
        price_tick_dec = None
        mpc = getattr(self._market_cfg, "min_price_change", None)
        if mpc is not None:
            try:
                p = Decimal(str(mpc))
                if p > 0:
                    price_tick_dec = p
            except Exception:
                pass
        if price_tick_dec is None:
            pt = getattr(self._market_cfg, "price_tick", None)
            if pt is not None:
                try:
                    p = Decimal(str(pt))
                    if p > 0:
                        price_tick_dec = p
                except Exception:
                    pass
        if price_tick_dec is None or price_tick_dec <= 0:
            price_tick_dec = Decimal("0.00001")  # 安全預設

        # --- 2) 對齊數量（向下取整到步進，至少滿足最小單量） ---
        rounded_size = (size / quantity_step).to_integral_value(rounding=ROUND_FLOOR) * quantity_step
        if rounded_size < min_order_size:
            # 拉到最小單量，並向上扣齊步進
            rounded_size = (min_order_size / quantity_step).to_integral_value(rounding=ROUND_CEILING) * quantity_step

        # --- 3) 對齊價格（買單 floor、賣單 ceil） ---
        if side == OrderSide.BUY:
            rounded_price = (price / price_tick_dec).to_integral_value(rounding=ROUND_FLOOR) * price_tick_dec
        else:
            rounded_price = (price / price_tick_dec).to_integral_value(rounding=ROUND_CEILING) * price_tick_dec

        logging.debug(
            f"[{self._market}] qty_step={quantity_step} raw_size={size} -> {rounded_size}; "
            f"min_qty={min_order_size} | price_tick={price_tick_dec} raw_px={price} -> {rounded_price}"
        )

        # --- 4) 送單 ---
        order = await self._client.place_order(
            market_name=self._market,
            amount_of_synthetic=rounded_size,
            price=rounded_price,
            side=side,
            post_only=self._post_only,
            time_in_force=TimeInForce.GTT,
            self_trade_protection_level=self._stp_level,
        )

        # --- 5) 記錄 live order ---
        order_id = getattr(order, "id", None)
        if order_id is None:
            data = getattr(order, "data", None)
            if data is not None:
                order_id = getattr(data, "id", None)
        if order_id is None:
            return

        # 建議把 live 狀態記成「已對齊後的值」
        self._orders[side] = LiveOrder(order_id=order_id, price=rounded_price, size=rounded_size, side=side)
        if self._risk_manager:
            self._risk_manager.register_order()


    async def _cancel(self, live: LiveOrder) -> None:
        await self._client.cancel_order(order_id=live.order_id)
        self._orders.pop(live.side, None)
        if self._risk_manager:
            self._risk_manager.register_cancel()
