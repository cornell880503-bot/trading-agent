"""Configuration loading.

Secrets come from the environment only. Limits come from ``config.yaml`` and
are checked into the repo on purpose -- a risk limit you cannot diff in code
review is not a risk limit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import ConfigError
from .okx.auth import Credentials
from .okx.rest import LIVE_BASE

DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass(frozen=True)
class RiskLimits:
    """Hard ceilings the execution layer enforces. The analysis layer cannot see
    or modify these -- that asymmetry is the entire point."""

    symbol_whitelist: tuple[str, ...] = ("BTC-USDT", "ETH-USDT")
    quote_ccy: str = "USDT"
    max_notional_per_trade: float = 500.0
    max_account_fraction_per_trade: float = 0.20
    max_risk_pct_per_trade: float = 1.5
    max_open_plans: int = 3
    max_daily_loss_quote: float = 150.0
    min_risk_reward: float = 1.5
    max_plan_age_seconds: int = 3600
    max_slippage_pct: float = 0.5

    @classmethod
    def from_dict(cls, payload: dict) -> "RiskLimits":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(payload) - known
        if unknown:
            # A typo'd limit key silently falling back to a default is exactly
            # the kind of quiet failure that costs money.
            raise ConfigError(f"unknown risk limit(s): {sorted(unknown)}")
        data = dict(payload)
        if "symbol_whitelist" in data:
            data["symbol_whitelist"] = tuple(str(s).upper() for s in data["symbol_whitelist"])
        return cls(**data)


@dataclass
class Config:
    credentials: Credentials | None = None
    risk: RiskLimits = field(default_factory=RiskLimits)
    base_url: str = LIVE_BASE
    db_path: str = "okxbot.sqlite3"
    timeframes: tuple[str, ...] = ("1H", "4H", "1D")
    candle_limit: int = 300
    plans_dir: str = "plans"
    read_only: bool = False

    @property
    def simulated(self) -> bool:
        return bool(self.credentials and self.credentials.simulated)

    @property
    def environment_label(self) -> str:
        env = "demo (paper)" if self.simulated else "LIVE"
        return f"{env}, read-only" if self.read_only else env


def load_credentials(require: bool = True) -> Credentials | None:
    key = os.environ.get("OKX_API_KEY", "").strip()
    secret = os.environ.get("OKX_API_SECRET", "").strip()
    passphrase = os.environ.get("OKX_PASSPHRASE", "").strip()

    if not (key and secret and passphrase):
        if require:
            missing = [
                name
                for name, value in (
                    ("OKX_API_KEY", key),
                    ("OKX_API_SECRET", secret),
                    ("OKX_PASSPHRASE", passphrase),
                )
                if not value
            ]
            raise ConfigError(f"missing environment variable(s): {', '.join(missing)}")
        return None

    # Live trading is opt-in and must be spelled out in full. Anything else,
    # including an unset variable or a stray "0", stays on the demo endpoint.
    simulated = os.environ.get("OKX_LIVE_TRADING", "").strip().lower() != "i-understand-the-risk"
    return Credentials(api_key=key, api_secret=secret, passphrase=passphrase, simulated=simulated)


def load_config(path: str | os.PathLike | None = None, require_credentials: bool = True) -> Config:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    payload: dict = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ConfigError(f"{config_path}: expected a YAML mapping at the top level")
            payload = loaded
    elif path is not None:
        raise ConfigError(f"config file not found: {config_path}")

    risk = RiskLimits.from_dict(payload.get("risk", {}) or {})
    creds = load_credentials(require=require_credentials)

    # Orthogonal to OKX_LIVE_TRADING on purpose: "point at the live account but
    # refuse every write" is a legitimate thing to want, and before this flag
    # existed the only way to reach live also armed trading.
    read_only = os.environ.get("OKX_READ_ONLY", "").strip().lower() in ("1", "true", "yes", "on")

    return Config(
        credentials=creds,
        risk=risk,
        read_only=read_only,
        base_url=str(payload.get("base_url", LIVE_BASE)),
        db_path=str(payload.get("db_path", "okxbot.sqlite3")),
        timeframes=tuple(payload.get("timeframes", ("1H", "4H", "1D"))),
        candle_limit=int(payload.get("candle_limit", 300)),
        plans_dir=str(payload.get("plans_dir", "plans")),
    )
