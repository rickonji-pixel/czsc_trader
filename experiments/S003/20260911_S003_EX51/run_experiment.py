from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.relative_style_evaluation import evaluate_relative_style_events


EXPERIMENT_ID = "20260911_S003_EX51"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _load_minute(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    return frame.rename(
        columns={
            "datetime": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "amount": "Amount",
        }
    ).assign(Date=lambda value: pd.to_datetime(value["Date"]))


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
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
        raise ValueError("EX51 may only evaluate the frozen cross-section mechanism")

    dataset = protocol["dataset"]
    event_experiment = repo / "experiments" / str(dataset["event_experiment"])
    minute_experiment = repo / "experiments" / str(dataset["intraday_experiment"])
    validate_experiment_archive(event_experiment)
    expected = {
        event_experiment / "experiment_manifest.json": dataset["event_manifest_sha256"],
        event_experiment / "artifacts" / "mechanism_events.csv": dataset["event_file_sha256"],
        minute_experiment / "artifacts" / "512100_5m.csv.gz": dataset["intraday_file_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")

    events = pd.read_csv(event_experiment / "artifacts" / "mechanism_events.csv")
    five_minute = _load_minute(minute_experiment / "artifacts" / "512100_5m.csv.gz")
    name = str(protocol["research_target"]["mechanism"])
    mechanism = protocol["mechanism"]
    acceptance = protocol["acceptance"]
    result = evaluate_relative_style_events(
        events,
        five_minute,
        {
            name: {
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
    for filename, frame in {
        "episodes.csv.gz": result.episodes,
        "mechanism_metrics.csv": result.metrics,
        "annual_metrics.csv": result.annual,
        "account_metrics.csv": result.account,
    }.items():
        compression = (
            {"method": "gzip", "compresslevel": 9, "mtime": 0}
            if filename.endswith(".gz")
            else None
        )
        frame.to_csv(
            artifacts / filename,
            index=False,
            encoding="utf-8-sig",
            compression=compression,
            lineterminator="\n",
        )

    row = result.metrics.iloc[0]
    replicated = bool(row["eligible_for_audit"])
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "independent_economic_replication": replicated,
        "evidence_label": (
            protocol["interpretation"]["pass_label"] if replicated else "REPLICATION_FAILED"
        ),
        "next_step": (
            protocol["interpretation"]["pass_next_step"] if replicated else "STOP_MECHANISM"
        ),
        "does_not_override_ex47_dsr": True,
        "candidate_created": False,
    }
    _write_json(artifacts / "evaluation_summary.json", summary)
    status = "PASS" if replicated else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX51 执行\n\n"
        f"独立收益复现：`{status}`。{int(row['episodes'])}笔事件，毛收益均值"
        f"{row['gross_mean_return']:.3%}、基准均值{row['baseline_mean_return']:.3%}、"
        f"压力均值{row['stress_mean_return']:.3%}，压力盈亏比{row['stress_profit_factor']:.2f}，"
        f"正收益年份{int(row['positive_years'])}，最近252日事件均值"
        f"{row['recent_stress_mean_return']:.3%}，相对静态终值"
        f"{row['incremental_terminal_return_vs_static']:.2%}，账户最大回撤"
        f"{row['account_max_drawdown']:.2%}，卡玛{row['account_calmar']:.2f}。\n",
        encoding="utf-8",
    )
    conclusion = (
        "冻结机制取得独立经济复现证据，下一步进入跨市场综合审计；该结果不覆盖EX47的不利DSR。"
        if replicated
        else "冻结机制未取得独立经济复现证据，S003停止，不创建候选。"
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S003 EX51 结论\n\n结论：`{status}`。{conclusion}本轮没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": protocol["research_target"]["symbol"],
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "independent_economic_replication": replicated,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
