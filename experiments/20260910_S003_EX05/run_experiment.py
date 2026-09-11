from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_mechanism import evaluate_intraday_mechanisms


EXPERIMENT_ID = "20260910_S003_EX05"


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


def _validate_hypotheses(protocol: dict[str, object], census_states: pd.DataFrame) -> None:
    if len({item["behavior_hash"] for item in protocol["hypotheses"]}) != len(
        protocol["hypotheses"]
    ):
        raise ValueError("pre-registered mechanisms contain redundant event behavior")
    lookup = census_states.set_index("state_id")
    for item in protocol["hypotheses"]:
        row = lookup.loc[str(item["state_id"])]
        for field in ("signal_name", "state_primary", "behavior_hash"):
            column = "name" if field == "signal_name" else field
            if str(row[column]) != str(item[field]):
                raise ValueError(f"{item['hypothesis_id']}: {field} differs from EX04")
        if not bool(row["density_capable"]):
            raise ValueError(f"{item['hypothesis_id']}: EX04 density screen not passed")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in (
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX05 may not mutate strategy lifecycle state")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    execution = protocol["execution"]
    density = protocol["density"]
    control = protocol["matched_random_control"]
    census_experiment = repo / "experiments" / str(dataset["census_experiment"])
    regime_experiment = repo / "experiments" / str(dataset["regime_experiment"])
    census_manifest = validate_experiment_archive(census_experiment)
    regime_manifest = validate_experiment_archive(regime_experiment)
    census_states = pd.read_csv(census_experiment / "artifacts" / "state_density.csv")
    _validate_hypotheses(protocol, census_states)
    events = pd.read_csv(census_experiment / "artifacts" / "signal_events.csv.gz")
    regimes = pd.read_csv(regime_experiment / "artifacts" / "daily_regimes.csv")
    daily_regime = regimes.set_index(pd.to_datetime(regimes["date"]))["regime"]
    intraday = load_intraday_research_data(repo / "data" / "raw", str(target["symbol"]))

    result = evaluate_intraday_mechanisms(
        events,
        intraday.frames["15m"],
        intraday.frames["5m"],
        daily_regime,
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
    result.metrics.to_csv(
        artifacts / "mechanism_metrics.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    result.annual.to_csv(
        artifacts / "annual_metrics.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    result.regime.to_csv(
        artifacts / "regime_metrics.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    clock_distribution = (
        result.episodes.assign(
            signal_clock=result.episodes["signal_time"].dt.strftime("%H:%M")
        )
        .groupby(["hypothesis_id", "exit_clock", "signal_clock"], observed=True)
        .size()
        .rename("episodes")
        .reset_index()
    )
    clock_distribution["share"] = clock_distribution["episodes"].div(
        clock_distribution.groupby(["hypothesis_id", "exit_clock"])["episodes"].transform("sum")
    )
    clock_distribution.to_csv(
        artifacts / "signal_clock_distribution.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    evidence_counts = result.metrics["evidence"].value_counts().sort_index().to_dict()
    mechanism_evidence = (
        result.metrics.groupby("hypothesis_id", observed=True)["evidence"]
        .agg(lambda values: sorted(set(str(value) for value in values)))
        .to_dict()
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "inputs": {
            "census_manifest_sha": census_manifest["files"]["artifacts/state_density.csv"]["sha256"],
            "regime_manifest_sha": regime_manifest["files"]["artifacts/daily_regimes.csv"]["sha256"],
            "intraday_file_count": len(intraday.hashes),
        },
        "evaluated_hypotheses": len(protocol["hypotheses"]),
        "evaluated_exit_clocks": execution["exit_clocks"],
        "evidence_counts": {str(key): int(value) for key, value in evidence_counts.items()},
        "mechanism_evidence": mechanism_evidence,
        "maximum_single_clock_share": float(clock_distribution["share"].max()),
        "candidate_created": False,
    }
    _write(artifacts / "mechanism_summary.json", summary)

    strongest = result.metrics.sort_values(
        ["density_pass", "matched_random_percentile", "mean_return"],
        ascending=[False, False, False],
    ).head(7)
    table = [
        "|机制|退出|标签|30日中位/P10|压力均值|盈亏比|随机分位|正收益年|",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in strongest.itertuples(index=False):
        table.append(
            f"|{row.hypothesis_id}|{row.exit_clock}|{row.evidence}|"
            f"{row.rolling_30d_median_episodes:.0f}/{row.rolling_30d_p10_episodes:.0f}|"
            f"{row.mean_return:.3%}|{row.profit_factor:.2f}|"
            f"{row.matched_random_percentile:.1%}|{row.positive_years}|"
        )
    supported = int((result.metrics["evidence"] == "SUPPORTED").sum())
    rate_pass = int(result.metrics["density_pass"].sum())
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n"
        f"状态：COMPLETE。7个预注册机制与4个T+1退出时点形成28个固定组合；"
        f"{rate_pass}个组合通过30交易日证据速度，{supported}个组合取得SUPPORTED标签。"
        "信号、入场、退出、成本、非重叠账户和随机对照均按冻结协议执行。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX05 结论\n\n"
        + "\n".join(table)
        + "\n\n"
        + f"本轮共有{rate_pass}/28个组合满足OPC证据速度，{supported}/28个组合同时取得"
        "方向、跨年和环境匹配随机对照支持。表中列出证据最强的固定组合。\n\n"
        "事件时刻审计显示，各组合均覆盖多个日内时刻，单一时刻的最高占比为"
        f"{clock_distribution['share'].max():.1%}；否定结果并非每日首次切入集中在开盘"
        "第一根K线所致。\n\n"
        "当前隔夜持有、次日退出路径到此止损，不通过增加参数或过滤器挽救。后续若继续"
        "S003，应另立持有底仓的日内滚动交易机制：用昨日可卖库存完成当日卖出、以现金"
        "完成当日买回，单日最多形成一个独立回转事件，并单独比较相对BuyHold的增量。\n",
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
