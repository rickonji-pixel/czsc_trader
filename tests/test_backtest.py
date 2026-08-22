import pandas as pd
import pytest

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
