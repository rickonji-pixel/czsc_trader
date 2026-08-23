import pandas as pd
import pytest

import czsc_trader.backtest as backtest
from czsc_trader.backtest import run_backtest


@pytest.fixture
def tiny_daily() -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", periods=5)
    return pd.DataFrame(
        {
            "dt": dates,
            "open": [100.0, 102.0, 104.0, 103.0, 105.0],
            "high": [102.0, 104.0, 105.0, 105.0, 107.0],
            "low": [99.0, 101.0, 102.0, 102.0, 104.0],
            "close": [101.0, 103.0, 103.0, 104.0, 106.0],
        }
    )


def test_signal_executes_at_next_open(tiny_daily: pd.DataFrame) -> None:
    """Catch same-day fills or use of close instead of next open."""
    dates = pd.DatetimeIndex(tiny_daily["dt"])
    target = pd.Series([0.0, 1.0, 1.0, 0.0, 0.0], index=dates)

    result = run_backtest(tiny_daily, target)

    first = result.orders.iloc[0]
    assert first["signal_date"] == dates[1]
    assert first["execution_date"] == dates[2]
    assert first["price"] == 104.0
    assert (result.orders["signal_date"] < result.orders["execution_date"]).all()


def test_costed_equity_matches_hand_calculation(tiny_daily: pd.DataFrame) -> None:
    """Catch missing entry/exit fees or incorrect target-percent sizing."""
    dates = pd.DatetimeIndex(tiny_daily["dt"])
    target = pd.Series([0.0, 1.0, 1.0, 0.0, 0.0], index=dates)

    result = run_backtest(tiny_daily, target, fee_rate=0.0005, init_cash=1_000_000.0)

    shares = 1_000_000.0 / (104.0 * 1.0005)
    expected_terminal = shares * 105.0 * (1.0 - 0.0005)
    assert result.equity.iloc[-1] == pytest.approx(expected_terminal, rel=1e-10)
    assert result.metrics["trade_count"] == 2


def test_target_positions_must_be_long_or_cash(tiny_daily: pd.DataFrame) -> None:
    """Catch accidental leverage or short orders."""
    dates = pd.DatetimeIndex(tiny_daily["dt"])
    target = pd.Series([0.0, 1.0, 1.2, 0.0, 0.0], index=dates)

    with pytest.raises(ValueError, match="long/cash"):
        run_backtest(tiny_daily, target)


def test_period_backtests_reset_cash_and_use_only_prior_signal_on_first_open() -> None:
    """Catch target periods inheriting the continuous portfolio's old holdings."""
    assert hasattr(backtest, "run_period_backtests")
    dates = pd.to_datetime(["2025-12-31", "2026-01-02", "2026-01-05"])
    daily = pd.DataFrame(
        {
            "dt": dates,
            "open": [50.0, 100.0, 120.0],
            "close": [50.0, 110.0, 120.0],
        }
    )
    target = pd.Series([1.0, 0.0, 0.0], index=dates)
    factor_frame = pd.DataFrame(
        {
            "structure": [0.5, -0.5, -0.5],
            "trend": [0.5, -0.5, -0.5],
            "volume_position": [0.0, 0.0, 0.0],
            "factor_score": [0.4, -0.4, -0.4],
            "enter_threshold": [0.2, 0.2, 0.2],
            "exit_threshold": [0.0, 0.0, 0.0],
        },
        index=dates,
    )
    factor_events = pd.DataFrame(
        {
            "event_id": ["Factor:20260102:Exit"],
            "signal_date": [dates[1]],
            "event_type": ["Exit"],
            "factor_score": [-0.4],
            "after_position": [0.0],
        }
    )
    periods = {
        "short": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")),
        "long": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-05")),
    }

    results = backtest.run_period_backtests(
        daily,
        target,
        periods,
        fee_rate=0.0005,
        init_cash=1_000_000.0,
        factor_events=factor_events,
        factor_frame=factor_frame,
        return_targets={"short": 0.0, "long": 0.0},
    )

    expected_first_close_equity = 1_000_000.0 / (100.0 * 1.0005) * 110.0
    for result in results.values():
        assert result.equity.iloc[0] == pytest.approx(expected_first_close_equity)
        assert result.orders.iloc[0]["signal_date"] == pd.Timestamp("2025-12-31")
        assert result.orders.iloc[0]["execution_date"] == pd.Timestamp("2026-01-02")
        assert result.orders.iloc[0]["event_type"] == "InitialEntry"
        assert result.orders.iloc[0]["factor_event_id"].startswith("InitialEntry:")
    assert results["short"].metrics["buyhold_return"] == pytest.approx(
        expected_first_close_equity / 1_000_000.0 - 1.0
    )


def test_regular_orders_reference_matching_factor_transition() -> None:
    """Catch a normal trade being exported without its CZSC factor event."""
    dates = pd.to_datetime(["2025-12-31", "2026-01-02", "2026-01-05", "2026-01-06"])
    daily = pd.DataFrame({"dt": dates, "open": [10.0, 10.0, 11.0, 10.0], "close": [10.0, 10.5, 10.0, 9.5]})
    target = pd.Series([0.0, 1.0, 1.0, 0.0], index=dates)
    factor_frame = pd.DataFrame(
        {
            "structure": [0.0, 0.5, 0.5, -0.5],
            "trend": [0.0, 0.5, 0.5, -0.5],
            "volume_position": [0.0, 0.0, 0.0, 0.0],
            "factor_score": [0.0, 0.4, 0.4, -0.4],
            "enter_threshold": [0.2] * 4,
            "exit_threshold": [0.0] * 4,
        },
        index=dates,
    )
    factor_events = pd.DataFrame(
        {
            "event_id": ["Factor:20260102:Entry", "Factor:20260106:Exit"],
            "signal_date": [dates[1], dates[3]],
            "event_type": ["Entry", "Exit"],
            "factor_score": [0.4, -0.4],
        }
    )

    result = backtest.run_period_backtests(
        daily,
        target,
        {"sample": (dates[1], dates[3])},
        factor_events=factor_events,
        factor_frame=factor_frame,
        return_targets={"sample": 0.0},
    )["sample"]

    assert result.orders.iloc[0]["factor_event_id"] == "Factor:20260102:Entry"
    assert result.orders.iloc[0]["event_type"] == "Entry"


def test_period_pass_uses_absolute_target_not_buyhold() -> None:
    """Catch final acceptance reverting to a strategy-versus-Buy-Hold comparison."""
    dates = pd.to_datetime(["2025-12-31", "2026-01-02", "2026-01-05"])
    daily = pd.DataFrame(
        {
            "dt": dates,
            "open": [100.0, 100.0, 100.0],
            "close": [100.0, 100.0, 200.0],
        }
    )
    target = pd.Series([0.0, 0.0, 0.0], index=dates)

    result = backtest.run_period_backtests(
        daily,
        target,
        {"sample": (dates[1], dates[2])},
        return_targets={"sample": 0.0},
    )["sample"]

    assert result.metrics["strategy_return"] == 0.0
    assert float(result.metrics["buyhold_return"]) > 0.9
    assert result.metrics["target_return"] == 0.0
    assert result.metrics["target_margin"] == 0.0
    assert result.metrics["pass"] is True
