from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from trading_execution_engine import OrderSpec, execute_target_positions, resolve_fill


def test_ft_txe01_limit_touch_policy_is_explicit_and_slippage_is_adverse() -> None:
    session = datetime(2026, 9, 18, 9, 30)
    touch = datetime(2026, 9, 18, 10, 0)
    buy = resolve_fill(
        OrderSpec("BUY", "LIMIT", 100, 10.0),
        session_open=10.2,
        session_time=session,
        intraday_touches=[(touch, 10.0)],
        slippage_bp=10,
        inclusive_touch=True,
    )
    sell = resolve_fill(
        OrderSpec("SELL", "MARKET", 100),
        session_open=10.0,
        session_time=session,
        intraday_touches=[],
        slippage_bp=10,
    )
    assert buy.filled and buy.trigger == "INTRADAY_LIMIT"
    assert buy.price == pytest.approx(10.01)
    assert sell.price == pytest.approx(9.99)
    conservative = resolve_fill(
        OrderSpec("BUY", "LIMIT", 100, 10.0),
        session_open=10.2,
        session_time=session,
        intraday_touches=[(touch, 10.0)],
    )
    assert not conservative.filled


def test_ft_txe02_daily_ledger_preserves_cash_and_costs() -> None:
    index = pd.to_datetime(["2026-09-17", "2026-09-18", "2026-09-21"])
    prices = pd.DataFrame(
        {"open": [10.0, 10.0, 11.0], "close": [10.0, 10.5, 11.0]}, index=index
    )
    target = pd.Series([0.0, 1.0, 0.0], index=index)
    result = execute_target_positions(
        prices, target, fee_rate=0.001, initial_cash=100_000.0
    )
    assert list(result.orders["side"]) == ["BUY", "SELL"]
    assert result.equity.iloc[-1] == pytest.approx(109_780.21978021978)
    assert result.state.iloc[-1]["quantity"] == pytest.approx(0.0)
