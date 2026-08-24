from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.attribution_runner import run_attribution_from_frames
from czsc_trader.factors import aggregate_signal_groups, map_signal_frame, signal_groups
from czsc_trader.rules import Rule


def _protocol() -> dict[str, object]:
    return {
        "experiment_id": "test_attribution",
        "visible_sample_end": "2025-12-31",
        "holdout_access_allowed": False,
        "primary_windows": ["2021", "2022", "2023", "2024", "2025"],
        "diagnostic_windows": [
            f"{year}H{half}" for year in range(2021, 2026) for half in (1, 2)
        ],
        "materiality": {
            "return_delta": 0.001,
            "sharpe_delta": 0.02,
            "stable_year_count": 4,
            "dominant_position_difference_share": 0.5,
        },
        "state_sample_requirement": {
            "minimum_total_days": 20,
            "minimum_days_per_year": 5,
            "minimum_years": 3,
        },
        "weight_sensitivity": {"step": 0.05},
        "threshold_sensitivity": {
            "entry": [0.1, 0.15, 0.2],
            "exit": [-0.05, 0.0, 0.05],
            "combine_families": False,
        },
        "state_machine_sensitivity": {
            "entry_confirm_days": [1, 2],
            "min_hold_days": [2, 3, 4],
            "exit_confirm_days": [1, 2],
            "combine_families": False,
        },
        "promotion": {
            "select_challenger": False,
            "write_frozen_challenger": False,
            "update_champion": False,
            "force_negative_factor": False,
        },
    }


def test_frame_runner_writes_complete_diagnostic_without_promoting(tmp_path: Path) -> None:
    """Catch incomplete artifacts, holdout leakage, or accidental challenger promotion."""
    index = pd.date_range("2020-12-31", "2025-12-31", freq="BME")
    prices = 10.0 + np.linspace(0.0, 3.0, len(index)) + np.sin(np.arange(len(index)))
    daily = pd.DataFrame(
        {
            "dt": index,
            "open": prices,
            "close": prices + np.cos(np.arange(len(index))) * 0.1,
            "high": prices + 0.2,
            "low": prices - 0.2,
        }
    )
    states = np.where(np.arange(len(index)) % 2 == 0, "多头_任意_任意_0", "空头_任意_任意_0")
    raw = pd.DataFrame(
        {
            "raw__daily__cxt_demo": states,
            "raw__daily__tas_ma_demo": states,
            "raw__daily__vol_window_demo": states,
        },
        index=index.rename("dt"),
    )
    mapped, unknown = map_signal_frame(raw)
    memberships = signal_groups(mapped.columns)
    grouped = aggregate_signal_groups(mapped, memberships)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "protocol.json").write_text("{}\n", encoding="utf-8")

    summary = run_attribution_from_frames(
        daily,
        raw,
        mapped,
        memberships,
        grouped,
        Rule((0.3, 0.3, 0.4), 0.15, 0.0, 1, 3, 1, "none"),
        artifacts,
        _protocol(),
        data_hashes={"588080_daily_2025.csv": "abc"},
        unknown_values=unknown,
    )

    expected = {
        "factor_catalog.csv",
        "window_metrics.csv",
        "group_attribution.csv",
        "signal_attribution.csv",
        "state_attribution.csv",
        "weight_sensitivity.csv",
        "threshold_sensitivity.csv",
        "state_machine_sensitivity.csv",
        "classification.csv",
        "trade_diagnostics.csv",
        "metrics.json",
        "attribution_heatmap.html",
    }
    assert expected <= {path.name for path in artifacts.iterdir()}
    metrics = json.loads((artifacts / "metrics.json").read_text(encoding="utf-8"))
    assert summary["status"] == "COMPLETE"
    assert metrics["holdout_accessed"] is False
    assert metrics["frozen_challenger"] is None
    assert set(metrics["visible_data_hashes"]) == {"588080_daily_2025.csv"}
    assert not (artifacts / "frozen_challenger.json").exists()
    assert "plotly.js v" in (artifacts / "attribution_heatmap.html").read_text(
        encoding="utf-8"
    ).lower()
