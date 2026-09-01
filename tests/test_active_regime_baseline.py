from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import resolve_baseline


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = REPO_ROOT / "configs" / "rule_baselines"


def test_latest_baseline_is_candidate143_regime_strategy() -> None:
    baseline = resolve_baseline(BASELINE_ROOT, symbol="588080.SH")

    assert baseline.version == "baseline_20260901"
    assert baseline.strategy == "czsc_regime_weight"
    assert baseline.status == "active"
    assert baseline.source_path == "experiments/0901_EX20/artifacts/frozen_challenger.json"
    assert baseline.rule_payload["candidate_id"] == 143
    assert baseline.er_lookback == 60
    assert baseline.er_threshold == pytest.approx(0.12654614652711713)
    assert set(baseline.regime_factor_weights) == {"trend", "range"}


def test_previous_four_layer_baseline_is_archived_but_explicitly_resolvable() -> None:
    baseline = resolve_baseline(
        BASELINE_ROOT,
        "baseline_20260826",
        symbol="588080.SH",
    )

    assert baseline.status == "archived"
    assert baseline.strategy == "czsc_four_layer"


def test_regime_baseline_requires_causal_daily_close_series() -> None:
    baseline = resolve_baseline(BASELINE_ROOT, symbol="588080.SH")
    frame = pd.DataFrame(columns=baseline.factor_names)

    with pytest.raises(ValueError, match="daily close"):
        apply_resolved_baseline(frame, baseline)
