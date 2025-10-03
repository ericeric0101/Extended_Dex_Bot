"""Local order book reconstruction and statistics."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Deque, Dict, Iterable, Optional

from .schemas import OrderbookLevel, OrderbookSnapshot


@dataclass
class BestBidAsk:
    bid: Optional[OrderbookLevel]
    ask: Optional[OrderbookLevel]


class OrderBook:
    """Maintains a lightweight local view of the book for quoting decisions."""

    def __init__(self, market: str, max_depth: int = 25, sigma_window: int = 120) -> None:
        self.market = market
        self.max_depth = max_depth
        self.sigma_window = sigma_window
        self._bids: Dict[Decimal, Decimal] = {}
        self._asks: Dict[Decimal, Decimal] = {}
        self._mid_history: Deque[tuple[datetime, Decimal]] = deque(maxlen=sigma_window)

    def ingest_snapshot(self, snapshot: OrderbookSnapshot) -> None:
        if snapshot.market != self.market:
            return
        self._bids = {level.price: level.size for level in snapshot.bids[: self.max_depth]}
        self._asks = {level.price: level.size for level in snapshot.asks[: self.max_depth]}
        self._record_mid(snapshot.timestamp)

    def apply_levels(self, side: str, levels: Iterable[OrderbookLevel], timestamp: datetime) -> None:
        book = self._bids if side.lower() == "bid" else self._asks
        for level in levels:
            if level.size == 0:
                book.pop(level.price, None)
            else:
                book[level.price] = level.size
        self._truncate_book(book)
        self._record_mid(timestamp)

    def best_prices(self) -> BestBidAsk:
        bid = max(self._bids.items(), default=None)
        ask = min(self._asks.items(), default=None)
        best_bid = OrderbookLevel(price=bid[0], size=bid[1]) if bid else None
        best_ask = OrderbookLevel(price=ask[0], size=ask[1]) if ask else None
        return BestBidAsk(bid=best_bid, ask=best_ask)

    def mid_price(self) -> Optional[Decimal]:
        best = self.best_prices()
        if not best.bid or not best.ask:
            return None
        return (best.bid.price + best.ask.price) / Decimal("2")

    def _truncate_book(self, book: Dict[Decimal, Decimal]) -> None:
        if len(book) <= self.max_depth:
            return
        sorted_levels = sorted(book.items(), reverse=book is self._bids)
        trimmed = dict(sorted_levels[: self.max_depth])
        book.clear()
        book.update(trimmed)

    def _record_mid(self, timestamp: datetime) -> None:
        mid = self.mid_price()
        if mid is None:
            return
        self._mid_history.append((timestamp, mid))
        self._drop_stale(timestamp)

    def _drop_stale(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.sigma_window)
        while self._mid_history and self._mid_history[0][0] < cutoff:
            self._mid_history.popleft()

    def sigma(self) -> Optional[Decimal]:
        if len(self._mid_history) < 2:
            return None
        mids = [value for _, value in self._mid_history]
        returns: list[Decimal] = []
        for previous, current in zip(mids[:-1], mids[1:]):
            if previous == 0:
                continue
            returns.append((current - previous) / previous)
        if not returns:
            return None
        mean = sum(returns) / Decimal(len(returns))
        var = sum((r - mean) ** 2 for r in returns) / Decimal(len(returns))
        std = var.sqrt() if hasattr(var, "sqrt") else Decimal(var ** 0.5)

        # 添加：將標準差限制在合理範圍
        return min(std, Decimal("0.01"))
