from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def test_robustness_cscv_uses_all_complementary_splits() -> None:
    from czsc_trader.robustness import contiguous_blocks, cscv_pbo

    blocks = contiguous_blocks(20, 10)
    assert len(blocks) == 10
    assert np.array_equal(np.sort(np.concatenate(blocks)), np.arange(20))

    index = pd.date_range("2025-01-01", periods=20, freq="D")
    returns = pd.DataFrame(
        {
            "candidate_0": np.tile([0.01, -0.002], 10),
            "candidate_1": np.r_[np.full(10, 0.02), np.full(10, -0.02)],
            "candidate_2": np.r_[np.full(10, -0.02), np.full(10, 0.02)],
        },
        index=index,
    )
    splits, summary = cscv_pbo(returns, block_count=10)

    assert len(splits) == 252
    assert summary["split_count"] == 252
    assert set(splits["selected_candidate"]) <= set(returns.columns)
    assert splits["validation_percentile"].between(0.0, 1.0).all()
    assert np.isfinite(splits["logit"]).all()

    tied = pd.DataFrame(
        {"10": np.tile([0.01, -0.002], 10), "2": np.tile([0.01, -0.002], 10)},
        index=index,
    )
    tied_splits, _ = cscv_pbo(tied, block_count=10)
    assert set(tied_splits["selected_candidate"]) == {"2"}


def test_robustness_cyclic_shifts_are_complete_and_unique() -> None:
    from czsc_trader.robustness import cyclic_shifts, placebo_signal_paths

    source = pd.Series([0, 0, 1, 1, 0], index=pd.date_range("2026-01-01", periods=5))
    shifts = cyclic_shifts(source)

    assert len(shifts) == 4
    assert len({tuple(item.tolist()) for item in shifts}) == 4
    assert all(sorted(item.tolist()) == sorted(source.tolist()) for item in shifts)
    assert all(item.index.equals(source.index) for item in shifts)
    with pytest.raises(ValueError, match="unique"):
        cyclic_shifts(pd.Series([1, 1, 1]))

    paths = placebo_signal_paths(source, initial_target=1.0)
    assert paths[0][0] == 0
    assert paths[0][1].equals(source)
    assert all(initial == 1.0 for _, _, initial in paths)


def test_robustness_deflated_sharpe_increases_with_selected_sharpe() -> None:
    from czsc_trader.robustness import candidate_sharpes, deflated_sharpe_ratio
    from scipy.stats import norm

    index = pd.date_range("2021-01-01", periods=500, freq="B")
    low = pd.Series(np.tile([0.003, -0.002], 250), index=index)
    high = pd.Series(np.tile([0.006, -0.002], 250), index=index)
    trials = pd.Series(np.linspace(0.1, 1.1, 625))

    matrix = pd.DataFrame({"low": low, "high": high})
    same_scale = candidate_sharpes(matrix)
    assert same_scale["low"] == pytest.approx(
        deflated_sharpe_ratio(low, same_scale)["observed_sharpe"]
    )
    identical_trials = pd.Series([0.2, 0.4, 0.6, 0.8])
    expected_standard_max = (
        (1.0 - 0.5772156649015329)
        * norm.ppf(1.0 - 1.0 / len(identical_trials))
        + 0.5772156649015329
        * norm.ppf(
            1.0 - 1.0 / (len(identical_trials) * np.e)
        )
    )
    formula = deflated_sharpe_ratio(high, identical_trials)
    assert formula["expected_max_sharpe"] == pytest.approx(
        identical_trials.std(ddof=1) * expected_standard_max
    )

    low_result = deflated_sharpe_ratio(low, trials)
    high_result = deflated_sharpe_ratio(high, trials)

    required = {
        "observations",
        "trial_count",
        "observed_sharpe",
        "expected_max_sharpe",
        "skew",
        "pearson_kurtosis",
        "dsr_probability",
    }
    assert required <= set(low_result)
    assert all(np.isfinite(float(low_result[key])) for key in required)
    assert high_result["dsr_probability"] > low_result["dsr_probability"]


def test_robustness_parameter_geometry_finds_grid_neighbors() -> None:
    from czsc_trader.robustness import parameter_geometry

    rows = []
    candidate_id = 0
    for first in (0.5, 0.75, 1.0):
        for second in (0.5, 0.75, 1.0):
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "first": first,
                    "second": second,
                    "sharpe": first + second,
                }
            )
            candidate_id += 1
    candidates = pd.DataFrame(rows)
    selected_id = int(
        candidates.loc[
            candidates["first"].eq(0.75) & candidates["second"].eq(0.75),
            "candidate_id",
        ].iloc[0]
    )

    surface, neighbors = parameter_geometry(
        candidates, selected_id, ("first", "second")
    )

    assert len(surface) == 9
    assert (surface["manhattan_distance"] == 1.0).sum() == 4
    assert set(neighbors["manhattan_distance"]) == {1.0, 2.0}
    assert len(neighbors.loc[neighbors["manhattan_distance"].eq(1.0)]) == 4

