import numpy as np
import pandas as pd
import pytest

from czsc_trader.walk_forward import (
    CANDIDATES,
    Rule,
    build_factor_events,
    deduplicate_candidate_targets,
    positions_for_rule,
    rank_candidate_results,
    run_walk_forward,
    select_fixed_rule,
)


def _sample_inputs(periods: int = 360) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = pd.bdate_range("2024-01-02", periods=periods)
    trend = np.sin(np.arange(periods) / 17.0)
    close = 100 * np.cumprod(1 + 0.0015 * np.sign(trend) + 0.004 * np.sin(np.arange(periods)))
    daily = pd.DataFrame(
        {
            "dt": dates,
            "open": close * (1 + 0.001 * np.cos(np.arange(periods))),
            "close": close,
        }
    )
    factors = pd.DataFrame(
        {
            "structure": np.sign(trend),
            "trend": trend,
            "volume_position": np.cos(np.arange(periods) / 13.0),
        },
        index=pd.DatetimeIndex(dates, name="dt"),
    )
    return daily, factors


def test_rule_state_machine_confirms_entry_and_minimum_hold() -> None:
    """Catch entries without confirmation or exits before minimum hold."""
    index = pd.bdate_range("2025-01-02", periods=7)
    factors = pd.DataFrame(
        {
            "structure": [0.5, 0.5, 0.5, -0.5, -0.5, -0.5, -0.5],
            "trend": [0.5, 0.5, 0.5, -0.5, -0.5, -0.5, -0.5],
            "volume_position": [0.5, 0.5, 0.5, -0.5, -0.5, -0.5, -0.5],
        },
        index=index,
    )
    rule = Rule((0.4, 0.4, 0.2), enter=0.2, exit=0.0, confirm_days=2, min_hold_days=3)

    positions, scores = positions_for_rule(factors, rule)

    assert scores.tolist() == [0.5, 0.5, 0.5, -0.5, -0.5, -0.5, -0.5]
    assert positions.tolist() == [0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]


def test_entry_gate_blocks_positive_score_without_selected_factor_confirmation() -> None:
    """Catch a gated rule entering solely because another factor offsets trend."""
    index = pd.bdate_range("2025-01-02", periods=2)
    factors = pd.DataFrame(
        {
            "structure": [1.0, 1.0],
            "trend": [-0.5, 0.0],
            "volume_position": [1.0, 1.0],
        },
        index=index,
    )
    rule = Rule(
        (0.4, 0.4, 0.2),
        enter=0.2,
        exit=0.0,
        confirm_days=1,
        min_hold_days=1,
        entry_gate="trend",
    )

    positions, scores = positions_for_rule(factors, rule)

    np.testing.assert_allclose(scores.to_numpy(), [0.4, 0.6])
    assert positions.tolist() == [0.0, 1.0]


def test_exit_confirmation_resets_when_score_recovers() -> None:
    """Catch non-consecutive weak days being accumulated into an exit."""
    index = pd.bdate_range("2025-01-02", periods=5)
    values = [0.5, -0.5, 0.5, -0.5, -0.5]
    factors = pd.DataFrame(
        {column: values for column in ("structure", "trend", "volume_position")},
        index=index,
    )
    rule = Rule(
        (0.4, 0.4, 0.2),
        enter=0.2,
        exit=0.0,
        confirm_days=1,
        min_hold_days=1,
        exit_confirm_days=2,
    )

    positions, _ = positions_for_rule(factors, rule)

    assert positions.tolist() == [1.0, 1.0, 1.0, 1.0, 0.0]


def test_declared_candidate_grid_has_frozen_size() -> None:
    """Catch an accidental omission or post-design expansion of the frozen grid."""
    assert len(CANDIDATES) == 23_760


def test_equivalent_targets_keep_the_least_complex_rule() -> None:
    """Catch duplicate position histories inflating price-evaluated candidates."""
    index = pd.bdate_range("2025-01-02", periods=4)
    factors = pd.DataFrame(
        {column: [0.5] * 4 for column in ("structure", "trend", "volume_position")},
        index=index,
    )
    simple = Rule((0.4, 0.4, 0.2), 0.2, 0.0, 1, 1)
    complex_rule = Rule((0.4, 0.4, 0.2), 0.2, 0.0, 1, 5)

    unique = deduplicate_candidate_targets((complex_rule, simple), factors)

    assert len(unique) == 1
    assert unique[0][0] == simple


def test_walk_forward_is_long_cash_and_strictly_prior_trained() -> None:
    """Catch invalid exposure or training windows that include the as-of day."""
    daily, factors = _sample_inputs()
    result = run_walk_forward(daily, factors)

    assert set(result.target_position.unique()) <= {0.0, 1.0}
    trained = result.selections.dropna(subset=["train_end"])
    assert (pd.to_datetime(trained["train_end"]) < pd.to_datetime(trained["as_of_date"])).all()
    assert trained["candidate_count"].nunique() == 1


def test_future_prices_cannot_change_past_selections_or_positions() -> None:
    """Catch selector code that reads prices after its decision timestamp."""
    daily, factors = _sample_inputs()
    cutoff = pd.Timestamp("2025-02-28")
    first = run_walk_forward(daily, factors)
    changed = daily.copy()
    changed.loc[changed["dt"] > cutoff, ["open", "close"]] *= 3
    second = run_walk_forward(changed, factors)

    pd.testing.assert_frame_equal(
        first.selections.loc[first.selections["as_of_date"] <= cutoff].reset_index(drop=True),
        second.selections.loc[second.selections["as_of_date"] <= cutoff].reset_index(drop=True),
    )
    pd.testing.assert_series_equal(
        first.target_position.loc[:cutoff],
        second.target_position.loc[:cutoff],
    )


def test_factor_events_are_the_only_position_transitions() -> None:
    """Catch missing or fabricated event provenance for target changes."""
    index = pd.bdate_range("2025-01-02", periods=6)
    factors = pd.DataFrame(
        {
            "structure": [0.5, 0.5, -0.5, -0.5, 0.5, 0.5],
            "trend": [0.5, 0.5, -0.5, -0.5, 0.5, 0.5],
            "volume_position": [0.5, 0.5, -0.5, -0.5, 0.5, 0.5],
        },
        index=index,
    )
    rule = Rule((0.4, 0.4, 0.2), enter=0.2, exit=0.0, confirm_days=1, min_hold_days=1)
    target, scores = positions_for_rule(factors, rule)

    events = build_factor_events(target, scores, factors, rule)

    assert events["event_type"].tolist() == ["Entry", "Exit", "Entry"]
    assert events["after_position"].tolist() == [1.0, 0.0, 1.0]
    assert events["signal_date"].tolist() == [index[0], index[2], index[4]]
    assert events["factor_score"].tolist() == [0.5, -0.5, 0.5]


def test_candidate_ranking_prioritizes_worst_absolute_target_margin() -> None:
    """Catch a high-average candidate hiding a failed absolute target."""
    candidates = pd.DataFrame(
        [
            {"rule_id": "fragile", "pass_count": 2, "min_target_margin": -0.01, "mean_target_margin": 0.30, "turnover": 0.1, "max_drawdown": -0.1, "complexity": 2},
            {"rule_id": "robust", "pass_count": 3, "min_target_margin": 0.01, "mean_target_margin": 0.02, "turnover": 0.2, "max_drawdown": -0.2, "complexity": 4},
        ]
    )

    ranked = rank_candidate_results(candidates)

    assert ranked.iloc[0]["rule_id"] == "robust"


def test_turnover_cannot_change_absolute_target_ranking() -> None:
    """Catch churn diagnostics leaking back into candidate selection."""
    candidates = pd.DataFrame(
        [
            {"rule_id": "higher_turnover", "pass_count": 3, "min_target_margin": 0.02, "mean_target_margin": 0.03, "turnover": 0.9, "max_drawdown": -0.1, "complexity": 2},
            {"rule_id": "lower_turnover", "pass_count": 3, "min_target_margin": 0.01, "mean_target_margin": 0.50, "turnover": 0.0, "max_drawdown": -0.1, "complexity": 2},
        ]
    )

    ranked = rank_candidate_results(candidates)

    assert ranked.iloc[0]["rule_id"] == "higher_turnover"


@pytest.mark.slow
def test_fixed_selection_target_is_exactly_its_factor_rule() -> None:
    """Catch any post-selection performance overlay changing factor positions."""
    daily, factors = _sample_inputs(180)
    periods = {"sample": (daily["dt"].iloc[120], daily["dt"].iloc[-1])}

    result = select_fixed_rule(daily, factors, periods, return_targets={"sample": 0.0})
    expected_target, expected_scores = positions_for_rule(factors, result.rule)

    pd.testing.assert_series_equal(result.target_position, expected_target, check_freq=False)
    pd.testing.assert_series_equal(result.scores, expected_scores, check_freq=False)
