from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from czsc_trader.ex04_attribution_runner import (
    classify_local_geometry,
    component_attribution,
    difference_intervals,
    market_regimes,
    normalize_cash_sharpe,
    paired_circular_block_bootstrap,
    perturb_weight,
    validate_protocol,
)


def test_component_attribution_uses_exact_two_factor_shapley() -> None:
    """Catch weight/threshold main effects being confused with their interaction."""
    rows = pd.DataFrame(
        [
            {"window": "2021", "variant": "baseline", "strategy_return": 0.0, "sharpe": 0.0},
            {"window": "2021", "variant": "weights_only", "strategy_return": 2.0, "sharpe": 20.0},
            {"window": "2021", "variant": "thresholds_only", "strategy_return": 3.0, "sharpe": 30.0},
            {"window": "2021", "variant": "combined", "strategy_return": 7.0, "sharpe": 70.0},
        ]
    )

    result = component_attribution(rows).set_index("component")

    assert result.loc["weights", "return_contribution"] == pytest.approx(3.0)
    assert result.loc["thresholds", "return_contribution"] == pytest.approx(4.0)
    assert result.loc["interaction", "return_contribution"] == pytest.approx(2.0)
    assert result.loc["weights", "sharpe_contribution"] == pytest.approx(30.0)
    assert result.loc["thresholds", "sharpe_contribution"] == pytest.approx(40.0)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (0.1, [0.3, 0.2625, 0.4375]),
        (-0.1, [0.1, 0.3375, 0.5625]),
    ],
)
def test_perturb_weight_redistributes_proportionally(
    delta: float, expected: list[float]
) -> None:
    """Catch local perturbations changing L1 mass or another weight's sign."""
    weights = pd.Series([0.2, 0.3, 0.5], index=["a", "b", "c"])

    result = perturb_weight(weights, "a", delta)

    assert result.to_list() == pytest.approx(expected)
    assert float(result.sum()) == pytest.approx(1.0)
    assert bool(result.gt(0.0).all())


def test_local_geometry_applies_sharp_peak_before_plateau() -> None:
    """Catch overlapping local rules silently labeling a preregistered peak as plateau."""
    rows = pd.DataFrame(
        {
            "variant_id": [f"v{i}" for i in range(32)],
            "median_return_delta": [-0.002] * 32,
            "min_return_delta": [-0.06] * 16 + [-0.04] * 16,
        }
    )
    rules = {
        "nearby_median_absolute_return_delta": 0.01,
        "nearby_min_return_delta": -0.05,
        "plateau_min_count": 24,
        "peak_negative_median_threshold": -0.001,
        "peak_negative_median_min_count": 24,
        "peak_bad_min_threshold": -0.05,
        "peak_bad_min_count": 16,
        "total_perturbations": 32,
    }

    result = classify_local_geometry(rows, rules)

    assert result["classification"] == "sharp_local_peak"
    assert result["negative_median_count"] == 32
    assert result["bad_min_count"] == 16


def test_difference_intervals_keep_noncontiguous_disagreements_separate() -> None:
    """Catch path concentration merging unrelated holding disagreements."""
    index = pd.date_range("2025-01-01", periods=5, freq="D")
    baseline_target = pd.Series([0, 0, 1, 1, 0], index=index, dtype=float)
    ex04_target = pd.Series([0, 1, 1, 0, 0], index=index, dtype=float)
    baseline_returns = pd.Series([0.0, 0.01, 0.02, -0.01, 0.0], index=index)
    ex04_returns = pd.Series([0.0, 0.03, 0.02, 0.02, 0.0], index=index)

    result = difference_intervals(
        baseline_target, ex04_target, baseline_returns, ex04_returns
    )

    assert result["start"].to_list() == [index[1], index[3]]
    assert result["end"].to_list() == [index[1], index[3]]
    assert result["return_delta_contribution"].to_list() == pytest.approx([0.02, 0.03])
    assert result["absolute_contribution_share"].to_list() == pytest.approx([0.4, 0.6])


def test_market_regimes_use_only_lagged_lookback_return() -> None:
    """Catch a diagnostic regime label reading the current close."""
    index = pd.date_range("2025-01-01", periods=4, freq="D")
    close = pd.Series([100.0, 110.0, 121.0, 108.9], index=index)

    result = market_regimes(close, lookback=2, up=0.1, down=-0.1, lag=1)

    assert result.to_list() == ["warmup", "warmup", "warmup", "uptrend"]


def test_bootstrap_is_deterministic_and_zero_for_identical_paths() -> None:
    """Catch unpaired or unseeded resampling creating a false edge."""
    index = pd.date_range("2025-01-01", periods=6, freq="D")
    returns = pd.Series([0.01, -0.02, 0.03, 0.0, -0.01, 0.02], index=index)

    result = paired_circular_block_bootstrap(
        returns,
        returns.copy(),
        block_length=2,
        replications=20,
        seed=7,
        quantiles=(0.025, 0.5, 0.975),
    )

    assert result == {
        "method": "paired_circular_moving_block",
        "sample_days": 6,
        "block_length_trading_days": 2,
        "replications": 20,
        "seed": 7,
        "quantiles": {"0.025": 0.0, "0.5": 0.0, "0.975": 0.0},
        "probability_delta_above_zero": 0.0,
    }


def test_bootstrap_rejects_misaligned_paths() -> None:
    """Catch paired bootstrap silently pairing different dates."""
    left = pd.Series([0.0, 0.1], index=pd.date_range("2025-01-01", periods=2))
    right = pd.Series([0.0, 0.1], index=pd.date_range("2025-01-02", periods=2))

    with pytest.raises(ValueError, match="indices"):
        paired_circular_block_bootstrap(
            left,
            right,
            block_length=1,
            replications=2,
            seed=1,
            quantiles=(0.5,),
        )


def _formal_protocol() -> dict[str, object]:
    path = Path("experiments/0825_EX02/artifacts/protocol.json")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("holdout_access_allowed", True), "holdout"),
        (("promotion.run_holdout", True), "promotion"),
        (("local_geometry.total_perturbations", 31), "32"),
        (("visible_sample_end", "2026-01-01"), "2025-12-31"),
    ],
)
def test_protocol_rejects_research_boundary_changes(
    mutation: tuple[str, object], message: str
) -> None:
    """Catch formal diagnosis being widened into optimization or holdout access."""
    protocol = copy.deepcopy(_formal_protocol())
    dotted, value = mutation
    target: dict[str, object] = protocol
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = target[part]  # type: ignore[assignment]
    target[parts[-1]] = value

    with pytest.raises(ValueError, match=message):
        validate_protocol(protocol)


def test_zero_exposure_coalition_has_zero_sharpe_utility() -> None:
    """Catch vectorbt's infinite all-cash Sharpe contaminating Shapley values."""
    cash = {
        "strategy_return": 0.0,
        "sharpe": float("inf"),
        "exposure": 0.0,
        "trade_count": 0,
    }

    result = normalize_cash_sharpe(cash)

    assert result["sharpe"] == 0.0
    assert np.isfinite(float(result["sharpe"]))


def test_nonfinite_active_coalition_sharpe_is_rejected() -> None:
    """Catch a genuinely invalid active coalition being silently converted to cash."""
    active = {
        "strategy_return": 0.1,
        "sharpe": float("inf"),
        "exposure": 0.5,
        "trade_count": 2,
    }

    with pytest.raises(ValueError, match="non-finite"):
        normalize_cash_sharpe(active)
