"""Uniform execution evaluation for pre-registered medium-frequency events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from .intraday_opening_execution import (
    _account_metrics,
    _density,
    _performance,
    _trade_return,
)


@dataclass(frozen=True)
class MediumFrequencyEvaluationResult:
    """Episode, aggregate, annual, regime and account evidence."""

    episodes: pd.DataFrame
    metrics: pd.DataFrame
    annual: pd.DataFrame
    regime: pd.DataFrame
    account: pd.DataFrame


def evaluate_medium_frequency_mechanisms(
    events: pd.DataFrame,
    five_minute: pd.DataFrame,
    daily_regime: pd.Series,
    mechanism_ids: Sequence[str],
    *,
    evaluation_start: pd.Timestamp | str,
    entry_clock: str,
    exit_clock: str,
    baseline_one_way_cost: float,
    stress_one_way_cost: float,
    base_fraction: float,
    density_window_sessions: int,
    median_episodes_required: int,
    p10_episodes_required: int,
    positive_years_required: int,
) -> MediumFrequencyEvaluationResult:
    """Evaluate each fixed direction and its exact opposite on identical dates."""

    if not 0 < base_fraction < 1:
        raise ValueError("base_fraction must be between zero and one")
    source = events.copy()
    source["trade_date"] = pd.to_datetime(source["trade_date"]).dt.normalize()
    source["event_time"] = pd.to_datetime(source["event_time"])
    source["direction"] = pd.to_numeric(source["direction"], errors="raise").astype(int)
    if not source["direction"].isin([-1, 1]).all():
        raise ValueError("event directions must be -1 or 1")
    if source.duplicated(["mechanism_id", "trade_date"]).any():
        raise ValueError("mechanism events must be unique by trading day")
    actual_ids = set(source["mechanism_id"].astype(str))
    expected_ids = set(map(str, mechanism_ids))
    if actual_ids != expected_ids:
        raise ValueError("event mechanisms differ from frozen protocol")

    bars = five_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"])
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    start = pd.Timestamp(evaluation_start).normalize()
    calendar = pd.DatetimeIndex(
        sorted(bars.loc[bars["trade_date"] >= start, "trade_date"].unique()),
        name="trade_date",
    )
    entry = bars.loc[bars["clock"].eq(entry_clock)].set_index("trade_date")["Open"]
    exit_price = bars.loc[bars["clock"].eq(exit_clock)].set_index("trade_date")["Close"]
    daily_close = bars.loc[bars["clock"].eq("15:00")].set_index("trade_date")["Close"]
    regimes = daily_regime.copy()
    regimes.index = pd.DatetimeIndex(pd.to_datetime(regimes.index)).normalize()
    elapsed_years = max((calendar.max() - calendar.min()).days / 365.25, 1.0)

    episode_frames: list[pd.DataFrame] = []
    provisional: dict[tuple[str, str], dict[str, object]] = {}
    annual_rows: list[dict[str, object]] = []
    regime_rows: list[dict[str, object]] = []
    account_rows: list[dict[str, object]] = []
    for mechanism_id in mechanism_ids:
        selected = source.loc[source["mechanism_id"].eq(str(mechanism_id))].copy()
        selected = selected.loc[selected["trade_date"] >= start].sort_values("trade_date")
        median_count, p10_count, minimum_count, maximum_count = _density(
            pd.DatetimeIndex(selected["trade_date"]), calendar, density_window_sessions
        )
        density_pass = bool(
            median_count >= median_episodes_required and p10_count >= p10_episodes_required
        )
        for variant, multiplier in (("PRIMARY", 1), ("OPPOSITE", -1)):
            episodes = selected.copy()
            episodes["entry_price"] = entry.reindex(episodes["trade_date"]).to_numpy()
            episodes["exit_price"] = exit_price.reindex(episodes["trade_date"]).to_numpy()
            if episodes[["entry_price", "exit_price"]].isna().any().any():
                raise ValueError(f"{mechanism_id}: missing entry or exit price")
            episodes["executed_direction"] = episodes["direction"] * multiplier
            for column, cost in (
                ("gross_return", 0.0),
                ("baseline_return", baseline_one_way_cost),
                ("stress_return", stress_one_way_cost),
            ):
                episodes[column] = [
                    _trade_return(
                        float(row.entry_price),
                        float(row.exit_price),
                        int(row.executed_direction),
                        cost,
                    )
                    for row in episodes.itertuples(index=False)
                ]
            episodes["regime"] = regimes.reindex(episodes["trade_date"]).to_numpy()
            episodes.insert(1, "variant", variant)
            episode_frames.append(episodes)

            gross = _performance(episodes["gross_return"], elapsed_years)
            baseline = _performance(episodes["baseline_return"], elapsed_years)
            stress = _performance(episodes["stress_return"], elapsed_years)
            positive_years = 0
            for year, rows in episodes.groupby(episodes["trade_date"].dt.year, observed=True):
                item = _performance(rows["stress_return"], 1.0)
                annual_rows.append(
                    {
                        "mechanism_id": mechanism_id,
                        "variant": variant,
                        "year": int(year),
                        **item,
                    }
                )
                positive_years += int(float(item["mean_return"]) > 0)
            for regime, rows in episodes.groupby("regime", observed=True):
                regime_rows.append(
                    {
                        "mechanism_id": mechanism_id,
                        "variant": variant,
                        "regime": str(regime),
                        **_performance(rows["stress_return"], elapsed_years),
                    }
                )
            account = _account_metrics(
                episodes,
                daily_close.reindex(calendar),
                base_fraction=base_fraction,
            )
            account_rows.append(
                {"mechanism_id": mechanism_id, "variant": variant, **account}
            )
            provisional[(str(mechanism_id), variant)] = {
                "mechanism_id": mechanism_id,
                "variant": variant,
                "episodes": int(stress["episodes"]),
                "gross_mean_return": float(gross["mean_return"]),
                "baseline_mean_return": float(baseline["mean_return"]),
                "baseline_profit_factor": float(baseline["profit_factor"]),
                "stress_mean_return": float(stress["mean_return"]),
                "stress_profit_factor": float(stress["profit_factor"]),
                "stress_win_rate": float(stress["win_rate"]),
                "stress_cumulative_return": float(stress["cumulative_return"]),
                "positive_years": positive_years,
                "rolling_window_sessions": int(density_window_sessions),
                "rolling_median_episodes": median_count,
                "rolling_p10_episodes": p10_count,
                "rolling_min_episodes": minimum_count,
                "rolling_max_episodes": maximum_count,
                "density_pass": density_pass,
                "incremental_terminal_return_vs_static": account[
                    "incremental_terminal_return_vs_static"
                ],
            }

    metric_rows: list[dict[str, object]] = []
    for mechanism_id in mechanism_ids:
        primary = provisional[(str(mechanism_id), "PRIMARY")]
        opposite = provisional[(str(mechanism_id), "OPPOSITE")]
        advantage = float(primary["stress_mean_return"]) - float(
            opposite["stress_mean_return"]
        )
        feasible = bool(
            primary["density_pass"]
            and float(primary["stress_mean_return"]) > 0
            and float(primary["stress_profit_factor"]) > 1
            and float(primary["incremental_terminal_return_vs_static"]) > 0
            and int(primary["positive_years"]) >= positive_years_required
            and advantage > 0
        )
        metric_rows.append(
            {
                **primary,
                "opposite_stress_mean_return": opposite["stress_mean_return"],
                "stress_mean_advantage": advantage,
                "evidence": "FEASIBLE" if feasible else "DIRECTION_FAIL",
            }
        )
        metric_rows.append(
            {
                **opposite,
                "opposite_stress_mean_return": primary["stress_mean_return"],
                "stress_mean_advantage": -advantage,
                "evidence": "CONTROL",
            }
        )
    return MediumFrequencyEvaluationResult(
        episodes=pd.concat(episode_frames, ignore_index=True),
        metrics=pd.DataFrame(metric_rows),
        annual=pd.DataFrame(annual_rows),
        regime=pd.DataFrame(regime_rows),
        account=pd.DataFrame(account_rows),
    )
