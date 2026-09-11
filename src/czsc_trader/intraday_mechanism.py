"""Causal T+1 episode evaluation for pre-registered intraday mechanisms."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MechanismEvaluationResult:
    """Episode-level and aggregate evidence for fixed mechanism hypotheses."""

    episodes: pd.DataFrame
    metrics: pd.DataFrame
    annual: pd.DataFrame
    regime: pd.DataFrame


def _rolling_episode_density(
    exit_dates: pd.DatetimeIndex,
    calendar: pd.DatetimeIndex,
    window_sessions: int,
) -> tuple[float, float, int, int]:
    counts = pd.Series(0, index=calendar, dtype="int64")
    for value, count in Counter(exit_dates).items():
        if value in counts.index:
            counts.loc[value] = count
    rolling = counts.rolling(window_sessions, min_periods=window_sessions).sum().dropna()
    if rolling.empty:
        return float("nan"), float("nan"), 0, 0
    return (
        float(rolling.median()),
        float(rolling.quantile(0.10, interpolation="lower")),
        int(rolling.min()),
        int(rolling.max()),
    )


def _performance(returns: pd.Series, elapsed_years: float) -> dict[str, float | int]:
    values = returns.astype(float).dropna()
    if values.empty:
        return {
            "episodes": 0,
            "mean_return": float("nan"),
            "median_return": float("nan"),
            "win_rate": float("nan"),
            "profit_factor": float("nan"),
            "cagr": float("nan"),
            "max_drawdown": float("nan"),
            "calmar": float("nan"),
        }
    equity = (1.0 + values).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    maximum_drawdown = float(drawdown.min())
    cagr = float(equity.iloc[-1] ** (1.0 / elapsed_years) - 1.0)
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    profit_factor = gains / losses if losses else float("inf")
    return {
        "episodes": int(len(values)),
        "mean_return": float(values.mean()),
        "median_return": float(values.median()),
        "win_rate": float(values.gt(0).mean()),
        "profit_factor": profit_factor,
        "cagr": cagr,
        "max_drawdown": maximum_drawdown,
        "calmar": cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else float("inf"),
    }


def _outcome_table(
    signal_times: pd.DatetimeIndex,
    five_minute: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    *,
    exit_clock: str,
    baseline_one_way_cost: float,
    stress_one_way_cost: float,
) -> pd.DataFrame:
    bars = five_minute.sort_values("Date").reset_index(drop=True)
    timestamps = pd.DatetimeIndex(pd.to_datetime(bars["Date"]))
    positions = timestamps.searchsorted(signal_times, side="right")
    close_lookup = (
        bars.loc[bars["Date"].dt.strftime("%H:%M") == exit_clock]
        .assign(trade_date=lambda value: value["Date"].dt.normalize())
        .set_index("trade_date")[["Date", "Close"]]
    )
    calendar_positions = {value: index for index, value in enumerate(calendar)}
    rows: list[dict[str, object]] = []
    for signal_time, position in zip(signal_times, positions, strict=True):
        if position >= len(bars):
            continue
        entry = bars.iloc[int(position)]
        entry_date = pd.Timestamp(entry["Date"]).normalize()
        location = calendar_positions.get(entry_date)
        if location is None or location + 1 >= len(calendar):
            continue
        exit_date = calendar[location + 1]
        if exit_date not in close_lookup.index:
            continue
        exit_row = close_lookup.loc[exit_date]
        entry_price = float(entry["Open"])
        exit_price = float(exit_row["Close"])
        rows.append(
            {
                "signal_time": signal_time,
                "signal_clock": signal_time.strftime("%H:%M"),
                "entry_time": pd.Timestamp(entry["Date"]),
                "entry_date": entry_date,
                "entry_price": entry_price,
                "exit_time": pd.Timestamp(exit_row["Date"]),
                "exit_date": exit_date,
                "exit_price": exit_price,
                "gross_return": exit_price / entry_price - 1.0,
                "baseline_return": (
                    exit_price * (1.0 - baseline_one_way_cost)
                    / (entry_price * (1.0 + baseline_one_way_cost))
                    - 1.0
                ),
                "stress_return": (
                    exit_price * (1.0 - stress_one_way_cost)
                    / (entry_price * (1.0 + stress_one_way_cost))
                    - 1.0
                ),
            }
        )
    return pd.DataFrame(rows)


def _non_overlapping(outcomes: pd.DataFrame) -> pd.DataFrame:
    accepted: list[int] = []
    last_exit: pd.Timestamp | None = None
    for index, row in outcomes.sort_values("signal_time").iterrows():
        signal_time = pd.Timestamp(row["signal_time"])
        if last_exit is not None and signal_time <= last_exit:
            continue
        accepted.append(index)
        last_exit = pd.Timestamp(row["exit_time"])
    return outcomes.loc[accepted].sort_values("signal_time").reset_index(drop=True)


def _matched_random_percentile(
    episodes: pd.DataFrame,
    control: pd.DataFrame,
    *,
    simulations: int,
    seed: int,
) -> float:
    if episodes.empty:
        return float("nan")
    grouped = {
        key: values["stress_return"].dropna().to_numpy(dtype=float)
        for key, values in control.groupby(["signal_clock", "regime"], observed=True)
    }
    pools: list[np.ndarray] = []
    for row in episodes.itertuples(index=False):
        pool = grouped.get((str(row.signal_clock), str(row.regime)))
        if pool is None or not len(pool):
            return float("nan")
        pools.append(pool)
    rng = np.random.default_rng(seed)
    null_means = np.zeros(simulations, dtype=float)
    for pool in pools:
        null_means += rng.choice(pool, size=simulations, replace=True)
    null_means /= len(pools)
    observed = float(episodes["stress_return"].mean())
    return float((null_means < observed).mean())


def evaluate_intraday_mechanisms(
    events: pd.DataFrame,
    fifteen_minute: pd.DataFrame,
    five_minute: pd.DataFrame,
    daily_regime: pd.Series,
    hypotheses: Sequence[Mapping[str, object]],
    *,
    evaluation_start: pd.Timestamp | str,
    exit_clocks: Sequence[str],
    baseline_one_way_cost: float,
    stress_one_way_cost: float,
    density_window_sessions: int,
    median_episodes_required: int,
    p10_episodes_required: int,
    matched_random_simulations: int,
    random_seed: int,
) -> MechanismEvaluationResult:
    """Evaluate fixed signal-state hypotheses without overlapping capital use."""

    event_source = events.copy()
    event_source["event_time"] = pd.to_datetime(event_source["event_time"])
    event_source["event_date"] = pd.to_datetime(event_source["event_date"]).dt.normalize()
    fifteen = fifteen_minute.copy()
    fifteen["Date"] = pd.to_datetime(fifteen["Date"])
    five = five_minute.copy()
    five["Date"] = pd.to_datetime(five["Date"])
    evaluation_start = pd.Timestamp(evaluation_start).normalize()
    calendar = pd.DatetimeIndex(
        sorted(five.loc[five["Date"] >= evaluation_start, "Date"].dt.normalize().unique()),
        name="date",
    )
    regimes = daily_regime.copy()
    regimes.index = pd.DatetimeIndex(pd.to_datetime(regimes.index)).normalize()
    regimes = regimes.astype("string")
    all_signal_times = pd.DatetimeIndex(
        fifteen.loc[fifteen["Date"] >= evaluation_start, "Date"].sort_values().unique()
    )
    elapsed_years = max((calendar.max() - calendar.min()).days / 365.25, 1.0)
    episode_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    annual_rows: list[dict[str, object]] = []
    regime_rows: list[dict[str, object]] = []

    for clock_number, exit_clock in enumerate(exit_clocks):
        control = _outcome_table(
            all_signal_times,
            five,
            calendar,
            exit_clock=str(exit_clock),
            baseline_one_way_cost=baseline_one_way_cost,
            stress_one_way_cost=stress_one_way_cost,
        )
        control["regime"] = regimes.reindex(control["signal_time"].dt.normalize()).to_numpy()
        for number, hypothesis in enumerate(hypotheses):
            selected = event_source.loc[
                event_source["name"].eq(str(hypothesis["signal_name"]))
                & event_source["state_primary"].eq(str(hypothesis["state_primary"]))
            ].copy()
            outcomes = _outcome_table(
                pd.DatetimeIndex(selected["event_time"].sort_values()),
                five,
                calendar,
                exit_clock=str(exit_clock),
                baseline_one_way_cost=baseline_one_way_cost,
                stress_one_way_cost=stress_one_way_cost,
            )
            outcomes = outcomes.merge(
                selected[["event_time", "regime"]],
                left_on="signal_time",
                right_on="event_time",
                how="left",
                validate="one_to_one",
            ).drop(columns="event_time")
            episodes = _non_overlapping(outcomes)
            episodes.insert(0, "hypothesis_id", str(hypothesis["hypothesis_id"]))
            episodes.insert(1, "exit_clock", str(exit_clock))
            episode_frames.append(episodes)
            median_count, p10_count, minimum_count, maximum_count = _rolling_episode_density(
                pd.DatetimeIndex(episodes["exit_date"]),
                calendar,
                density_window_sessions,
            )
            metrics = _performance(episodes["stress_return"], elapsed_years)
            positive_years = 0
            for year, rows in episodes.groupby(episodes["exit_date"].dt.year, observed=True):
                item = _performance(rows["stress_return"], 1.0)
                annual_rows.append(
                    {
                        "hypothesis_id": hypothesis["hypothesis_id"],
                        "exit_clock": str(exit_clock),
                        "year": int(year),
                        **item,
                    }
                )
                positive_years += int(float(item["mean_return"]) > 0)
            for regime, rows in episodes.groupby("regime", observed=True):
                regime_rows.append(
                    {
                        "hypothesis_id": hypothesis["hypothesis_id"],
                        "exit_clock": str(exit_clock),
                        "regime": str(regime),
                        **_performance(rows["stress_return"], elapsed_years),
                    }
                )
            density_pass = bool(
                median_count >= median_episodes_required
                and p10_count >= p10_episodes_required
            )
            percentile = _matched_random_percentile(
                episodes,
                control,
                simulations=matched_random_simulations,
                seed=random_seed + number * 100 + clock_number,
            )
            if not density_pass:
                evidence = "EVIDENCE_RATE_FAIL"
            elif float(metrics["mean_return"]) <= 0 or float(metrics["profit_factor"]) <= 1:
                evidence = "DIRECTION_FAIL"
            elif percentile >= 0.90 and positive_years >= 4:
                evidence = "SUPPORTED"
            else:
                evidence = "MIXED"
            metric_rows.append(
                {
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "mechanism": hypothesis["mechanism"],
                    "signal_name": hypothesis["signal_name"],
                    "state_primary": hypothesis["state_primary"],
                    "exit_clock": str(exit_clock),
                    **metrics,
                    "positive_years": positive_years,
                    "rolling_30d_median_episodes": median_count,
                    "rolling_30d_p10_episodes": p10_count,
                    "rolling_30d_min_episodes": minimum_count,
                    "rolling_30d_max_episodes": maximum_count,
                    "density_pass": density_pass,
                    "matched_random_percentile": percentile,
                    "evidence": evidence,
                }
            )
    return MechanismEvaluationResult(
        episodes=pd.concat(episode_frames, ignore_index=True),
        metrics=pd.DataFrame(metric_rows),
        annual=pd.DataFrame(annual_rows),
        regime=pd.DataFrame(regime_rows),
    )
