"""Defensive mean-reversion / micro-range strategy on a single spot pair."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

from tradingbot.models import (
    Candle,
    OrderIntent,
    Position,
    ProposedOrder,
    Side,
)


@dataclass
class StrategyParams:
    lookback: int = 20
    entry_zscore: float = 1.5
    take_profit_pct: float = 0.008
    stop_loss_pct: float = 0.01
    max_position_usd: float = 50.0
    fee_rate: float = 0.001
    cooldown_seconds: int = 120
    # Soft metrics target — logging only, never drives risk
    soft_daily_target_usd: float = 1.0


class MeanReversionStrategy:
    """
    Simple z-score mean reversion on closes:

    - If price is below mean by entry_zscore * std -> consider BUY entry
    - Exit on take-profit, stop-loss, or mean reversion (zscore recovering)

    Fee-aware: require take-profit edge to cover round-trip fees before entry.
    Cooldown after each closed trade to avoid churn.
    """

    def __init__(self, params: StrategyParams, symbol: str = "BTCUSDT") -> None:
        self.params = params
        self.symbol = symbol
        self._last_trade_ts: float = 0.0

    def notify_trade_closed(self) -> None:
        self._last_trade_ts = time.time()

    def _in_cooldown(self) -> bool:
        return (time.time() - self._last_trade_ts) < self.params.cooldown_seconds

    @staticmethod
    def _mean_std(closes: list[float]) -> tuple[float, float]:
        n = len(closes)
        mean = sum(closes) / n
        var = sum((c - mean) ** 2 for c in closes) / max(n - 1, 1)
        return mean, math.sqrt(var)

    def evaluate(
        self,
        candles: list[Candle],
        position: Optional[Position],
        mark_price: float,
        max_notional_usd: float,
        step_size: float = 0.00001,
    ) -> Optional[ProposedOrder]:
        need = self.params.lookback
        if len(candles) < need:
            return None

        closes = [c.close for c in candles[-need:]]
        mean, std = self._mean_std(closes)
        if std <= 0:
            return None

        z = (mark_price - mean) / std
        round_trip_fee = 2.0 * self.params.fee_rate

        # ----- manage open long -----
        if position is not None and position.quantity > 0:
            if mark_price <= position.stop_loss_price:
                return ProposedOrder(
                    symbol=self.symbol,
                    side=Side.SELL,
                    quantity=position.quantity,
                    notional_usd=position.quantity * mark_price,
                    intent=OrderIntent.EXIT_STOP_LOSS,
                    reason=(
                        f"stop-loss hit @ {mark_price:.2f} "
                        f"<= {position.stop_loss_price:.2f}"
                    ),
                )
            if mark_price >= position.take_profit_price:
                return ProposedOrder(
                    symbol=self.symbol,
                    side=Side.SELL,
                    quantity=position.quantity,
                    notional_usd=position.quantity * mark_price,
                    intent=OrderIntent.EXIT_TAKE_PROFIT,
                    reason=(
                        f"take-profit hit @ {mark_price:.2f} "
                        f">= {position.take_profit_price:.2f}"
                    ),
                )
            # Mean reversion exit: price back near/above mean
            if z >= -0.2:
                return ProposedOrder(
                    symbol=self.symbol,
                    side=Side.SELL,
                    quantity=position.quantity,
                    notional_usd=position.quantity * mark_price,
                    intent=OrderIntent.EXIT_SIGNAL,
                    reason=f"mean-reversion exit z={z:.3f} mean={mean:.2f}",
                )
            return None

        # ----- entry -----
        if self._in_cooldown():
            return None

        # Need enough edge vs fees
        if self.params.take_profit_pct <= round_trip_fee:
            return None

        if z > -self.params.entry_zscore:
            return None  # not stretched enough to the downside

        notional = min(max_notional_usd, self.params.max_position_usd)
        if notional <= 0 or mark_price <= 0:
            return None

        raw_qty = notional / mark_price
        if step_size > 0:
            qty = (int(raw_qty / step_size)) * step_size
        else:
            qty = raw_qty
        qty = float(f"{qty:.8f}")
        if qty <= 0:
            return None

        actual_notional = qty * mark_price
        return ProposedOrder(
            symbol=self.symbol,
            side=Side.BUY,
            quantity=qty,
            notional_usd=actual_notional,
            intent=OrderIntent.ENTRY,
            reason=f"mean-reversion entry z={z:.3f} mean={mean:.2f} std={std:.4f}",
        )
