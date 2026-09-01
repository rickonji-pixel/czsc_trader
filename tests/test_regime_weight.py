from __future__ import annotations

from copy import deepcopy
import json

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
from czsc_trader.regime_weight_runner import (
    candidate_grid,
    rank_research_candidates,
    research_candidate_passes,
    run_regime_weight_experiment,
    validate_regime_weight_protocol,
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


def _valid_protocol() -> dict[str, object]:
    return {
        "schema_version": 1,
        "experiment_id": "0901_EX19",
        "handler": "regime_conditioned_weight_challenge",
        "experiment_type": "regime_conditioned_weight_challenge",
        "status": "PRE_REGISTERED",
        "symbol": "588080.SH",
        "asset_type": "etf",
        "baseline": {
            "version": "baseline_20260826",
            "sha256": "fc22ca5a973f77faf528cdb3efba4900163e18fe0c08cf79234d79ef22f5c822",
        },
        "calibration_start": "2020-01-01",
        "research_start": "2021-01-01",
        "research_end": "2025-12-31",
        "test_start": "2026-01-01",
        "test_end": "2026-08-28",
        "er_lookback": 60,
        "regime_labels": ["trend", "range", "warmup"],
        "er_threshold_method": "research_valid_median",
        "group_reference": "structure",
        "multipliers": [0.5, 0.75, 1.0, 1.25, 1.5],
        "candidate_count": 625,
        "entry_threshold": 0.175,
        "exit_threshold": 0.025,
        "fee_rate": 0.0005,
        "init_cash": 1_000_000.0,
        "research_min_closed_trades": 20,
        "test_min_closed_trades": 5,
        "annual_double_win_minimum": 3,
        "tolerance": 1e-12,
        "access_2026_after_freeze_only": True,
        "report_only_metrics": ["strategy_return", "sharpe", "exposure", "trade_count"],
    }


def _corrected_protocol() -> dict[str, object]:
    protocol = _valid_protocol()
    protocol.update(
        {
            "experiment_id": "0901_EX20",
            "selection_metrics": ["max_drawdown", "calmar", "win_loss_ratio"],
            "annual_metrics_role": "report_only",
            "ranking": [
                "maximin_improvement_desc",
                "weight_shift_asc",
                "candidate_id_asc",
            ],
            "report_only_metrics": [
                "strategy_return",
                "sharpe",
                "exposure",
                "trade_count",
                "annual_metrics",
            ],
        }
    )
    del protocol["annual_double_win_minimum"]
    return protocol


def test_protocol_freezes_research_test_and_grid_boundaries() -> None:
    protocol = _valid_protocol()
    validate_regime_weight_protocol(protocol)
    for key, value in (
        ("er_lookback", 59),
        ("research_end", "2026-01-01"),
        ("test_end", "2026-08-29"),
        ("candidate_count", 624),
    ):
        changed = deepcopy(protocol)
        changed[key] = value
        with pytest.raises(ValueError):
            validate_regime_weight_protocol(changed)


def test_corrected_protocol_makes_annual_metrics_report_only() -> None:
    protocol = _corrected_protocol()
    validate_regime_weight_protocol(protocol)

    changed = deepcopy(protocol)
    changed["annual_double_win_minimum"] = 3
    with pytest.raises(ValueError, match="annual_double_win_minimum"):
        validate_regime_weight_protocol(changed)


def test_grid_has_625_stable_unique_candidates_and_baseline() -> None:
    grid = candidate_grid((0.5, 0.75, 1.0, 1.25, 1.5))

    assert len(grid) == len(set(grid)) == 625
    assert grid[0] == (0.5, 0.5, 0.5, 0.5)
    assert (1.0, 1.0, 1.0, 1.0) in grid


def test_research_gate_and_rank_use_annual_support_then_maximin() -> None:
    baseline = {
        "max_drawdown": -0.2,
        "calmar": 1.0,
        "win_loss_ratio": 1.0,
        "closed_trade_count": 30,
        "has_wins_and_losses": True,
    }
    challenger = {
        "max_drawdown": -0.15,
        "calmar": 1.2,
        "win_loss_ratio": 1.1,
        "closed_trade_count": 20,
        "has_wins_and_losses": True,
    }
    assert research_candidate_passes(baseline, challenger, annual_double_wins=3)
    assert not research_candidate_passes(baseline, challenger, annual_double_wins=2)

    rows = pd.DataFrame(
        [
            {"candidate_id": 2, "pass": True, "maximin_improvement": 0.10, "annual_double_wins": 4, "weight_shift": 0.1},
            {"candidate_id": 1, "pass": True, "maximin_improvement": 0.11, "annual_double_wins": 3, "weight_shift": 0.2},
            {"candidate_id": 0, "pass": False, "maximin_improvement": 9.00, "annual_double_wins": 5, "weight_shift": 0.0},
        ]
    )
    ranked = rank_research_candidates(rows)
    assert ranked["candidate_id"].tolist() == [1, 2, 0]
    assert ranked["rank"].tolist() == [1, 2, 3]


def test_corrected_gate_uses_three_metrics_and_ranking_ignores_annual_results() -> None:
    baseline = {
        "max_drawdown": -0.2,
        "calmar": 1.0,
        "win_loss_ratio": 1.0,
        "closed_trade_count": 30,
        "has_wins_and_losses": True,
    }
    challenger = {
        "max_drawdown": -0.15,
        "calmar": 1.2,
        "win_loss_ratio": 1.1,
        "closed_trade_count": 20,
        "has_wins_and_losses": True,
    }
    assert research_candidate_passes(
        baseline,
        challenger,
        annual_double_wins=0,
        annual_minimum=None,
    )

    rows = pd.DataFrame(
        [
            {"candidate_id": 2, "pass": True, "maximin_improvement": 0.10, "annual_double_wins": 5, "weight_shift": 0.1},
            {"candidate_id": 1, "pass": True, "maximin_improvement": 0.11, "annual_double_wins": 0, "weight_shift": 0.2},
            {"candidate_id": 0, "pass": False, "maximin_improvement": 9.00, "annual_double_wins": 5, "weight_shift": 0.0},
        ]
    )
    ranked = rank_research_candidates(rows, annual_support_tiebreak=False)
    assert ranked["candidate_id"].tolist() == [1, 2, 0]


def test_runner_rejects_protocol_before_market_data_access(tmp_path) -> None:
    experiment = tmp_path / "0901_EX19"
    artifacts = experiment / "artifacts"
    artifacts.mkdir(parents=True)
    bad = _valid_protocol()
    bad["research_end"] = "2026-01-01"
    (artifacts / "protocol.json").write_text(
        json.dumps(bad, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="research_end"):
        run_regime_weight_experiment(
            tmp_path / "raw",
            tmp_path / "baselines",
            experiment,
            execution_commit="deadbeef",
        )
