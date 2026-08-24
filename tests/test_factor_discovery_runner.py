from __future__ import annotations

import pandas as pd
import pytest

from czsc_trader.factor_discovery_runner import (
    build_comparison_window,
    build_factor_specs,
    factor_holdout_pass,
    training_windows_before,
    validate_factor_protocol,
)
from czsc_trader.return_only_runner import half_year_periods


def _protocol() -> dict[str, object]:
    return {
        "experiment_type": "state_expanded_factor_discovery",
        "status": "PRE_REGISTERED",
        "selection_sample_end": "2025-12-31",
        "validation_windows": [
            "2022H1", "2022H2", "2023H1", "2023H2",
            "2024H1", "2024H2", "2025H1", "2025H2",
        ],
        "holdout_windows": ["2026Q1", "2026H1", "2026M1-M8"],
        "weight_steps": [0.025, 0.05],
        "coordinate_rounds": [1, 2],
        "enter_thresholds": [0.125, 0.15, 0.175, 0.2],
        "exit_thresholds": [-0.025, 0.0, 0.025, 0.05],
        "algorithm_config_count": 64,
        "selection_metric": "strategy_return_only",
        "holdout_access_before_freeze": False,
        "new_daily_signals": [f"signal_{index}" for index in range(12)],
        "state_min_coverage": 0.8,
        "state_min_active_days": 30,
        "state_max_active_ratio": 0.9,
        "interaction_count": 4,
        "maximum_active_factors": 18,
        "minimum_absolute_weight": 0.0125,
        "minimum_trend_weight": 0.1,
        "protect_volume_window": True,
    }


def test_protocol_and_grid_freeze_exact_sixty_four_configs() -> None:
    protocol = _protocol()

    validate_factor_protocol(protocol)
    specs = build_factor_specs(protocol)

    assert len(specs) == 64
    assert len({spec.spec_id for spec in specs}) == 64
    assert all(spec.exit < spec.enter for spec in specs)

    protocol["selection_metric"] = "sharpe"
    with pytest.raises(ValueError, match="return only"):
        validate_factor_protocol(protocol)


def test_training_windows_are_strictly_before_validation() -> None:
    periods = half_year_periods(2021, 2025)

    assert training_windows_before(periods, "2022H1") == ("2021H1", "2021H2")
    assert all(periods[name][1] < periods["2024H2"][0] for name in training_windows_before(periods, "2024H2"))


def test_holdout_pass_uses_only_ex04_return() -> None:
    windows = {
        name: {
            "ex04_return": 0.10,
            "challenger_return": 0.11,
            "ex04_sharpe": 100.0,
            "challenger_sharpe": -100.0,
        }
        for name in ("2026Q1", "2026H1", "2026M1-M8")
    }

    assert factor_holdout_pass(windows)
    windows["2026H1"]["challenger_return"] = 0.10
    assert not factor_holdout_pass(windows)


def test_comparison_window_reports_three_baselines_and_challenger() -> None:
    ex04 = {"strategy_return": 0.20, "sharpe": 2.0, "exposure": 0.5, "max_drawdown": -0.1, "trade_count": 4}
    legacy = {"strategy_return": 0.25, "sharpe": 2.5, "exposure": 0.6, "max_drawdown": -0.2, "trade_count": 6}
    buyhold = {"strategy_return": 0.10, "sharpe": 1.0, "exposure": 1.0, "max_drawdown": -0.3, "trade_count": 1}
    challenger = {"strategy_return": 0.21, "sharpe": -9.0, "exposure": 0.4, "max_drawdown": -0.05, "trade_count": 3}

    row = build_comparison_window(ex04, legacy, buyhold, challenger)

    assert set(row) >= {"ex04", "legacy_champion", "buyhold", "challenger"}
    assert row["return_delta_vs_ex04"] == pytest.approx(0.01)
    assert row["pass"] is True
    assert row["challenger"]["sharpe"] == -9.0
