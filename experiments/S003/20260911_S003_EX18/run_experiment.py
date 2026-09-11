from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.early_session_evaluation import evaluate_early_session_events
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260911_S003_EX18"


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
        raise ValueError("EX18 may not mutate strategy lifecycle state")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    execution = protocol["execution"]
    acceptance = protocol["acceptance"]
    event_dir = repo / "experiments" / str(dataset["event_experiment"])
    opportunity_dir = repo / "experiments" / str(dataset["opportunity_experiment"])
    event_manifest = validate_experiment_archive(event_dir)
    opportunity_manifest = validate_experiment_archive(opportunity_dir)
    events = pd.read_csv(event_dir / "artifacts" / "mechanism_events.csv")
    opportunity_metrics = pd.read_csv(opportunity_dir / "artifacts" / "segment_metrics.csv")
    intraday = load_intraday_research_data(repo / "data" / "raw", str(target["symbol"]))
    result = evaluate_early_session_events(
        events,
        intraday.frames[str(dataset["frequency"])],
        protocol["mechanisms"],
        opportunity_metrics,
        evaluation_start=str(dataset["evaluation_start"]),
        baseline_one_way_cost=float(execution["baseline_one_way_cost"]),
        stress_one_way_cost=float(execution["stress_one_way_cost"]),
        base_fraction=float(execution["base_fraction"]),
        positive_years_required=int(acceptance["positive_years_required"]),
    )
    for name, frame in {
        "episodes.csv.gz": result.episodes,
        "mechanism_metrics.csv": result.metrics,
        "annual_metrics.csv": result.annual,
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
    eligible = result.metrics.loc[
        result.metrics["evidence"].isin(["FEASIBLE_SIGNED", "FEASIBLE_TIMING"])
    ]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "event_manifest_sha256": event_manifest["files"]["artifacts/mechanism_events.csv"]["sha256"],
        "opportunity_manifest_sha256": opportunity_manifest["files"]["artifacts/segment_metrics.csv"]["sha256"],
        "evaluated_mechanisms": int(result.metrics["mechanism_id"].nunique()),
        "eligible_for_audit": [
            f"{row.mechanism_id}:{row.variant}" for row in eligible.itertuples(index=False)
        ],
        "route_decision": "CONTINUE_AUDIT" if len(eligible) else "STOP_TESTED_MECHANISMS",
        "candidate_created": False,
    }
    _write(artifacts / "evaluation_summary.json", summary)
    table = [
        "|机制|变体|笔数|基准均值|压力均值|压力盈亏比|正收益年份|相对静态终值|对手均值|标签|",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in result.metrics.itertuples(index=False):
        opponent = (
            row.fixed_long_stress_mean_return
            if row.variant == "PRIMARY"
            else row.unconditional_baseline_mean_return
        )
        table.append(
            f"|{row.mechanism_id}|{row.variant}|{row.episodes}|{row.baseline_mean_return:.3%}|"
            f"{row.stress_mean_return:.3%}|{row.stress_profit_factor:.2f}|{row.positive_years}|"
            f"{row.incremental_terminal_return_vs_static:.1%}|{opponent:.3%}|{row.evidence}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S003 EX18 执行\n\n"
        f"状态：COMPLETE。3个机制、3类变体按冻结口径完成评估；{len(eligible)}个变体获得"
        "统计与结构审计资格。\n",
        encoding="utf-8",
    )
    decision = (
        "存在通过初筛的方向或日期选择能力，下一步只审计合格变体。"
        if len(eligible)
        else "三类早盘机制均未形成可交易优势，停止已测机制。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX18 结论\n\n"
        + "\n".join(table)
        + "\n\n"
        + decision
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
