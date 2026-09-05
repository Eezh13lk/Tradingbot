"""Unit tests for futures RiskManager math and isolated-margin workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tradingbot_futures.config import load_settings
from tradingbot_futures.exchange.client import FuturesExchangeClient
from tradingbot_futures.models import (
    OrderIntent,
    Position,
    PositionSide,
    ProposedOrder,
    Side,
)
from tradingbot_futures.risk.manager import RiskManager


def _entry_long(
    notional: float = 5.0,
    margin: float = 0.5,
    leverage: int = 10,
    qty: float = 0.0001,
) -> ProposedOrder:
    return ProposedOrder(
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=qty,
        notional_usd=notional,
        margin_usd=margin,
        leverage=leverage,
        intent=OrderIntent.ENTRY,
        reason="test long",
        position_side=PositionSide.LONG,
    )


def _entry_short(
    notional: float = 5.0,
    margin: float = 0.5,
    leverage: int = 10,
    qty: float = 0.0001,
) -> ProposedOrder:
    return ProposedOrder(
        symbol="BTCUSDT",
        side=Side.SELL,
        quantity=qty,
        notional_usd=notional,
        margin_usd=margin,
        leverage=leverage,
        intent=OrderIntent.ENTRY,
        reason="test short",
        position_side=PositionSide.SHORT,
    )


def _exit(
    qty: float = 0.0001,
    intent: OrderIntent = OrderIntent.EXIT_STOP_LOSS,
    side: Side = Side.SELL,
) -> ProposedOrder:
    return ProposedOrder(
        symbol="BTCUSDT",
        side=side,
        quantity=qty,
        notional_usd=qty * 50000,
        margin_usd=5.0,
        leverage=10,
        intent=intent,
        reason="test exit",
        position_side=PositionSide.LONG,
    )


def _position(
    side: PositionSide = PositionSide.LONG,
    qty: float = 0.0001,
    entry: float = 50000.0,
) -> Position:
    # Default stop ≈ 10% for new risk model
    sl = entry * 0.90 if side == PositionSide.LONG else entry * 1.10
    tp = entry * 1.008 if side == PositionSide.LONG else entry * 0.992
    return Position(
        symbol="BTCUSDT",
        side=side,
        quantity=qty,
        entry_price=entry,
        entry_time=datetime.now(timezone.utc),
        stop_loss_price=sl,
        take_profit_price=tp,
        margin_usd=5.0,
        leverage=10,
        notional_usd=qty * entry,
    )


# ----- risk math: open 0.5 × 10 → ~$5 notional; RISK $0.50 → 10% stop -----


def test_risk_math_defaults():
    rm = RiskManager()
    assert rm.open_margin_usd == 0.5
    assert rm.target_margin_usd == 5.0
    assert rm.add_margin_usd == pytest.approx(4.5)
    assert rm.leverage == 10
    assert rm.risk_usd == 0.5
    assert rm.notional_usd == pytest.approx(5.0)
    assert rm.max_risk_usd == pytest.approx(0.50)
    assert rm.stop_loss_pct == pytest.approx(0.10)
    assert rm.max_daily_loss_usd == 2.0
    assert rm.margin_type == "ISOLATED"


def test_settings_risk_math(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("OPEN_MARGIN_USD", "0.5")
    monkeypatch.setenv("TARGET_MARGIN_USD", "5")
    monkeypatch.setenv("LEVERAGE", "10")
    monkeypatch.setenv("RISK_USD", "0.5")
    monkeypatch.chdir(tmp_path)
    s = load_settings()
    assert s.notional_usd == pytest.approx(5.0)
    assert s.max_risk_usd == pytest.approx(0.50)
    assert s.stop_loss_pct == pytest.approx(0.10)  # 0.50 / 5
    assert s.add_margin_usd == pytest.approx(4.5)


def test_settings_risk_from_pct_of_target(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("RISK_USD", raising=False)
    monkeypatch.setenv("TARGET_MARGIN_USD", "5")
    monkeypatch.setenv("OPEN_MARGIN_USD", "0.5")
    monkeypatch.setenv("RISK_PCT_OF_MARGIN", "0.10")
    s = load_settings()
    assert s.risk_usd == pytest.approx(0.50)


def test_stop_loss_price_long_and_short():
    rm = RiskManager(open_margin_usd=0.5, leverage=10, risk_usd=0.5)
    assert rm.stop_loss_price(100.0, PositionSide.LONG) == pytest.approx(90.0)
    assert rm.stop_loss_price(100.0, PositionSide.SHORT) == pytest.approx(110.0)


def test_rejects_invalid_construction():
    with pytest.raises(ValueError):
        RiskManager(open_margin_usd=0)
    with pytest.raises(ValueError):
        RiskManager(target_margin_usd=0.1, open_margin_usd=0.5)
    with pytest.raises(ValueError):
        RiskManager(leverage=11)
    with pytest.raises(ValueError):
        RiskManager(leverage=0)
    with pytest.raises(ValueError):
        RiskManager(max_leverage=20)
    with pytest.raises(ValueError):
        RiskManager(margin_type="CROSSED")
    with pytest.raises(ValueError):
        RiskManager(risk_usd=0)
    with pytest.raises(ValueError):
        RiskManager(max_daily_loss_usd=-1)


def test_approve_long_and_short_within_caps():
    rm = RiskManager()
    d_long = rm.approve(_entry_long())
    assert d_long.allowed
    d_short = rm.approve(_entry_short())
    assert d_short.allowed


def test_reject_legacy_large_notional():
    """Old $50 notional / $5 open margin must be rejected under new caps."""
    rm = RiskManager()
    d = rm.approve(_entry_long(notional=50.0, margin=5.0, leverage=10))
    assert not d.allowed


def test_reject_leverage_above_10():
    rm = RiskManager()
    order = _entry_long(leverage=11)
    d = rm.approve(order)
    assert not d.allowed
    assert "leverage" in d.reason.lower()


def test_reject_margin_over_open_cap():
    rm = RiskManager(open_margin_usd=0.5)
    d = rm.approve(_entry_long(margin=0.51, notional=5.1))
    assert not d.allowed
    assert "OPEN_MARGIN_USD" in d.reason


def test_reject_notional_over_cap():
    rm = RiskManager(open_margin_usd=0.5, leverage=10)
    d = rm.approve(_entry_long(notional=6.0, margin=0.5, leverage=10))
    assert not d.allowed
    assert "notional" in d.reason.lower()


def test_reject_pyramiding():
    rm = RiskManager()
    d = rm.approve(_entry_long(), open_position=_position())
    assert not d.allowed
    assert "pyramiding" in d.reason.lower()


def test_daily_loss_halts_new_entries_but_allows_exits(tmp_path: Path):
    state = tmp_path / "state.json"
    rm = RiskManager(max_daily_loss_usd=2.0, state_path=state)
    rm.record_realized_pnl(-2.0)
    assert rm.daily_loss_halted

    entry = rm.approve(_entry_long())
    assert not entry.allowed
    assert "daily loss halt" in entry.reason.lower()

    pos = _position()
    exit_d = rm.approve(_exit(), open_position=pos)
    assert exit_d.allowed


def test_daily_loss_exactly_at_cap_halts():
    rm = RiskManager(max_daily_loss_usd=2.0)
    rm.record_realized_pnl(-1.5)
    assert not rm.daily_loss_halted
    rm.record_realized_pnl(-0.5)
    assert rm.daily_loss_halted
    assert rm.realized_pnl_today == pytest.approx(-2.0)


def test_exit_requires_position():
    rm = RiskManager()
    d = rm.approve(_exit())
    assert not d.allowed
    assert "without open position" in d.reason


def test_exit_short_uses_buy():
    rm = RiskManager()
    pos = _position(side=PositionSide.SHORT)
    d = rm.approve(
        _exit(side=Side.BUY, intent=OrderIntent.EXIT_TAKE_PROFIT),
        open_position=pos,
    )
    assert d.allowed


def test_exit_wrong_side_rejected():
    rm = RiskManager()
    pos = _position(side=PositionSide.LONG)
    d = rm.approve(_exit(side=Side.BUY), open_position=pos)
    assert not d.allowed


def test_long_entry_must_be_buy():
    rm = RiskManager()
    bad = ProposedOrder(
        symbol="BTCUSDT",
        side=Side.SELL,
        quantity=0.0001,
        notional_usd=5,
        margin_usd=0.5,
        leverage=10,
        intent=OrderIntent.ENTRY,
        reason="bad",
        position_side=PositionSide.LONG,
    )
    d = rm.approve(bad)
    assert not d.allowed


def test_short_entry_must_be_sell():
    rm = RiskManager()
    bad = ProposedOrder(
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=0.0001,
        notional_usd=5,
        margin_usd=0.5,
        leverage=10,
        intent=OrderIntent.ENTRY,
        reason="bad",
        position_side=PositionSide.SHORT,
    )
    d = rm.approve(bad)
    assert not d.allowed


def test_reject_zero_quantity():
    rm = RiskManager()
    order = ProposedOrder(
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=0,
        notional_usd=0,
        margin_usd=0.5,
        leverage=10,
        intent=OrderIntent.ENTRY,
        reason="zero",
        position_side=PositionSide.LONG,
    )
    d = rm.approve(order)
    assert not d.allowed


def test_persistence_roundtrip(tmp_path: Path):
    path = tmp_path / "risk_state.json"
    rm1 = RiskManager(max_daily_loss_usd=2.0, state_path=path)
    rm1.record_realized_pnl(-1.25)
    rm2 = RiskManager(max_daily_loss_usd=2.0, state_path=path)
    assert rm2.realized_pnl_today == pytest.approx(-1.25)
    assert not rm2.daily_loss_halted


def test_settings_rejects_cross_margin(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MARGIN_TYPE", "CROSSED")
    with pytest.raises(ValueError, match="ISOLATED"):
        load_settings()


def test_settings_rejects_leverage_over_10(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LEVERAGE", "20")
    with pytest.raises(ValueError, match="LEVERAGE"):
        load_settings()


def test_per_trade_risk_approx_half_dollar():
    """Invariant: RISK_USD=$0.50 on $5 open notional → 10% stop."""
    rm = RiskManager(open_margin_usd=0.5, leverage=10, risk_usd=0.5)
    assert rm.max_risk_usd == pytest.approx(0.50)
    assert (rm.notional_usd * rm.stop_loss_pct) == pytest.approx(0.50)


def test_add_isolated_margin_dry_run():
    client = FuturesExchangeClient(dry_run=True, leverage=10)
    res = client.add_isolated_margin("BTCUSDT", 4.5)
    assert res["ok"] is True
    assert res["added"] == pytest.approx(4.5)
    assert res.get("dry_run") is True


def test_add_isolated_margin_zero_noop():
    client = FuturesExchangeClient(dry_run=True)
    res = client.add_isolated_margin("BTCUSDT", 0)
    assert res["ok"] is True
    assert res["added"] == 0.0


def test_add_isolated_margin_live_calls_api():
    client = FuturesExchangeClient(dry_run=True, leverage=10)
    # Simulate authenticated path without real keys
    client.dry_run = False
    mock = MagicMock()
    mock.futures_change_position_margin.return_value = {"code": 200, "msg": "success"}
    client._client = mock
    res = client.add_isolated_margin("BTCUSDT", 4.5)
    assert res["ok"] is True
    mock.futures_change_position_margin.assert_called_once()
    kwargs = mock.futures_change_position_margin.call_args.kwargs
    assert kwargs["symbol"] == "BTCUSDT"
    assert kwargs["amount"] == 4.5
    assert kwargs["type"] == 1
