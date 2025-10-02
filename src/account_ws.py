"""Account WebSocket consumer for private updates (Extended Starknet)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Tuple

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from .config import EndpointConfig, RuntimeSettings


class AccountStream:
    """
    Streams private account events (orders, trades, balances, positions).
    """

    def __init__(self, settings: RuntimeSettings, endpoints: EndpointConfig) -> None:
        if not settings.api_key:
            raise ValueError("EXTENDED_API_KEY is required for account stream")

        self._api_key = settings.api_key
        self._rest_base = str(endpoints.rest_base)

        # Per documentation, the private account stream uses ws://, not wss://
        self._url = "ws://api.starknet.extended.exchange/stream.extended.exchange/v1/account"

        self._headers: List[Tuple[str, str]] = [
            ("X-Api-Key", self._api_key),
            ("User-Agent", settings.user_agent),
            ("Origin", "https://app.extended.exchange"),
        ]

        subaccount_id = os.getenv("SUBACCOUNT_ID", "").strip()
        if hasattr(settings, "subaccount_id") and settings.subaccount_id:
            subaccount_id = str(settings.subaccount_id)
        if subaccount_id:
            self._headers.append(("X-Subaccount-Id", subaccount_id))

        self._debug_ws = os.getenv("EXTENDED_DEBUG_ACCOUNT_WS", "0") == "1"

    async def _fetch_fees(self) -> Dict[str, Any]:
        """Fetches the current maker/taker fee rates."""
        url = f"{self._rest_base}/user/fees"
        headers = {key: value for key, value in self._headers}
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json().get("data", [])
            return {
                row.get("market", "ALL"): {
                    "makerFeeRate": row.get("makerFeeRate"),
                    "takerFeeRate": row.get("takerFeeRate"),
                }
                for row in data
            }

    async def updates(self) -> AsyncIterator[Dict[str, Any]]:
        """Connects to the account WebSocket and yields events."""
        try:
            fees_map = await self._fetch_fees()
            yield {"type": "CONFIG", "data": {"fees": fees_map}}
        except Exception as e:
            logging.warning(f"[account_ws] Could not fetch fees: {e} (continuing without fee rates)")

        backoff = 0.5
        while True:
            try:
                if self._debug_ws:
                    logging.info(f"[account_ws] Connecting to {self._url}")

                async with websockets.connect(
                    self._url,
                    extra_headers=self._headers,
                    ping_interval=15,
                    ping_timeout=10,
                ) as ws:
                    logging.info(f"[account_ws] Connection successful to {self._url}")
                    backoff = 0.5  # Reset backoff on successful connection

                    async for raw in ws:
                        try:
                            yield json.loads(raw)
                        except json.JSONDecodeError:
                            yield {"type": "RAW", "data": raw}

            except (InvalidStatusCode, OSError, ConnectionClosed) as e:
                logging.error(f"[account_ws] Connection error: {e}. Retrying in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
            except Exception as e:
                logging.error(f"[account_ws] Unexpected error: {e}. Retrying in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
