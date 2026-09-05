"""Shared data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


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
    quantity: float
    entry_price: float
    entry_time: datetime
    stop_loss_price: float
    take_profit_price: float
    quote_spent: float  # USD-ish notional including entry fee estimate

    @property
    def notional_usd(self) -> float:
        return abs(self.quantity * self.entry_price)

    def unrealized_pnl(self, mark: float) -> float:
        return (mark - self.entry_price) * self.quantity


@dataclass
class ProposedOrder:
    symbol: str
    side: Side
    quantity: float
    notional_usd: float
    intent: OrderIntent
    reason: str
    limit_price: Optional[float] = None  # None => market-style


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
