"""Configuration loaded from environment / .env. Secrets never hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _float(val: str | None, default: float) -> float:
    if val is None or val.strip() == "":
        return default
    return float(val)


def _int(val: str | None, default: int) -> int:
    if val is None or val.strip() == "":
        return default
    return int(val)


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings. Spot-only; no leverage/futures knobs."""

    api_key: str
    api_secret: str
    dry_run: bool
    testnet: bool
    symbol: str
    max_position_usd: float
    max_daily_loss_usd: float
    stop_loss_pct: float
    lookback_candles: int
    entry_zscore: float
    take_profit_pct: float
    cooldown_seconds: int
    fee_rate: float
    soft_daily_target_usd: float
    poll_interval_seconds: int
    state_path: Path

    def validate(self) -> None:
        if self.max_position_usd <= 0:
            raise ValueError("MAX_POSITION_USD must be > 0")
        if self.max_daily_loss_usd <= 0:
            raise ValueError("MAX_DAILY_LOSS_USD must be > 0")
        if not (0 < self.stop_loss_pct < 1):
            raise ValueError("STOP_LOSS_PCT must be between 0 and 1 (e.g. 0.01)")
        if self.lookback_candles < 5:
            raise ValueError("LOOKBACK_CANDLES must be >= 5")
        if not self.dry_run and (not self.api_key or not self.api_secret):
            raise ValueError(
                "BINANCE_API_KEY and BINANCE_API_SECRET required when DRY_RUN=false"
            )


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load settings from .env then process environment."""
    if env_file:
        load_dotenv(env_file)
    else:
        # Prefer project root .env if present
        cwd_env = Path.cwd() / ".env"
        if cwd_env.exists():
            load_dotenv(cwd_env)
        else:
            load_dotenv()

    settings = Settings(
        api_key=os.getenv("BINANCE_API_KEY", "").strip(),
        api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
        dry_run=_bool(os.getenv("DRY_RUN"), default=True),
        testnet=_bool(os.getenv("BINANCE_TESTNET"), default=False),
        symbol=os.getenv("SYMBOL", "BTCUSDT").strip().upper(),
        max_position_usd=_float(os.getenv("MAX_POSITION_USD"), 50.0),
        max_daily_loss_usd=_float(os.getenv("MAX_DAILY_LOSS_USD"), 2.0),
        stop_loss_pct=_float(os.getenv("STOP_LOSS_PCT"), 0.01),
        lookback_candles=_int(os.getenv("LOOKBACK_CANDLES"), 20),
        entry_zscore=_float(os.getenv("ENTRY_ZSCORE"), 1.5),
        take_profit_pct=_float(os.getenv("TAKE_PROFIT_PCT"), 0.008),
        cooldown_seconds=_int(os.getenv("COOLDOWN_SECONDS"), 120),
        fee_rate=_float(os.getenv("FEE_RATE"), 0.001),
        soft_daily_target_usd=_float(os.getenv("SOFT_DAILY_TARGET_USD"), 1.0),
        poll_interval_seconds=_int(os.getenv("POLL_INTERVAL_SECONDS"), 15),
        state_path=Path(os.getenv("STATE_PATH", "state.json")),
    )
    settings.validate()
    return settings
