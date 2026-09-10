from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.relative_style_evaluation import evaluate_relative_style_events


EXPERIMENT_ID = "20260911_S003_EX23"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "parameter_selection", "candidate_generation", "promotion_allowed",
            "mutates_strategy_manager", "mutates_pte",
        )
    ):
        raise ValueError("EX23 may only evaluate frozen mechanisms")

    dataset = protocol["dataset"]
    event_experiment = repo_root / "experiments" / dataset["event_experiment"]
    validate_experiment_archive(event_experiment)
    expected = {
        event_experiment / "experiment_manifest.json": dataset["event_manifest_sha256"],
        event_experiment / "artifacts" / "mechanism_events.csv": dataset["event_file_sha256"],
        event_experiment / "artifacts" / "density_metrics.csv": dataset["density_file_sha256"],
        repo_root / "data" / "raw" / "510500_intraday_manifest.json": dataset[
            "intraday_manifest_sha256"
        ],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    density = pd.read_csv(event_experiment / "artifacts" / "density_metrics.csv")
    eligible = set(density.loc[density["density_eligible"], "mechanism"])
    if eligible != set(protocol["mechanisms"]):
        raise ValueError("evaluated mechanisms differ from EX22 density qualifiers")

    events = pd.read_csv(event_experiment / "artifacts" / "mechanism_events.csv")
    intraday = load_intraday_research_data(repo_root / "data" / "raw", "510500.SH")
    result = evaluate_relative_style_events(
        events,
        intraday.frames[dataset["frequency"]],
        protocol["mechanisms"],
        evaluation_start=dataset["evaluation_start"],
        baseline_one_way_cost=float(protocol["execution"]["baseline_one_way_cost"]),
        stress_one_way_cost=float(protocol["execution"]["stress_one_way_cost"]),
        base_fraction=float(protocol["execution"]["base_fraction"]),
        positive_years_required=int(protocol["acceptance"]["positive_years_required"]),
        recent_sessions=int(protocol["acceptance"]["recent_sessions"]),
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
    qualifiers = result.metrics.loc[result.metrics["eligible_for_audit"], "mechanism"].tolist()
    _write_json(
        artifacts / "evaluation_summary.json",
        {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "eligible_for_audit": qualifiers,
            "route_decision": "CONTINUE_AUDIT" if qualifiers else "STOP_TESTED_MECHANISMS",
            "candidate_created": False,
        },
    )
    table = [
        "|机制|事件|压力均值|盈亏比|正收益年份|近期均值|相对静态终值|最大回撤|卡玛|审计资格|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in result.metrics.itertuples(index=False):
        table.append(
            f"|{row.mechanism}|{row.episodes}|{row.stress_mean_return:.3%}|"
            f"{row.stress_profit_factor:.2f}|{row.positive_years}|{row.recent_stress_mean_return:.3%}|"
            f"{row.incremental_terminal_return_vs_static:.2%}|{row.account_max_drawdown:.2%}|"
            f"{row.account_calmar:.2f}|{'通过' if row.eligible_for_audit else '未通过'}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S003 EX23 执行\n\n三条冻结机制按统一执行、成本和对手口径完成评价。\n",
        encoding="utf-8",
    )
    decision = (
        f"获得下一轮统计稳健性审计资格：{qualifiers}。"
        if qualifiers
        else "没有机制通过全部初筛条件，停止本轮已测试机制。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX23 结论\n\n" + "\n".join(table) + "\n\n" + decision
        + " 本轮没有创建候选，也没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": "COMPLETE",
            "eligible_for_audit": qualifiers,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
