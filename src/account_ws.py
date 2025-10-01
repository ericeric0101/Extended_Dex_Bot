"""Account WebSocket consumer for private updates (Extended Starknet)."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Dict, List, Tuple

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from x10.utils import http as http_utils  # 只沿用你的 UA 管理
from .config import EndpointConfig, RuntimeSettings


class AccountStream:
    """
    Streams private account events (orders, trades, balances, positions) and emits a CONFIG
    event with live fee rates and preferred self-trade protection level at startup.
    Also supports several auth/host patterns to avoid 401 during WS handshake.
    """

    def __init__(self, settings: RuntimeSettings, endpoints: EndpointConfig) -> None:
        if not settings.api_key:
            raise ValueError("EXTENDED_API_KEY is required for account stream")

        self._api_key = settings.api_key
        http_utils.USER_AGENT = settings.user_agent

        # REST base e.g. (testnet): https://api.starknet.sepolia.extended.exchange/api/v1
        self._rest_base = str(endpoints.rest_base)

        # Primary WS base from config (通常是無 api. 的 host)
        primary_ws_base = str(endpoints.ws_base)  # e.g. wss://starknet.sepolia.extended.exchange/stream.extended.exchange/v1

        # 有些私有 WS 需要 api. 子網域：嘗試備援 host
        # 將 host 部分插入 api. 作為備援，例如：
        #   wss://api.starknet.sepolia.extended.exchange/stream.extended.exchange/v1
        # 若你的 endpoints 物件已有「明確的」private_ws_base，可直接改用它。
        if primary_ws_base.startswith("wss://"):
            after_scheme = primary_ws_base[len("wss://") :]
            if not after_scheme.startswith("api."):
                backup_ws_base = "wss://api." + after_scheme
            else:
                backup_ws_base = primary_ws_base
        else:
            backup_ws_base = primary_ws_base

        # 嘗試順序：先 primary，再 backup（不同 host）
        self._candidate_ws_urls: List[str] = [
            f"{primary_ws_base}/account",
            f"{backup_ws_base}/account",
        ]

        # 必備握手標頭
        base_headers: List[Tuple[str, str]] = [
            ("X-Api-Key", self._api_key),
            ("User-Agent", http_utils.USER_AGENT),
            # 某些後端檢查 Origin；帶上官方網域（若有 Web 客戶端）
            ("Origin", "https://app.extended.exchange"),
        ]

        # 子帳號（若需要）
        subaccount_id = os.getenv("SUBACCOUNT_ID", "").strip()
        if hasattr(settings, "subaccount_id") and settings.subaccount_id:
            subaccount_id = settings.subaccount_id
        if subaccount_id:
            base_headers.append(("X-Subaccount-Id", subaccount_id))

        self._base_headers = base_headers

        # WS 短期 token（若需要）
        # WS_AUTH_MODE: 'query' 或 'subprotocol'
        self._ws_auth_token = os.getenv("WS_AUTH_TOKEN", "").strip()
        self._ws_auth_mode = os.getenv("WS_AUTH_MODE", "query").strip().lower()

        # STP 預設
        self._stp_level = getattr(settings, "stp_level", "ACCOUNT").upper()

    async def _fetch_fees(self) -> Dict[str, Any]:
        """抓取目前 maker/taker 費率。"""
        url = f"{self._rest_base}/user/fees"
        params = {}
        timeout = httpx.Timeout(10.0, read=10.0, connect=10.0)
        headers = {
            "X-Api-Key": self._api_key,
            "User-Agent": http_utils.USER_AGENT,
        }
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json().get("data", [])
            mapped = {}
            for row in data:
                market = row.get("market", "ALL")
                mapped[market] = {
                    "makerFeeRate": row.get("makerFeeRate"),
                    "takerFeeRate": row.get("takerFeeRate"),
                    "builderFeeRate": row.get("builderFeeRate"),
                }
            return mapped

    def _build_ws_target(self, base_url: str) -> Dict[str, Any]:
        """
        依照 WS_AUTH_MODE 組裝最終 URL / headers / subprotocols。
        支援：
          - query 模式：wss://.../account?token=...
          - subprotocol 模式：Sec-WebSocket-Protocol: bearer, <token>
        """
        url = base_url
        headers = list(self._base_headers)
        subprotocols: List[str] = []

        token = self._ws_auth_token
        mode = self._ws_auth_mode

        if token:
            if mode == "query":
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}token={token}"
            elif mode == "subprotocol":
                # 常見：指定自定義 subprotocol（某些服務採用 'bearer' 或 'token'）
                # 這裡用兩個：'bearer' 與實際 token，伺服器會取需要的那個
                subprotocols = ["bearer", token]
            else:
                # 未知模式就忽略，僅用 Header + Key
                pass

        return {"url": url, "headers": headers, "subprotocols": subprotocols}

    async def updates(self) -> AsyncIterator[Dict[str, Any]]:
        """連接帳戶 WS；先 emit CONFIG（費率/STP），再持續輸出帳戶事件。"""
        # 先同步費率
        fees_map: Dict[str, Any] = {}
        try:
            fees_map = await self._fetch_fees()
            yield {"type": "CONFIG", "data": {"fees": fees_map, "stp": self._stp_level}}
        except Exception as e:
            print(f"[account_ws] WARN: fetch fees failed: {e} (continue without CONFIG fees)")

        backoff = 0.5
        while True:
            # 逐一嘗試候選 WS URL（不同 host）
            for ws_base_url in self._candidate_ws_urls:
                target = self._build_ws_target(ws_base_url)
                url = target["url"]
                headers = target["headers"]
                subprotocols = target["subprotocols"]

                try:
                    if self._debug_ws:
                        sanitized_headers = {
                            "User-Agent": dict(headers).get("User-Agent"),
                            "X-Api-Key": f"{dict(headers).get('X-Api-Key')[:6]}..." if dict(headers).get("X-Api-Key") else None,
                            "X-Subaccount-Id": dict(headers).get("X-Subaccount-Id", None),
                            "Origin": dict(headers).get("Origin"),
                            "WS_AUTH_MODE": self._ws_auth_mode,
                            "WS_AUTH_TOKEN?": bool(self._ws_auth_token),
                        }
                        print(
                            f"[account_ws] connecting to {url} headers={sanitized_headers} subprotocols={subprotocols}"
                        )

                    async with websockets.connect(
                        url,
                        extra_headers=headers,
                        subprotocols=subprotocols if subprotocols else None,
                        ping_interval=15,
                        ping_timeout=10,
                        max_queue=None,
                    ) as ws:
                        backoff = 0.5

                        # 連上了，再次（可選）廣播 CONFIG
                        if fees_map:
                            yield {"type": "CONFIG", "data": {"fees": fees_map, "stp": self._stp_level}}

                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                msg = {"type": "RAW", "data": raw}
                            yield msg

                except InvalidStatusCode as e:
                    if self._debug_ws:
                        try:
                            headers_dbg = getattr(e, "headers", None)
                            print(f"[account_ws] HTTP {e.status_code} response headers: {headers_dbg}")
                        except Exception:
                            pass
                    if e.status_code == 401:
                        # 401 常見原因提示
                        # print(
                        #     "[account_ws] 401 tips: "
                        #     "1) 檢查 API Key 是否為該環境(Testnet/Mainnet)與子帳號；"
                        #     "2) 嘗試設置 SUBACCOUNT_ID；"
                        #     "3) 若文件要求 WS token，請在 .env 設定 WS_AUTH_TOKEN 與 WS_AUTH_MODE=query|subprotocol；"
                        #     "4) 若此 host 不通會自動嘗試下一個候選 host。"
                        # )
                        # 換下一個候選 URL
                        continue
                    else:
                        # 非 401，直接換下一個候選 URL
                        continue

                except (OSError, ConnectionClosed) as e:
                    # print(f"[account_ws] disconnect: {e} (host={ws_base_url}); will rotate/Retry after {backoff:.1f}s")
                    # 換下一個候選 URL
                    continue

            # 所有候選都失敗：整體退避
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 8.0)
        self._debug_ws = os.getenv("EXTENDED_DEBUG_ACCOUNT_WS", "0") == "1"
