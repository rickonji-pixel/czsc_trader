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
