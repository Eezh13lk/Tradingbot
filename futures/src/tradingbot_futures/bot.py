"""Main trading loop: strategy -> risk gate -> futures exchange."""

from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tradingbot_futures.config import Settings
from tradingbot_futures.exchange.client import FuturesExchangeClient
from tradingbot_futures.models import OrderIntent, Position, PositionSide
from tradingbot_futures.risk.manager import RiskManager
from tradingbot_futures.strategy.mean_reversion import MeanReversionStrategy, StrategyParams

logger = logging.getLogger(__name__)


class FuturesTradingBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.exchange = FuturesExchangeClient(
            api_key=settings.api_key,
            api_secret=settings.api_secret,
            dry_run=settings.dry_run,
            testnet=settings.testnet,
            fee_rate=settings.fee_rate,
            leverage=settings.leverage,
            margin_type=settings.margin_type,
        )
        self.risk = RiskManager(
            open_margin_usd=settings.open_margin_usd,
            target_margin_usd=settings.target_margin_usd,
            leverage=settings.leverage,
            risk_usd=settings.risk_usd,
            max_daily_loss_usd=settings.max_daily_loss_usd,
            max_leverage=10,
            margin_type=settings.margin_type,
            state_path=settings.state_path,
        )
        self.strategy = MeanReversionStrategy(
            StrategyParams(
                lookback=settings.lookback_candles,
                entry_zscore=settings.entry_zscore,
                take_profit_pct=settings.take_profit_pct,
                stop_loss_pct=settings.stop_loss_pct,
                open_margin_usd=settings.open_margin_usd,
                target_margin_usd=settings.target_margin_usd,
                leverage=settings.leverage,
                fee_rate=settings.fee_rate,
                cooldown_seconds=settings.cooldown_seconds,
                max_vol_pct=settings.max_vol_pct,
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
                side=PositionSide(data["side"]),
                quantity=float(data["quantity"]),
                entry_price=float(data["entry_price"]),
                entry_time=datetime.fromisoformat(data["entry_time"]),
                stop_loss_price=float(data["stop_loss_price"]),
                take_profit_price=float(data["take_profit_price"]),
                margin_usd=float(data["margin_usd"]),
                leverage=int(data["leverage"]),
                notional_usd=float(data["notional_usd"]),
                fee_paid_usd=float(data.get("fee_paid_usd", 0.0)),
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
            "side": self.position.side.value,
            "quantity": self.position.quantity,
            "entry_price": self.position.entry_price,
            "entry_time": self.position.entry_time.isoformat(),
            "stop_loss_price": self.position.stop_loss_price,
            "take_profit_price": self.position.take_profit_price,
            "margin_usd": self.position.margin_usd,
            "leverage": self.position.leverage,
            "notional_usd": self.position.notional_usd,
            "fee_paid_usd": self.position.fee_paid_usd,
        }
        self._position_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _log_soft_target(self) -> None:
        pnl = self.risk.realized_pnl_today
        target = self.settings.soft_daily_target_usd
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

        proposed = self.strategy.evaluate(
            candles=candles,
            position=self.position,
            mark_price=mark,
            step_size=step,
        )

        logger.debug(
            "tick mark=%.2f position=%s daily_pnl=%.4f halt=%s proposed=%s",
            mark,
            self.position.side.value if self.position else None,
            self.risk.realized_pnl_today,
            self.risk.daily_loss_halted,
            proposed.intent.value if proposed else None,
        )

        if proposed is None:
            return

        decision = self.risk.approve(proposed, open_position=self.position)
        if not decision.allowed:
            logger.warning("Risk REJECTED %s: %s", proposed.intent.value, decision.reason)
            return

        logger.info("Risk APPROVED %s: %s", proposed.intent.value, decision.reason)
        result = self.exchange.place_order(proposed, mark_price=mark)
        if not result.ok:
            logger.error("Order failed: %s", result.message)
            return

        if proposed.intent == OrderIntent.ENTRY:
            pos_side = proposed.position_side
            sl = self.risk.stop_loss_price(result.price, pos_side)
            tp = self.risk.take_profit_price(
                result.price, pos_side, self.settings.take_profit_pct
            )
            notional = result.quantity * result.price
            open_margin = notional / float(self.settings.leverage)
            # Top up isolated margin to TARGET — does NOT increase size
            add_amt = max(0.0, self.settings.target_margin_usd - open_margin)
            if add_amt > 0:
                add_res = self.exchange.add_isolated_margin(symbol, add_amt)
                if not add_res.get("ok"):
                    logger.error(
                        "addIsolatedMargin failed after fill: %s — "
                        "position remains at open margin %.4f",
                        add_res.get("message"),
                        open_margin,
                    )
                    margin = open_margin
                else:
                    margin = open_margin + float(add_res.get("added", add_amt))
                    logger.info(
                        "Added isolated margin +%.4f → total margin≈%.4f "
                        "(size/notional unchanged; liquidation widened)",
                        add_res.get("added", add_amt),
                        margin,
                    )
            else:
                margin = open_margin
            self.position = Position(
                symbol=symbol,
                side=pos_side,
                quantity=result.quantity,
                entry_price=result.price,
                entry_time=result.filled_at,
                stop_loss_price=sl,
                take_profit_price=tp,
                margin_usd=margin,
                leverage=self.settings.leverage,
                notional_usd=notional,
                fee_paid_usd=result.fee_usd,
            )
            self._save_position()
            logger.info(
                "Opened %s qty=%.8f entry=%.2f SL=%.2f TP=%.2f "
                "open_margin=%.4f total_margin=%.4f notional=%.2f lev=%sx "
                "risk≈$%.2f stop_pct=%.4f (mandatory SL set)",
                pos_side.value,
                result.quantity,
                result.price,
                sl,
                tp,
                open_margin,
                margin,
                notional,
                self.settings.leverage,
                self.settings.max_risk_usd,
                self.settings.stop_loss_pct,
            )
        elif proposed.intent in {
            OrderIntent.EXIT_STOP_LOSS,
            OrderIntent.EXIT_TAKE_PROFIT,
            OrderIntent.EXIT_SIGNAL,
        }:
            if self.position is None:
                return
            # Realized PnL on notional move minus fees
            pnl = self.position.unrealized_pnl(result.price)
            pnl -= self.position.fee_paid_usd + result.fee_usd
            self.risk.record_realized_pnl(pnl)
            logger.info(
                "Closed %s intent=%s pnl=%.4f (fees included in estimate)",
                self.position.side.value,
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

        mode = "DRY_RUN" if self.settings.dry_run else "LIVE FUTURES"
        logger.info(
            "Starting bot mode=%s symbol=%s OPEN_MARGIN=%.2f TARGET_MARGIN=%.2f "
            "LEVERAGE=%sx open_notional≈%.2f add_margin≈%.2f max_risk≈$%.2f "
            "STOP_LOSS_PCT=%.4f MAX_DAILY_LOSS_USD=%.2f margin_type=%s",
            mode,
            self.settings.symbol,
            self.settings.open_margin_usd,
            self.settings.target_margin_usd,
            self.settings.leverage,
            self.settings.notional_usd,
            self.settings.add_margin_usd,
            self.settings.max_risk_usd,
            self.settings.stop_loss_pct,
            self.settings.max_daily_loss_usd,
            self.settings.margin_type,
        )
        logger.warning(
            "FUTURES RISK: 10x isolated leverage can liquidate quickly. "
            "Soft daily target is metrics-only, not a profit guarantee. "
            "No profit is promised."
        )

        while self._running:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("Tick error — will retry")
            time.sleep(self.settings.poll_interval_seconds)

        logger.info(
            "Bot stopped. Daily realized PnL (UTC)=%.4f",
            self.risk.realized_pnl_today,
        )
