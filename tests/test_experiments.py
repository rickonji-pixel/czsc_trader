from __future__ import annotations

import pandas as pd

from czsc_trader.experiments import (
    ReentryChallenger,
    all_windows_pass,
    build_reentry_candidates,
    positions_with_reentry,
    rank_challengers,
    window_passes,
)
from czsc_trader.walk_forward import Rule


CHAMPION = Rule((0.3, 0.3, 0.4), 0.15, 0.0, 1, 1)


def _factors(values: list[float]) -> pd.DataFrame:
    index = pd.bdate_range("2025-01-02", periods=len(values))
    return pd.DataFrame(
        {column: values for column in ("structure", "trend", "volume_position")},
        index=index,
    )


def test_cooldown_blocks_exact_number_of_post_exit_signal_days() -> None:
    factors = _factors([0.5, -0.5, 0.5, 0.5, 0.5])
    challenger = ReentryChallenger(cooldown_days=2, reentry_gate="none")

    target, _ = positions_with_reentry(factors, CHAMPION, challenger)

    assert target.tolist() == [1.0, 0.0, 0.0, 0.0, 1.0]


def test_reentry_gate_does_not_block_initial_entry_but_blocks_reentry() -> None:
    index = pd.bdate_range("2025-01-02", periods=4)
    factors = pd.DataFrame(
        {
            "structure": [-0.1, -0.5, -0.1, 0.0],
            "trend": [0.5, -0.5, 0.5, 0.5],
            "volume_position": [0.5, -0.5, 0.5, 0.5],
        },
        index=index,
    )
    challenger = ReentryChallenger(cooldown_days=0, reentry_gate="structure")

    target, _ = positions_with_reentry(factors, CHAMPION, challenger)

    assert target.tolist() == [1.0, 0.0, 0.0, 1.0]


def test_window_pass_requires_strictly_higher_return_and_sharpe() -> None:
    champion = {"strategy_return": 0.10, "sharpe": 1.5}

    assert window_passes(champion, {"strategy_return": 0.11, "sharpe": 1.6})
    assert not window_passes(champion, {"strategy_return": 0.10, "sharpe": 1.6})
    assert not window_passes(champion, {"strategy_return": 0.11, "sharpe": 1.5})


def test_holdout_requires_every_declared_window_to_pass() -> None:
    champion = {
        name: {"strategy_return": 0.10, "sharpe": 1.0}
        for name in ("Q1", "H1", "M1-M8")
    }
    challenger = {
        name: {"strategy_return": 0.11, "sharpe": 1.1}
        for name in champion
    }

    assert all_windows_pass(champion, challenger)
    challenger["H1"]["sharpe"] = 1.0
    assert not all_windows_pass(champion, challenger)


def test_declared_reentry_grid_has_twenty_candidates() -> None:
    candidates = build_reentry_candidates()

    assert len(candidates) == 20
    assert len({candidate.candidate_id for candidate in candidates}) == 20


def test_ranking_uses_only_return_sharpe_and_deterministic_ties() -> None:
    rows = pd.DataFrame(
        [
            {
                "candidate_id": "fragile",
                "pass_count": 3,
                "min_return_delta": 0.01,
                "min_sharpe_delta": 0.20,
                "mean_return_delta": 0.30,
                "mean_sharpe_delta": 0.40,
                "complexity": 1,
                "max_drawdown": -0.01,
            },
            {
                "candidate_id": "robust",
                "pass_count": 3,
                "min_return_delta": 0.02,
                "min_sharpe_delta": 0.01,
                "mean_return_delta": 0.02,
                "mean_sharpe_delta": 0.02,
                "complexity": 9,
                "max_drawdown": -0.90,
            },
        ]
    )

    ranked = rank_challengers(rows)

    assert ranked.iloc[0]["candidate_id"] == "robust"
