import numpy as np
import pandas as pd

from czsc_trader.walk_forward import Rule, apply_annual_alpha_lock, positions_for_rule, run_walk_forward


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


def test_alpha_lock_changes_only_decisions_after_completed_q1() -> None:
    """Catch alpha protection that rewrites Q1 or reads beyond its boundary."""
    dates = pd.to_datetime(["2025-12-31", "2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02"])
    daily = pd.DataFrame({"dt": dates, "close": [100.0, 98.0, 95.0, 96.0, 97.0]})
    base_target = pd.Series(0.0, index=dates)
    base_equity = pd.Series([1_000_000.0, 1_040_000.0, 1_050_000.0, 1_030_000.0, 1_020_000.0], index=dates)

    result = apply_annual_alpha_lock(daily, base_target, base_equity, min_history=1)

    assert result.target_position.loc[:"2026-03-30"].eq(0.0).all()
    assert result.target_position.loc["2026-03-31":].eq(1.0).all()
    assert result.events.iloc[0]["decision_date"] == pd.Timestamp("2026-03-31")
    assert result.events.iloc[0]["q1_excess_return"] > 0.0
