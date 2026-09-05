"""Unit tests for RiskManager hard caps."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradingbot.models import OrderIntent, Position, ProposedOrder, Side
from tradingbot.risk.manager import RiskManager


def _entry(notional: float = 40.0, qty: float = 0.001) -> ProposedOrder:
    return ProposedOrder(
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=qty,
        notional_usd=notional,
        intent=OrderIntent.ENTRY,
        reason="test entry",
    )


def _exit(qty: float = 0.001, intent: OrderIntent = OrderIntent.EXIT_STOP_LOSS) -> ProposedOrder:
    return ProposedOrder(
        symbol="BTCUSDT",
        side=Side.SELL,
        quantity=qty,
        notional_usd=qty * 50000,
        intent=intent,
        reason="test exit",
    )


def _position(qty: float = 0.001, entry: float = 50000.0) -> Position:
    return Position(
        symbol="BTCUSDT",
        quantity=qty,
        entry_price=entry,
        entry_time=datetime.now(timezone.utc),
        stop_loss_price=entry * 0.99,
        take_profit_price=entry * 1.008,
        quote_spent=qty * entry,
    )


def test_defaults():
    rm = RiskManager()
    assert rm.max_position_usd == 50.0
    assert rm.max_daily_loss_usd == 2.0
    assert rm.stop_loss_pct == 0.01


def test_rejects_invalid_construction():
    with pytest.raises(ValueError):
        RiskManager(max_position_usd=0)
    with pytest.raises(ValueError):
        RiskManager(max_daily_loss_usd=-1)
    with pytest.raises(ValueError):
        RiskManager(stop_loss_pct=0)


def test_approve_entry_within_cap():
    rm = RiskManager(max_position_usd=50)
    d = rm.approve(_entry(40), open_notional_usd=0)
    assert d.allowed
    assert "approved" in d.reason.lower()


def test_reject_entry_over_max_position():
    rm = RiskManager(max_position_usd=50)
    d = rm.approve(_entry(50.01), open_notional_usd=0)
    assert not d.allowed
    assert "MAX_POSITION_USD" in d.reason


def test_reject_entry_when_projected_exceeds_cap():
    rm = RiskManager(max_position_usd=50)
    # already somehow counting open notional (should also block pyramiding)
    d = rm.approve(_entry(30), open_position=_position(), open_notional_usd=30)
    assert not d.allowed
    assert "pyramiding" in d.reason.lower() or "MAX_POSITION" in d.reason


def test_reject_pyramiding():
    rm = RiskManager(max_position_usd=50)
    d = rm.approve(_entry(20), open_position=_position(), open_notional_usd=20)
    assert not d.allowed
    assert "pyramiding" in d.reason.lower()


def test_daily_loss_halts_new_entries_but_allows_exits(tmp_path: Path):
    state = tmp_path / "state.json"
    rm = RiskManager(max_daily_loss_usd=2.0, state_path=state)
    rm.record_realized_pnl(-2.0)
    assert rm.daily_loss_halted

    entry = rm.approve(_entry(20), open_notional_usd=0)
    assert not entry.allowed
    assert "daily loss halt" in entry.reason.lower()

    pos = _position()
    exit_d = rm.approve(_exit(), open_position=pos, open_notional_usd=pos.notional_usd)
    assert exit_d.allowed


def test_daily_loss_exactly_at_cap_halts():
    rm = RiskManager(max_daily_loss_usd=2.0)
    rm.record_realized_pnl(-1.5)
    assert not rm.daily_loss_halted
    rm.record_realized_pnl(-0.5)
    assert rm.daily_loss_halted
    assert rm.realized_pnl_today == pytest.approx(-2.0)


def test_spot_cannot_sell_without_position():
    rm = RiskManager()
    d = rm.approve(_exit())
    assert not d.allowed
    assert "cannot SELL" in d.reason


def test_spot_cannot_oversell():
    rm = RiskManager()
    pos = _position(qty=0.001)
    d = rm.approve(_exit(qty=0.002), open_position=pos)
    assert not d.allowed
    assert "exceeds position" in d.reason


def test_entry_must_be_buy():
    rm = RiskManager()
    bad = ProposedOrder(
        symbol="BTCUSDT",
        side=Side.SELL,
        quantity=0.001,
        notional_usd=40,
        intent=OrderIntent.ENTRY,
        reason="bad",
    )
    # Without position, SELL fails first; with weird combo still rejected
    d = rm.approve(bad)
    assert not d.allowed


def test_stop_loss_price_long():
    rm = RiskManager(stop_loss_pct=0.01)
    assert rm.stop_loss_price(100.0) == pytest.approx(99.0)


def test_reject_zero_quantity():
    rm = RiskManager()
    order = ProposedOrder(
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=0,
        notional_usd=0,
        intent=OrderIntent.ENTRY,
        reason="zero",
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


def test_exit_take_profit_allowed():
    rm = RiskManager()
    pos = _position()
    d = rm.approve(
        _exit(intent=OrderIntent.EXIT_TAKE_PROFIT),
        open_position=pos,
    )
    assert d.allowed
