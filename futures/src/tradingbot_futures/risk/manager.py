"""Hard risk caps for USDT-M futures — every order must pass RiskManager.approve()."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tradingbot_futures.models import (
    OrderIntent,
    Position,
    PositionSide,
    ProposedOrder,
    RiskDecision,
    Side,
)


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class DailyState:
    date_utc: str
    realized_pnl_usd: float = 0.0
    trades_today: int = 0

    def to_dict(self) -> dict:
        return {
            "date_utc": self.date_utc,
            "realized_pnl_usd": self.realized_pnl_usd,
            "trades_today": self.trades_today,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DailyState":
        return cls(
            date_utc=str(data.get("date_utc", _utc_today())),
            realized_pnl_usd=float(data.get("realized_pnl_usd", 0.0)),
            trades_today=int(data.get("trades_today", 0)),
        )


class RiskManager:
    """
    Enforces hard caps before any futures order is sent:

    - Isolated margin only (cross rejected)
    - LEVERAGE <= max_leverage (default 10)
    - Size from OPEN_MARGIN_USD × LEVERAGE (e.g. 0.5 × 10 ≈ $5 notional)
    - After fill, TARGET_MARGIN_USD is added separately (does not change size)
    - Per-trade $ risk = RISK_USD (default $0.50 ≈ 10% of target margin)
    - MAX_DAILY_LOSS_USD: when UTC-day realized PnL <= -cap, block NEW entries
      (exits / stop-loss / take-profit still allowed so we can flatten)
    - Mandatory stop-loss price on every entry (derived from risk math)
    """

    def __init__(
        self,
        open_margin_usd: float = 0.5,
        target_margin_usd: float = 5.0,
        leverage: int = 10,
        risk_usd: float = 0.5,
        max_daily_loss_usd: float = 2.0,
        max_leverage: int = 10,
        margin_type: str = "ISOLATED",
        state_path: Optional[Path] = None,
    ) -> None:
        if open_margin_usd <= 0:
            raise ValueError("open_margin_usd must be > 0")
        if target_margin_usd < open_margin_usd:
            raise ValueError("target_margin_usd must be >= open_margin_usd")
        if leverage < 1 or leverage > max_leverage:
            raise ValueError(f"leverage must be in [1, {max_leverage}]")
        if max_leverage > 10:
            raise ValueError("max_leverage hard cap is 10x")
        if risk_usd <= 0:
            raise ValueError("risk_usd must be > 0")
        if max_daily_loss_usd <= 0:
            raise ValueError("max_daily_loss_usd must be > 0")
        if margin_type.upper() != "ISOLATED":
            raise ValueError("only ISOLATED margin is allowed")

        self.open_margin_usd = float(open_margin_usd)
        self.target_margin_usd = float(target_margin_usd)
        self.leverage = int(leverage)
        self.risk_usd = float(risk_usd)
        self.max_daily_loss_usd = float(max_daily_loss_usd)
        self.max_leverage = int(max_leverage)
        self.margin_type = "ISOLATED"
        self.state_path = Path(state_path) if state_path else None
        self._daily = DailyState(date_utc=_utc_today())
        self._load()

    # ----- derived risk math -----

    @property
    def margin_usd(self) -> float:
        """Alias: open margin used for sizing (backward-friendly name in logs)."""
        return self.open_margin_usd

    @property
    def notional_usd(self) -> float:
        """Open notional = OPEN_MARGIN_USD × LEVERAGE (e.g. 0.5 × 10 = 5)."""
        return self.open_margin_usd * float(self.leverage)

    @property
    def max_risk_usd(self) -> float:
        """Per-trade max $ loss ≈ RISK_USD (default 0.50)."""
        return self.risk_usd

    @property
    def stop_loss_pct(self) -> float:
        """
        Price move that realizes ~RISK_USD on open notional:
          stop_loss_pct = RISK_USD / (OPEN_MARGIN × LEVERAGE)
        Defaults: 0.50 / 5.0 = 0.10 (10% adverse move on $5 notional ≈ $0.50).
        """
        return self.risk_usd / self.notional_usd

    @property
    def add_margin_usd(self) -> float:
        """Isolated margin to add after fill (widens liquidation; no size change)."""
        return max(0.0, self.target_margin_usd - self.open_margin_usd)

    # ----- persistence -----

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            daily = DailyState.from_dict(data.get("daily", {}))
            if daily.date_utc != _utc_today():
                self._daily = DailyState(date_utc=_utc_today())
            else:
                self._daily = daily
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._daily = DailyState(date_utc=_utc_today())

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"daily": self._daily.to_dict()}
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _rollover_if_needed(self) -> None:
        today = _utc_today()
        if self._daily.date_utc != today:
            self._daily = DailyState(date_utc=today)
            self._save()

    # ----- public API -----

    @property
    def realized_pnl_today(self) -> float:
        self._rollover_if_needed()
        return self._daily.realized_pnl_usd

    @property
    def daily_loss_halted(self) -> bool:
        """True when new entries must stop for the UTC day."""
        self._rollover_if_needed()
        return self._daily.realized_pnl_usd <= -self.max_daily_loss_usd

    def stop_loss_price(self, entry_price: float, position_side: PositionSide) -> float:
        """Mandatory SL: ~10% adverse move at default 0.5 open / 10x / $0.50 risk."""
        pct = self.stop_loss_pct
        if position_side == PositionSide.LONG:
            return entry_price * (1.0 - pct)
        if position_side == PositionSide.SHORT:
            return entry_price * (1.0 + pct)
        raise ValueError("stop_loss_price requires LONG or SHORT")

    def take_profit_price(
        self, entry_price: float, position_side: PositionSide, take_profit_pct: float
    ) -> float:
        if position_side == PositionSide.LONG:
            return entry_price * (1.0 + take_profit_pct)
        if position_side == PositionSide.SHORT:
            return entry_price * (1.0 - take_profit_pct)
        raise ValueError("take_profit_price requires LONG or SHORT")

    def record_realized_pnl(self, pnl_usd: float) -> None:
        """Call after a closing fill (include fees in the PnL figure)."""
        self._rollover_if_needed()
        self._daily.realized_pnl_usd += float(pnl_usd)
        self._daily.trades_today += 1
        self._save()

    def approve(
        self,
        order: ProposedOrder,
        open_position: Optional[Position] = None,
    ) -> RiskDecision:
        """
        Gate every order. Returns RiskDecision(allowed=..., reason=...).

        Exits (stop / TP / signal) are allowed even during daily-loss halt so
        the bot can flatten. New ENTRY orders are blocked when halted or when
        open-margin / leverage / notional / risk caps would be violated.
        """
        self._rollover_if_needed()

        if order.quantity <= 0 or order.notional_usd <= 0:
            return RiskDecision(False, "quantity/notional must be positive")

        is_entry = order.intent == OrderIntent.ENTRY
        is_exit = order.intent in {
            OrderIntent.EXIT_STOP_LOSS,
            OrderIntent.EXIT_TAKE_PROFIT,
            OrderIntent.EXIT_SIGNAL,
        }

        # ----- exits: must flatten existing position -----
        if is_exit:
            if open_position is None or open_position.quantity <= 0:
                return RiskDecision(False, "cannot exit without open position")
            if order.quantity > open_position.quantity * 1.0000001:
                return RiskDecision(
                    False,
                    f"exit qty {order.quantity} exceeds position {open_position.quantity}",
                )
            if order.side != open_position.close_side:
                return RiskDecision(
                    False,
                    f"exit side {order.side.value} must be {open_position.close_side.value} "
                    f"to close {open_position.side.value}",
                )
            return RiskDecision(True, "approved exit")

        if not is_entry:
            return RiskDecision(False, f"unknown intent {order.intent}")

        # ----- entries -----
        if open_position is not None and open_position.quantity > 0:
            return RiskDecision(False, "already in a position; no pyramiding")

        if self.daily_loss_halted:
            return RiskDecision(
                False,
                f"daily loss halt: PnL {self._daily.realized_pnl_usd:.4f} "
                f"<= -{self.max_daily_loss_usd} (UTC day {self._daily.date_utc})",
            )

        if order.leverage < 1 or order.leverage > self.max_leverage:
            return RiskDecision(
                False,
                f"leverage {order.leverage}x exceeds hard max {self.max_leverage}x",
            )
        if order.leverage > self.leverage:
            return RiskDecision(
                False,
                f"order leverage {order.leverage}x > configured LEVERAGE {self.leverage}",
            )

        if order.margin_usd > self.open_margin_usd + 1e-9:
            return RiskDecision(
                False,
                f"OPEN_MARGIN_USD exceeded: {order.margin_usd:.4f} > {self.open_margin_usd}",
            )

        # Notional must stay near open_margin × leverage (allow tiny float/step rounding)
        expected_notional = order.margin_usd * float(order.leverage)
        if order.notional_usd > self.notional_usd * 1.02 + 1e-9:
            return RiskDecision(
                False,
                f"notional {order.notional_usd:.4f} > cap {self.notional_usd:.4f} "
                f"(OPEN_MARGIN_USD×LEVERAGE)",
            )
        # Per-trade $ risk check: notional × stop_loss_pct ≈ RISK_USD
        implied_risk = order.notional_usd * self.stop_loss_pct
        if implied_risk > self.max_risk_usd * 1.02 + 1e-9:
            return RiskDecision(
                False,
                f"per-trade risk ${implied_risk:.4f} > max ${self.max_risk_usd:.4f} "
                f"(RISK_USD)",
            )

        if order.position_side not in {PositionSide.LONG, PositionSide.SHORT}:
            return RiskDecision(False, "entry must declare LONG or SHORT position_side")

        if order.position_side == PositionSide.LONG and order.side != Side.BUY:
            return RiskDecision(False, "LONG entry must be BUY")
        if order.position_side == PositionSide.SHORT and order.side != Side.SELL:
            return RiskDecision(False, "SHORT entry must be SELL")

        # Sanity: expected notional should match margin×leverage closely
        if (
            expected_notional > 0
            and abs(order.notional_usd - expected_notional)
            > expected_notional * 0.05 + 1e-6
        ):
            return RiskDecision(
                False,
                f"notional {order.notional_usd:.4f} inconsistent with "
                f"margin×leverage {expected_notional:.4f}",
            )

        return RiskDecision(True, "approved")
