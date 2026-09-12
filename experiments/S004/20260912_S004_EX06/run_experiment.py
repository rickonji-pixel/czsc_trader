from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_regime import classify_lagged_daily_regime


EXPERIMENT_ID = "20260912_S004_EX06"


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


def _smd(left: pd.Series, right: pd.Series) -> float:
    pooled = float(np.sqrt((left.var(ddof=1) + right.var(ddof=1)) / 2.0))
    if pooled == 0.0:
        return 0.0 if float(left.mean()) == float(right.mean()) else float("inf")
    return float((left.mean() - right.mean()) / pooled)


def _all_session_outcomes(five_minute: pd.DataFrame, *, one_way_cost: float) -> pd.Series:
    """Reproduce EX04's late-signal execution return for every eligible session."""
    bars = five_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    open_0935 = bars.loc[bars["clock"].eq("09:35")].set_index("trade_date")["Open"].astype(float)
    calendar = pd.DatetimeIndex(sorted(bars["trade_date"].unique()), name="event_date")
    values: dict[pd.Timestamp, float] = {}
    for position, event_date in enumerate(calendar[:-2]):
        entry = float(open_0935.loc[calendar[position + 1]])
        exit_ = float(open_0935.loc[calendar[position + 2]])
        values[event_date] = exit_ * (1.0 - one_way_cost) / (entry * (1.0 + one_way_cost)) - 1.0
    return pd.Series(values, dtype="float64", name="outcome")


def _bootstrap_months(
    pairs: pd.DataFrame,
    *,
    iterations: int,
    confidence: float,
    seed: int,
) -> tuple[float, float, float, float]:
    grouped = [group["paired_effect"].to_numpy(dtype=float) for _, group in pairs.groupby("event_month", sort=True)]
    if len(grouped) < 2:
        raise ValueError("matched attribution requires at least two event months")
    rng = np.random.default_rng(seed)
    values = np.empty(iterations, dtype=float)
    for index in range(iterations):
        selected = rng.integers(0, len(grouped), size=len(grouped))
        sample = np.concatenate([grouped[item] for item in selected])
        values[index] = float(sample.mean())
    alpha = (1.0 - confidence) / 2.0
    return (
        float(pairs["paired_effect"].mean()),
        float(np.quantile(values, alpha)),
        float(np.quantile(values, 1.0 - alpha)),
        float((values > 0.0).mean()),
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(bool(protocol.get(key)) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("attribution experiment may not mutate lifecycle state")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    matching = protocol["matching"]
    regime_spec = protocol["regime"]
    acceptance = protocol["acceptance"]
    event_dir = repo / "experiments" / "S004" / str(dataset["event_experiment"])
    evaluation_dir = repo / "experiments" / "S004" / str(dataset["evaluation_experiment"])
    event_manifest = validate_experiment_archive(event_dir)
    evaluation_manifest = validate_experiment_archive(evaluation_dir)
    evaluation_protocol = _read(evaluation_dir / "artifacts" / "protocol.json")
    events = pd.read_csv(event_dir / "artifacts" / "mechanism_events.csv", parse_dates=["trade_date"])
    events = events.loc[events["mechanism_id"].eq(str(target["mechanism_id"]))].copy()
    all_features = pd.read_csv(event_dir / "artifacts" / "mechanism_features.csv", parse_dates=["trade_date"])
    recorded_outcome = pd.read_csv(evaluation_dir / "artifacts" / "episodes.csv.gz", parse_dates=["event_date"])
    recorded_outcome = recorded_outcome.loc[
        recorded_outcome["mechanism_id"].eq(str(target["mechanism_id"]))
    ].set_index("event_date")["stress_return"].astype(float)

    loaded = load_intraday_research_data(repo / str(dataset["directory"]), str(target["symbol"]))
    outcome = _all_session_outcomes(
        loaded.frames["5m"],
        one_way_cost=float(evaluation_protocol["execution"]["stress_one_way_cost"]),
    )
    reconstruction = outcome.reindex(recorded_outcome.index).sub(recorded_outcome).abs().max()
    if not np.isfinite(reconstruction) or float(reconstruction) > 1e-12:
        raise ValueError(f"all-session return construction differs from EX04: {reconstruction}")
    one = loaded.frames["1m"].copy()
    one["Date"] = pd.to_datetime(one["Date"], errors="raise")
    one["trade_date"] = one["Date"].dt.normalize()
    grouped = one.groupby("trade_date", sort=True, observed=True)
    daily = pd.DataFrame(index=grouped.size().index)
    daily["open"] = grouped["Open"].first().astype(float)
    daily["high"] = grouped["High"].max().astype(float)
    daily["low"] = grouped["Low"].min().astype(float)
    daily["close"] = grouped["Close"].last().astype(float)
    daily["volume"] = grouped["Volume"].sum().astype(float)
    daily["daily_return"] = daily["close"] / daily["open"] - 1.0
    daily["daily_range"] = daily["high"] / daily["low"] - 1.0
    daily["volume_ratio"] = daily["volume"] / daily["volume"].shift(1).rolling(20, min_periods=20).median()
    daily["lagged_20d_return"] = daily["close"].shift(1) / daily["close"].shift(21) - 1.0
    regimes = classify_lagged_daily_regime(
        daily["close"],
        lookback=int(regime_spec["lookback"]),
        er_threshold=float(regime_spec["er_threshold"]),
    )
    daily["regime"] = regimes["regime"].astype(str)
    daily["year"] = daily.index.year
    opening = all_features.set_index("trade_date")["opening_return"].astype(float)
    daily["opening_return"] = opening.reindex(daily.index)
    raw_event = all_features.set_index("trade_date")[f"{target['mechanism_id']}_raw_event"]
    if raw_event.dtype != bool:
        raw_event = raw_event.astype(str).str.lower().eq("true")
    daily["raw_event"] = raw_event.reindex(daily.index).fillna(False).astype(bool)
    daily["outcome"] = outcome.reindex(daily.index)
    daily = daily.loc[daily.index >= pd.Timestamp(dataset["evaluation_start"])].dropna(subset=list(matching["continuous"]) + ["outcome"])

    covariates = list(map(str, matching["continuous"]))
    scale = daily[covariates].std(ddof=1).replace(0.0, 1.0)
    event_dates = pd.DatetimeIndex(events["trade_date"]).intersection(daily.index)
    calendar_positions = {value: index for index, value in enumerate(daily.index)}
    pairs: list[dict[str, object]] = []
    for event_date in event_dates:
        event_row = daily.loc[event_date]
        controls = daily.loc[
            ~daily["raw_event"]
            & daily["year"].eq(event_row["year"])
            & daily["regime"].eq(event_row["regime"])
            & daily["daily_return"].sub(float(event_row["daily_return"])).abs().le(float(matching["daily_return_caliper"]))
        ].copy()
        controls = controls.loc[
            [abs(calendar_positions[value] - calendar_positions[event_date]) >= int(matching["minimum_session_distance"]) for value in controls.index]
        ]
        if controls.empty:
            continue
        distance = ((controls[covariates] - event_row[covariates]) / scale).pow(2).sum(axis=1)
        control_date = pd.Timestamp(distance.idxmin())
        row = {
            "event_date": event_date,
            "control_date": control_date,
            "regime": event_row["regime"],
            "year": int(event_row["year"]),
            "event_return": float(event_row["outcome"]),
            "control_return": float(daily.loc[control_date, "outcome"]),
        }
        row["paired_effect"] = row["event_return"] - row["control_return"]
        for covariate in covariates:
            row[f"event_{covariate}"] = float(event_row[covariate])
            row[f"control_{covariate}"] = float(daily.loc[control_date, covariate])
        pairs.append(row)
    matched = pd.DataFrame(pairs)
    if matched.empty:
        raise ValueError("no events passed the pre-registered matching rules")
    matched["event_month"] = pd.to_datetime(matched["event_date"]).dt.to_period("M").astype(str)
    balance_rows = []
    for covariate in covariates:
        value = _smd(matched[f"event_{covariate}"], matched[f"control_{covariate}"])
        balance_rows.append({"covariate": covariate, "smd": value, "absolute_smd": abs(value)})
    balance = pd.DataFrame(balance_rows)
    bootstrap_spec = protocol["bootstrap"]
    effect, lower, upper, positive_probability = _bootstrap_months(
        matched,
        iterations=int(bootstrap_spec["iterations"]),
        confidence=float(bootstrap_spec["confidence"]),
        seed=int(bootstrap_spec["seed"]),
    )
    retention = float(len(matched) / len(event_dates))
    max_smd = float(balance["absolute_smd"].max())
    checks = {
        "retention": retention >= float(acceptance["event_retention_min"]),
        "balance": max_smd <= float(acceptance["max_absolute_smd"]),
        "effect_mean": effect > float(acceptance["paired_effect_mean_min_exclusive"]),
        "effect_ci_lower": lower > float(acceptance["paired_effect_ci_lower_min_exclusive"]),
    }
    label = "ATTRIBUTION_SUPPORTED" if all(checks.values()) else "ATTRIBUTION_UNRESOLVED"
    matched.to_csv(artifacts / "matched_pairs.csv.gz", index=False, encoding="utf-8-sig", compression={"method": "gzip", "compresslevel": 9, "mtime": 0}, lineterminator="\n")
    balance.to_csv(artifacts / "covariate_balance.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "event_source_sha256": event_manifest["files"]["artifacts/mechanism_events.csv"]["sha256"],
        "outcome_source_sha256": evaluation_manifest["files"]["artifacts/episodes.csv.gz"]["sha256"],
        "eligible_events": int(len(event_dates)),
        "matched_events": int(len(matched)),
        "event_retention": retention,
        "unique_controls": int(matched["control_date"].nunique()),
        "max_absolute_smd": max_smd,
        "matched_event_mean_return": float(matched["event_return"].mean()),
        "matched_control_mean_return": float(matched["control_return"].mean()),
        "paired_effect_mean": effect,
        "paired_effect_ci_90": [lower, upper],
        "bootstrap_positive_probability": positive_probability,
        "checks": checks,
        "evidence": label,
        "candidate_created": False,
    }
    _write(artifacts / "attribution_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n"
        "首次执行因误把事件收益表当作全交易日收益表，普通控制日没有可比收益，零匹配后明确失败。"
        "实现修正为按EX04冻结的09:35时点和压力成本，从同一份5分钟数据构造全交易日收益；"
        "事件收益逐笔重建误差不超过1e-12，匹配协议没有变化。\n\n"
        f"状态：COMPLETE。{len(event_dates)}个可评价事件中{len(matched)}个"
        f"完成预注册匹配，使用{matched['control_date'].nunique()}个唯一控制日；5000次事件月份"
        "整块Bootstrap完成。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n"
        f"- 事件保留率：{retention:.1%}；\n"
        f"- 最大绝对标准化均值差：{max_smd:.3f}；\n"
        f"- 匹配后压力收益差：{effect:.3%}；\n"
        f"- 90%区间：[{lower:.3%}, {upper:.3%}]；\n"
        f"- Bootstrap收益差为正概率：{positive_probability:.1%}。\n\n"
        f"归因标签：`{label}`。本轮没有修改机制或创建候选。\n",
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
