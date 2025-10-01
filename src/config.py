"""Runtime configuration for the extended market making bot."""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Optional

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, AnyUrl


class EndpointConfig(BaseModel):
    rest_base: AnyUrl
    ws_base: AnyUrl


class RuntimeSettings(BaseModel):
    environment: str = Field(default="testnet", pattern="^(testnet|mainnet)$")
    api_key: str | None = Field(default=None, alias="EXTENDED_API_KEY")
    public_key: str | None = Field(default=None, alias="EXTENDED_PUBLIC_KEY")
    private_key: str | None = Field(default=None, alias="EXTENDED_PRIVATE_KEY")
    vault_id: int | None = Field(default=None, alias="EXTENDED_VAULT_ID")
    user_agent: str = Field(default="extended-mm-bot/0.1", alias="USER_AGENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    dead_mans_switch_sec: int = Field(default=120, alias="DEAD_MANS_SWITCH_SEC")

    model_config = {
        "populate_by_name": True,
        "extra": "ignore",
    }


class MarketConfig(BaseModel):
    name: str
    leverage: int = 5
    K: float = 0.0005
    alpha: float = 0.5
    beta: float = 0.25
    base_spread: float = 0.001
    quote_notional_cap_usd: float = 50.0
    replace_threshold_bps: float = 2.0
    min_order_size: float = 0.001
    post_only: bool = True
    enabled: bool = True


class RiskSettings(BaseModel):
    max_net_position_usd: float = 200.0
    max_open_orders: int = 30
    min_balance_usd: float = 50.0


class FeesOverride(BaseModel):
    makerFeeRate: Optional[str] = None
    takerFeeRate: Optional[str] = None


class BotConfig(BaseModel):
    stp: str = "ACCOUNT"
    quote_loop_ms: int = 250
    replace_coalesce_ms: int = 400
    dead_mans_switch_sec: int = 120
    risk: RiskSettings = RiskSettings()
    markets: List[MarketConfig] = Field(default_factory=list)
    fees_override: FeesOverride = FeesOverride()


BOT_CONFIG_PATH = Path("config.json")


def load_bot_config() -> BotConfig:
    if not BOT_CONFIG_PATH.exists():
        return BotConfig(markets=[MarketConfig(name="BTC-USD")])
    data = json.loads(BOT_CONFIG_PATH.read_text())
    return BotConfig(**data)



ENDPOINTS: Dict[str, EndpointConfig] = {
    "testnet": EndpointConfig(
        rest_base="https://api.starknet.sepolia.extended.exchange/api/v1",
        ws_base="wss://starknet.sepolia.extended.exchange/stream.extended.exchange/v1",
    ),
    "mainnet": EndpointConfig(
        rest_base="https://api.starknet.extended.exchange/api/v1",
        ws_base="wss://api.starknet.extended.exchange/stream.extended.exchange/v1",
    ),
}


def _load_environment() -> RuntimeSettings:
    load_dotenv(override=False)
    data = {key: os.getenv(key) for key in os.environ.keys() if key.startswith("EXTENDED_")}
    data["USER_AGENT"] = os.getenv("USER_AGENT", "extended-mm-bot/0.1")
    data["LOG_LEVEL"] = os.getenv("LOG_LEVEL", "INFO")
    data["DEAD_MANS_SWITCH_SEC"] = os.getenv("DEAD_MANS_SWITCH_SEC", "120")
    environment = os.getenv("EXTENDED_ENV", "testnet")
    return RuntimeSettings(environment=environment, **data)


@lru_cache(maxsize=1)
def get_settings() -> RuntimeSettings:
    """Return cached runtime settings."""

    return _load_environment()


def get_endpoints() -> EndpointConfig:
    """Return API endpoints for the configured environment."""

    settings = get_settings()
    if settings.environment not in ENDPOINTS:
        raise ValueError(f"unsupported EXTENDED_ENV: {settings.environment}")
    return ENDPOINTS[settings.environment]
