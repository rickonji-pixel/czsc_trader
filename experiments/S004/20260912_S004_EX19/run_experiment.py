from __future__ import annotations

from itertools import product
import json
from pathlib import Path

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data
from czsc_trader.backtesting.closing_dislocation_replay import _cooldown, _daily_features
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260912_S004_EX19"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0.0].sum())
    losses = float(-values.loc[values < 0.0].sum())
    return gains / losses if losses > 0 else float("inf") if gains > 0 else 0.0


def _outcomes(
    sessions: pd.DatetimeIndex,
    raw_open: pd.Series,
    fee_rate: float,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for position, event_date in enumerate(sessions[:-2]):
        entry_date = sessions[position + 1]
        exit_date = sessions[position + 2]
        entry = float(raw_open.loc[entry_date])
        exit_ = float(raw_open.loc[exit_date])
        rows.append(
            {
                "event_date": event_date,
                "entry_date": entry_date,
                "entry_year": entry_date.year,
                "stress_return": (exit_ * (1.0 - fee_rate))
                / (entry * (1.0 + fee_rate))
                - 1.0,
            }
        )
    return pd.DataFrame(rows).set_index("event_date")


def _cell_metrics(
    features: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    outcomes: pd.DataFrame,
    *,
    mechanisms: list[str],
    lookback: int,
    quantile: float,
    lag: int,
    votes_required: int,
    cooldown: int,
    evaluation_start: pd.Timestamp,
    evaluation_end: pd.Timestamp,
    acceptance: dict[str, object],
) -> dict[str, object]:
    votes = pd.DataFrame(index=features.index)
    for mechanism in mechanisms:
        score = features[mechanism]
        threshold = score.shift(lag).rolling(lookback, min_periods=lookback).quantile(quantile)
        votes[mechanism] = threshold.notna() & score.gt(0.0) & score.ge(threshold)
    raw_dates = pd.DatetimeIndex(votes.index[votes.sum(axis=1).ge(votes_required)])
    events = _cooldown(raw_dates, sessions, cooldown)
    evaluation_sessions = sessions[
        (sessions >= evaluation_start) & (sessions <= evaluation_end)
    ]
    selected = pd.DatetimeIndex(sorted(events.intersection(outcomes.index)))
    selected = selected[(selected >= evaluation_start) & (selected <= evaluation_end)]
    trades = outcomes.loc[selected].copy()
    flags = pd.Series(0, index=evaluation_sessions, dtype=int)
    flags.loc[flags.index.intersection(events)] = 1
    rolling = flags.rolling(60, min_periods=60).sum().dropna()
    recent_start = evaluation_sessions[-252]
    recent = trades.loc[trades.index >= recent_start, "stress_return"]
    annual = trades.groupby("entry_year", observed=True)["stress_return"].mean()
    mean_return = float(trades["stress_return"].mean())
    profit_factor = _profit_factor(trades["stress_return"])
    rolling_median = float(rolling.median())
    rolling_p10 = float(rolling.quantile(0.10, interpolation="lower"))
    positive_years = int(annual.gt(0.0).sum())
    recent_mean = float(recent.mean())
    checks = {
        "sample": len(trades) >= int(acceptance["minimum_closed_trades"]),
        "density_median": int(acceptance["rolling_60_median_min"])
        <= rolling_median
        <= int(acceptance["rolling_60_median_max"]),
        "density_p10": rolling_p10 >= int(acceptance["rolling_60_p10_min"]),
        "mean_return": mean_return > float(acceptance["mean_return_min_exclusive"]),
        "profit_factor": profit_factor
        > float(acceptance["profit_factor_min_exclusive"]),
        "positive_years": positive_years >= int(acceptance["positive_years_min"]),
        "recent_mean": recent_mean
        > float(acceptance["recent_252_mean_min_exclusive"]),
    }
    return {
        "closed_trades": int(len(trades)),
        "rolling_60_median": rolling_median,
        "rolling_60_p10": rolling_p10,
        "stress_mean_return": mean_return,
        "stress_profit_factor": profit_factor,
        "positive_years": positive_years,
        "recent_252_trades": int(len(recent)),
        "recent_252_mean_return": recent_mean,
        "viable": all(checks.values()),
        **{f"check_{name}": value for name, value in checks.items()},
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(bool(protocol.get(key)) for key in forbidden):
        raise ValueError("neighborhood audit cannot select, promote or deploy")
    validate_experiment_archive(repo / "experiments/S004/20260912_S004_EX18")

    source = protocol["source_candidate"]
    payload = _read(repo / str(source["payload"]))
    if payload.get("candidate_hash") != source["candidate_hash"]:
        raise ValueError("source candidate identity differs from frozen protocol")
    feature = payload["rule"]["feature"]
    grid = protocol["grid"]
    center = (
        int(feature["late_window_minutes"]),
        int(feature["threshold_lookback_sessions"]),
        float(feature["threshold_quantile"]),
    )
    expected_center = (60, 120, 0.75)
    if center != expected_center:
        raise ValueError("candidate center differs from pre-registered neighborhood")
    if int(protocol["diagnostic_trials"]) != 27:
        raise ValueError("diagnostic trial count differs from pre-registration")

    dataset = protocol["dataset"]
    context = RepositoryContext.discover(repo)
    data = load_replay_data(
        context,
        str(dataset["name"]),
        str(dataset["symbol"]),
        str(dataset["asset_type"]),
        pd.Timestamp(dataset["cutoff"]).date(),
        include_one_minute=True,
    )
    sessions = pd.DatetimeIndex(pd.to_datetime(data.adjusted.daily["dt"])).normalize()
    raw_daily = data.execution_daily.copy()
    raw_daily["dt"] = pd.to_datetime(raw_daily["dt"]).dt.normalize()
    raw_open = raw_daily.set_index("dt")["open"].astype(float)
    outcomes = _outcomes(sessions, raw_open, float(protocol["stress_one_way_cost"]))
    feature_cache = {
        int(late): _daily_features(data.signal_one_minute, int(late))
        for late in grid["late_window_minutes"]
    }
    if any(not frame.index.equals(sessions) for frame in feature_cache.values()):
        raise ValueError("one-minute feature sessions differ from daily signal sessions")

    rows: list[dict[str, object]] = []
    for late, lookback, quantile in product(
        grid["late_window_minutes"],
        grid["threshold_lookback_sessions"],
        grid["threshold_quantile"],
    ):
        metrics = _cell_metrics(
            feature_cache[int(late)],
            sessions,
            outcomes,
            mechanisms=list(map(str, grid["mechanisms"])),
            lookback=int(lookback),
            quantile=float(quantile),
            lag=int(grid["threshold_lag_sessions"]),
            votes_required=int(grid["votes_required"]),
            cooldown=int(grid["cooldown_sessions"]),
            evaluation_start=pd.Timestamp(dataset["evaluation_start"]),
            evaluation_end=pd.Timestamp(dataset["evaluation_end"]),
            acceptance=protocol["cell_acceptance"],
        )
        rows.append(
            {
                "cell_id": f"LW{int(late)}-LB{int(lookback)}-Q{float(quantile):.2f}",
                "late_window_minutes": int(late),
                "threshold_lookback_sessions": int(lookback),
                "threshold_quantile": float(quantile),
                "is_center": (int(late), int(lookback), float(quantile)) == center,
                **metrics,
            }
        )
    cells = pd.DataFrame(rows)
    if len(cells) != int(protocol["diagnostic_trials"]) or cells["is_center"].sum() != 1:
        raise AssertionError("neighborhood grid is incomplete or has no unique center")

    viable = cells.loc[cells["viable"]]
    platform = protocol["platform_acceptance"]
    represented = {
        column: int(viable[column].nunique())
        for column in (
            "late_window_minutes",
            "threshold_lookback_sessions",
            "threshold_quantile",
        )
    }
    viable_fraction = float(cells["viable"].mean())
    median_mean = float(cells["stress_mean_return"].median())
    median_pf = float(cells["stress_profit_factor"].median())
    center_viable = bool(cells.loc[cells["is_center"], "viable"].iloc[0])
    checks = {
        "viable_fraction": viable_fraction
        >= float(platform["viable_cell_fraction_min"]),
        "dimension_coverage": min(represented.values())
        >= int(platform["represented_levels_per_dimension_min"]),
        "median_mean_return": median_mean
        > float(platform["median_mean_return_min_exclusive"]),
        "median_profit_factor": median_pf
        > float(platform["median_profit_factor_min_exclusive"]),
        "center_cell": center_viable if platform["center_cell_must_be_viable"] else True,
    }
    passed = all(checks.values())
    evidence_label = "FAVORABLE" if passed else "MIXED"
    route = "LOCAL_PLATFORM_SUPPORTED" if passed else "LOCAL_PLATFORM_NOT_ESTABLISHED"
    cells.to_csv(
        artifacts / "neighborhood_cells.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "evidence_label": evidence_label,
        "cells": int(len(cells)),
        "viable_cells": int(len(viable)),
        "viable_cell_fraction": viable_fraction,
        "represented_levels": represented,
        "median_stress_mean_return": median_mean,
        "median_stress_profit_factor": median_pf,
        "center_cell_viable": center_viable,
        "checks": checks,
        "source_candidate_unchanged": True,
        "diagnostic_trials_not_used_for_selection": True,
        "route_decision": route,
    }
    _write(artifacts / "neighborhood_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# 20260912_S004_EX19 执行\n\n状态：COMPLETE。完成{len(cells)}个预注册局部格点，"
        f"其中{len(viable)}个通过统一标准。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# 20260912_S004_EX19 结论\n\n"
        f"局部平台标签：`{evidence_label}`；结论：`{route}`。\n\n"
        f"通过格点{len(viable)}/{len(cells)}（{viable_fraction:.1%}）；各维度有效取值覆盖为"
        f"尾盘窗口{represented['late_window_minutes']}档、回看长度{represented['threshold_lookback_sessions']}档、"
        f"阈值分位{represented['threshold_quantile']}档；全格点压力收益中位数{median_mean:.3%}，"
        f"盈亏比中位数{median_pf:.2f}，中心格点{'通过' if center_viable else '未通过'}。\n\n"
        "本轮格点只用于诊断，没有据此选择参数，也不改变候选、SM或PTE状态。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S004",
            "symbol": dataset["symbol"],
            "candidate_id": source["candidate_id"],
            "development_cutoff": dataset["cutoff"],
            "evidence_label": evidence_label,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
