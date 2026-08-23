import pandas as pd
import pytest

from czsc_trader.diagnostics import build_trade_diagnostics


def test_trade_diagnostics_pair_round_trip_and_include_fees() -> None:
    """Catch a completed round trip omitting either execution fee."""
    dates = pd.bdate_range("2026-07-01", periods=5)
    orders = pd.DataFrame(
        [
            {
                "period": "sample",
                "signal_date": dates[0],
                "execution_date": dates[1],
                "side": "Buy",
                "size": 100.0,
                "price": 10.0,
                "fees": 1.0,
                "factor_event_id": "entry",
            },
            {
                "period": "sample",
                "signal_date": dates[1],
                "execution_date": dates[2],
                "side": "Sell",
                "size": 100.0,
                "price": 11.0,
                "fees": 1.1,
                "factor_event_id": "exit",
            },
        ]
    )

    diagnostics = build_trade_diagnostics(orders, dates)

    assert len(diagnostics) == 1
    row = diagnostics.iloc[0]
    assert row["holding_trading_days"] == 1
    assert row["net_return"] == pytest.approx((1098.9 / 1001.0) - 1.0)
    assert bool(row["one_signal_day_exit"]) is True
    assert bool(row["july_august_2026"]) is True


def test_trade_diagnostics_ignore_unclosed_entry() -> None:
    """Catch an open position being fabricated into a completed round trip."""
    dates = pd.bdate_range("2026-01-02", periods=2)
    orders = pd.DataFrame(
        [
            {
                "period": "sample",
                "signal_date": dates[0],
                "execution_date": dates[1],
                "side": "Buy",
                "size": 100.0,
                "price": 10.0,
                "fees": 1.0,
                "factor_event_id": "entry",
            }
        ]
    )

    diagnostics = build_trade_diagnostics(orders, dates)

    assert diagnostics.empty
