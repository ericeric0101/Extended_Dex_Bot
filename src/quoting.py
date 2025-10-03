"""Simplified Avellaneda-Stoikov style quoting engine."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Optional

from .schemas import QuoteDecision


@dataclass
class MarketQuotingConfig:
    market: str
    k: Decimal
    base_spread: Decimal
    alpha: Decimal
    beta: Decimal
    quote_notional_cap: Decimal
    max_order_size: Optional[Decimal] = None
    min_order_size: Decimal = Decimal("0")
    price_tick: Decimal = Decimal("0.01")
    inventory_sensitivity: Decimal = Decimal("5.0")
    inventory_spread_multiplier: Decimal = Decimal("50.0")

class QuoteEngine:
    """Compute bid/ask quotes based on inventory and volatility."""

    def __init__(self, config: MarketQuotingConfig) -> None:
        self._config = config

    def compute_quote(
        self,
        mid_price: Decimal,
        inventory: Decimal,
        sigma: Optional[Decimal],
        funding_rate: Optional[Decimal],
    ) -> QuoteDecision:
        # 🔥 核心修改 1：根據庫存調整公允價格
        # 如果持有多單 (inventory > 0)，降低 fair_price，讓賣單更有吸引力
        # 如果持有空單 (inventory < 0)，提高 fair_price，讓買單更有吸引力
        inventory_price_adjustment = inventory * self._config.k
        fair_price = mid_price - inventory_price_adjustment  # 🔥 注意這裡是減號

        sigma_term = sigma if sigma is not None else Decimal("0")
        funding_term = funding_rate if funding_rate is not None else Decimal("0")

        # 🔥 核心修改 2：根據庫存大小動態擴大 spread
        # 庫存越大，spread 越大，更激進地推動成交
        inventory_spread_adjustment = abs(inventory) * self._config.inventory_spread_multiplier * self._config.base_spread

        half_spread = (
            self._config.base_spread
            + self._config.alpha * sigma_term
            + self._config.beta * (funding_term / Decimal("3"))
            + inventory_spread_adjustment  # 🔥 庫存越大，價差越大
        )
        
        bid_price = self._floor_to_tick(fair_price * (Decimal("1") - half_spread))
        ask_price = self._ceil_to_tick(fair_price * (Decimal("1") + half_spread))

        # 🔥 核心修改 3：根據庫存方向調整訂單大小
        base_size = self._base_size(mid_price)
        skew = inventory * self._config.inventory_sensitivity
        
        # 持有多單時：減小買單、增大賣單
        # 持有空單時：增大買單、減小賣單
        bid_size = self._clip_size(base_size - skew)
        ask_size = self._clip_size(base_size + skew)
        
        # 🔥 新增：極端情況處理 - 如果庫存過大，完全取消同向訂單
        inventory_threshold = self._config.quote_notional_cap / mid_price * Decimal("0.8")
        
        if inventory > inventory_threshold:
            # 持有太多多單，停止買入
            bid_size = Decimal("0")
        elif inventory < -inventory_threshold:
            # 持有太多空單，停止賣出
            ask_size = Decimal("0")

        return QuoteDecision(
            market=self._config.market,
            bid_price=bid_price,
            bid_size=bid_size,
            ask_price=ask_price,
            ask_size=ask_size,
            fair_price=fair_price,
            half_spread=half_spread,
            sigma=sigma_term,
            inventory=inventory,
        )

    def _base_size(self, mid_price: Decimal) -> Decimal:
        if mid_price <= Decimal("0"):
            return Decimal("0")
        cap_size = self._config.quote_notional_cap / mid_price
        return self._clip_size(cap_size)

    def _clip_size(self, size: Decimal) -> Decimal:
        if size <= Decimal("0"):
            return Decimal("0")

        adjusted = max(size, self._config.min_order_size)
        if self._config.max_order_size is not None and adjusted > self._config.max_order_size:
            return self._config.max_order_size
        return adjusted

    def _floor_to_tick(self, price: Decimal) -> Decimal:
        if price <= 0:
            return Decimal("0")
        step = self._config.price_tick
        if step <= 0:
            return price
        multiples = (price / step).quantize(Decimal("1"), rounding=ROUND_DOWN)
        return multiples * step

    def _ceil_to_tick(self, price: Decimal) -> Decimal:
        if price <= 0:
            return Decimal("0")
        step = self._config.price_tick
        if step <= 0:
            return price
        multiples = (price / step).quantize(Decimal("1"), rounding=ROUND_UP)
        return multiples * step
