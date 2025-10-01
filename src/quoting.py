"""Simplified Avellaneda-Stoikov style quoting engine."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
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
    inventory_sensitivity: Decimal = Decimal("0.1")


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
        fair_price = mid_price + inventory * self._config.k
        sigma_term = sigma if sigma is not None else Decimal("0")
        funding_term = funding_rate if funding_rate is not None else Decimal("0")
        half_spread = (
            self._config.base_spread
            + self._config.alpha * sigma_term
            + self._config.beta * (funding_term / Decimal("3"))
        )
        bid_price = fair_price * (Decimal("1") - half_spread)
        ask_price = fair_price * (Decimal("1") + half_spread)

        base_size = self._base_size(mid_price)
        skew = inventory * self._config.inventory_sensitivity
        bid_size = self._clip_size(base_size - skew)
        ask_size = self._clip_size(base_size + skew)

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
        if size < self._config.min_order_size:
            return Decimal("0")
        if self._config.max_order_size is not None:
            return min(size, self._config.max_order_size)
        return size
