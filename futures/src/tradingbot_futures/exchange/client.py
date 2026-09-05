"""Binance USDT-M FUTURES client. Dry-run uses public futures market data only."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from tradingbot_futures.models import Candle, OrderResult, ProposedOrder, Side

logger = logging.getLogger(__name__)

# USDT-M futures public hosts (+ testnet as geo-block fallback for dry-run market data)
PUBLIC_BASES = (
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
    # Accessible from many restricted regions for public futures market data
    "https://testnet.binancefuture.com",
)
TESTNET_BASE = "https://testnet.binancefuture.com"


class FuturesExchangeClient:
    """
    USDT-M futures exchange wrapper (isolated margin).

    - DRY_RUN=true: public klines/ticker; simulated fills; never signs orders
    - DRY_RUN=false: python-binance Client for authenticated futures orders;
      sets leverage and ISOLATED margin type before orders
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        dry_run: bool = True,
        testnet: bool = False,
        fee_rate: float = 0.0004,
        leverage: int = 10,
        margin_type: str = "ISOLATED",
    ) -> None:
        if margin_type.upper() != "ISOLATED":
            raise ValueError("only ISOLATED margin is supported")
        if leverage < 1 or leverage > 10:
            raise ValueError("leverage must be in [1, 10]")

        self.dry_run = dry_run
        self.testnet = testnet
        self.fee_rate = fee_rate
        self.leverage = leverage
        self.margin_type = "ISOLATED"
        if testnet:
            self._public_bases = (TESTNET_BASE,)
        else:
            self._public_bases = PUBLIC_BASES
        self._working_base: Optional[str] = None
        self._client: Any = None
        self._prepared_symbols: set[str] = set()

        if not dry_run:
            from binance.client import Client  # lazy import

            self._client = Client(
                api_key,
                api_secret,
                testnet=testnet,
            )
            logger.info(
                "Authenticated Binance USDT-M FUTURES client ready "
                "(testnet=%s, leverage=%sx, margin=%s)",
                testnet,
                leverage,
                self.margin_type,
            )

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
                    logger.warning(
                        "Geo-blocked on %s (HTTP 451) — trying next host", base
                    )
                    last_err = requests.HTTPError(f"451 from {base}")
                    continue
                if r.status_code >= 400:
                    logger.warning(
                        "HTTP %s on %s — trying next host", r.status_code, base
                    )
                    last_err = requests.HTTPError(f"{r.status_code} from {base}")
                    continue
                # Reject non-JSON / empty HTML error pages
                try:
                    data = r.json()
                except ValueError as exc:
                    logger.warning("Non-JSON from %s: %s", base, exc)
                    last_err = exc
                    continue
                if "testnet" in base and not self.testnet:
                    logger.info(
                        "Using %s for public market data (mainnet fapi unreachable)",
                        base,
                    )
                self._working_base = base
                return data
            except requests.RequestException as exc:
                last_err = exc
                logger.warning("Public GET failed on %s: %s", base, exc)
        raise RuntimeError(
            f"All Binance futures public endpoints failed for {path}: {last_err}"
        )

    # ----- market data (public) -----

    def get_price(self, symbol: str) -> float:
        if self._client is not None:
            ticker = self._client.futures_symbol_ticker(symbol=symbol)
            return float(ticker["price"])
        data = self._public_get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_klines(
        self, symbol: str, interval: str = "1m", limit: int = 50
    ) -> list[Candle]:
        if self._client is not None:
            raw = self._client.futures_klines(
                symbol=symbol, interval=interval, limit=limit
            )
        else:
            raw = self._public_get(
                "/fapi/v1/klines",
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
            info = self._client.futures_exchange_info()
            symbols = [s for s in info.get("symbols", []) if s.get("symbol") == symbol]
            info_sym = symbols[0] if symbols else None
        else:
            data = self._public_get("/fapi/v1/exchangeInfo", {})
            symbols = [s for s in data.get("symbols", []) if s.get("symbol") == symbol]
            info_sym = symbols[0] if symbols else None

        step = 0.001
        min_qty = 0.0
        min_notional = 5.0
        if info_sym:
            for f in info_sym.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f["stepSize"])
                    min_qty = float(f["minQty"])
                if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(
                        f.get("notional", f.get("minNotional", 5.0))
                    )
        return {"step_size": step, "min_qty": min_qty, "min_notional": min_notional}

    def ensure_isolated_leverage(self, symbol: str) -> None:
        """Set ISOLATED margin + configured leverage before live orders."""
        if self.dry_run or self._client is None:
            return
        if symbol in self._prepared_symbols:
            return
        try:
            self._client.futures_change_margin_type(
                symbol=symbol, marginType="ISOLATED"
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "no need to change" not in msg and "already" not in msg:
                logger.warning("futures_change_margin_type(%s): %s", symbol, exc)
        try:
            self._client.futures_change_leverage(symbol=symbol, leverage=self.leverage)
        except Exception as exc:  # noqa: BLE001
            logger.error("futures_change_leverage failed: %s", exc)
            raise
        self._prepared_symbols.add(symbol)
        logger.info(
            "Prepared %s: margin=ISOLATED leverage=%sx",
            symbol,
            self.leverage,
        )

    def add_isolated_margin(
        self,
        symbol: str,
        amount_usd: float,
        position_side: str | None = None,
    ) -> dict:
        """
        Add isolated margin to an open position WITHOUT changing size.

        Binance: POST /fapi/v1/positionMargin (type=1 add).
        Used after entry fill to top up from OPEN_MARGIN toward TARGET_MARGIN,
        widening liquidation distance only.
        """
        amount = float(amount_usd)
        if amount <= 0:
            return {"ok": True, "added": 0.0, "message": "nothing to add"}

        if self.dry_run or self._client is None:
            logger.info(
                "[DRY_RUN] addIsolatedMargin %s amount=%.4f USDT "
                "(widens liquidation only; size unchanged)",
                symbol,
                amount,
            )
            return {
                "ok": True,
                "added": amount,
                "dry_run": True,
                "message": "simulated add isolated margin",
            }

        params: dict = {
            "symbol": symbol,
            "amount": amount,
            "type": 1,  # 1 = add, 2 = reduce
        }
        # Hedge mode only; one-way mode rejects positionSide — omit by default
        if position_side:
            params["positionSide"] = position_side

        try:
            raw = self._client.futures_change_position_margin(**params)
            logger.info(
                "Added isolated margin %s amount=%.4f raw=%s",
                symbol,
                amount,
                raw,
            )
            return {
                "ok": True,
                "added": amount,
                "dry_run": False,
                "message": "added",
                "raw": raw,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("addIsolatedMargin failed for %s", symbol)
            return {
                "ok": False,
                "added": 0.0,
                "dry_run": False,
                "message": str(exc),
            }


    # ----- orders -----

    def place_order(self, order: ProposedOrder, mark_price: float) -> OrderResult:
        """
        Execute a USDT-M futures market order (or simulate in dry-run).
        Caller MUST have already passed RiskManager.approve().
        """
        price = mark_price
        fee = abs(order.quantity * price * self.fee_rate)

        if self.dry_run:
            oid = f"dry-{int(time.time() * 1000)}"
            logger.info(
                "[DRY_RUN] FUTURES %s %s qty=%.8f @ ~%.2f "
                "notional=%.2f margin=%.2f lev=%sx intent=%s (%s)",
                order.side.value,
                order.symbol,
                order.quantity,
                price,
                order.notional_usd,
                order.margin_usd,
                order.leverage,
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
                message="simulated futures fill",
            )

        assert self._client is not None
        self.ensure_isolated_leverage(order.symbol)

        side = "BUY" if order.side == Side.BUY else "SELL"
        try:
            raw = self._client.futures_create_order(
                symbol=order.symbol,
                side=side,
                type="MARKET",
                quantity=order.quantity,
            )
            avg = raw.get("avgPrice")
            qty = float(raw.get("executedQty") or order.quantity)
            if avg and float(avg) > 0:
                price = float(avg)
            else:
                price = mark_price
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("Futures order failed")
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
