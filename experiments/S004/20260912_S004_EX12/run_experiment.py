from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260912_S004_EX12"


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


def _execution(five_minute: pd.DataFrame, base_cost: float, stress_cost: float) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    bars = five_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    session_open = bars.loc[bars["clock"].eq("09:35")].set_index("trade_date")["Open"].astype(float)
    calendar = pd.DatetimeIndex(sorted(bars["trade_date"].unique()), name="event_date")
    rows: list[dict[str, object]] = []
    for position, event_date in enumerate(calendar[:-2]):
        entry_date = calendar[position + 1]
        exit_date = calendar[position + 2]
        entry = float(session_open.loc[entry_date])
        exit_ = float(session_open.loc[exit_date])
        gross = exit_ / entry - 1.0
        rows.append(
            {
                "event_date": event_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_year": entry_date.year,
                "entry_price": entry,
                "exit_price": exit_,
                "gross_return": gross,
                "baseline_return": (1.0 + gross) * (1.0 - base_cost) / (1.0 + base_cost) - 1.0,
                "stress_return": (1.0 + gross) * (1.0 - stress_cost) / (1.0 + stress_cost) - 1.0,
            }
        )
    return pd.DataFrame(rows).set_index("event_date"), calendar


def _random_percentile(dates: pd.DatetimeIndex, outcomes: pd.Series, actual: float, iterations: int, seed: int) -> tuple[float, float, float]:
    available = pd.DatetimeIndex(outcomes.index)
    rng = np.random.default_rng(seed)
    controls: list[float] = []
    for _ in range(iterations):
        shifted: list[pd.Timestamp] = []
        for year in sorted(set(dates.year)):
            actual_year = dates[dates.year == year]
            year_calendar = available[available.year == year]
            positions = {value: index for index, value in enumerate(year_calendar)}
            source = [value for value in actual_year if value in positions]
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
    return float((values <= actual).mean() * 100.0), float(np.median(values)), float(np.quantile(values, 0.90))


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if not protocol.get("candidate_generation") or protocol.get("promotion_allowed"):
        raise ValueError("candidate construction contract differs from protocol")
    if any(bool(protocol.get(key)) for key in ("mutates_strategy_manager", "mutates_pte")):
        raise ValueError("candidate construction may not mutate SM or PTE")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    consensus = protocol["consensus"]
    density_spec = protocol["density"]
    acceptance = protocol["acceptance"]
    random_spec = protocol["random_control"]
    feature_dir = repo / "experiments" / "S004" / str(dataset["feature_experiment"])
    structural_dir = repo / "experiments" / "S004" / str(dataset["structural_experiment"])
    feature_manifest = validate_experiment_archive(feature_dir)
    structural_manifest = validate_experiment_archive(structural_dir)
    structural = _read(structural_dir / "artifacts" / "structural_summary.json")
    mechanisms = list(map(str, consensus["mechanisms"]))
    if not set(mechanisms).issubset(set(structural["supported_mechanisms"])):
        raise ValueError("one or more consensus inputs lost structural support")
    features = pd.read_csv(feature_dir / "artifacts" / "proxy_features.csv", parse_dates=["trade_date"]).set_index("trade_date")
    vote_columns = [f"{mechanism}_raw_event" for mechanism in mechanisms]
    votes = pd.DataFrame(index=features.index)
    for column in vote_columns:
        value = features[column]
        votes[column] = value if value.dtype == bool else value.astype(str).str.lower().eq("true")
    vote_count = votes.sum(axis=1)
    loaded = load_intraday_research_data(repo / str(dataset["directory"]), str(target["symbol"]))
    execution, calendar = _execution(
        loaded.frames["5m"],
        float(protocol["execution"]["baseline_one_way_cost"]),
        float(protocol["execution"]["stress_one_way_cost"]),
    )
    evaluation_start = pd.Timestamp(density_spec["evaluation_start"])
    raw_dates = pd.DatetimeIndex(vote_count.index[(vote_count >= int(consensus["votes_required"])) & (vote_count.index >= evaluation_start)])
    kept = _cooldown(raw_dates, calendar, int(consensus["cooldown_sessions"]))
    valid = kept.intersection(execution.index)
    episode = execution.loc[valid].copy()
    episode.insert(0, "vote_count", vote_count.reindex(valid).astype(int))
    episode.index.name = "event_date"
    density_calendar = calendar[calendar >= evaluation_start]
    flags = pd.Series(0, index=density_calendar, dtype="int64")
    flags.loc[flags.index.intersection(kept)] = 1
    rolling = flags.rolling(int(density_spec["window_sessions"]), min_periods=int(density_spec["window_sessions"])).sum().dropna()
    median_density = float(rolling.median())
    p10_density = float(rolling.quantile(0.10, interpolation="lower"))
    annual = episode.groupby("entry_year", observed=True)["stress_return"].agg(["size", "mean"]).reset_index()
    recent_start = calendar[-int(acceptance["recent_sessions"])]
    recent = episode.loc[episode.index >= recent_start, "stress_return"]
    stress_mean = float(episode["stress_return"].mean())
    percentile, random_median, random_p90 = _random_percentile(
        valid,
        execution["stress_return"],
        stress_mean,
        int(random_spec["iterations"]),
        int(random_spec["seed"]),
    )
    pf = _profit_factor(episode["stress_return"])
    checks = {
        "density_median": int(density_spec["median_min"]) <= median_density <= int(density_spec["median_max"]),
        "density_p10": p10_density >= int(density_spec["p10_min"]),
        "stress_mean": stress_mean > float(acceptance["stress_mean_min_exclusive"]),
        "profit_factor": pf > float(acceptance["profit_factor_min_exclusive"]),
        "positive_years": int(annual["mean"].gt(0.0).sum()) >= int(acceptance["positive_years_min"]),
        "recent_mean": bool(not recent.empty and recent.mean() > float(acceptance["recent_mean_min_exclusive"])),
        "random_control": percentile >= float(acceptance["random_percentile_min"]),
    }
    passed = all(checks.values())
    episode.reset_index().to_csv(artifacts / "candidate_episodes.csv.gz", index=False, encoding="utf-8-sig", compression={"method": "gzip", "compresslevel": 9, "mtime": 0}, lineterminator="\n")
    annual.to_csv(artifacts / "annual_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    metrics = {
        "raw_events": int(len(raw_dates)),
        "independent_events": int(len(kept)),
        "evaluated_episodes": int(len(episode)),
        "rolling_60_median": median_density,
        "rolling_60_p10": p10_density,
        "baseline_mean_return": float(episode["baseline_return"].mean()),
        "stress_mean_return": stress_mean,
        "stress_profit_factor": pf,
        "positive_years": int(annual["mean"].gt(0.0).sum()),
        "recent_episodes": int(len(recent)),
        "recent_stress_mean_return": float(recent.mean()),
        "random_percentile": percentile,
        "random_median_return": random_median,
        "random_p90_return": random_p90,
        "checks": checks,
    }
    _write(artifacts / "candidate_metrics.json", metrics)
    candidate = {
        "schema_version": 1,
        "candidate_id": target["candidate_id"],
        "strategy_id": target["strategy_id"],
        "symbol": target["symbol"],
        "status": "RESEARCH_CANDIDATE" if passed else "CONSTRUCTION_FAILED",
        "signal": {"time": "15:00", "votes_required": int(consensus["votes_required"]), "mechanisms": mechanisms},
        "execution": {"entry": "NEXT_SESSION_OPEN", "exit": "SESSION_AFTER_ENTRY_OPEN"},
        "development_cutoff": dataset["cutoff"],
        "cumulative_registered_trials": int(protocol["cumulative_registered_trials"]),
        "known_risks": [
            "EX07 showed that the raw late-return parameter family was narrow",
            "most edge occurs before 10:00 on the entry session",
            "candidate still requires formal implementation, PK and health audit"
        ],
    }
    _write(artifacts / "candidate_spec.json", candidate)
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "feature_source_sha256": feature_manifest["files"]["artifacts/proxy_features.csv"]["sha256"],
        "structural_source_sha256": structural_manifest["files"]["artifacts/structural_summary.json"]["sha256"],
        "candidate_id": target["candidate_id"],
        "candidate_created": passed,
        "route_decision": "REGISTER_RESEARCH_CANDIDATE" if passed else "STOP_CONSENSUS_RULE",
    }
    _write(artifacts / "construction_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# 20260912_S004_EX12 执行\n\n状态：COMPLETE。预注册两票共识规则完成密度、执行成本、年度、近期和随机对照评价。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# 20260912_S004_EX12 结论\n\n"
        f"- 独立事件：{len(kept)}；滚动60日中位数/P10：{median_density:.0f}/{p10_density:.0f}；\n"
        f"- 压力收益均值：{stress_mean:.3%}；盈亏比：{pf:.2f}；\n"
        f"- 正收益年：{int(annual['mean'].gt(0.0).sum())}；近期均值：{recent.mean():.3%}；\n"
        f"- 随机分位：{percentile:.1f}%。\n\n"
        f"结论：`{summary['route_decision']}`。通过只登记研究候选，尚未获得冻结或模拟盘资格。\n",
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
