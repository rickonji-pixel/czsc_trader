"""Paired-state same-day inventory roll feasibility research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pandas as pd

from .factors import _to_raw_bars
from .intraday_inventory import _account_metrics
from .intraday_mechanism import _performance, _rolling_episode_density
from .intraday_signal_census import _standardize_bars
from .signal_census import _evaluate_batch, primary_value


@dataclass(frozen=True)
class PairedStateResult:
    episodes: pd.DataFrame
    metrics: pd.DataFrame
    annual: pd.DataFrame
    account: pd.DataFrame
    failures: tuple[dict[str, str], ...]


def _next_open(
    timestamps: pd.DatetimeIndex,
    opens: pd.Series,
    signal_time: pd.Timestamp,
) -> tuple[pd.Timestamp, float] | None:
    position = int(timestamps.searchsorted(signal_time, side="right"))
    if position >= len(timestamps):
        return None
    fill_time = timestamps[position]
    if fill_time.normalize() != signal_time.normalize():
        return None
    return fill_time, float(opens.iloc[position])


def evaluate_paired_state_inventory(
    fifteen_minute: pd.DataFrame,
    five_minute: pd.DataFrame,
    hypotheses: Sequence[Mapping[str, object]],
    *,
    symbol: str,
    evaluation_start: pd.Timestamp | str,
    warmup_bars: int,
    baseline_one_way_cost: float,
    stress_one_way_cost: float,
    base_fraction: float,
    density_window_sessions: int,
    median_episodes_required: int,
    p10_episodes_required: int,
    force_close_unpaired_entries: bool = False,
) -> PairedStateResult:
    """Pair the first entry transition with the first later exit transition each day."""

    standard = _standardize_bars(fifteen_minute, symbol).sort_values("dt").reset_index(drop=True)
    configs = [
        {"name": name, "freq": "15分钟"}
        for name in sorted({str(item["signal_name"]) for item in hypotheses})
    ]
    mapped, failures = _evaluate_batch(
        _to_raw_bars(standard, "15分钟"),
        configs,
        sdt=str(standard["dt"].min().date()),
        init_n=warmup_bars,
        frequency="15分钟",
    )
    missing = sorted({str(item["signal_name"]) for item in hypotheses} - set(mapped))
    if missing:
        raise ValueError(f"paired state signals failed to generate: {missing}")
    five = five_minute.sort_values("Date").reset_index(drop=True)
    five_times = pd.DatetimeIndex(pd.to_datetime(five["Date"]))
    five_opens = five["Open"].astype(float)
    close_lookup = (
        five.loc[pd.to_datetime(five["Date"]).dt.strftime("%H:%M") == "15:00"]
        .assign(trade_date=lambda value: pd.to_datetime(value["Date"]).dt.normalize())
        .set_index("trade_date")[["Date", "Close"]]
    )
    evaluation_start = pd.Timestamp(evaluation_start).normalize()
    calendar = pd.DatetimeIndex(
        sorted(five.loc[five["Date"] >= evaluation_start, "Date"].dt.normalize().unique()),
        name="date",
    )
    daily_close = (
        five.loc[pd.to_datetime(five["Date"]).dt.strftime("%H:%M") == "15:00"]
        .assign(trade_date=lambda value: pd.to_datetime(value["Date"]).dt.normalize())
        .set_index("trade_date")["Close"]
        .reindex(calendar)
    )
    elapsed_years = max((calendar.max() - calendar.min()).days / 365.25, 1.0)
    episode_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    annual_rows: list[dict[str, object]] = []
    account_rows: list[dict[str, object]] = []

    for hypothesis in hypotheses:
        raw = mapped[str(hypothesis["signal_name"])][0]
        primary = raw.loc[raw.index.normalize() >= evaluation_start].map(primary_value).astype("string")
        entry_active = primary.eq(str(hypothesis["entry_state"]))
        exit_active = primary.eq(str(hypothesis["exit_state"]))
        entry_times = pd.DatetimeIndex(primary.index[entry_active & ~entry_active.shift(1, fill_value=False)])
        exit_times = pd.DatetimeIndex(primary.index[exit_active & ~exit_active.shift(1, fill_value=False)])
        exits_by_day: dict[pd.Timestamp, list[pd.Timestamp]] = {}
        for value in exit_times:
            exits_by_day.setdefault(value.normalize(), []).append(value)
        rows: list[dict[str, object]] = []
        used_days: set[pd.Timestamp] = set()
        for entry_signal in entry_times:
            day = entry_signal.normalize()
            if day in used_days:
                continue
            entry_fill = _next_open(five_times, five_opens, entry_signal)
            if entry_fill is None:
                continue
            later_exits = [value for value in exits_by_day.get(day, []) if value > entry_signal]
            exit_signal = later_exits[0] if later_exits else pd.NaT
            exit_fill = (
                _next_open(five_times, five_opens, exit_signal)
                if not pd.isna(exit_signal)
                else None
            )
            if exit_fill is not None and exit_fill[0] > entry_fill[0]:
                exit_time, exit_price = exit_fill
                exit_reason = "paired_state"
            elif force_close_unpaired_entries and day in close_lookup.index:
                forced = close_lookup.loc[day]
                exit_time = pd.Timestamp(forced["Date"])
                exit_price = float(forced["Close"])
                exit_reason = "forced_close"
                if exit_time <= entry_fill[0]:
                    continue
            else:
                continue
            entry_time, entry_price = entry_fill
            gross = exit_price / entry_price - 1.0
            rows.append(
                {
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "entry_signal_time": entry_signal,
                    "exit_signal_time": exit_signal,
                    "exit_reason": exit_reason,
                    "entry_time": entry_time,
                    "exit_time": exit_time,
                    "entry_date": day,
                    "exit_date": day,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "holding_minutes": int(
                        (five_times.searchsorted(exit_time) - five_times.searchsorted(entry_time)) * 5
                    ),
                    "gross_return": gross,
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
            used_days.add(day)
        episodes = pd.DataFrame(rows)
        episode_frames.append(episodes)
        median_count, p10_count, minimum_count, maximum_count = _rolling_episode_density(
            pd.DatetimeIndex(episodes["exit_date"]), calendar, density_window_sessions
        )
        baseline_metrics = _performance(episodes["baseline_return"], elapsed_years)
        stress_metrics = _performance(episodes["stress_return"], elapsed_years)
        positive_years = 0
        for year, sample in episodes.groupby(episodes["exit_date"].dt.year, observed=True):
            item = _performance(sample["stress_return"], 1.0)
            annual_rows.append(
                {"hypothesis_id": hypothesis["hypothesis_id"], "year": int(year), **item}
            )
            positive_years += int(float(item["mean_return"]) > 0)
        account = _account_metrics(episodes, daily_close, base_fraction=base_fraction)
        account_rows.append({"hypothesis_id": hypothesis["hypothesis_id"], **account})
        density_pass = bool(
            median_count >= median_episodes_required and p10_count >= p10_episodes_required
        )
        if not density_pass:
            evidence = "EVIDENCE_RATE_FAIL"
        elif (
            float(baseline_metrics["mean_return"]) <= 0
            or float(baseline_metrics["profit_factor"]) <= 1
        ):
            evidence = "BASELINE_DIRECTION_FAIL"
        elif (
            float(stress_metrics["mean_return"]) > 0
            and float(stress_metrics["profit_factor"]) > 1
            and positive_years >= 4
            and float(account["incremental_terminal_return_vs_static"]) > 0
        ):
            evidence = "FEASIBLE"
        else:
            evidence = "STRESS_FAIL"
        metric_rows.append(
            {
                "hypothesis_id": hypothesis["hypothesis_id"],
                "mechanism": hypothesis["mechanism"],
                "signal_name": hypothesis["signal_name"],
                "entry_state": hypothesis["entry_state"],
                "exit_state": hypothesis["exit_state"],
                "episodes": len(episodes),
                "median_holding_minutes": float(episodes["holding_minutes"].median()),
                "baseline_mean_return": baseline_metrics["mean_return"],
                "baseline_profit_factor": baseline_metrics["profit_factor"],
                "stress_mean_return": stress_metrics["mean_return"],
                "stress_profit_factor": stress_metrics["profit_factor"],
                "positive_stress_years": positive_years,
                "rolling_30d_median_episodes": median_count,
                "rolling_30d_p10_episodes": p10_count,
                "rolling_30d_min_episodes": minimum_count,
                "rolling_30d_max_episodes": maximum_count,
                "density_pass": density_pass,
                "incremental_terminal_return_vs_static": account[
                    "incremental_terminal_return_vs_static"
                ],
                "evidence": evidence,
            }
        )
    return PairedStateResult(
        episodes=pd.concat(episode_frames, ignore_index=True),
        metrics=pd.DataFrame(metric_rows),
        annual=pd.DataFrame(annual_rows),
        account=pd.DataFrame(account_rows),
        failures=tuple(failures),
    )
