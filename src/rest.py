"""Async REST client backed by httpx with retry/backoff."""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from tenacity import AsyncRetrying, RetryError, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import EndpointConfig, RuntimeSettings


class RateLimitError(Exception):
    """Raised when the API responds with HTTP 429."""


class RestClient:
    """Thin wrapper around httpx.AsyncClient with retry logic."""

    def __init__(self, settings: RuntimeSettings, endpoints: EndpointConfig) -> None:
        headers = {
            "User-Agent": settings.user_agent,
        }
        if settings.api_key:
            headers["X-Api-Key"] = settings.api_key

        timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
        self._client = httpx.AsyncClient(base_url=str(endpoints.rest_base), headers=headers, timeout=timeout)

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, RateLimitError)),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                response = await self._client.request(method=method, url=path, **kwargs)
                if response.status_code == 429:
                    raise RateLimitError("rate limited")
                response.raise_for_status()
                return response
        raise RetryError("unreachable")

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = await self._send("GET", path, params=params)
        return response.json()

    async def post(
        self, path: str, json: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        response = await self._send("POST", path, json=json, params=params)
        return response.json()

    async def delete(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = await self._send("DELETE", path, params=params)
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
