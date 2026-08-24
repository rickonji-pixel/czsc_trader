from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from czsc_trader.four_layer_runner import (
    _PeriodEvaluator,
    all_holdout_windows_pass,
    build_optimizer_specs,
    coordinate_optimize,
    rank_optimizer_results,
    window_passes,
)


def _protocol() -> dict[str, object]:
    return {
        "steps": [0.025, 0.05],
        "rounds": [1, 2],
        "allow_sign_flip": [False, True],
        "enter_thresholds": [0.1, 0.15, 0.2],
        "exit_thresholds": [-0.05, 0.0, 0.05],
    }


def test_period_evaluator_reports_cache_stats(monkeypatch) -> None:
    index = pd.date_range("2021-01-01", periods=4, freq="D")
    daily = pd.DataFrame(
        {"dt": index, "open": [1.0] * 4, "close": [1.0] * 4},
        index=index,
    )
    target = pd.Series([0.0, 1.0, 1.0, 0.0], index=index)
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {"2021H1": SimpleNamespace(metrics={"strategy_return": 0.0})}

    monkeypatch.setattr("czsc_trader.four_layer_runner.run_period_backtests", fake_run)
    evaluator = _PeriodEvaluator(
        daily,
        {"2021H1": (index[0], index[-1])},
        0.0005,
        1_000_000,
    )

    evaluator.evaluate(target)
    evaluator.evaluate(target.copy())

    assert calls == 1
    assert evaluator.cache_stats == {"hits": 1, "misses": 1, "entries": 1}


def test_optimizer_grid_has_seventy_two_fixed_factor_configs() -> None:
    specs = build_optimizer_specs(_protocol())

    assert len(specs) == 72
    assert len({spec.optimizer_id for spec in specs}) == 72
    assert all(spec.exit < spec.enter for spec in specs)


def test_coordinate_search_preserves_every_nonzero_factor() -> None:
    start = pd.Series({"30m": 0.05, "daily": 0.75, "weekly": 0.20})

    result = coordinate_optimize(
        start,
        step=0.05,
        rounds=2,
        allow_sign_flip=True,
        minimum_absolute_weight=0.005,
        evaluator=lambda weights: (float(weights["weekly"]),),
    )

    assert result.index.tolist() == start.index.tolist()
    assert result.ne(0.0).all()
    assert result.abs().ge(0.005).all()
    assert abs(result.abs().sum() - 1.0) < 1e-12


def test_ranking_and_holdout_require_strict_dual_metric_wins() -> None:
    rows = pd.DataFrame(
        [
            {"optimizer_id": "a", "pass_count": 2, "min_return_delta": -0.02, "min_sharpe_delta": 0.1, "mean_return_delta": 0.2, "mean_sharpe_delta": 0.2, "weight_shift": 0.1},
            {"optimizer_id": "b", "pass_count": 2, "min_return_delta": -0.01, "min_sharpe_delta": -0.2, "mean_return_delta": 0.1, "mean_sharpe_delta": 0.1, "weight_shift": 0.2},
        ]
    )
    assert rank_optimizer_results(rows).iloc[0]["optimizer_id"] == "b"

    champion = {"strategy_return": 0.1, "sharpe": 1.0}
    assert window_passes(champion, {"strategy_return": 0.11, "sharpe": 1.01})
    assert not window_passes(champion, {"strategy_return": 0.1, "sharpe": 1.01})
    windows = {
        name: {"champion": champion, "challenger": {"strategy_return": 0.11, "sharpe": 1.01}}
        for name in ("2026Q1", "2026H1", "2026M1-M8")
    }
    assert all_holdout_windows_pass(windows)
    windows["2026H1"]["challenger"]["sharpe"] = 1.0
    assert not all_holdout_windows_pass(windows)

