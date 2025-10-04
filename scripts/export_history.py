"""Export private trading history via REST API."""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import httpx

from src.config import get_endpoints, get_settings
from src.rest import RestClient


@dataclass
class TradeRecord:
    trade_id: str
    market: str
    side: str
    qty: Decimal
    price: Decimal
    value: Decimal
    fee: Decimal
    is_taker: bool
    trade_type: str
    created_time: int

    def to_row(self) -> List[str]:
        return [
            self.trade_id,
            self.market,
            self.side,
            format(self.qty, "f"),
            format(self.price, "f"),
            format(self.value, "f"),
            format(self.fee, "f"),
            "TAKER" if self.is_taker else "MAKER",
            self.trade_type,
            ms_to_iso(self.created_time),
        ]


@dataclass
class PositionRecord:
    position_id: str
    market: str
    side: str
    size: Decimal
    open_price: Decimal
    exit_price: Optional[Decimal]
    realised_pnl: Decimal
    created_time: int
    closed_time: Optional[int]

    def to_row(self) -> List[str]:
        return [
            self.position_id,
            self.market,
            self.side,
            format(self.size, "f"),
            format(self.open_price, "f"),
            format(self.exit_price, "f") if self.exit_price is not None else "",
            format(self.realised_pnl, "f"),
            ms_to_iso(self.created_time),
            ms_to_iso(self.closed_time) if self.closed_time is not None else "",
        ]


@dataclass
class FundingRecord:
    record_id: str
    market: str
    side: str
    size: Decimal
    value: Decimal
    funding_fee: Decimal
    funding_rate: Decimal
    paid_time: int

    def to_row(self) -> List[str]:
        return [
            self.record_id,
            self.market,
            self.side,
            format(self.size, "f"),
            format(self.value, "f"),
            format(self.funding_fee, "f"),
            format(self.funding_rate, "f"),
            ms_to_iso(self.paid_time),
        ]


def ms_to_iso(timestamp_ms: Optional[int]) -> str:
    if not timestamp_ms:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


async def fetch_paginated(
    client: RestClient,
    path: str,
    params: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    query = dict(params or {})
    while True:
        response = await client.get(path, params=query)
        items = response.get("data") or []
        for item in items:
            yield item
        pagination = response.get("pagination") or {}
        cursor = pagination.get("cursor")
        count = pagination.get("count")
        if not cursor or not count or not items:
            break
        query = dict(query)
        if query.get("cursor") == cursor:
            break
        query["cursor"] = cursor


async def collect_trades(client: RestClient, market: Optional[str]) -> List[TradeRecord]:
    params: Dict[str, Any] = {"limit": 200}
    if market:
        params["market"] = market

    records: List[TradeRecord] = []
    async for trade in fetch_paginated(client, "/user/trades", params=params):
        records.append(
            TradeRecord(
                trade_id=str(trade.get("id")),
                market=str(trade.get("market")),
                side=str(trade.get("side")),
                qty=Decimal(str(trade.get("qty") or trade.get("filledQty") or "0")),
                price=Decimal(str(trade.get("price") or trade.get("averagePrice") or "0")),
                value=Decimal(str(trade.get("value") or "0")),
                fee=Decimal(str(trade.get("fee") or "0")),
                is_taker=bool(trade.get("isTaker", False)),
                trade_type=str(trade.get("tradeType")),
                created_time=int(trade.get("createdTime") or 0),
            )
        )
    return records


async def collect_positions_history(client: RestClient, market: Optional[str]) -> List[PositionRecord]:
    params: Dict[str, Any] = {"limit": 200}
    if market:
        params["market"] = market

    records: List[PositionRecord] = []
    async for position in fetch_paginated(client, "/user/positions/history", params=params):
        exit_price_raw = position.get("exitPrice")
        closed_time_raw = position.get("closedTime")
        records.append(
            PositionRecord(
                position_id=str(position.get("id")),
                market=str(position.get("market")),
                side=str(position.get("side")),
                size=Decimal(str(position.get("size") or "0")),
                open_price=Decimal(str(position.get("openPrice") or "0")),
                exit_price=Decimal(str(exit_price_raw)) if exit_price_raw is not None else None,
                realised_pnl=Decimal(str(position.get("realisedPnl") or "0")),
                created_time=int(position.get("createdTime") or 0),
                closed_time=int(closed_time_raw) if closed_time_raw is not None else None,
            )
        )
    return records


async def collect_funding_history(
    client: RestClient,
    market: Optional[str],
    from_time: int,
) -> List[FundingRecord]:
    params: Dict[str, Any] = {"limit": 200, "fromTime": from_time}
    if market:
        params["market"] = market

    records: List[FundingRecord] = []
    async for funding in fetch_paginated(client, "/user/funding/history", params=params):
        records.append(
            FundingRecord(
                record_id=str(funding.get("id")),
                market=str(funding.get("market")),
                side=str(funding.get("side")),
                size=Decimal(str(funding.get("size") or "0")),
                value=Decimal(str(funding.get("value") or "0")),
                funding_fee=Decimal(str(funding.get("fundingFee") or "0")),
                funding_rate=Decimal(str(funding.get("fundingRate") or "0")),
                paid_time=int(funding.get("paidTime") or 0),
            )
        )
    return records


def write_csv(path: Path, headers: Iterable[str], rows: Iterable[List[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(list(headers))
        for row in rows:
            writer.writerow(row)


async def async_main(args: argparse.Namespace) -> None:
    settings = get_settings()
    endpoints = get_endpoints()
    client = RestClient(settings, endpoints)

    try:
        trades = await collect_trades(client, args.market)
        try:
            positions = await collect_positions_history(client, args.market)
        except httpx.HTTPStatusError as exc:
            print(
                f"⚠️ positions history API failed ({exc.response.status_code}): {exc.response.url}"
                " — skipping positions.csv"
            )
            positions = []
        funding = await collect_funding_history(client, args.market, args.funding_from)
    finally:
        await client.aclose()

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    write_csv(
        output_dir / "trades.csv",
        ["trade_id", "market", "side", "qty", "price", "value", "fee", "liquidity", "trade_type", "created_at"],
        (record.to_row() for record in trades),
    )

    write_csv(
        output_dir / "positions.csv",
        ["position_id", "market", "side", "size", "open_price", "exit_price", "realised_pnl", "created_at", "closed_at"],
        (record.to_row() for record in positions),
    )

    write_csv(
        output_dir / "funding.csv",
        ["funding_id", "market", "side", "size", "value", "funding_fee", "funding_rate", "paid_time"],
        (record.to_row() for record in funding),
    )

    print(
        f"Exported {len(trades)} trades, {len(positions)} positions, {len(funding)} funding records to {output_dir}"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export account history via Extended private REST API")
    parser.add_argument("--market", help="Filter by market symbol (e.g. ETH-USD)", default=None)
    parser.add_argument(
        "--funding-from",
        type=int,
        default=0,
        dest="funding_from",
        help="Starting timestamp in ms for funding history (default: 0)",
    )
    parser.add_argument(
        "--output",
        default="history_export",
        help="Output directory for CSV files (default: history_export)",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
