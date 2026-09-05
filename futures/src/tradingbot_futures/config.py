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
    """
    Immutable runtime settings for USDT-M futures (isolated margin).

    Workflow (defaults):
      OPEN_MARGIN_USD=0.5, LEVERAGE=10 → open notional ≈ $5 (sizes the position)
      After fill: add isolated margin up to TARGET_MARGIN_USD=5 (~+$4.5)
        → does NOT increase size; only widens liquidation distance
      RISK_USD=0.5 (≈10% of target margin) → stop on ~$5 notional ≈ 10% price
    """

    api_key: str
    api_secret: str
    dry_run: bool
    testnet: bool
    symbol: str
    leverage: int
    open_margin_usd: float
    target_margin_usd: float
    risk_usd: float
    max_daily_loss_usd: float
    margin_type: str  # ISOLATED only
    lookback_candles: int
    entry_zscore: float
    take_profit_pct: float
    max_vol_pct: float  # skip entry if lookback std/mean exceeds this
    cooldown_seconds: int
    fee_rate: float
    soft_daily_target_usd: float
    poll_interval_seconds: int
    state_path: Path

    @property
    def notional_usd(self) -> float:
        """Open notional = OPEN_MARGIN_USD × LEVERAGE (size is fixed at open)."""
        return self.open_margin_usd * float(self.leverage)

    @property
    def max_risk_usd(self) -> float:
        """Per-trade dollar risk budget (default $0.50)."""
        return self.risk_usd

    @property
    def stop_loss_pct(self) -> float:
        """
        Price move that loses ~RISK_USD on the open notional.
        stop_loss_pct = RISK_USD / (OPEN_MARGIN_USD × LEVERAGE)
        Defaults: 0.50 / 5.0 = 0.10 (10% adverse price).
        """
        notional = self.notional_usd
        if notional <= 0:
            raise ValueError("open notional must be > 0")
        return self.risk_usd / notional

    @property
    def add_margin_usd(self) -> float:
        """Extra isolated margin to add after fill (does not change size)."""
        return max(0.0, self.target_margin_usd - self.open_margin_usd)

    def validate(self) -> None:
        if self.open_margin_usd <= 0:
            raise ValueError("OPEN_MARGIN_USD must be > 0")
        if self.target_margin_usd < self.open_margin_usd:
            raise ValueError(
                "TARGET_MARGIN_USD must be >= OPEN_MARGIN_USD "
                "(add-margin only widens liquidation distance)"
            )
        if self.leverage < 1 or self.leverage > 10:
            raise ValueError("LEVERAGE must be between 1 and 10 (hard max 10x)")
        if self.risk_usd <= 0:
            raise ValueError("RISK_USD must be > 0")
        if self.max_daily_loss_usd <= 0:
            raise ValueError("MAX_DAILY_LOSS_USD must be > 0")
        if self.margin_type.upper() != "ISOLATED":
            raise ValueError("MARGIN_TYPE must be ISOLATED (cross margin is forbidden)")
        if self.lookback_candles < 5:
            raise ValueError("LOOKBACK_CANDLES must be >= 5")
        if self.stop_loss_pct <= 0 or self.stop_loss_pct >= 1:
            raise ValueError("derived STOP_LOSS_PCT must be in (0, 1)")
        if not self.dry_run and (not self.api_key or not self.api_secret):
            raise ValueError(
                "BINANCE_API_KEY and BINANCE_API_SECRET required when DRY_RUN=false"
            )


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load settings from .env then process environment."""
    if env_file:
        load_dotenv(env_file)
    else:
        cwd_env = Path.cwd() / ".env"
        if cwd_env.exists():
            load_dotenv(cwd_env)
        else:
            load_dotenv()

    open_margin = _float(os.getenv("OPEN_MARGIN_USD"), 0.5)
    target_margin = _float(os.getenv("TARGET_MARGIN_USD"), 5.0)

    # RISK_USD preferred; else ~10% of target margin via RISK_PCT_OF_MARGIN
    risk_raw = os.getenv("RISK_USD")
    if risk_raw is not None and risk_raw.strip() != "":
        risk_usd = float(risk_raw)
    else:
        pct = _float(os.getenv("RISK_PCT_OF_MARGIN"), 0.10)
        risk_usd = target_margin * pct

    settings = Settings(
        api_key=os.getenv("BINANCE_API_KEY", "").strip(),
        api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
        dry_run=_bool(os.getenv("DRY_RUN"), default=True),
        testnet=_bool(os.getenv("BINANCE_TESTNET"), default=False),
        symbol=os.getenv("SYMBOL", "BTCUSDT").strip().upper(),
        leverage=_int(os.getenv("LEVERAGE"), 10),
        open_margin_usd=open_margin,
        target_margin_usd=target_margin,
        risk_usd=risk_usd,
        max_daily_loss_usd=_float(os.getenv("MAX_DAILY_LOSS_USD"), 2.0),
        margin_type=os.getenv("MARGIN_TYPE", "ISOLATED").strip().upper(),
        lookback_candles=_int(os.getenv("LOOKBACK_CANDLES"), 20),
        entry_zscore=_float(os.getenv("ENTRY_ZSCORE"), 1.5),
        take_profit_pct=_float(os.getenv("TAKE_PROFIT_PCT"), 0.008),
        max_vol_pct=_float(os.getenv("MAX_VOL_PCT"), 0.02),
        cooldown_seconds=_int(os.getenv("COOLDOWN_SECONDS"), 120),
        fee_rate=_float(os.getenv("FEE_RATE"), 0.0004),  # futures taker ~0.04% typical
        soft_daily_target_usd=_float(os.getenv("SOFT_DAILY_TARGET_USD"), 1.0),
        poll_interval_seconds=_int(os.getenv("POLL_INTERVAL_SECONDS"), 15),
        state_path=Path(os.getenv("STATE_PATH", "state.json")),
    )
    settings.validate()
    return settings
