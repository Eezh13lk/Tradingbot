"""Shared data models for USDT-M futures (long and short)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    """Order side relative to opening/closing a position."""

    BUY = "BUY"  # open/increase long, or close short
    SELL = "SELL"  # open/increase short, or close long


class PositionSide(str, Enum):
    """Net position direction (one-way mode)."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class OrderIntent(str, Enum):
    """Why the strategy wants an order."""

    ENTRY = "ENTRY"
    EXIT_TAKE_PROFIT = "EXIT_TAKE_PROFIT"
    EXIT_STOP_LOSS = "EXIT_STOP_LOSS"
    EXIT_SIGNAL = "EXIT_SIGNAL"


@dataclass
class Candle:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Position:
    symbol: str
    side: PositionSide  # LONG or SHORT
    quantity: float  # always > 0 (absolute size)
    entry_price: float
    entry_time: datetime
    stop_loss_price: float
    take_profit_price: float
    margin_usd: float
    leverage: int
    notional_usd: float
    fee_paid_usd: float = 0.0

    def unrealized_pnl(self, mark: float) -> float:
        if self.side == PositionSide.LONG:
            return (mark - self.entry_price) * self.quantity
        if self.side == PositionSide.SHORT:
            return (self.entry_price - mark) * self.quantity
        return 0.0

    @property
    def close_side(self) -> Side:
        """Side required to flatten this position."""
        if self.side == PositionSide.LONG:
            return Side.SELL
        return Side.BUY


@dataclass
class ProposedOrder:
    symbol: str
    side: Side
    quantity: float
    notional_usd: float
    margin_usd: float
    leverage: int
    intent: OrderIntent
    reason: str
    position_side: PositionSide = PositionSide.FLAT  # intended position after entry
    limit_price: Optional[float] = None


@dataclass
class OrderResult:
    ok: bool
    order_id: Optional[str]
    symbol: str
    side: Side
    quantity: float
    price: float
    fee_usd: float
    dry_run: bool
    message: str
    filled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
