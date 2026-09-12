from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260912_S004_EX04"


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


def _net_long(entry: float, exit_: float, cost: float) -> float:
    return exit_ * (1.0 - cost) / (entry * (1.0 + cost)) - 1.0


def _net_short(entry: float, exit_: float, cost: float) -> float:
    return entry * (1.0 - cost) / (exit_ * (1.0 + cost)) - 1.0


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    if losses == 0.0:
        return float("inf") if gains > 0.0 else 0.0
    return gains / losses


def _execution_returns(
    five_minute: pd.DataFrame,
    mechanisms: list[str],
) -> tuple[pd.DataFrame, dict[str, pd.Series], pd.DatetimeIndex]:
    bars = five_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    calendar = pd.DatetimeIndex(sorted(bars["trade_date"].unique()), name="trade_date")
    open_by_clock = {
        clock: bars.loc[bars["clock"].eq(clock)].set_index("trade_date")["Open"].astype(float)
        for clock in ("09:35", "10:35")
    }
    returns: dict[str, pd.Series] = {}
    mapping_rows: list[dict[str, object]] = []
    for mechanism in mechanisms:
        opening = mechanism.startswith("OPENING")
        entry_shift = 0 if opening else 1
        exit_shift = 1 if opening else 2
        clock = "10:35" if opening else "09:35"
        values: dict[pd.Timestamp, float] = {}
        for position, event_date in enumerate(calendar):
            if position + exit_shift >= len(calendar):
                continue
            entry_date = calendar[position + entry_shift]
            exit_date = calendar[position + exit_shift]
            entry = float(open_by_clock[clock].loc[entry_date])
            exit_ = float(open_by_clock[clock].loc[exit_date])
            values[event_date] = exit_ / entry - 1.0
            mapping_rows.append(
                {
                    "mechanism_id": mechanism,
                    "event_date": event_date,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "execution_clock": clock,
                    "entry_price": entry,
                    "exit_price": exit_,
                }
            )
        returns[mechanism] = pd.Series(values, dtype="float64", name="gross_return")
    return pd.DataFrame(mapping_rows), returns, calendar


def _random_percentile(
    actual_dates: pd.DatetimeIndex,
    available_returns: pd.Series,
    actual_mean: float,
    cost: float,
    iterations: int,
    rng: np.random.Generator,
) -> tuple[float, float, float]:
    control_means: list[float] = []
    actual_by_year = pd.Series(actual_dates.year, index=actual_dates).groupby(lambda x: x.year)
    available_dates = pd.DatetimeIndex(available_returns.index)
    for _ in range(iterations):
        shifted_dates: list[pd.Timestamp] = []
        for year, group in actual_by_year:
            year_calendar = available_dates[available_dates.year == year]
            if len(year_calendar) < 2:
                continue
            positions = {value: index for index, value in enumerate(year_calendar)}
            source = [value for value in group.index if value in positions]
            shift = int(rng.integers(1, len(year_calendar)))
            shifted_dates.extend(year_calendar[(positions[value] + shift) % len(year_calendar)] for value in source)
        gross = available_returns.reindex(pd.DatetimeIndex(shifted_dates)).dropna()
        if not gross.empty:
            control_means.append(float(((1.0 + gross) * (1.0 - cost) / (1.0 + cost) - 1.0).mean()))
    controls = np.asarray(control_means, dtype=float)
    if controls.size != iterations:
        raise ValueError("random control did not produce every registered iteration")
    percentile = float((controls <= actual_mean).mean() * 100.0)
    return percentile, float(np.median(controls)), float(np.quantile(controls, 0.90))


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(bool(protocol.get(key)) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("direction evaluation may not mutate lifecycle state")

    dataset = protocol["dataset"]
    target = protocol["research_target"]
    execution = protocol["execution"]
    acceptance = protocol["acceptance"]
    random_spec = protocol["random_control"]
    event_dir = repo / "experiments" / "S004" / str(dataset["event_experiment"])
    event_manifest = validate_experiment_archive(event_dir)
    events = pd.read_csv(event_dir / "artifacts" / "mechanism_events.csv", parse_dates=["trade_date"])
    density = pd.read_csv(event_dir / "artifacts" / "density_metrics.csv")
    mechanisms = list(map(str, protocol["mechanisms"]))
    if set(events["mechanism_id"]) != set(mechanisms):
        raise ValueError("registered mechanisms differ from frozen event archive")
    if not density.set_index("mechanism_id").loc[mechanisms, "evidence"].eq(str(acceptance["density_evidence_required"])).all():
        raise ValueError("one or more mechanisms lost the density prerequisite")

    loaded = load_intraday_research_data(repo / str(dataset["directory"]), str(target["symbol"]))
    mapping, available, calendar = _execution_returns(loaded.frames["5m"], mechanisms)
    base_cost = float(execution["baseline_one_way_cost"])
    stress_cost = float(execution["stress_one_way_cost"])
    recent_start = calendar[-int(acceptance["recent_sessions"])]
    rng = np.random.default_rng(int(random_spec["seed"]))
    episode_frames: list[pd.DataFrame] = []
    annual_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    for mechanism in mechanisms:
        selected_dates = pd.DatetimeIndex(events.loc[events["mechanism_id"].eq(mechanism), "trade_date"])
        execution_map = mapping.loc[mapping["mechanism_id"].eq(mechanism)].set_index("event_date")
        valid_dates = selected_dates.intersection(execution_map.index)
        episode = execution_map.loc[valid_dates].copy()
        episode.index.name = "event_date"
        episode = episode.reset_index()
        episode["gross_return"] = available[mechanism].reindex(valid_dates).to_numpy()
        episode["baseline_return"] = episode.apply(lambda row: _net_long(float(row.entry_price), float(row.exit_price), base_cost), axis=1)
        episode["stress_return"] = episode.apply(lambda row: _net_long(float(row.entry_price), float(row.exit_price), stress_cost), axis=1)
        episode["opposite_stress_return"] = episode.apply(lambda row: _net_short(float(row.entry_price), float(row.exit_price), stress_cost), axis=1)
        episode["entry_year"] = pd.to_datetime(episode["entry_date"]).dt.year
        episode_frames.append(episode)
        annual = episode.groupby("entry_year", observed=True).agg(
            episodes=("stress_return", "size"),
            stress_mean_return=("stress_return", "mean"),
            stress_total_return=("stress_return", lambda x: float((1.0 + x).prod() - 1.0)),
        ).reset_index()
        annual.insert(0, "mechanism_id", mechanism)
        annual_frames.append(annual)
        stress_mean = float(episode["stress_return"].mean())
        random_percentile, random_median, random_p90 = _random_percentile(
            valid_dates,
            available[mechanism],
            stress_mean,
            stress_cost,
            int(random_spec["iterations"]),
            rng,
        )
        recent = episode.loc[pd.to_datetime(episode["event_date"]) >= recent_start, "stress_return"]
        opposite_mean = float(episode["opposite_stress_return"].mean())
        positive_years = int(annual["stress_mean_return"].gt(0.0).sum())
        pf = _profit_factor(episode["stress_return"])
        checks = {
            "stress_positive": stress_mean > float(acceptance["stress_mean_return_min_exclusive"]),
            "profit_factor": pf > float(acceptance["stress_profit_factor_min_exclusive"]),
            "positive_years": positive_years >= int(acceptance["positive_years_required"]),
            "recent_positive": bool(not recent.empty and recent.mean() > float(acceptance["recent_stress_mean_min_exclusive"])),
            "direction": stress_mean > opposite_mean,
            "random_control": random_percentile >= float(acceptance["random_percentile_min"]),
        }
        metric_rows.append(
            {
                "mechanism_id": mechanism,
                "episodes": int(len(episode)),
                "terminal_events_skipped": int(len(selected_dates) - len(valid_dates)),
                "baseline_mean_return": float(episode["baseline_return"].mean()),
                "stress_mean_return": stress_mean,
                "stress_profit_factor": pf,
                "positive_years": positive_years,
                "recent_episodes": int(len(recent)),
                "recent_stress_mean_return": float(recent.mean()) if not recent.empty else None,
                "opposite_stress_mean_return": opposite_mean,
                "random_percentile": random_percentile,
                "random_median_return": random_median,
                "random_p90_return": random_p90,
                "checks": json.dumps(checks, ensure_ascii=False, sort_keys=True),
                "evidence": "FEASIBLE" if all(checks.values()) else "DIRECTION_FAIL",
            }
        )

    episodes = pd.concat(episode_frames, ignore_index=True)
    annual = pd.concat(annual_frames, ignore_index=True)
    metrics = pd.DataFrame(metric_rows).sort_values("stress_mean_return", ascending=False).reset_index(drop=True)
    episodes.to_csv(artifacts / "episodes.csv.gz", index=False, encoding="utf-8-sig", compression={"method": "gzip", "compresslevel": 9, "mtime": 0}, lineterminator="\n")
    annual.to_csv(artifacts / "annual_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    metrics.to_csv(artifacts / "mechanism_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    feasible = metrics.loc[metrics["evidence"].eq("FEASIBLE")]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "event_manifest_sha256": event_manifest["files"]["artifacts/mechanism_events.csv"]["sha256"],
        "evaluated_mechanisms": int(len(metrics)),
        "feasible_mechanisms": int(len(feasible)),
        "eligible_for_structural_audit": feasible["mechanism_id"].tolist(),
        "route_decision": "CONTINUE_AUDIT" if len(feasible) else "STOP_REGISTERED_MECHANISMS",
        "candidate_created": False,
    }
    _write(artifacts / "evaluation_summary.json", summary)
    table = [
        "|机制|笔数|基准均值|压力均值|压力盈亏比|正收益年|近期均值|随机分位|反向均值|标签|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in metrics.itertuples(index=False):
        table.append(
            f"|{row.mechanism_id}|{row.episodes}|{row.baseline_mean_return:.3%}|"
            f"{row.stress_mean_return:.3%}|{row.stress_profit_factor:.2f}|{row.positive_years}|"
            f"{row.recent_stress_mean_return:.3%}|{row.random_percentile:.1f}%|"
            f"{row.opposite_stress_mean_return:.3%}|{row.evidence}|"
        )
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。四个机制均按预注册T+1时点、成本、"
        f"反向对照和1000次年度循环位移完成评价；{len(feasible)}个机制取得结构审计资格。\n\n"
        "首次运行因Pandas索引名继承为trade_date而读取event_date失败；仅修正输出字段名后"
        "按原冻结协议完整重跑，未修改事件、执行、成本或裁决口径。\n",
        encoding="utf-8",
    )
    decision = "通过者进入低频状态分层与统计审计。" if len(feasible) else "四类机制均未同时通过预注册收益门，当前机制集合停止。"
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n" + "\n".join(table) + f"\n\n{decision} 本轮没有创建候选，也没有修改SM或PTE。\n",
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
