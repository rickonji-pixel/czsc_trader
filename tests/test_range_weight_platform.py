from __future__ import annotations

import numpy as np
import pandas as pd


def test_project_group_shares_preserves_within_group_ratios() -> None:
    from czsc_trader.range_platform import project_group_shares

    base = pd.Series({"s1": 0.2, "s2": 0.3, "t1": 0.4, "v1": 0.1})
    groups = {
        "structure": ("s1", "s2"),
        "trend": ("t1",),
        "volume_position": ("v1",),
    }

    projected = project_group_shares(
        base,
        groups,
        {"structure": 0.4, "trend": 0.3, "volume_position": 0.3},
    )

    np.testing.assert_allclose(projected.loc[["s1", "s2"]], [0.16, 0.24])
    np.testing.assert_allclose(projected.loc[["t1", "v1"]], [0.3, 0.3])
    np.testing.assert_allclose(projected.abs().sum(), 1.0)


def test_pareto_layers_keeps_tradeoffs_on_same_front() -> None:
    from czsc_trader.range_platform import pareto_layers

    candidates = pd.DataFrame(
        {
            "candidate_id": [0, 1, 2, 3],
            "max_drawdown": [-0.10, -0.20, -0.20, -0.30],
            "calmar": [1.0, 2.0, 1.0, 0.5],
            "win_loss_ratio": [2.0, 2.0, 1.0, 0.5],
        }
    )

    layers = pareto_layers(
        candidates,
        ("max_drawdown", "calmar", "win_loss_ratio"),
        tolerance=1e-12,
    )

    assert layers.to_dict() == {0: 1, 1: 1, 2: 2, 3: 3}


def test_simplex_components_connects_one_step_neighbors() -> None:
    from czsc_trader.range_platform import simplex_components

    candidates = pd.DataFrame(
        {
            "candidate_id": [0, 1, 2, 3],
            "structure_share": [0.250, 0.275, 0.300, 0.600],
            "trend_share": [0.250, 0.225, 0.200, 0.200],
            "volume_share": [0.500, 0.500, 0.500, 0.200],
        }
    )

    components = simplex_components(candidates, [0, 1, 2, 3], step=0.025)

    assert components == [[0, 1, 2], [3]]


def test_dirichlet_weight_candidates_are_reproducible_and_include_anchors() -> None:
    from czsc_trader.range_platform import dirichlet_weight_candidates

    anchors = {
        "control": pd.Series({"f1": 0.6, "f2": 0.3, "f3": 0.1}),
        "balanced": pd.Series({"f1": 1 / 3, "f2": 1 / 3, "f3": 1 / 3}),
    }

    first = dirichlet_weight_candidates(
        anchors,
        samples_per_anchor=3,
        concentrations={"control": 40.0, "balanced": 10.0},
        seed=20260903,
    )
    second = dirichlet_weight_candidates(
        anchors,
        samples_per_anchor=3,
        concentrations={"control": 40.0, "balanced": 10.0},
        seed=20260903,
    )

    pd.testing.assert_frame_equal(first, second)
    assert first["candidate_id"].tolist() == list(range(8))
    assert first.groupby("anchor_name")["is_anchor"].sum().to_dict() == {
        "balanced": 1,
        "control": 1,
    }
    weights = first[["f1", "f2", "f3"]]
    np.testing.assert_allclose(weights.sum(axis=1), 1.0, rtol=0.0, atol=1e-12)
    assert weights.gt(0.0).all().all()
    np.testing.assert_allclose(
        first.loc[first["anchor_name"].eq("control") & first["is_anchor"], ["f1", "f2", "f3"]],
        [[0.6, 0.3, 0.1]],
    )


def test_dirichlet_weight_candidates_reject_invalid_anchor_identity() -> None:
    from czsc_trader.range_platform import dirichlet_weight_candidates

    anchors = {
        "left": pd.Series({"f1": 0.5, "f2": 0.5}),
        "right": pd.Series({"f2": 0.5, "f3": 0.5}),
    }

    with np.testing.assert_raises_regex(ValueError, "same positive factor weights"):
        dirichlet_weight_candidates(
            anchors,
            samples_per_anchor=1,
            concentrations={"left": 10.0, "right": 10.0},
            seed=1,
        )


def test_robust_pareto_profiles_rank_candidates_across_windows() -> None:
    from czsc_trader.range_platform import robust_pareto_profiles, select_robust_seeds

    metrics = pd.DataFrame(
        {
            "candidate_id": [0, 1, 2, 0, 1, 2],
            "period": ["early"] * 3 + ["late"] * 3,
            "max_drawdown": [-0.10, -0.20, -0.30, -0.30, -0.10, -0.20],
            "calmar": [1.0, 2.0, 0.5, 0.5, 2.0, 1.0],
            "win_loss_ratio": [2.0, 2.0, 0.5, 0.5, 2.0, 1.0],
        }
    )

    period_metrics, profiles = robust_pareto_profiles(
        metrics, ("max_drawdown", "calmar", "win_loss_ratio")
    )

    assert period_metrics["pareto_layer"].tolist() == [1, 1, 2, 3, 1, 2]
    assert profiles.set_index("candidate_id").to_dict("index") == {
        0: {"first_front_count": 1, "mean_pareto_layer": 2.0, "worst_pareto_layer": 3},
        1: {"first_front_count": 2, "mean_pareto_layer": 1.0, "worst_pareto_layer": 1},
        2: {"first_front_count": 0, "mean_pareto_layer": 2.0, "worst_pareto_layer": 2},
    }
    assert select_robust_seeds(profiles, limit=2) == [1, 0]


def test_dirichlet_weight_candidates_enforce_minimum_factor_weight() -> None:
    from czsc_trader.range_platform import dirichlet_weight_candidates

    candidates = dirichlet_weight_candidates(
        {"deployable": pd.Series({"f1": 0.6, "f2": 0.3, "f3": 0.1})},
        samples_per_anchor=20,
        concentrations={"deployable": 30.0},
        seed=7,
        minimum_weight=0.1,
    )

    weights = candidates[["f1", "f2", "f3"]]
    assert weights.ge(0.1 - 1e-12).all().all()
    np.testing.assert_allclose(weights.sum(axis=1), 1.0, rtol=0.0, atol=1e-12)
