"""Simplified Avellaneda-Stoikov style quoting engine."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Optional

from .schemas import QuoteDecision


@dataclass
class MarketQuotingConfig:
    market: str
    k_relative_bps: Decimal
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
        # 🔥 核心修改 1：根據庫存佔資金比例調整公允價格（相對 K）
        cap = self._config.quote_notional_cap
        inventory_notional = abs(inventory) * mid_price
        if cap > Decimal("0"):
            inventory_ratio = min(inventory_notional / cap, Decimal("1"))
        else:
            inventory_ratio = Decimal("0")

        if inventory > Decimal("0"):
            direction = Decimal("1")
        elif inventory < Decimal("0"):
            direction = Decimal("-1")
        else:
            direction = Decimal("0")

        k_term = self._config.k_relative_bps / Decimal("10000")
        inventory_price_adjustment = direction * mid_price * k_term * inventory_ratio
        fair_price = mid_price - inventory_price_adjustment

        sigma_term = sigma if sigma is not None else Decimal("0")
        funding_term = funding_rate if funding_rate is not None else Decimal("0")

        # 🔥 核心修改 2：根據庫存比例動態擴大 spread
        inventory_spread_adjustment = (
            inventory_ratio
            * self._config.inventory_spread_multiplier
            * self._config.base_spread
        )

        half_spread = (
            self._config.base_spread
            + self._config.alpha * sigma_term
            + self._config.beta * (funding_term / Decimal("3"))
            + inventory_spread_adjustment  # 🔥 庫存越大，價差越大
        )
        
        bid_price = self._floor_to_tick(fair_price * (Decimal("1") - half_spread))
        ask_price = self._ceil_to_tick(fair_price * (Decimal("1") + half_spread))

        # 🔥 核心修改 3：根據庫存方向與比例調整訂單大小
        base_size = self._base_size(mid_price)
        skew = direction * base_size * self._config.inventory_sensitivity * inventory_ratio

        # 持有多單時：減小買單、增大賣單
        # 持有空單時：增大買單、減小賣單
        bid_size = self._clip_size(base_size - skew)
        ask_size = self._clip_size(base_size + skew)

        # 🔥 新增：極端情況處理 - 如果庫存過大，完全取消同向訂單
        inventory_threshold_ratio = Decimal("0.8")

        if inventory_ratio > inventory_threshold_ratio:
            if direction > 0:
                bid_size = Decimal("0")
            elif direction < 0:
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
