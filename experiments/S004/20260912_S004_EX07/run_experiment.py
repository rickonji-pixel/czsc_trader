from __future__ import annotations

import json
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260912_S004_EX07"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _cooldown(raw_dates: pd.DatetimeIndex, calendar: pd.DatetimeIndex, sessions: int) -> pd.DatetimeIndex:
    positions = {value: index for index, value in enumerate(calendar)}
    kept: list[pd.Timestamp] = []
    last = -10**9
    for value in raw_dates.sort_values():
        position = positions[value]
        if position - last > sessions:
            kept.append(value)
            last = position
    return pd.DatetimeIndex(kept, name="event_date")


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0.0].sum())
    losses = float(-values.loc[values < 0.0].sum())
    if losses == 0.0:
        return float("inf") if gains > 0.0 else 0.0
    return gains / losses


def _execution_table(five_minute: pd.DataFrame, cost: float) -> pd.DataFrame:
    bars = five_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    open_0935 = bars.loc[bars["clock"].eq("09:35")].set_index("trade_date")["Open"].astype(float)
    calendar = pd.DatetimeIndex(sorted(bars["trade_date"].unique()), name="event_date")
    rows: list[dict[str, object]] = []
    for position, event_date in enumerate(calendar[:-2]):
        entry_date = calendar[position + 1]
        exit_date = calendar[position + 2]
        entry = float(open_0935.loc[entry_date])
        exit_ = float(open_0935.loc[exit_date])
        rows.append(
            {
                "event_date": event_date,
                "entry_date": entry_date,
                "entry_year": entry_date.year,
                "stress_return": exit_ * (1.0 - cost) / (entry * (1.0 + cost)) - 1.0,
            }
        )
    return pd.DataFrame(rows).set_index("event_date")


def _late_returns(one_minute: pd.DataFrame, windows: list[int]) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    bars = one_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars["trade_date"] = bars["Date"].dt.normalize()
    rows: list[dict[str, object]] = []
    for trade_date, day in bars.groupby("trade_date", sort=True, observed=True):
        if len(day) != 240:
            raise ValueError(f"{trade_date.date()}: incomplete minute session")
        row: dict[str, object] = {"trade_date": trade_date}
        for window in windows:
            section = day.iloc[-window:]
            row[f"late_return_{window}"] = float(section.iloc[-1]["Close"] / section.iloc[0]["Open"] - 1.0)
        rows.append(row)
    frame = pd.DataFrame(rows).set_index("trade_date")
    return frame, pd.DatetimeIndex(frame.index, name="trade_date")


def _density(dates: pd.DatetimeIndex, calendar: pd.DatetimeIndex, window: int) -> tuple[float, float]:
    flags = pd.Series(0, index=calendar, dtype="int64")
    flags.loc[flags.index.intersection(dates)] = 1
    rolling = flags.rolling(window, min_periods=window).sum().dropna()
    return float(rolling.median()), float(rolling.quantile(0.10, interpolation="lower"))


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(bool(protocol.get(key)) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("platform audit may not mutate lifecycle state")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    grid = protocol["grid"]
    execution = protocol["execution"]
    density_spec = protocol["density"]
    cell_acceptance = protocol["cell_acceptance"]
    platform_acceptance = protocol["platform_acceptance"]
    windows = list(map(int, grid["late_window_minutes"]))
    lookbacks = list(map(int, grid["lookback_sessions"]))
    quantiles = list(map(float, grid["quantiles"]))
    if len(windows) * len(lookbacks) * len(quantiles) != int(protocol["registered_trials"]):
        raise ValueError("grid size differs from registered trial count")

    loaded = load_intraday_research_data(repo / str(dataset["directory"]), str(target["symbol"]))
    late, calendar = _late_returns(loaded.frames["1m"], windows)
    execution_table = _execution_table(loaded.frames["5m"], float(execution["stress_one_way_cost"]))
    evaluation_start = pd.Timestamp(dataset["evaluation_start"])
    density_calendar = calendar[calendar >= evaluation_start]
    recent_start = calendar[-int(cell_acceptance["recent_sessions"])]
    rows: list[dict[str, object]] = []
    episode_frames: list[pd.DataFrame] = []
    for window, lookback, quantile in product(windows, lookbacks, quantiles):
        score = (-late[f"late_return_{window}"]).clip(lower=0.0)
        threshold = score.shift(int(grid["lag_sessions"])).rolling(lookback, min_periods=lookback).quantile(quantile)
        raw = threshold.notna() & score.gt(0.0) & score.ge(threshold)
        raw_dates = pd.DatetimeIndex(late.index[raw & (late.index >= evaluation_start)])
        kept = _cooldown(raw_dates, calendar, int(grid["cooldown_sessions"]))
        valid = kept.intersection(execution_table.index)
        episode = execution_table.loc[valid].copy().reset_index()
        cell_id = f"W{window:03d}_L{lookback:03d}_Q{int(round(quantile * 100)):02d}"
        episode.insert(0, "cell_id", cell_id)
        episode_frames.append(episode)
        median_density, p10_density = _density(kept, density_calendar, int(density_spec["window_sessions"]))
        returns = episode["stress_return"]
        annual = episode.groupby("entry_year", observed=True)["stress_return"].mean()
        recent = episode.loc[episode["event_date"] >= recent_start, "stress_return"]
        pf = _profit_factor(returns)
        checks = {
            "density_median": float(density_spec["median_min"]) <= median_density <= float(density_spec["median_max"]),
            "density_p10": p10_density >= float(density_spec["p10_min"]),
            "stress_mean": float(returns.mean()) > float(cell_acceptance["stress_mean_min_exclusive"]),
            "profit_factor": pf > float(cell_acceptance["profit_factor_min_exclusive"]),
            "positive_years": int(annual.gt(0.0).sum()) >= int(cell_acceptance["positive_years_min"]),
            "recent_mean": bool(not recent.empty and recent.mean() > float(cell_acceptance["recent_mean_min_exclusive"])),
        }
        rows.append(
            {
                "cell_id": cell_id,
                "late_window_minutes": window,
                "lookback_sessions": lookback,
                "quantile": quantile,
                "raw_events": int(len(raw_dates)),
                "independent_events": int(len(kept)),
                "rolling_60_median": median_density,
                "rolling_60_p10": p10_density,
                "stress_mean_return": float(returns.mean()),
                "stress_profit_factor": pf,
                "positive_years": int(annual.gt(0.0).sum()),
                "recent_episodes": int(len(recent)),
                "recent_stress_mean_return": float(recent.mean()),
                "checks": json.dumps(checks, ensure_ascii=False, sort_keys=True),
                "evidence": "VIABLE" if all(checks.values()) else "CELL_FAIL",
            }
        )
    metrics = pd.DataFrame(rows)
    episodes = pd.concat(episode_frames, ignore_index=True)
    viable = metrics.loc[metrics["evidence"].eq("VIABLE")]
    primary = platform_acceptance["primary"]
    primary_mask = (
        metrics["late_window_minutes"].eq(int(primary["late_window_minutes"]))
        & metrics["lookback_sessions"].eq(int(primary["lookback_sessions"]))
        & metrics["quantile"].eq(float(primary["quantile"]))
    )
    represented = {
        "late_window_minutes": int(viable["late_window_minutes"].nunique()),
        "lookback_sessions": int(viable["lookback_sessions"].nunique()),
        "quantile": int(viable["quantile"].nunique()),
    }
    required_levels = int(platform_acceptance["required_distinct_levels_per_dimension"])
    checks = {
        "primary_viable": bool(metrics.loc[primary_mask, "evidence"].eq("VIABLE").all() and primary_mask.sum() == 1),
        "majority_viable": len(viable) >= int(platform_acceptance["viable_cells_min"]),
        "all_window_levels": represented["late_window_minutes"] >= required_levels,
        "all_lookback_levels": represented["lookback_sessions"] >= required_levels,
        "all_quantile_levels": represented["quantile"] >= required_levels,
        "median_return_positive": float(metrics["stress_mean_return"].median()) > float(platform_acceptance["median_stress_mean_min_exclusive"]),
    }
    label = "BROAD_PLATFORM" if all(checks.values()) else "NARROW_OR_UNSTABLE"
    metrics.to_csv(artifacts / "platform_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    episodes.to_csv(artifacts / "platform_episodes.csv.gz", index=False, encoding="utf-8-sig", compression={"method": "gzip", "compresslevel": 9, "mtime": 0}, lineterminator="\n")
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "registered_trials": int(protocol["registered_trials"]),
        "viable_cells": int(len(viable)),
        "represented_levels": represented,
        "platform_median_stress_mean_return": float(metrics["stress_mean_return"].median()),
        "platform_min_stress_mean_return": float(metrics["stress_mean_return"].min()),
        "platform_max_stress_mean_return": float(metrics["stress_mean_return"].max()),
        "checks": checks,
        "evidence": label,
        "candidate_created": False,
    }
    _write(artifacts / "platform_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。27个预注册参数单元均使用同一数据、执行和成本口径完成评价。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n"
        f"- 可行单元：{len(viable)}/27；\n"
        f"- 三个维度覆盖水平数：{represented}；\n"
        f"- 平台压力收益中位数：{metrics['stress_mean_return'].median():.3%}；\n"
        f"- 平台压力收益范围：[{metrics['stress_mean_return'].min():.3%}, {metrics['stress_mean_return'].max():.3%}]。\n\n"
        f"平台标签：`{label}`。本轮没有选择最优参数或创建候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": dataset["cutoff"],
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
