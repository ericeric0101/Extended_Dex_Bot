"""PnL decomposition utilities."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict


@dataclass
class PnLBreakdown:
    spread_pnl: Decimal
    inventory_pnl: Decimal
    fees: Decimal
    funding: Decimal


class PnLTracker:
    """Track realised PnL components over the session."""

    def __init__(self) -> None:
        self._data = PnLBreakdown(
            spread_pnl=Decimal("0"),
            inventory_pnl=Decimal("0"),
            fees=Decimal("0"),
            funding=Decimal("0"),
        )

    def record_fill(self, price: Decimal, size: Decimal, side: str, mid_at_fill: Decimal) -> None:
        direction = Decimal("1") if side.upper() == "SELL" else Decimal("-1")
        self._data.spread_pnl += (price - mid_at_fill) * size * direction

    def record_fee(self, fee: Decimal) -> None:
        self._data.fees += fee

    def record_funding(self, funding: Decimal) -> None:
        self._data.funding += funding

    def mark_to_market(self, inventory: Decimal, current_mid: Decimal, entry_price: Decimal) -> None:
        self._data.inventory_pnl = (current_mid - entry_price) * inventory

    def snapshot(self) -> Dict[str, Decimal]:
        return {
            "spread_pnl": self._data.spread_pnl,
            "inventory_pnl": self._data.inventory_pnl,
            "fees": self._data.fees,
            "funding": self._data.funding,
            "net_pnl": self._data.spread_pnl + self._data.inventory_pnl + self._data.fees + self._data.funding,
        }
