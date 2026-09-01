from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.testing import assert_series_equal
import pytest

from czsc_trader.four_layer import score_four_layer
from czsc_trader.regime_weight import (
    candidate_relative_improvements,
    classify_regimes,
    closed_trade_ledger,
    fit_regime_threshold,
    lagged_efficiency_ratio,
    project_group_weights,
    risk_quality_metrics,
    score_with_regime_weights,
    strict_quality_pass,
)


BASE = pd.Series(
    [0.2, 0.1, 0.3, 0.4],
    index=["structure_a", "trend_a", "trend_b", "volume_a"],
    dtype=float,
)
GROUPS = {
    "structure": ("structure_a",),
    "trend": ("trend_a", "trend_b"),
    "volume_position": ("volume_a",),
}


def test_er_uses_only_previous_closes_and_frozen_threshold() -> None:
    index = pd.date_range("2025-01-01", periods=8)
    close = pd.Series(range(1, 9), index=index, dtype=float)
    er = lagged_efficiency_ratio(close, lookback=3)

    assert er.iloc[:4].isna().all()
    assert er.iloc[4] == pytest.approx(1.0)
    changed = close.copy()
    changed.iloc[4:] = 10_000.0
    assert lagged_efficiency_ratio(changed, 3).iloc[4] == er.iloc[4]

    threshold = fit_regime_threshold(
        pd.Series([np.nan, 0.2, 0.4], index=index[:3]), index[0], index[2]
    )
    assert threshold == pytest.approx(0.3)
    labels = classify_regimes(pd.Series([0.2, 0.3, np.nan]), threshold)
    assert labels.tolist() == ["range", "trend", "warmup"]


def test_group_projection_preserves_identity_sign_and_l1() -> None:
    projected = project_group_weights(BASE, GROUPS, 1.5, 0.5)

    assert list(projected.index) == list(BASE.index)
    assert projected.abs().sum() == pytest.approx(1.0)
    assert np.sign(projected).equals(np.sign(BASE))
    assert projected["trend_a"] / projected["trend_b"] == pytest.approx(
        BASE["trend_a"] / BASE["trend_b"]
    )


def test_all_one_multipliers_replay_baseline_score() -> None:
    factors = pd.DataFrame(
        [[1.0, -1.0, 1.0, 0.0], [0.0, 1.0, -1.0, 1.0]],
        columns=BASE.index,
        index=pd.date_range("2025-01-01", periods=2),
    )
    regimes = pd.Series(["trend", "range"], index=factors.index)
    same = project_group_weights(BASE, GROUPS, 1.0, 1.0)

    scores = score_with_regime_weights(
        factors, regimes, {"trend": same, "range": same}, same
    )

    assert_series_equal(scores, score_four_layer(factors, BASE))


def test_closed_trade_ledger_ignores_open_tail_and_deducts_fees() -> None:
    orders = pd.DataFrame(
        [
            {"signal_date": "2025-01-01", "execution_date": "2025-01-02", "side": "Buy", "size": 100.0, "price": 10.0, "fees": 1.0},
            {"signal_date": "2025-01-03", "execution_date": "2025-01-04", "side": "Sell", "size": 100.0, "price": 12.0, "fees": 1.2},
            {"signal_date": "2025-01-05", "execution_date": "2025-01-06", "side": "Buy", "size": 80.0, "price": 11.0, "fees": 0.88},
        ]
    )

    ledger = closed_trade_ledger(orders)

    assert len(ledger) == 1
    expected = (100 * 12 - 1.2) / (100 * 10 + 1.0) - 1
    assert ledger.iloc[0]["net_return"] == pytest.approx(expected)


def test_risk_quality_metrics_and_strict_gate() -> None:
    equity = pd.Series(
        [100.0, 110.0, 105.0, 120.0], index=pd.date_range("2025-01-01", periods=4)
    )
    orders = pd.DataFrame(
        [
            {"signal_date": "2025-01-01", "execution_date": "2025-01-02", "side": "Buy", "size": 10.0, "price": 10.0, "fees": 0.0},
            {"signal_date": "2025-01-02", "execution_date": "2025-01-03", "side": "Sell", "size": 10.0, "price": 11.0, "fees": 0.0},
            {"signal_date": "2025-01-03", "execution_date": "2025-01-04", "side": "Buy", "size": 10.0, "price": 10.0, "fees": 0.0},
            {"signal_date": "2025-01-04", "execution_date": "2025-01-05", "side": "Sell", "size": 10.0, "price": 9.0, "fees": 0.0},
        ]
    )
    metrics = risk_quality_metrics(equity, orders, init_cash=100.0)

    assert metrics["closed_trade_count"] == 2
    assert metrics["winning_trade_count"] == 1
    assert metrics["losing_trade_count"] == 1
    assert metrics["win_loss_ratio"] == pytest.approx(1.0)
    assert metrics["max_drawdown"] == pytest.approx(105 / 110 - 1)
    assert np.isfinite(float(metrics["calmar"]))

    baseline = {
        "max_drawdown": -0.20,
        "calmar": 1.0,
        "win_loss_ratio": 1.2,
        "closed_trade_count": 8,
        "has_wins_and_losses": True,
    }
    better = {
        "max_drawdown": -0.15,
        "calmar": 1.1,
        "win_loss_ratio": 1.3,
        "closed_trade_count": 5,
        "has_wins_and_losses": True,
    }
    assert strict_quality_pass(baseline, better, minimum_closed_trades=5)
    assert not strict_quality_pass(
        baseline, {**better, "calmar": baseline["calmar"]}, 5
    )
    assert not strict_quality_pass(baseline, {**better, "closed_trade_count": 3}, 5)
    relative = candidate_relative_improvements(baseline, better)
    assert relative["max_drawdown"] == pytest.approx(0.25)
    assert relative["calmar"] == pytest.approx(0.1)
    assert relative["win_loss_ratio"] == pytest.approx(1 / 12)


def test_invalid_order_sequence_is_rejected() -> None:
    orders = pd.DataFrame(
        [
            {"signal_date": "2025-01-01", "execution_date": "2025-01-02", "side": "Sell", "size": 1.0, "price": 10.0, "fees": 0.0}
        ]
    )
    with pytest.raises(ValueError, match="start with Buy"):
        closed_trade_ledger(orders)
