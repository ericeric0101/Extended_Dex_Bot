from decimal import Decimal

from src.risk import RiskConfig, RiskManager


def test_risk_limits_block_excess_position():
    cfg = RiskConfig(
        max_net_position=Decimal("1"),
        max_order_size=Decimal("0.5"),
        max_open_orders=2,
    )
    risk = RiskManager(cfg)
    assert risk.can_place_order(Decimal("0.4"), Decimal("0.4"))
    assert not risk.can_place_order(Decimal("0.8"), Decimal("0.4"))


def test_risk_limits_open_order_tracking():
    cfg = RiskConfig(
        max_net_position=Decimal("5"),
        max_order_size=Decimal("1"),
        max_open_orders=1,
    )
    risk = RiskManager(cfg)
    assert risk.can_place_order(Decimal("0"), Decimal("0.5"))
    risk.register_order()
    assert not risk.can_place_order(Decimal("0"), Decimal("0.5"))
    risk.register_cancel()
    assert risk.can_place_order(Decimal("0"), Decimal("0.5"))
