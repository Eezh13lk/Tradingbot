"""Binance SPOT client. Dry-run uses public market data only."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from tradingbot.models import Candle, OrderResult, ProposedOrder, Side

logger = logging.getLogger(__name__)

# Primary + fallbacks (some regions get HTTP 451 on api.binance.com)
PUBLIC_BASES = (
    "https://data-api.binance.vision",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.com",
)
TESTNET_BASE = "https://testnet.binance.vision"


class ExchangeClient:
    """
    Spot-only exchange wrapper.

    - DRY_RUN=true: public klines/ticker; simulated fills; never signs orders
    - DRY_RUN=false: python-binance Client for authenticated SPOT orders only
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        dry_run: bool = True,
        testnet: bool = False,
        fee_rate: float = 0.001,
    ) -> None:
        self.dry_run = dry_run
        self.testnet = testnet
        self.fee_rate = fee_rate
        self._public_bases = (TESTNET_BASE,) if testnet else PUBLIC_BASES
        self._working_base: Optional[str] = None
        self._client: Any = None

        if not dry_run:
            from binance.client import Client  # lazy import

            self._client = Client(
                api_key,
                api_secret,
                testnet=testnet,
            )
            logger.info("Authenticated Binance SPOT client ready (testnet=%s)", testnet)

    def _public_get(self, path: str, params: dict) -> Any:
        bases = list(self._public_bases)
        if self._working_base and self._working_base in bases:
            bases.remove(self._working_base)
            bases.insert(0, self._working_base)

        last_err: Optional[Exception] = None
        for base in bases:
            url = f"{base}{path}"
            try:
                r = requests.get(url, params=params, timeout=15)
                if r.status_code == 451:
                    logger.warning("Geo-blocked on %s (HTTP 451) — trying next host", base)
                    last_err = requests.HTTPError(f"451 from {base}")
                    continue
                r.raise_for_status()
                self._working_base = base
                return r.json()
            except requests.RequestException as exc:
                last_err = exc
                logger.warning("Public GET failed on %s: %s", base, exc)
        raise RuntimeError(f"All Binance public endpoints failed for {path}: {last_err}")

    # ----- market data (public) -----

    def get_price(self, symbol: str) -> float:
        if self._client is not None:
            ticker = self._client.get_symbol_ticker(symbol=symbol)
            return float(ticker["price"])
        data = self._public_get("/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 50) -> list[Candle]:
        if self._client is not None:
            raw = self._client.get_klines(symbol=symbol, interval=interval, limit=limit)
        else:
            raw = self._public_get(
                "/api/v3/klines",
                {"symbol": symbol, "interval": interval, "limit": limit},
            )

        candles: list[Candle] = []
        for row in raw:
            candles.append(
                Candle(
                    open_time=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        return candles

    def get_lot_filters(self, symbol: str) -> dict[str, float]:
        """Return stepSize / minQty / minNotional for sizing. Public exchangeInfo."""
        if self._client is not None:
            info = self._client.get_symbol_info(symbol)
        else:
            data = self._public_get("/api/v3/exchangeInfo", {"symbol": symbol})
            symbols = data.get("symbols", [])
            info = symbols[0] if symbols else None

        step = 0.00001
        min_qty = 0.0
        min_notional = 5.0
        if info:
            for f in info.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f["stepSize"])
                    min_qty = float(f["minQty"])
                if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(f.get("minNotional") or f.get("notional", 5.0))
        return {"step_size": step, "min_qty": min_qty, "min_notional": min_notional}

    @staticmethod
    def round_step(qty: float, step: float) -> float:
        if step <= 0:
            return qty
        precision = max(
            0,
            len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0,
        )
        rounded = (int(qty / step)) * step
        return float(f"{rounded:.{precision}f}")

    # ----- orders -----

    def place_order(self, order: ProposedOrder, mark_price: float) -> OrderResult:
        """
        Execute a SPOT market order (or simulate in dry-run).
        Caller MUST have already passed RiskManager.approve().
        """
        price = mark_price
        fee = abs(order.quantity * price * self.fee_rate)

        if self.dry_run:
            oid = f"dry-{int(time.time() * 1000)}"
            logger.info(
                "[DRY_RUN] %s %s qty=%.8f @ ~%.2f intent=%s (%s)",
                order.side.value,
                order.symbol,
                order.quantity,
                price,
                order.intent.value,
                order.reason,
            )
            return OrderResult(
                ok=True,
                order_id=oid,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                price=price,
                fee_usd=fee,
                dry_run=True,
                message="simulated fill",
            )

        assert self._client is not None
        # Spot market order only — never futures, never margin
        side = "BUY" if order.side == Side.BUY else "SELL"
        try:
            raw = self._client.create_order(
                symbol=order.symbol,
                side=side,
                type="MARKET",
                quantity=order.quantity,
            )
            fills = raw.get("fills") or []
            if fills:
                notional = sum(float(f["price"]) * float(f["qty"]) for f in fills)
                qty = sum(float(f["qty"]) for f in fills)
                price = notional / qty if qty else mark_price
                fee = sum(float(f.get("commission", 0)) for f in fills)
            else:
                price = mark_price
                qty = order.quantity
                fee = abs(qty * price * self.fee_rate)

            return OrderResult(
                ok=True,
                order_id=str(raw.get("orderId")),
                symbol=order.symbol,
                side=order.side,
                quantity=float(qty),
                price=float(price),
                fee_usd=float(fee),
                dry_run=False,
                message="filled",
            )
        except Exception as exc:  # noqa: BLE001 — surface exchange errors cleanly
            logger.exception("Order failed")
            return OrderResult(
                ok=False,
                order_id=None,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                price=mark_price,
                fee_usd=0.0,
                dry_run=False,
                message=str(exc),
            )
