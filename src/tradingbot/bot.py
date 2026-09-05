"""Main trading loop: strategy -> risk gate -> exchange."""

from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tradingbot.config import Settings
from tradingbot.exchange.client import ExchangeClient
from tradingbot.models import OrderIntent, Position, Side
from tradingbot.risk.manager import RiskManager
from tradingbot.strategy.mean_reversion import MeanReversionStrategy, StrategyParams

logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.exchange = ExchangeClient(
            api_key=settings.api_key,
            api_secret=settings.api_secret,
            dry_run=settings.dry_run,
            testnet=settings.testnet,
            fee_rate=settings.fee_rate,
        )
        self.risk = RiskManager(
            max_position_usd=settings.max_position_usd,
            max_daily_loss_usd=settings.max_daily_loss_usd,
            stop_loss_pct=settings.stop_loss_pct,
            state_path=settings.state_path,
        )
        self.strategy = MeanReversionStrategy(
            StrategyParams(
                lookback=settings.lookback_candles,
                entry_zscore=settings.entry_zscore,
                take_profit_pct=settings.take_profit_pct,
                stop_loss_pct=settings.stop_loss_pct,
                max_position_usd=settings.max_position_usd,
                fee_rate=settings.fee_rate,
                cooldown_seconds=settings.cooldown_seconds,
                soft_daily_target_usd=settings.soft_daily_target_usd,
            ),
            symbol=settings.symbol,
        )
        self.position: Optional[Position] = None
        self._position_path = Path("position.json")
        self._running = False
        self._load_position()

    def _load_position(self) -> None:
        if not self._position_path.exists():
            return
        try:
            data = json.loads(self._position_path.read_text(encoding="utf-8"))
            self.position = Position(
                symbol=data["symbol"],
                quantity=float(data["quantity"]),
                entry_price=float(data["entry_price"]),
                entry_time=datetime.fromisoformat(data["entry_time"]),
                stop_loss_price=float(data["stop_loss_price"]),
                take_profit_price=float(data["take_profit_price"]),
                quote_spent=float(data["quote_spent"]),
            )
            logger.info("Restored position: %s", self.position)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Could not load position.json: %s", exc)
            self.position = None

    def _save_position(self) -> None:
        if self.position is None:
            if self._position_path.exists():
                self._position_path.unlink()
            return
        payload = {
            "symbol": self.position.symbol,
            "quantity": self.position.quantity,
            "entry_price": self.position.entry_price,
            "entry_time": self.position.entry_time.isoformat(),
            "stop_loss_price": self.position.stop_loss_price,
            "take_profit_price": self.position.take_profit_price,
            "quote_spent": self.position.quote_spent,
        }
        self._position_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _log_soft_target(self) -> None:
        pnl = self.risk.realized_pnl_today
        target = self.settings.soft_daily_target_usd
        # Metrics only — never a guarantee
        logger.info(
            "Metrics (not a guarantee): UTC-day realized PnL=%.4f | soft target=%.2f",
            pnl,
            target,
        )

    def tick(self) -> None:
        symbol = self.settings.symbol
        mark = self.exchange.get_price(symbol)
        candles = self.exchange.get_klines(
            symbol, interval="1m", limit=max(50, self.settings.lookback_candles + 5)
        )
        filters = self.exchange.get_lot_filters(symbol)
        step = filters["step_size"]

        open_notional = self.position.notional_usd if self.position else 0.0
        proposed = self.strategy.evaluate(
            candles=candles,
            position=self.position,
            mark_price=mark,
            max_notional_usd=self.settings.max_position_usd,
            step_size=step,
        )

        logger.debug(
            "tick mark=%.2f position=%s daily_pnl=%.4f halt=%s proposed=%s",
            mark,
            self.position is not None,
            self.risk.realized_pnl_today,
            self.risk.daily_loss_halted,
            proposed.intent.value if proposed else None,
        )

        if proposed is None:
            return

        decision = self.risk.approve(
            proposed,
            open_position=self.position,
            open_notional_usd=open_notional,
        )
        if not decision.allowed:
            logger.warning("Risk REJECTED %s: %s", proposed.intent.value, decision.reason)
            return

        logger.info("Risk APPROVED %s: %s", proposed.intent.value, decision.reason)
        result = self.exchange.place_order(proposed, mark_price=mark)
        if not result.ok:
            logger.error("Order failed: %s", result.message)
            return

        if proposed.intent == OrderIntent.ENTRY and proposed.side == Side.BUY:
            sl = self.risk.stop_loss_price(result.price, Side.BUY)
            tp = result.price * (1.0 + self.settings.take_profit_pct)
            spent = result.quantity * result.price + result.fee_usd
            self.position = Position(
                symbol=symbol,
                quantity=result.quantity,
                entry_price=result.price,
                entry_time=result.filled_at,
                stop_loss_price=sl,
                take_profit_price=tp,
                quote_spent=spent,
            )
            self._save_position()
            logger.info(
                "Opened long qty=%.8f entry=%.2f SL=%.2f TP=%.2f",
                result.quantity,
                result.price,
                sl,
                tp,
            )
        elif proposed.intent in {
            OrderIntent.EXIT_STOP_LOSS,
            OrderIntent.EXIT_TAKE_PROFIT,
            OrderIntent.EXIT_SIGNAL,
        }:
            if self.position is None:
                return
            proceeds = result.quantity * result.price - result.fee_usd
            pnl = proceeds - self.position.quote_spent
            self.risk.record_realized_pnl(pnl)
            logger.info(
                "Closed position intent=%s pnl=%.4f (fees included in estimate)",
                proposed.intent.value,
                pnl,
            )
            self.position = None
            self._save_position()
            self.strategy.notify_trade_closed()
            self._log_soft_target()

    def run(self) -> None:
        self._running = True

        def _stop(signum, frame):  # noqa: ARG001
            logger.info("Signal %s received — stopping after current tick", signum)
            self._running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        mode = "DRY_RUN" if self.settings.dry_run else "LIVE SPOT"
        logger.info(
            "Starting bot mode=%s symbol=%s MAX_POSITION_USD=%.2f "
            "MAX_DAILY_LOSS_USD=%.2f STOP_LOSS_PCT=%.4f",
            mode,
            self.settings.symbol,
            self.settings.max_position_usd,
            self.settings.max_daily_loss_usd,
            self.settings.stop_loss_pct,
        )
        logger.warning(
            "Trading involves risk of loss. Soft daily target is metrics-only, "
            "not a profit guarantee."
        )

        while self._running:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("Tick error — will retry")
            time.sleep(self.settings.poll_interval_seconds)

        logger.info("Bot stopped. Daily realized PnL (UTC)=%.4f", self.risk.realized_pnl_today)
