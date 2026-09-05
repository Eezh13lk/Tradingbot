"""Mean-reversion / candle z-score strategy for USDT-M futures (long & short)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

from tradingbot_futures.models import (
    Candle,
    OrderIntent,
    Position,
    PositionSide,
    ProposedOrder,
    Side,
)


@dataclass
class StrategyParams:
    lookback: int = 20
    entry_zscore: float = 1.5
    take_profit_pct: float = 0.008
    stop_loss_pct: float = 0.10  # derived: RISK_USD / open notional
    open_margin_usd: float = 0.5  # sizes position (notional = open × leverage)
    target_margin_usd: float = 5.0  # post-fill isolated top-up target
    leverage: int = 10
    fee_rate: float = 0.0004
    cooldown_seconds: int = 120
    max_vol_pct: float = 0.02  # skip if std/mean > this (high vol)
    soft_daily_target_usd: float = 1.0  # metrics only


class MeanReversionStrategy:
    """
    Z-score mean reversion on closes (long and short):

    - z <= -entry_zscore → LONG entry (oversold)
    - z >= +entry_zscore → SHORT entry (overbought)
    - Skip when lookback volatility (std/mean) > max_vol_pct
    - Exit on stop-loss, take-profit, or mean reversion
    - Fee-aware: require take-profit edge to cover round-trip fees
    - Cooldown after each closed trade
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

    @property
    def target_notional(self) -> float:
        return self.params.open_margin_usd * float(self.params.leverage)

    def evaluate(
        self,
        candles: list[Candle],
        position: Optional[Position],
        mark_price: float,
        step_size: float = 0.001,
    ) -> Optional[ProposedOrder]:
        need = self.params.lookback
        if len(candles) < need:
            return None

        closes = [c.close for c in candles[-need:]]
        mean, std = self._mean_std(closes)
        if std <= 0 or mean <= 0:
            return None

        z = (mark_price - mean) / std
        vol_pct = std / mean
        round_trip_fee = 2.0 * self.params.fee_rate

        # ----- manage open position -----
        if position is not None and position.quantity > 0:
            return self._manage_open(position, mark_price, z, mean)

        # ----- entry filters -----
        if self._in_cooldown():
            return None

        if self.params.take_profit_pct <= round_trip_fee:
            return None

        # Skip high volatility regimes
        if vol_pct > self.params.max_vol_pct:
            return None

        pos_side: Optional[PositionSide] = None
        side: Optional[Side] = None
        reason = ""

        if z <= -self.params.entry_zscore:
            pos_side = PositionSide.LONG
            side = Side.BUY
            reason = (
                f"mean-reversion LONG z={z:.3f} mean={mean:.2f} "
                f"std={std:.4f} vol_pct={vol_pct:.4f}"
            )
        elif z >= self.params.entry_zscore:
            pos_side = PositionSide.SHORT
            side = Side.SELL
            reason = (
                f"mean-reversion SHORT z={z:.3f} mean={mean:.2f} "
                f"std={std:.4f} vol_pct={vol_pct:.4f}"
            )
        else:
            return None

        notional = self.target_notional
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
        # Margin used for this size at configured leverage
        margin = actual_notional / float(self.params.leverage)

        return ProposedOrder(
            symbol=self.symbol,
            side=side,
            quantity=qty,
            notional_usd=actual_notional,
            margin_usd=margin,
            leverage=self.params.leverage,
            intent=OrderIntent.ENTRY,
            reason=reason,
            position_side=pos_side,
        )

    def _manage_open(
        self,
        position: Position,
        mark_price: float,
        z: float,
        mean: float,
    ) -> Optional[ProposedOrder]:
        close_side = position.close_side
        notional = position.quantity * mark_price

        # Stop-loss
        if position.side == PositionSide.LONG and mark_price <= position.stop_loss_price:
            return ProposedOrder(
                symbol=self.symbol,
                side=close_side,
                quantity=position.quantity,
                notional_usd=notional,
                margin_usd=position.margin_usd,
                leverage=position.leverage,
                intent=OrderIntent.EXIT_STOP_LOSS,
                reason=(
                    f"stop-loss hit @ {mark_price:.2f} "
                    f"<= {position.stop_loss_price:.2f}"
                ),
                position_side=position.side,
            )
        if position.side == PositionSide.SHORT and mark_price >= position.stop_loss_price:
            return ProposedOrder(
                symbol=self.symbol,
                side=close_side,
                quantity=position.quantity,
                notional_usd=notional,
                margin_usd=position.margin_usd,
                leverage=position.leverage,
                intent=OrderIntent.EXIT_STOP_LOSS,
                reason=(
                    f"stop-loss hit @ {mark_price:.2f} "
                    f">= {position.stop_loss_price:.2f}"
                ),
                position_side=position.side,
            )

        # Take-profit
        if position.side == PositionSide.LONG and mark_price >= position.take_profit_price:
            return ProposedOrder(
                symbol=self.symbol,
                side=close_side,
                quantity=position.quantity,
                notional_usd=notional,
                margin_usd=position.margin_usd,
                leverage=position.leverage,
                intent=OrderIntent.EXIT_TAKE_PROFIT,
                reason=(
                    f"take-profit hit @ {mark_price:.2f} "
                    f">= {position.take_profit_price:.2f}"
                ),
                position_side=position.side,
            )
        if position.side == PositionSide.SHORT and mark_price <= position.take_profit_price:
            return ProposedOrder(
                symbol=self.symbol,
                side=close_side,
                quantity=position.quantity,
                notional_usd=notional,
                margin_usd=position.margin_usd,
                leverage=position.leverage,
                intent=OrderIntent.EXIT_TAKE_PROFIT,
                reason=(
                    f"take-profit hit @ {mark_price:.2f} "
                    f"<= {position.take_profit_price:.2f}"
                ),
                position_side=position.side,
            )

        # Mean-reversion signal exit
        if position.side == PositionSide.LONG and z >= -0.2:
            return ProposedOrder(
                symbol=self.symbol,
                side=close_side,
                quantity=position.quantity,
                notional_usd=notional,
                margin_usd=position.margin_usd,
                leverage=position.leverage,
                intent=OrderIntent.EXIT_SIGNAL,
                reason=f"mean-reversion exit LONG z={z:.3f} mean={mean:.2f}",
                position_side=position.side,
            )
        if position.side == PositionSide.SHORT and z <= 0.2:
            return ProposedOrder(
                symbol=self.symbol,
                side=close_side,
                quantity=position.quantity,
                notional_usd=notional,
                margin_usd=position.margin_usd,
                leverage=position.leverage,
                intent=OrderIntent.EXIT_SIGNAL,
                reason=f"mean-reversion exit SHORT z={z:.3f} mean={mean:.2f}",
                position_side=position.side,
            )

        return None
