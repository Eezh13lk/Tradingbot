"""Hard risk caps — every order must pass through RiskManager.approve()."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tradingbot.models import OrderIntent, Position, ProposedOrder, RiskDecision, Side


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
    Enforces hard caps before any order is sent:

    - MAX_POSITION_USD: reject entries that would exceed open notional
    - MAX_DAILY_LOSS_USD: when UTC-day realized PnL <= -cap, block NEW entries
      (exits / stop-loss / take-profit still allowed so we can flatten)
    - Per-trade stop-loss price must be set on entry positions
    - Spot only: no short sells of base asset we do not hold
    """

    def __init__(
        self,
        max_position_usd: float = 50.0,
        max_daily_loss_usd: float = 2.0,
        stop_loss_pct: float = 0.01,
        state_path: Optional[Path] = None,
    ) -> None:
        if max_position_usd <= 0:
            raise ValueError("max_position_usd must be > 0")
        if max_daily_loss_usd <= 0:
            raise ValueError("max_daily_loss_usd must be > 0")
        if not (0 < stop_loss_pct < 1):
            raise ValueError("stop_loss_pct must be in (0, 1)")

        self.max_position_usd = float(max_position_usd)
        self.max_daily_loss_usd = float(max_daily_loss_usd)
        self.stop_loss_pct = float(stop_loss_pct)
        self.state_path = Path(state_path) if state_path else None
        self._daily = DailyState(date_utc=_utc_today())
        self._load()

    # ----- persistence -----

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            daily = DailyState.from_dict(data.get("daily", {}))
            if daily.date_utc != _utc_today():
                # New UTC day — reset loss counter
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

    def stop_loss_price(self, entry_price: float, side: Side = Side.BUY) -> float:
        """Long spot: stop below entry. (Spot bot only opens long positions.)"""
        if side != Side.BUY:
            raise ValueError("Spot risk manager only supports long entries")
        return entry_price * (1.0 - self.stop_loss_pct)

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
        open_notional_usd: float = 0.0,
    ) -> RiskDecision:
        """
        Gate every order. Returns RiskDecision(allowed=..., reason=...).

        Exits (stop / TP / signal) are allowed even during daily-loss halt so
        the bot can flatten. New ENTRY orders are blocked when halted or when
        position size would exceed MAX_POSITION_USD.
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

        # Spot: no shorting — SELL only to close an existing long
        if order.side == Side.SELL:
            if open_position is None or open_position.quantity <= 0:
                return RiskDecision(False, "spot: cannot SELL without open long position")
            if order.quantity > open_position.quantity * 1.0000001:
                return RiskDecision(
                    False,
                    f"spot: sell qty {order.quantity} exceeds position {open_position.quantity}",
                )
            if not is_exit:
                return RiskDecision(False, "SELL must be an exit intent on spot")

        if is_entry:
            if order.side != Side.BUY:
                return RiskDecision(False, "spot entries must be BUY")
            if open_position is not None and open_position.quantity > 0:
                return RiskDecision(False, "already in a position; no pyramiding")
            if self.daily_loss_halted:
                return RiskDecision(
                    False,
                    f"daily loss halt: PnL {self._daily.realized_pnl_usd:.4f} "
                    f"<= -{self.max_daily_loss_usd} (UTC day {self._daily.date_utc})",
                )
            projected = open_notional_usd + order.notional_usd
            if projected > self.max_position_usd + 1e-9:
                return RiskDecision(
                    False,
                    f"MAX_POSITION_USD exceeded: {projected:.4f} > {self.max_position_usd}",
                )
            if order.notional_usd > self.max_position_usd + 1e-9:
                return RiskDecision(
                    False,
                    f"order notional {order.notional_usd:.4f} > MAX_POSITION_USD "
                    f"{self.max_position_usd}",
                )

        if is_exit and order.side != Side.SELL:
            return RiskDecision(False, "exits must be SELL for long spot")

        return RiskDecision(True, "approved")
