from datetime import datetime, timezone
from decimal import Decimal

from src.orderbook import OrderBook
from src.schemas import OrderbookLevel, OrderbookSnapshot


def make_snapshot(mid: Decimal) -> OrderbookSnapshot:
    bids = [OrderbookLevel(price=mid - Decimal("10"), size=Decimal("1"))]
    asks = [OrderbookLevel(price=mid + Decimal("10"), size=Decimal("1"))]
    return OrderbookSnapshot(market="BTC-USD", bids=bids, asks=asks, timestamp=datetime.now(timezone.utc))


def test_orderbook_mid_and_sigma():
    book = OrderBook(market="BTC-USD")
    mids = [Decimal("63000"), Decimal("63020"), Decimal("63010"), Decimal("63050")]
    for mid in mids:
        book.ingest_snapshot(make_snapshot(mid))
    mid_price = book.mid_price()
    assert mid_price == mids[-1]
    sigma = book.sigma()
    assert sigma is not None
    assert sigma >= Decimal("0")
