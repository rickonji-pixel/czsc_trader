from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_mechanism import evaluate_intraday_mechanisms
from czsc_trader.intraday_regime import classify_lagged_daily_regime
from czsc_trader.strategy_runtime import apply_resolved_strategy


EXPERIMENT_ID = "20260913_S001_EX17"


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


def _validate_hypotheses(protocol: dict[str, object], coverage: pd.DataFrame) -> None:
    if len({item["behavior_hash"] for item in protocol["hypotheses"]}) != len(protocol["hypotheses"]):
        raise ValueError("pre-registered mechanisms contain redundant event behavior")
    lookup = coverage.set_index("state_id")
    for item in protocol["hypotheses"]:
        row = lookup.loc[str(item["state_id"])]
        for field in ("signal_name", "state_primary", "behavior_hash"):
            column = "name" if field == "signal_name" else field
            if str(row[column]) != str(item[field]):
                raise ValueError(f"{item['hypothesis_id']}: {field} differs from EX16")
        if not bool(row["coverage_pass"]):
            raise ValueError(f"{item['hypothesis_id']}: EX16 coverage screen not passed")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX17 may not mutate strategy lifecycle state")

    target = dict(protocol["research_target"])
    dataset = dict(protocol["dataset"])
    execution = dict(protocol["execution"])
    density = dict(protocol["density"])
    control = dict(protocol["matched_random_control"])
    context_spec = dict(protocol["daily_context"])
    coverage_dir = repo / "experiments" / "S001" / str(dataset["coverage_experiment"])
    coverage_manifest = validate_experiment_archive(coverage_dir)
    coverage = pd.read_csv(coverage_dir / "artifacts" / "flat_coverage.csv")
    _validate_hypotheses(protocol, coverage)
    events = pd.read_csv(
        coverage_dir / "artifacts" / "clean_flat_events.csv.gz",
        parse_dates=["event_time", "event_date"],
    )
    selected_ids = {str(item["state_id"]) for item in protocol["hypotheses"]}
    events = events.loc[events["state_id"].isin(selected_ids)].copy()

    context = RepositoryContext.discover(repo)
    source = resolve_registered_strategy(context, str(target["strategy_id"]), str(target["source_version"]))
    if source.source_hash != target["source_release_hash"]:
        raise ValueError("source strategy release differs from frozen protocol")
    replay = load_replay_data(
        context,
        "research",
        str(target["symbol"]),
        str(target["asset_type"]),
        pd.Timestamp(str(dataset["cutoff"])).date(),
    )
    applied = apply_resolved_strategy(replay.adjusted, source.resolved_rule)
    loaded = load_intraday_research_data(repo / str(dataset["directory"]), str(target["symbol"]))
    one = loaded.frames["1m"].copy()
    one["trade_date"] = pd.to_datetime(one["Date"], errors="raise").dt.normalize()
    daily_close = one.groupby("trade_date", sort=True, observed=True)["Close"].last().astype(float)
    regimes = classify_lagged_daily_regime(
        daily_close,
        lookback=int(context_spec["lookback"]),
        er_threshold=float(context_spec["er_threshold"]),
    )["regime"].astype("string")
    close_target = applied.target_position.reindex(regimes.index).astype(float)
    clean_flat = close_target.shift(1, fill_value=0.0).eq(0.0) & close_target.eq(0.0)
    matched_context = regimes.where(clean_flat, "ineligible_nonflat")

    result = evaluate_intraday_mechanisms(
        events,
        loaded.frames["15m"],
        loaded.frames["5m"],
        matched_context,
        protocol["hypotheses"],
        evaluation_start=str(dataset["evaluation_start"]),
        exit_clocks=execution["exit_clocks"],
        baseline_one_way_cost=float(execution["baseline_one_way_cost"]),
        stress_one_way_cost=float(execution["stress_one_way_cost"]),
        density_window_sessions=int(density["window_sessions"]),
        median_episodes_required=int(density["median_closed_episodes_required"]),
        p10_episodes_required=int(density["p10_closed_episodes_required"]),
        matched_random_simulations=int(control["simulations"]),
        random_seed=int(control["seed"]),
    )
    result.episodes.to_csv(
        artifacts / "episodes.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    result.metrics.to_csv(artifacts / "mechanism_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    result.annual.to_csv(artifacts / "annual_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    result.regime.to_csv(artifacts / "regime_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    counts = result.metrics["evidence"].value_counts().sort_index().to_dict()
    strongest = result.metrics.sort_values(
        ["evidence", "matched_random_percentile", "mean_return"],
        ascending=[False, False, False],
    ).head(7)
    strongest.to_csv(artifacts / "strongest_paths.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "coverage_manifest_sha256": coverage_manifest["files"]["artifacts/flat_coverage.csv"]["sha256"],
        "evaluated_hypotheses": len(protocol["hypotheses"]),
        "evaluated_exit_clocks": execution["exit_clocks"],
        "evaluated_paths": int(len(result.metrics)),
        "evidence_counts": {str(key): int(value) for key, value in counts.items()},
        "supported_paths": result.metrics.loc[result.metrics["evidence"].eq("SUPPORTED"), ["hypothesis_id", "exit_clock"]].to_dict("records"),
        "candidate_created": False,
    }
    _write(artifacts / "mechanism_summary.json", summary)
    lines = [
        "|机制|退出|证据|30日中位/P10|压力均值|盈亏比|随机分位|正收益年|",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in strongest.itertuples(index=False):
        lines.append(
            f"|{row.hypothesis_id}|{row.exit_clock}|{row.evidence}|"
            f"{row.rolling_30d_median_episodes:.0f}/{row.rolling_30d_p10_episodes:.0f}|"
            f"{row.mean_return:.3%}|{row.profit_factor:.2f}|"
            f"{row.matched_random_percentile:.1%}|{row.positive_years}|"
        )
    supported = len(summary["supported_paths"])
    (experiment / "03_execution.md").write_text(
        "# S001 EX17 执行\n\n"
        f"状态：COMPLETE。7个预注册机制与3个T+1退出时点形成21条固定路径；"
        f"{supported}条取得SUPPORTED标签。全部事件来自S001连续空仓日。\n",
        encoding="utf-8",
    )
    route = "PROCEED_TO_INCREMENTAL_ACCOUNT_REPLAY" if supported else "STOP_INTRADAY_COMPLEMENT_PATH"
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX17 结论\n\n"
        + "\n".join(lines)
        + "\n\n"
        + f"取得SUPPORTED标签的固定路径共{supported}条。裁决：`{route}`。"
        "本轮仍是单机制事件研究；只有下一轮把支持路径接入S001互斥账户重放，并实际增加"
        "闭合交易且不破坏收益、回撤和交易独立性，才可能形成研究候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "strategy_version": target["source_version"],
            "symbol": target["symbol"],
            "development_cutoff": dataset["cutoff"],
            "decision": route,
            "promotion_allowed": False,
            "trial_count_added": len(result.metrics),
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
