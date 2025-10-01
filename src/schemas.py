"""Pydantic schemas for public and private payloads."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field


class OrderbookLevel(BaseModel):
    price: Decimal
    size: Decimal


class OrderbookSnapshot(BaseModel):
    market: str
    bids: List[OrderbookLevel]
    asks: List[OrderbookLevel]
    timestamp: datetime

    def best_bid(self) -> Optional[OrderbookLevel]:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> Optional[OrderbookLevel]:
        return self.asks[0] if self.asks else None


class TickerStats(BaseModel):
    market: str
    mark_price: Decimal
    index_price: Decimal
    funding_rate: Decimal | None = None
    asof: datetime


class AccountBalance(BaseModel):
    equity: Decimal
    available: Decimal
    timestamp: datetime


class Position(BaseModel):
    market: str
    side: str
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealised_pnl: Decimal
    realised_pnl: Decimal
    leverage: Decimal
    updated_at: datetime


class OrderModel(BaseModel):
    id: int
    market: str
    side: str
    price: Decimal
    size: Decimal
    status: str
    created_time: datetime
    updated_time: datetime
    post_only: bool = False
    reduce_only: bool = False


class TradeModel(BaseModel):
    id: int
    order_id: int
    market: str
    side: str
    price: Decimal
    size: Decimal
    fee: Decimal
    liquidity: str
    created_time: datetime


class FundingUpdate(BaseModel):
    market: str
    next_rate: Decimal
    eta_seconds: int
    timestamp: datetime


class QuoteDecision(BaseModel):
    market: str
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal
    fair_price: Decimal
    half_spread: Decimal
    sigma: Decimal
    inventory: Decimal


class RiskLimits(BaseModel):
    max_net_position: Decimal
    max_order_size: Decimal
    max_open_orders: int


class CircuitBreakerState(BaseModel):
    market: str
    triggered: bool
    reason: Optional[str] = Field(default=None)
    timestamp: datetime
