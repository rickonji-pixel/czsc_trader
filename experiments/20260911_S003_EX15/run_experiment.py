from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_medium_evaluation import evaluate_medium_frequency_mechanisms


EXPERIMENT_ID = "20260911_S003_EX15"


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


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX15 may not mutate strategy lifecycle state")
    target = protocol["research_target"]
    dataset = protocol["dataset"]
    execution = protocol["execution"]
    density = protocol["density"]
    acceptance = protocol["acceptance"]
    event_dir = repo / "experiments" / str(dataset["event_experiment"])
    regime_dir = repo / "experiments" / str(dataset["regime_experiment"])
    event_manifest = validate_experiment_archive(event_dir)
    regime_manifest = validate_experiment_archive(regime_dir)
    events = pd.read_csv(event_dir / "artifacts" / "mechanism_events.csv")
    mechanisms = list(map(str, protocol["mechanisms"]))
    if set(events["mechanism_id"]) != set(mechanisms):
        raise ValueError("EX14 events differ from EX15 mechanism registration")
    regimes = pd.read_csv(regime_dir / "artifacts" / "daily_regimes.csv")
    daily_regime = regimes.set_index(pd.to_datetime(regimes["date"]))["regime"]
    intraday = load_intraday_research_data(repo / "data" / "raw", str(target["symbol"]))
    result = evaluate_medium_frequency_mechanisms(
        events,
        intraday.frames["5m"],
        daily_regime,
        mechanisms,
        evaluation_start=str(dataset["evaluation_start"]),
        entry_clock=str(execution["entry_clock"]),
        exit_clock=str(execution["exit_clock"]),
        baseline_one_way_cost=float(execution["baseline_one_way_cost"]),
        stress_one_way_cost=float(execution["stress_one_way_cost"]),
        base_fraction=float(execution["base_fraction"]),
        density_window_sessions=int(density["window_sessions"]),
        median_episodes_required=int(density["median_closed_episodes_required"]),
        p10_episodes_required=int(density["p10_closed_episodes_required"]),
        positive_years_required=int(acceptance["positive_years_required"]),
    )
    for name, frame in {
        "episodes.csv.gz": result.episodes,
        "mechanism_metrics.csv": result.metrics,
        "annual_metrics.csv": result.annual,
        "regime_metrics.csv": result.regime,
        "account_metrics.csv": result.account,
    }.items():
        compression = {"method": "gzip", "compresslevel": 9, "mtime": 0} if name.endswith(".gz") else None
        frame.to_csv(
            artifacts / name,
            index=False,
            encoding="utf-8-sig",
            compression=compression,
            lineterminator="\n",
        )
    primary = result.metrics.loc[result.metrics["variant"].eq("PRIMARY")]
    feasible = primary.loc[primary["evidence"].eq("FEASIBLE")]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "event_manifest_sha256": event_manifest["files"]["artifacts/mechanism_events.csv"]["sha256"],
        "regime_manifest_sha256": regime_manifest["files"]["artifacts/daily_regimes.csv"]["sha256"],
        "evaluated_mechanisms": int(len(primary)),
        "feasible_mechanisms": int(len(feasible)),
        "eligible_for_audit": feasible["mechanism_id"].tolist(),
        "route_decision": "CONTINUE_AUDIT" if len(feasible) else "STOP_MEDIUM_FREQUENCY_ROUTE",
        "candidate_created": False,
    }
    _write(artifacts / "evaluation_summary.json", summary)
    table = [
        "|机制|笔数|60日中位/P10|基准均值|压力均值|压力盈亏比|正收益年份|相反方向|相对静态终值|标签|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in primary.sort_values("stress_mean_return", ascending=False).itertuples(index=False):
        table.append(
            f"|{row.mechanism_id}|{row.episodes}|{row.rolling_median_episodes:.0f}/"
            f"{row.rolling_p10_episodes:.0f}|{row.baseline_mean_return:.3%}|"
            f"{row.stress_mean_return:.3%}|{row.stress_profit_factor:.2f}|{row.positive_years}|"
            f"{row.opposite_stress_mean_return:.3%}|"
            f"{row.incremental_terminal_return_vs_static:.1%}|{row.evidence}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S003 EX15 执行\n\n"
        f"状态：COMPLETE。3个中频机制及各自相反方向均按统一10:35入场、15:00退出执行；"
        f"{len(feasible)}个主机制达到FEASIBLE。\n",
        encoding="utf-8",
    )
    decision = (
        "存在方向可行的中频机制，下一步只对合格机制执行统计与结构审计。"
        if len(feasible)
        else "三类中频机制均未通过预注册收益门槛，本轮中频路线停止。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX15 结论\n\n" + "\n".join(table) + "\n\n" + decision
        + " 本实验没有创建候选，也没有修改SM或PTE。\n",
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
