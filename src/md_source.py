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
        bids = payload.get("bids") or payload.get("buy" )
        asks = payload.get("asks") or payload.get("sell")
        if not bids or not asks:
            return None
        bid_levels = [OrderbookLevel(price=Decimal(level[0]), size=Decimal(level[1])) for level in bids]
        ask_levels = [OrderbookLevel(price=Decimal(level[0]), size=Decimal(level[1])) for level in asks]
        timestamp = payload.get("timestamp") or payload.get("ts") or datetime.now(timezone.utc).isoformat()
        ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")) if isinstance(timestamp, str) else datetime.now(timezone.utc)
        return OrderbookSnapshot(market=market, bids=bid_levels, asks=ask_levels, timestamp=ts)


async def stream_orderbook_to_local_book(book: OrderBook, source: MarketDataSource) -> None:
    """Populate a local OrderBook from the public feed."""

    async for snapshot in source.orderbook_snapshots(book.market):
        book.ingest_snapshot(snapshot)
