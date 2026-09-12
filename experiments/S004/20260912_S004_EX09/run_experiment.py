from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260912_S004_EX09"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _execution_table(five_minute: pd.DataFrame) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
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
                "exit_date": exit_date,
                "entry_year": entry_date.year,
                "entry_price": entry,
                "exit_price": exit_,
                "gross_return": exit_ / entry - 1.0,
            }
        )
    return pd.DataFrame(rows).set_index("event_date"), calendar


def _net_long(gross: pd.Series, cost: float) -> pd.Series:
    return (1.0 + gross) * (1.0 - cost) / (1.0 + cost) - 1.0


def _net_short(entry: pd.Series, exit_: pd.Series, cost: float) -> pd.Series:
    return entry * (1.0 - cost) / (exit_ * (1.0 + cost)) - 1.0


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0.0].sum())
    losses = float(-values.loc[values < 0.0].sum())
    if losses == 0.0:
        return float("inf") if gains > 0.0 else 0.0
    return gains / losses


def _random_percentile(
    actual_dates: pd.DatetimeIndex,
    outcomes: pd.Series,
    actual_mean: float,
    *,
    iterations: int,
    rng: np.random.Generator,
) -> tuple[float, float, float]:
    available = pd.DatetimeIndex(outcomes.index)
    controls: list[float] = []
    for _ in range(iterations):
        shifted: list[pd.Timestamp] = []
        for year in sorted(set(actual_dates.year)):
            year_dates = actual_dates[actual_dates.year == year]
            year_calendar = available[available.year == year]
            positions = {value: index for index, value in enumerate(year_calendar)}
            source = [value for value in year_dates if value in positions]
            if not source or len(year_calendar) < 2:
                continue
            offset = int(rng.integers(1, len(year_calendar)))
            shifted.extend(year_calendar[(positions[value] + offset) % len(year_calendar)] for value in source)
        sample = outcomes.reindex(pd.DatetimeIndex(shifted)).dropna()
        if not sample.empty:
            controls.append(float(sample.mean()))
    values = np.asarray(controls, dtype=float)
    if values.size != iterations:
        raise ValueError("random control did not produce every registered iteration")
    return float((values <= actual_mean).mean() * 100.0), float(np.median(values)), float(np.quantile(values, 0.90))


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(bool(protocol.get(key)) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("direction test may not mutate lifecycle state")
    if len(protocol["mechanisms"]) != int(protocol["registered_trials"]):
        raise ValueError("mechanism count differs from registered trials")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    execution = protocol["execution"]
    acceptance = protocol["acceptance"]
    random_spec = protocol["random_control"]
    event_dir = repo / "experiments" / "S004" / str(dataset["event_experiment"])
    event_manifest = validate_experiment_archive(event_dir)
    density = pd.read_csv(event_dir / "artifacts" / "density_metrics.csv").set_index("mechanism_id")
    mechanisms = list(map(str, protocol["mechanisms"]))
    if not density.loc[mechanisms, "evidence"].eq(str(acceptance["density_evidence_required"])).all():
        raise ValueError("one or more proxies lost the density prerequisite")
    events = pd.read_csv(event_dir / "artifacts" / "proxy_events.csv.gz", parse_dates=["trade_date"])
    loaded = load_intraday_research_data(repo / str(dataset["directory"]), str(target["symbol"]))
    execution_table, calendar = _execution_table(loaded.frames["5m"])
    base_cost = float(execution["baseline_one_way_cost"])
    stress_cost = float(execution["stress_one_way_cost"])
    all_stress = _net_long(execution_table["gross_return"], stress_cost)
    recent_start = calendar[-int(acceptance["recent_sessions"])]
    rng = np.random.default_rng(int(random_spec["seed"]))
    episode_frames: list[pd.DataFrame] = []
    annual_frames: list[pd.DataFrame] = []
    rows: list[dict[str, object]] = []
    for mechanism in mechanisms:
        selected = pd.DatetimeIndex(events.loc[events["mechanism_id"].eq(mechanism), "trade_date"])
        valid = selected.intersection(execution_table.index)
        episode = execution_table.loc[valid].copy()
        episode.insert(0, "mechanism_id", mechanism)
        episode["baseline_return"] = _net_long(episode["gross_return"], base_cost)
        episode["stress_return"] = _net_long(episode["gross_return"], stress_cost)
        episode["opposite_stress_return"] = _net_short(episode["entry_price"], episode["exit_price"], stress_cost)
        episode.index.name = "event_date"
        episode_frames.append(episode.reset_index())
        annual = episode.groupby("entry_year", observed=True)["stress_return"].agg(["size", "mean"]).reset_index()
        annual.insert(0, "mechanism_id", mechanism)
        annual_frames.append(annual)
        recent = episode.loc[episode.index >= recent_start, "stress_return"]
        stress_mean = float(episode["stress_return"].mean())
        percentile, random_median, random_p90 = _random_percentile(
            valid,
            all_stress,
            stress_mean,
            iterations=int(random_spec["iterations"]),
            rng=rng,
        )
        pf = _profit_factor(episode["stress_return"])
        positive_years = int(annual["mean"].gt(0.0).sum())
        opposite = float(episode["opposite_stress_return"].mean())
        checks = {
            "stress_mean": stress_mean > float(acceptance["stress_mean_min_exclusive"]),
            "profit_factor": pf > float(acceptance["profit_factor_min_exclusive"]),
            "positive_years": positive_years >= int(acceptance["positive_years_min"]),
            "recent_mean": bool(not recent.empty and recent.mean() > float(acceptance["recent_mean_min_exclusive"])),
            "direction": stress_mean > opposite,
            "random_control": percentile >= float(acceptance["random_percentile_min"]),
        }
        rows.append(
            {
                "mechanism_id": mechanism,
                "episodes": int(len(episode)),
                "baseline_mean_return": float(episode["baseline_return"].mean()),
                "stress_mean_return": stress_mean,
                "stress_profit_factor": pf,
                "positive_years": positive_years,
                "recent_episodes": int(len(recent)),
                "recent_stress_mean_return": float(recent.mean()),
                "opposite_stress_mean_return": opposite,
                "random_percentile": percentile,
                "random_median_return": random_median,
                "random_p90_return": random_p90,
                "checks": json.dumps(checks, ensure_ascii=False, sort_keys=True),
                "evidence": "FEASIBLE" if all(checks.values()) else "DIRECTION_FAIL",
            }
        )
    metrics = pd.DataFrame(rows).sort_values("stress_mean_return", ascending=False).reset_index(drop=True)
    episodes = pd.concat(episode_frames, ignore_index=True)
    annual = pd.concat(annual_frames, ignore_index=True)
    metrics.to_csv(artifacts / "proxy_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    episodes.to_csv(artifacts / "proxy_episodes.csv.gz", index=False, encoding="utf-8-sig", compression={"method": "gzip", "compresslevel": 9, "mtime": 0}, lineterminator="\n")
    annual.to_csv(artifacts / "annual_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    feasible = metrics.loc[metrics["evidence"].eq("FEASIBLE")]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "event_source_sha256": event_manifest["files"]["artifacts/proxy_events.csv.gz"]["sha256"],
        "registered_trials": int(protocol["registered_trials"]),
        "feasible_mechanisms": feasible["mechanism_id"].tolist(),
        "route_decision": "CONTINUE_STRUCTURAL_AUDIT" if not feasible.empty else "STOP_PROXY_FAMILY",
        "candidate_created": False,
    }
    _write(artifacts / "direction_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# 20260912_S004_EX09 执行\n\n状态：COMPLETE。四个预注册代理均按统一执行、成本、年度、近期和随机对照口径完成评价。\n",
        encoding="utf-8",
    )
    lines = ["# 20260912_S004_EX09 结论", "", "|代理|事件|压力均值|盈亏比|正收益年|近期均值|随机分位|标签|", "|---|---:|---:|---:|---:|---:|---:|---|"]
    for row in metrics.itertuples(index=False):
        lines.append(
            f"|{row.mechanism_id}|{row.episodes}|{row.stress_mean_return:.3%}|{row.stress_profit_factor:.2f}|"
            f"{row.positive_years}|{row.recent_stress_mean_return:.3%}|{row.random_percentile:.1f}%|{row.evidence}|"
        )
    lines.extend(["", f"下一步：`{summary['route_decision']}`。本轮没有选择最优代理或创建候选。", ""])
    (experiment / "04_conclusion.md").write_text("\n".join(lines), encoding="utf-8")
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
