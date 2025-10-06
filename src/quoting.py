"""Simplified Avellaneda-Stoikov style quoting engine."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Optional
import logging
import time

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
    inventory_sensitivity: Decimal = Decimal("1.0")
    inventory_spread_multiplier: Decimal = Decimal("0")
    min_half_spread: Decimal = Decimal("0")
    volatility_spread_multiplier: Decimal = Decimal("0")
    funding_bias_strength: Decimal = Decimal("0")
    inventory_disable_same_side_threshold: Decimal = Decimal("0.8")
    position_age_minutes: int = 0
    position_age_spread_multiplier: Decimal = Decimal("0")
    position_age_k_multiplier: Decimal = Decimal("1.0")

class QuoteEngine:
    """Compute bid/ask quotes based on inventory and volatility."""

    def __init__(self, config: MarketQuotingConfig) -> None:
        self._config = config
        self._last_ratio_log = 0.0
        self._last_ratio = Decimal("0")

    def compute_quote(
        self,
        mid_price: Decimal,
        inventory: Decimal,
        sigma: Optional[Decimal],
        funding_rate: Optional[Decimal],
        position_age_minutes: Optional[float] = None,
        best_bid: Optional[Decimal] = None,
        best_ask: Optional[Decimal] = None,
    ) -> QuoteDecision:
        # 🔥 核心修改 1：根據庫存佔資金比例調整公允價格（相對 K）
        cap = self._config.quote_notional_cap
        inventory_notional = abs(inventory) * mid_price
        if cap > Decimal("0"):
            inventory_ratio = min(inventory_notional / cap, Decimal("1"))
        else:
            inventory_ratio = Decimal("0")

        now = time.monotonic()
        if now - self._last_ratio_log >= 60.0:
            logging.info(
                "[%s] inventory_ratio=%.4f (inventory=%s, cap=%s, mid=%s)",
                self._config.market,
                float(inventory_ratio),
                str(inventory),
                str(cap),
                str(mid_price),
            )
            self._last_ratio_log = now
        self._last_ratio = inventory_ratio

        position_age_triggered = (
            position_age_minutes is not None
            and self._config.position_age_minutes > 0
            and position_age_minutes >= self._config.position_age_minutes
        )

        if inventory > Decimal("0"):
            direction = Decimal("1")
        elif inventory < Decimal("0"):
            direction = Decimal("-1")
        else:
            direction = Decimal("0")

        k_multiplier = Decimal("1")
        if position_age_triggered and self._config.position_age_k_multiplier > Decimal("0"):
            k_multiplier *= self._config.position_age_k_multiplier

        k_term = (self._config.k_relative_bps / Decimal("10000")) * k_multiplier
        inventory_price_adjustment = direction * mid_price * k_term * inventory_ratio
        fair_price = mid_price - inventory_price_adjustment

        sigma_term = sigma if sigma is not None else Decimal("0")
        funding_term = funding_rate if funding_rate is not None else Decimal("0")

        dynamic_multiplier = Decimal("1") + (self._config.volatility_spread_multiplier * sigma_term)
        if dynamic_multiplier < Decimal("0"):
            dynamic_multiplier = Decimal("0")
        base_component = self._config.base_spread * dynamic_multiplier

        inventory_spread_adjustment = (
            inventory_ratio * self._config.inventory_spread_multiplier * self._config.base_spread
        )

        funding_component = self._config.beta * abs(funding_term)
        half_spread = base_component + (self._config.alpha * sigma_term) + funding_component + inventory_spread_adjustment

        age_spread_multiplier = Decimal("1")
        if position_age_triggered and self._config.position_age_spread_multiplier > Decimal("0"):
            age_spread_multiplier += self._config.position_age_spread_multiplier
        half_spread *= age_spread_multiplier

        if self._config.min_half_spread > Decimal("0") and half_spread < self._config.min_half_spread:
            half_spread = self._config.min_half_spread

        funding_bias = self._config.funding_bias_strength * funding_term * inventory_ratio
        if funding_bias != 0 and direction != 0:
            fair_price -= direction * mid_price * funding_bias

        if position_age_triggered and direction != 0 and self._config.position_age_spread_multiplier > Decimal("0"):
            flatten_bias = inventory_ratio * self._config.position_age_spread_multiplier
            fair_price -= direction * mid_price * flatten_bias
        
        raw_bid = fair_price * (Decimal("1") - half_spread)
        raw_ask = fair_price * (Decimal("1") + half_spread)

        tick = self._config.price_tick if self._config.price_tick > 0 else Decimal("0.0001")

        bid_price = self._sanitize_price(raw_bid, tick, is_bid=True)
        ask_price = self._sanitize_price(raw_ask, tick, is_bid=False)

        book_valid = (
            best_bid is not None
            and best_ask is not None
            and best_bid > Decimal("0")
            and best_ask > Decimal("0")
            and best_ask - best_bid >= tick
        )

        if book_valid:
            buffer_ticks = Decimal("1")
            if self._last_ratio > Decimal("0"):
                buffer_ticks += (self._last_ratio * Decimal("1")).to_integral_value(rounding=ROUND_UP)
            if buffer_ticks < Decimal("1"):
                buffer_ticks = Decimal("1")
            if buffer_ticks > Decimal("3"):
                buffer_ticks = Decimal("3")

            if bid_price >= best_ask:
                adjusted_bid = best_ask - tick * buffer_ticks
                bid_price = self._sanitize_price(adjusted_bid, tick, is_bid=True)
            if ask_price <= best_bid:
                adjusted_ask = best_bid + tick * buffer_ticks
                ask_price = self._sanitize_price(adjusted_ask, tick, is_bid=False)

        # 若簿面不穩定或調整後的價格偏離中價太多，直接取消該方向下單
        price_band = Decimal("0.05")  # 允許 ±5% 偏離
        max_deviation = mid_price * price_band

        if bid_price <= Decimal("0") or abs(bid_price - mid_price) > max_deviation:
            bid_price = Decimal("0")
            bid_size = Decimal("0")
        if ask_price <= Decimal("0") or abs(ask_price - mid_price) > max_deviation:
            ask_price = Decimal("0")
            ask_size = Decimal("0")

        # 🔥 核心修改 3：根據庫存方向與比例調整訂單大小
        base_size = self._base_size(mid_price)
        inventory_sensitivity = self._config.inventory_sensitivity
        if position_age_triggered and self._config.position_age_spread_multiplier > Decimal("0"):
            inventory_sensitivity *= Decimal("1") + (self._config.position_age_spread_multiplier / Decimal("2"))
        skew = direction * base_size * inventory_sensitivity * inventory_ratio

        # 持有多單時：減小買單、增大賣單
        # 持有空單時：增大買單、減小賣單
        bid_size = self._clip_size(base_size - skew)
        ask_size = self._clip_size(base_size + skew)

        # 🔥 新增：極端情況處理 - 如果庫存過大，完全取消同向訂單
        inventory_threshold_ratio = self._config.inventory_disable_same_side_threshold
        if inventory_threshold_ratio <= Decimal("0"):
            inventory_threshold_ratio = Decimal("1")
        if inventory_threshold_ratio > Decimal("1"):
            inventory_threshold_ratio = Decimal("1")

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

    def last_inventory_ratio(self) -> Decimal:
        """Return the last computed inventory ratio for adaptive logic."""

        return self._last_ratio

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

    def _sanitize_price(self, price: Decimal, tick: Decimal, *, is_bid: bool) -> Decimal:
        """Ensure price aligns to tick and remains positive."""

        if tick <= Decimal("0"):
            tick = Decimal("0.0001")

        if price <= Decimal("0"):
            price = tick

        if is_bid:
            units = (price / tick).to_integral_value(rounding=ROUND_DOWN)
        else:
            units = (price / tick).to_integral_value(rounding=ROUND_UP)

        sanitized = units * tick
        if sanitized <= Decimal("0"):
            sanitized = tick
        return sanitized

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
