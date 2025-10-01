"""Market data ingestion via WebSocket feeds."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Dict, Optional

import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosed

from .config import EndpointConfig, RuntimeSettings
from .orderbook import OrderBook
from .schemas import OrderbookLevel, OrderbookSnapshot


class MarketDataSource:
    """Handles streaming public market data."""

    def __init__(self, settings: RuntimeSettings, endpoints: EndpointConfig) -> None:
        self._settings = settings
        self._endpoints = endpoints
        self._headers = {
            "User-Agent": settings.user_agent,
        }
        if settings.api_key:
            self._headers["X-Api-Key"] = settings.api_key

    async def orderbook_snapshots(self, market: str) -> AsyncIterator[OrderbookSnapshot]:
        url = f"{self._endpoints.ws_base}/orderbooks/{market}"
        backoff = 0.5
        while True:
            try:
                async with websockets.connect(url, extra_headers=self._headers, ping_interval=20) as ws:
                    backoff = 0.5
                    async for message in self._listen(ws):
                        snapshot = self._parse_orderbook(market, message)
                        if snapshot:
                            yield snapshot
            except (OSError, ConnectionClosed):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)

    async def _listen(self, ws: WebSocketClientProtocol) -> AsyncIterator[Dict]:
        while True:
            raw = await ws.recv()
            yield json.loads(raw)

    def _parse_orderbook(self, market: str, payload: Dict) -> Optional[OrderbookSnapshot]:
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            return None

        book_section = data.get("orderbook", data)
        if isinstance(book_section, dict):
            bids = book_section.get("bids") or book_section.get("buy")
            asks = book_section.get("asks") or book_section.get("sell")
        else:
            bids = data.get("bids") or data.get("buy")
            asks = data.get("asks") or data.get("sell")

        if not bids or not asks:
            return None
        def _convert(level: Dict | list | tuple) -> OrderbookLevel:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price, size = level[0], level[1]
            elif isinstance(level, dict):
                price = level.get("price")
                size = level.get("size") or level.get("quantity")
            else:
                raise ValueError("unknown level format")
            return OrderbookLevel(price=Decimal(str(price)), size=Decimal(str(size)))

        bid_levels = [_convert(level) for level in bids]
        ask_levels = [_convert(level) for level in asks]
        timestamp = (
            data.get("timestamp")
            or payload.get("timestamp")
            or payload.get("ts")
            or data.get("ts")
            or datetime.now(timezone.utc).isoformat()
        )
        ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")) if isinstance(timestamp, str) else datetime.now(timezone.utc)
        return OrderbookSnapshot(market=market, bids=bid_levels, asks=ask_levels, timestamp=ts)


async def stream_orderbook_to_local_book(book: OrderBook, source: MarketDataSource) -> None:
    """Populate a local OrderBook from the public feed."""

    async for snapshot in source.orderbook_snapshots(book.market):
        book.ingest_snapshot(snapshot)
