from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260913_S004_EX41"


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    return gains / losses if losses > 0 else float("inf")


def _execution_returns(
    five_minute: pd.DataFrame,
    stress_cost: float,
) -> tuple[pd.Series, pd.DatetimeIndex]:
    bars = five_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    session_open = bars.loc[bars["clock"].eq("09:35")].set_index("trade_date")["Open"].astype(float)
    calendar = pd.DatetimeIndex(sorted(bars["trade_date"].unique()), name="event_date")
    returns: dict[pd.Timestamp, float] = {}
    for position, event_date in enumerate(calendar[:-2]):
        entry = float(session_open.loc[calendar[position + 1]])
        exit_price = float(session_open.loc[calendar[position + 2]])
        gross = exit_price / entry - 1.0
        returns[event_date] = (1.0 + gross) * (1.0 - stress_cost) / (1.0 + stress_cost) - 1.0
    return pd.Series(returns, name="stress_return"), calendar


def _random_percentile(
    dates: pd.DatetimeIndex,
    outcomes: pd.Series,
    actual: float,
    iterations: int,
    seed: int,
) -> tuple[float, float, float]:
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
            shifted.extend(
                year_calendar[(positions[value] + offset) % len(year_calendar)]
                for value in source
            )
        sample = outcomes.reindex(pd.DatetimeIndex(shifted)).dropna()
        if not sample.empty:
            controls.append(float(sample.mean()))
    values = np.asarray(controls, dtype=float)
    if values.size != iterations:
        raise ValueError("random control did not produce every registered iteration")
    return (
        float((values <= actual).mean() * 100.0),
        float(np.median(values)),
        float(np.quantile(values, 0.90)),
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if not protocol.get("candidate_generation") or protocol.get("promotion_allowed"):
        raise ValueError("candidate construction contract differs from protocol")
    if any(protocol.get(key) for key in ("mutates_strategy_manager", "mutates_pte")):
        raise ValueError("candidate construction may not mutate SM or PTE")

    upstream = protocol["upstream"]
    expected_hashes = {
        repo / str(upstream["manifest"]): str(upstream["manifest_sha256"]),
        repo / str(upstream["episodes"]): str(upstream["episodes_sha256"]),
        repo / str(upstream["summary"]): str(upstream["summary_sha256"]),
    }
    for path, digest in expected_hashes.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")
    validate_experiment_archive((repo / str(upstream["manifest"])).parent)
    overlay_summary = _read_json(repo / str(upstream["summary"]))
    if overlay_summary.get("risk_overlap_episodes", 0) <= 0:
        raise ValueError("fixed risk overlay did not remove any episodes")

    episodes = pd.read_csv(repo / str(upstream["episodes"]), compression="gzip")
    for column in ("event_date", "entry_date", "exit_date"):
        episodes[column] = pd.to_datetime(episodes[column]).dt.normalize()
    base = episodes.copy()
    filtered = episodes.loc[~episodes["risk_denied"].astype(bool)].copy()
    if filtered.empty:
        raise ValueError("fixed risk overlay removed every episode")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    loaded = load_intraday_research_data(repo / str(dataset["directory"]), str(target["symbol"]))
    outcomes, calendar = _execution_returns(loaded.frames["5m"], stress_cost=0.0006)
    pd.testing.assert_series_equal(
        filtered.set_index("event_date")["stress_return"].astype(float),
        outcomes.reindex(pd.DatetimeIndex(filtered["event_date"])).astype(float),
        check_names=False,
        atol=1e-12,
        rtol=1e-12,
    )
    frequency = protocol["frequency_policy"]
    density_calendar = calendar[calendar >= pd.Timestamp(str(dataset["evaluation_start"]))]
    flags = pd.Series(0, index=density_calendar, dtype="int64")
    flags.loc[flags.index.intersection(pd.DatetimeIndex(filtered["event_date"]))] = 1
    rolling = flags.rolling(
        int(frequency["rolling_sessions"]), min_periods=int(frequency["rolling_sessions"])
    ).sum().dropna()
    density_median = float(rolling.median())
    density_p10 = float(rolling.quantile(0.10, interpolation="lower"))

    annual = filtered.groupby(filtered["entry_date"].dt.year)["stress_return"].agg(
        trades="size", stress_mean="mean"
    ).reset_index(names="year")
    recent_start = pd.Timestamp(calendar[-int(protocol["acceptance"]["recent_sessions"])])
    recent = filtered.loc[filtered["event_date"].ge(recent_start), "stress_return"]
    stress_mean = float(filtered["stress_return"].mean())
    profit_factor = _profit_factor(filtered["stress_return"])
    base_mean = float(base["stress_return"].mean())
    base_pf = _profit_factor(base["stress_return"])
    random_percentile, random_median, random_p90 = _random_percentile(
        pd.DatetimeIndex(filtered["event_date"]),
        outcomes,
        stress_mean,
        int(protocol["random_control"]["iterations"]),
        int(protocol["random_control"]["seed"]),
    )
    low_vol = filtered.loc[filtered["low_volatility"].astype(bool), "stress_return"]
    gate = protocol["acceptance"]
    checks = {
        "stress_mean": stress_mean > float(gate["stress_mean_min_exclusive"]),
        "profit_factor": profit_factor > float(gate["profit_factor_min_exclusive"]),
        "positive_years": int(annual["stress_mean"].gt(0).sum()) >= int(gate["positive_years_minimum"]),
        "recent_stress_mean": not recent.empty and float(recent.mean()) > float(gate["recent_stress_mean_min_exclusive"]),
        "random_percentile": random_percentile >= float(gate["random_percentile_minimum"]),
        "exceeds_base_stress_mean": stress_mean > base_mean,
        "exceeds_base_profit_factor": profit_factor > base_pf,
    }
    passed = all(checks.values())
    metrics = {
        "base_episodes": int(len(base)),
        "filtered_episodes": int(len(filtered)),
        "removed_episodes": int(len(base) - len(filtered)),
        "rolling_60_median_observation": density_median,
        "rolling_60_p10_observation": density_p10,
        "base_stress_mean": base_mean,
        "stress_mean": stress_mean,
        "base_profit_factor": base_pf,
        "profit_factor": profit_factor,
        "positive_years": int(annual["stress_mean"].gt(0).sum()),
        "recent_episodes": int(len(recent)),
        "recent_stress_mean": float(recent.mean()),
        "low_vol_episodes": int(len(low_vol)),
        "low_vol_stress_mean_observation": float(low_vol.mean()),
        "low_vol_profit_factor_observation": _profit_factor(low_vol),
        "random_percentile": random_percentile,
        "random_median_return": random_median,
        "random_p90_return": random_p90,
        "checks": checks,
    }
    candidate = {
        "schema_version": 1,
        "candidate_id": str(target["candidate_id"]),
        "strategy_id": str(target["strategy_id"]),
        "name": "尾盘流动性错位修复（融资风险过滤）",
        "symbol": str(target["symbol"]),
        "status": "RESEARCH_CANDIDATE" if passed else "CONSTRUCTION_FAILED",
        "base_candidate": str(target["base_candidate"]),
        "signal": {
            "time": "15:00",
            "mechanisms": [
                "LATE_PATH_SELLING",
                "LATE_VOLUME_PRESSURE",
                "CLOSE_VWAP_DISLOCATION"
            ],
            "votes_required": 2,
            "cooldown_sessions": 1
        },
        "entry_risk_filter": {
            "source": "TUSHARE_MARGIN_DETAIL",
            "source_path": "experiments/S004/20260913_S004_EX36/artifacts/margin_detail.csv.gz",
            "source_sha256": "91861db5d2a8ba2cc90a34c993143fca95cf5167d7f40b4ec08163a9610b8134",
            "action": "DENY_NEXT_SESSION_ENTRY",
            "net_flow": "RZMRE_MINUS_RZCHE_DIVIDED_BY_PREVIOUS_RZYE",
            "rolling_sessions": 120,
            "quantile": 0.60,
            "requires_positive_net_flow": True,
            "same_day_return_maximum": 0.0,
            "threshold_excludes_current_session": True,
            "live_preopen_arrival_proven": False
        },
        "execution": {
            "entry": "NEXT_SESSION_OPEN",
            "exit": "SESSION_AFTER_ENTRY_OPEN"
        },
        "frequency_policy": "OBSERVATION_ONLY",
        "development_cutoff": str(dataset["development_cutoff"]),
        "cumulative_registered_trials": int(protocol["cumulative_registered_trials"]),
        "known_risks": [
            "rolling 60-session trade count median is 7 and P10 is 3",
            "low-volatility stress mean remains slightly negative",
            "margin_detail live arrival before market open is not yet proven",
            "formal replay, statistical audit, PK and freeze health remain required"
        ]
    }
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS" if passed else "FAIL",
        "candidate_id": str(target["candidate_id"]),
        "candidate_created": passed,
        "route_decision": (
            "REGISTER_RESEARCH_CANDIDATE" if passed else "STOP_S004_C002_CONSTRUCTION"
        )
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    output = filtered.copy()
    for column in ("event_date", "entry_date", "exit_date"):
        output[column] = output[column].dt.strftime("%Y-%m-%d")
    output.to_csv(artifacts / "candidate_episodes.csv.gz", index=False, compression=compression)
    annual.to_csv(artifacts / "annual_metrics.csv", index=False, lineterminator="\n")
    _write_json(artifacts / "candidate_metrics.json", metrics)
    _write_json(artifacts / "candidate_spec.json", candidate)
    _write_json(artifacts / "construction_summary.json", result)

    (experiment / "03_execution.md").write_text(
        "# S004 EX41 执行\n\n"
        f"固定过滤后{len(filtered)}笔；滚动60日频率中位数/P10为"
        f"{density_median:.0f}/{density_p10:.0f}（仅观察）；压力均值{stress_mean:.3%}、"
        f"盈亏比{profit_factor:.2f}、正收益年度{int(annual['stress_mean'].gt(0).sum())}/"
        f"{len(annual)}、近期均值{recent.mean():.3%}、随机对照分位{random_percentile:.1f}%。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX41 结论\n\n"
        f"结论：`{result['route_decision']}`。交易频率已按用户决策降级为观察指标；"
        "通过只登记研究候选，尚未获得冻结或模拟盘资格。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": str(target["strategy_id"]),
            "symbol": str(target["symbol"]),
            "development_cutoff": str(dataset["development_cutoff"]),
            "status": "COMPLETE",
            "route_decision": result["route_decision"],
            "candidate_generation": True,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
