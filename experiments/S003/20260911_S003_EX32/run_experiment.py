from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.market_breadth_events import (
    build_market_breadth_features,
    generate_breadth_thrust_events,
)


EXPERIMENT_ID = "20260911_S003_EX32"


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
            "conditional_return_analysis",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX32 may only generate return-free market-breadth events")

    dataset = protocol["dataset"]
    source = repo_root / "experiments" / dataset["source_experiment"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": dataset["source_manifest_sha256"],
        source / "artifacts" / "lifecycle_aware_constituent_panel.csv.gz": dataset[
            "source_panel_sha256"
        ],
        source / "artifacts" / "data_quality.json": dataset["source_quality_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    quality = _read_json(source / "artifacts" / "data_quality.json")
    if not quality["passed"]:
        raise ValueError("EX31 lifecycle-aware data gate did not pass")

    panel = pd.read_csv(source / "artifacts" / "lifecycle_aware_constituent_panel.csv.gz")
    features = build_market_breadth_features(panel)
    spec = protocol["feature"]
    events, density, evidence = generate_breadth_thrust_events(
        features,
        evaluation_start=dataset["evaluation_start"],
        threshold_lookback=int(spec["threshold_lookback"]),
        upper_quantile=float(spec["threshold_quantile"]),
        minimum_observed_ratio=float(spec["minimum_observed_ratio"]),
    )
    events.to_csv(artifacts / "mechanism_events.csv", index=False, lineterminator="\n")
    density.to_csv(artifacts / "density_metrics.csv", index=False, lineterminator="\n")
    evidence.to_csv(
        artifacts / "breadth_features.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    row = density.iloc[0]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "density_pass": bool(row["density_eligible"]),
        "event_count": int(row["event_count"]),
        "minimum_observed_ratio_actual": float(features["observed_ratio"].min()),
        "conditional_return_analysis": False,
        "next_step": "RETURN_EVALUATION" if row["density_eligible"] else "STOP_MECHANISM",
    }
    _write_json(artifacts / "census_summary.json", summary)
    status = "PASS" if summary["density_pass"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX32 执行\n\n"
        f"宽度推动事件密度门：`{status}`。共产生{summary['event_count']}个事件，滚动60日中位数"
        f"{row['rolling_60_median']:.1f}，第10百分位{row['rolling_60_p10']:.1f}，最小值"
        f"{row['rolling_60_min']:.1f}；实际最低成分观测比例"
        f"{summary['minimum_observed_ratio_actual']:.2%}。\n\n"
        "本轮没有读取510500事件后收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        "机制获得下一轮收益评价资格。"
        if summary["density_pass"]
        else "机制未达到三个月观察目标所需密度，停止该固定定义。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX32 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮没有创建候选或修改SM/PTE。\n",
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
            "density_pass": summary["density_pass"],
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
