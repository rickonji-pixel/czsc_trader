from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from czsc_trader.strategy_metrics import annualized_sharpe, strategy_comparison_metrics


def test_annualized_sharpe_includes_first_session_return() -> None:
    equity = pd.Series([102.0, 101.0, 104.0])
    returns = pd.Series([0.02, 101.0 / 102.0 - 1.0, 104.0 / 101.0 - 1.0])
    expected = np.sqrt(252.0) * returns.mean() / returns.std(ddof=1)

    assert annualized_sharpe(equity, 100.0) == pytest.approx(expected)


def test_annualized_sharpe_rejects_degenerate_series() -> None:
    assert annualized_sharpe(pd.Series([100.0]), 100.0) is None
    assert annualized_sharpe(pd.Series([100.0, 100.0]), 100.0) is None


def test_annualized_sharpe_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="equity must be finite and non-empty"):
        annualized_sharpe(pd.Series([100.0, np.nan]), 100.0)
    with pytest.raises(ValueError, match="initial cash must be positive and finite"):
        annualized_sharpe(pd.Series([100.0, 101.0]), 0.0)


@pytest.mark.parametrize(
    ("prices", "expected_status", "expected_ratio"),
    [
        ([], "NO_CLOSED_TRADES", None),
        ([(10.0, 11.0)], "NO_LOSSES", None),
        ([(10.0, 9.0)], "NO_WINS", None),
        ([(10.0, 11.0), (10.0, 9.0)], "VALID", 1.0),
    ],
)
def test_comparison_metrics_preserves_win_loss_availability_reason(
    prices: list[tuple[float, float]],
    expected_status: str,
    expected_ratio: float | None,
) -> None:
    rows: list[dict[str, object]] = []
    for index, (entry, exit_) in enumerate(prices):
        rows.extend(
            [
                {
                    "signal_date": f"2026-01-{index * 2 + 1:02d}",
                    "execution_date": f"2026-01-{index * 2 + 2:02d}",
                    "side": "Buy",
                    "size": 100.0,
                    "price": entry,
                    "fees": 0.0,
                },
                {
                    "signal_date": f"2026-01-{index * 2 + 2:02d}",
                    "execution_date": f"2026-01-{index * 2 + 3:02d}",
                    "side": "Sell",
                    "size": 100.0,
                    "price": exit_,
                    "fees": 0.0,
                },
            ]
        )
    orders = pd.DataFrame(
        rows,
        columns=("signal_date", "execution_date", "side", "size", "price", "fees"),
    )

    metrics = strategy_comparison_metrics(
        pd.Series([100.0, 101.0, 99.0, 102.0]),
        orders,
        100.0,
    )

    assert metrics["win_loss_ratio_status"] == expected_status
    if expected_ratio is None:
        assert metrics["win_loss_ratio"] is None
    else:
        assert metrics["win_loss_ratio"] == pytest.approx(expected_ratio)
