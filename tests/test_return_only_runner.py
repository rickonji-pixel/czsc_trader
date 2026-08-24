from __future__ import annotations

import pandas as pd
import numpy as np

from czsc_trader.return_only_runner import (
    build_return_specs,
    half_year_periods,
    rank_return_results,
    return_holdout_pass,
    return_objective,
    training_windows_before,
)


def _protocol() -> dict[str, object]:
    return {
        "steps": [0.0125, 0.025, 0.05],
        "rounds": [1, 2],
        "allow_sign_flip": [False, True],
        "enter_thresholds": [0.125, 0.15, 0.175],
        "exit_thresholds": [-0.025, 0.0, 0.025, 0.05],
    }


def test_return_grid_has_144_deterministic_configs() -> None:
    specs = build_return_specs(_protocol())

    assert len(specs) == 144
    assert len({spec.spec_id for spec in specs}) == 144
    assert all(spec.exit < spec.enter for spec in specs)


def test_return_objective_ignores_sharpe_and_prioritizes_worst_return() -> None:
    champion = {
        "a": {"strategy_return": 0.10, "sharpe": 10.0},
        "b": {"strategy_return": 0.10, "sharpe": 10.0},
    }
    challenger = {
        "a": {"strategy_return": 0.12, "sharpe": -99.0},
        "b": {"strategy_return": 0.11, "sharpe": -99.0},
    }
    weights = pd.Series({"x": 0.4, "y": 0.6})

    objective = return_objective(champion, challenger, weights, weights)

    assert objective[0] == 2.0
    np.testing.assert_allclose(objective[1:4], [0.01, 0.015, 0.015])


def test_return_ranking_and_holdout_use_no_risk_metrics() -> None:
    rows = pd.DataFrame(
        [
            {"spec_id": "mean", "win_count": 7, "min_return_delta": -0.02, "median_return_delta": 0.3, "mean_return_delta": 0.3, "weight_shift": 0.1, "sharpe": 100.0},
            {"spec_id": "worst", "win_count": 7, "min_return_delta": -0.01, "median_return_delta": 0.1, "mean_return_delta": 0.1, "weight_shift": 0.2, "sharpe": -100.0},
        ]
    )
    assert rank_return_results(rows).iloc[0]["spec_id"] == "worst"

    windows = {
        name: {"champion_return": 0.1, "challenger_return": 0.11, "challenger_sharpe": -99.0}
        for name in ("2026Q1", "2026H1", "2026M1-M8")
    }
    assert return_holdout_pass(windows)
    windows["2026H1"]["challenger_return"] = 0.1
    assert not return_holdout_pass(windows)


def test_half_year_training_uses_only_strictly_prior_windows() -> None:
    periods = half_year_periods(2021, 2025)

    assert list(periods) == [
        "2021H1", "2021H2", "2022H1", "2022H2", "2023H1",
        "2023H2", "2024H1", "2024H2", "2025H1", "2025H2",
    ]
    assert periods["2024H1"] == (pd.Timestamp("2024-01-01"), pd.Timestamp("2024-06-30"))
    assert training_windows_before(periods, "2022H1") == ("2021H1", "2021H2")
    assert training_windows_before(periods, "2025H2")[-1] == "2025H1"
