from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.relative_style_evaluation import evaluate_relative_style_events


EXPERIMENT_ID = "20260911_S003_EX27"


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
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX27 may only evaluate its frozen mechanism")

    dataset = protocol["dataset"]
    event_experiment = repo_root / "experiments" / dataset["event_experiment"]
    validate_experiment_archive(event_experiment)
    expected = {
        event_experiment / "experiment_manifest.json": dataset["event_manifest_sha256"],
        event_experiment / "artifacts" / "mechanism_events.csv": dataset["event_file_sha256"],
        repo_root / "data" / "raw" / "510500_intraday_manifest.json": dataset[
            "intraday_manifest_sha256"
        ],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")

    events = pd.read_csv(event_experiment / "artifacts" / "mechanism_events.csv")
    intraday = load_intraday_research_data(repo_root / "data" / "raw", "510500.SH")
    mechanism_name = protocol["research_target"]["mechanism"]
    mechanism = protocol["mechanism"]
    acceptance = protocol["acceptance"]
    result = evaluate_relative_style_events(
        events,
        intraday.frames["5m"],
        {
            mechanism_name: {
                "entry_checkpoint": mechanism["entry_checkpoint"],
                "exit_checkpoint": mechanism["exit_checkpoint"],
            }
        },
        evaluation_start=dataset["evaluation_start"],
        baseline_one_way_cost=float(protocol["execution"]["baseline_one_way_cost"]),
        stress_one_way_cost=float(protocol["execution"]["stress_one_way_cost"]),
        base_fraction=float(protocol["execution"]["base_fraction"]),
        positive_years_required=int(acceptance["positive_years_required"]),
        recent_sessions=int(acceptance["recent_sessions"]),
    )
    for name, frame in {
        "episodes.csv.gz": result.episodes,
        "mechanism_metrics.csv": result.metrics,
        "annual_metrics.csv": result.annual,
        "account_metrics.csv": result.account,
    }.items():
        compression = (
            {"method": "gzip", "compresslevel": 9, "mtime": 0}
            if name.endswith(".gz")
            else None
        )
        frame.to_csv(
            artifacts / name,
            index=False,
            encoding="utf-8-sig",
            compression=compression,
            lineterminator="\n",
        )

    row = result.metrics.iloc[0]
    comparator = protocol["diagnostic_comparator"]
    comparison = {
        "schema_version": 1,
        "mechanism": mechanism_name,
        "comparator": comparator["mechanism"],
        "stress_mean_return_delta": float(
            row["stress_mean_return"] - comparator["stress_mean_return"]
        ),
        "stress_profit_factor_delta": float(
            row["stress_profit_factor"] - comparator["stress_profit_factor"]
        ),
        "positive_years_delta": int(row["positive_years"] - comparator["positive_years"]),
        "recent_stress_mean_return_delta": float(
            row["recent_stress_mean_return"] - comparator["recent_stress_mean_return"]
        ),
        "incremental_terminal_return_vs_static_delta": float(
            row["incremental_terminal_return_vs_static"]
            - comparator["incremental_terminal_return_vs_static"]
        ),
    }
    _write_json(artifacts / "comparison_vs_ex23.json", comparison)
    qualified = bool(row["eligible_for_audit"])
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "eligible_for_statistical_audit": qualified,
        "candidate_created": False,
        "tested_path_scope": protocol["tested_path_scope"],
    }
    _write_json(artifacts / "evaluation_summary.json", summary)
    status = "PASS" if qualified else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX27 执行\n\n"
        f"收益评价结果：`{status}`。{int(row['episodes'])}笔事件，压力均值"
        f"{row['stress_mean_return']:.3%}，压力盈亏比{row['stress_profit_factor']:.2f}，"
        f"正收益年份{int(row['positive_years'])}，最近252日事件均值"
        f"{row['recent_stress_mean_return']:.3%}，相对静态终值"
        f"{row['incremental_terminal_return_vs_static']:.2%}，账户最大回撤"
        f"{row['account_max_drawdown']:.2%}，卡玛{row['account_calmar']:.2f}。\n\n"
        f"相对EX23纯相对强势机制，压力均值变化{comparison['stress_mean_return_delta']:.3%}，"
        f"压力盈亏比变化{comparison['stress_profit_factor_delta']:.2f}，正收益年份变化"
        f"{comparison['positive_years_delta']}。\n",
        encoding="utf-8",
    )
    conclusion = (
        "机制取得统计稳健性审计资格；审计必须计入EX22至EX27全部已测试路径。"
        if qualified
        else "机制未取得统计稳健性审计资格，不创建候选。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX27 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "eligible_for_statistical_audit": qualified,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
