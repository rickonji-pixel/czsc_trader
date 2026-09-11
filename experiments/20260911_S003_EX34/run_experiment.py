from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_opening_execution import _performance, _trade_return
from czsc_trader.intraday_opportunity_map import _price_checkpoints


EXPERIMENT_ID = "20260911_S003_EX34"


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
        raise ValueError("EX34 is diagnostic only")

    dataset = protocol["dataset"]
    event_experiment = repo_root / "experiments" / dataset["event_experiment"]
    evaluation_experiment = repo_root / "experiments" / dataset["evaluation_experiment"]
    validate_experiment_archive(event_experiment)
    validate_experiment_archive(evaluation_experiment)
    expected = {
        event_experiment / "experiment_manifest.json": dataset["event_manifest_sha256"],
        event_experiment / "artifacts" / "mechanism_events.csv": dataset["event_file_sha256"],
        evaluation_experiment / "experiment_manifest.json": dataset[
            "evaluation_manifest_sha256"
        ],
        evaluation_experiment / "artifacts" / "mechanism_metrics.csv": dataset[
            "evaluation_metrics_sha256"
        ],
        repo_root / "data" / "raw" / "510500_intraday_manifest.json": dataset[
            "intraday_manifest_sha256"
        ],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")

    events = pd.read_csv(event_experiment / "artifacts" / "mechanism_events.csv")
    events["event_date"] = pd.to_datetime(events["event_date"]).dt.normalize()
    events = events.sort_values("event_date").reset_index(drop=True)
    intraday = load_intraday_research_data(repo_root / "data" / "raw", "510500.SH")
    checkpoints = _price_checkpoints(intraday.frames["5m"])
    checkpoints = checkpoints.loc[
        checkpoints.index >= pd.Timestamp(dataset["evaluation_start"])
    ]

    baseline_cost = float(protocol["execution"]["baseline_one_way_cost"])
    stress_cost = float(protocol["execution"]["stress_one_way_cost"])
    episode_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    annual_rows: list[dict[str, object]] = []
    qualifying_segments: list[str] = []
    required_years = int(
        protocol["diagnostic_continuation_rule"]["stress_positive_years_required"]
    )

    for segment in protocol["segments"]:
        segment_id = str(segment["segment_id"])
        tradable = bool(segment["tradable"])
        selected = events.copy()
        selected["segment_id"] = segment_id
        selected["tradable"] = tradable
        selected["entry_checkpoint"] = str(segment["entry"])
        selected["exit_checkpoint"] = str(segment["exit"])
        selected["entry_price"] = checkpoints[str(segment["entry"])].reindex(
            selected["event_date"]
        ).to_numpy()
        selected["exit_price"] = checkpoints[str(segment["exit"])].reindex(
            selected["event_date"]
        ).to_numpy()
        if selected[["entry_price", "exit_price"]].isna().any().any():
            raise ValueError(f"{segment_id}: missing price checkpoint")
        for label, cost in (
            ("gross", 0.0),
            ("baseline", baseline_cost),
            ("stress", stress_cost),
        ):
            selected[f"{label}_return"] = [
                _trade_return(float(row.entry_price), float(row.exit_price), 1, cost)
                for row in selected.itertuples(index=False)
            ]
        episode_frames.append(selected)

        positive_years = {"gross": 0, "baseline": 0, "stress": 0}
        for year, rows in selected.groupby(selected["event_date"].dt.year, observed=True):
            annual_row: dict[str, object] = {
                "segment_id": segment_id,
                "tradable": tradable,
                "year": int(year),
                "episodes": int(len(rows)),
            }
            for label in positive_years:
                result = _performance(rows[f"{label}_return"], 1.0)
                annual_row[f"{label}_mean_return"] = result["mean_return"]
                annual_row[f"{label}_profit_factor"] = result["profit_factor"]
                positive_years[label] += int(float(result["mean_return"]) > 0)
            annual_rows.append(annual_row)

        metrics: dict[str, object] = {
            "segment_id": segment_id,
            "tradable": tradable,
            "episodes": int(len(selected)),
        }
        for label in positive_years:
            result = _performance(selected[f"{label}_return"], 1.0)
            metrics[f"{label}_mean_return"] = result["mean_return"]
            metrics[f"{label}_profit_factor"] = result["profit_factor"]
            metrics[f"{label}_win_rate"] = result["win_rate"]
            metrics[f"{label}_positive_years"] = positive_years[label]
        continuation = bool(
            tradable
            and float(metrics["stress_mean_return"]) > 0
            and float(metrics["stress_profit_factor"]) > 1.0
            and int(metrics["stress_positive_years"]) >= required_years
        )
        metrics["supports_confirmation_experiment"] = continuation
        if continuation:
            qualifying_segments.append(segment_id)
        metric_rows.append(metrics)

    episodes_frame = pd.concat(episode_frames, ignore_index=True)
    metrics_frame = pd.DataFrame(metric_rows)
    annual_frame = pd.DataFrame(annual_rows)
    episodes_frame.to_csv(
        artifacts / "episode_segments.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    metrics_frame.to_csv(
        artifacts / "segment_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    annual_frame.to_csv(
        artifacts / "annual_segment_metrics.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "diagnostic_only": True,
        "segments_supporting_confirmation": qualifying_segments,
        "candidate_created": False,
        "tested_path_scope": protocol["tested_path_scope"],
    }
    _write_json(artifacts / "diagnostic_summary.json", summary)
    best = metrics_frame.loc[metrics_frame["tradable"]].sort_values(
        "stress_mean_return", ascending=False
    ).iloc[0]
    (experiment / "03_execution.md").write_text(
        "# S003 EX34 执行\n\n"
        f"完成{len(metrics_frame)}个冻结时段、每段{int(best['episodes'])}笔事件的收益归因。"
        f"压力成本均值最高的可交易时段为`{best['segment_id']}`："
        f"{best['stress_mean_return']:.3%}，盈亏比{best['stress_profit_factor']:.2f}，"
        f"正收益年份{int(best['stress_positive_years'])}。\n",
        encoding="utf-8",
    )
    conclusion = (
        "存在满足诊断条件的时段，可另开一次预注册确认实验。"
        if qualifying_segments
        else "没有时段满足诊断条件，停止宽度推动机制的执行时段细化。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX34 结论\n\n"
        f"{conclusion}EX33的失败结论保持不变，本轮没有创建候选，也没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": "DIAGNOSTIC",
            "segments_supporting_confirmation": qualifying_segments,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
