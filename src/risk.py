"""Risk limits and circuit breaker logic."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from .schemas import CircuitBreakerState


@dataclass
class RiskConfig:
    max_net_position: Decimal
    max_order_size: Decimal
    max_open_orders: int


class RiskManager:
    """Evaluate inventory, order sizing, and simple circuit breakers."""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._open_orders = 0

    def update_limits(self, max_net_position: Decimal, max_order_size: Decimal) -> None:
        self._config.max_net_position = max_net_position
        self._config.max_order_size = max_order_size

    def can_place_order(self, net_position: Decimal, order_size: Decimal) -> bool:
        if self._config.max_net_position > Decimal("0") and abs(net_position) + order_size > self._config.max_net_position:
            return False
        if self._config.max_order_size > Decimal("0") and order_size > self._config.max_order_size:
            return False
        if self._open_orders >= self._config.max_open_orders:
            return False
        return True

    def register_order(self) -> None:
        self._open_orders += 1

    def register_cancel(self) -> None:
        if self._open_orders > 0:
            self._open_orders -= 1

    def circuit_breaker_due_to_latency(self, latency_seconds: float, threshold: float) -> CircuitBreakerState:
        triggered = latency_seconds > threshold
        reason = None
        if triggered:
            reason = f"latency {latency_seconds:.2f}s exceeds {threshold:.2f}s"
        return CircuitBreakerState(market="*", triggered=triggered, reason=reason, timestamp=_utc_now())

    def circuit_breaker_due_to_volatility(self, sigma: Decimal, sigma_limit: Decimal, market: str) -> CircuitBreakerState:
        triggered = sigma > sigma_limit
        reason: Optional[str] = None
        if triggered:
            reason = f"sigma {sigma} > limit {sigma_limit}"
        return CircuitBreakerState(market=market, triggered=triggered, reason=reason, timestamp=_utc_now())



def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
