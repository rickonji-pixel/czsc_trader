from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260911_S003_EX29"


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
            "breadth_feature_generation",
            "signal_generation",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX29 may only review frozen EX28 data evidence")

    source_spec = protocol["source"]
    source = repo_root / "experiments" / source_spec["experiment_id"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["experiment_manifest_sha256"],
        source / "artifacts" / "data_quality.json": source_spec["data_quality_sha256"],
        source / "artifacts" / "index_weight_snapshots.csv.gz": source_spec[
            "membership_sha256"
        ],
        source / "artifacts" / "sample_constituent_daily.csv": source_spec[
            "sample_daily_sha256"
        ],
        source / "artifacts" / "sample_suspensions.csv": source_spec[
            "sample_suspensions_sha256"
        ],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")

    source_quality = _read_json(source / "artifacts" / "data_quality.json")
    snapshots = pd.read_csv(source / "artifacts" / "index_weight_snapshots.csv.gz")
    snapshots["trade_date"] = pd.to_datetime(snapshots["trade_date"])
    start = pd.Timestamp(protocol["dataset"]["start"])
    cutoff = pd.Timestamp(protocol["dataset"]["development_cutoff"])
    completed_months = {
        period.strftime("%Y-%m")
        for period in pd.period_range(start, cutoff, freq="M")
        if period < cutoff.to_period("M")
    }
    observed_months = set(snapshots["trade_date"].dt.strftime("%Y-%m"))
    missing_completed_months = sorted(completed_months.difference(observed_months))
    last_snapshot_age_days = int((cutoff - snapshots["trade_date"].max()).days)
    gate = protocol["quality_gate"]
    reviewed = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "source_experiment": source_spec["experiment_id"],
        "missing_completed_months": missing_completed_months,
        "last_snapshot_age_days": last_snapshot_age_days,
        "member_count_min": source_quality["member_count_min"],
        "member_count_max": source_quality["member_count_max"],
        "weight_sum_min": source_quality["weight_sum_min"],
        "weight_sum_max": source_quality["weight_sum_max"],
        "historical_constituent_union": source_quality["historical_constituent_union"],
        "first_last_overlap": source_quality["first_last_overlap"],
        "sample_unexplained_missing_rows": len(
            source_quality["sample_unexplained_missing_rows"]
        ),
        "finite_observed_pct_change": source_quality["finite_observed_pct_change"],
        "estimated_initial_daily_calls": source_quality["estimated_initial_daily_calls"],
        "estimated_compressed_storage_mb": source_quality["estimated_compressed_storage_mb"],
        "incremental_daily_calls": source_quality["incremental_daily_calls"],
    }
    reviewed["passed"] = bool(
        not missing_completed_months
        and last_snapshot_age_days <= gate["maximum_last_snapshot_age_days"]
        and reviewed["member_count_min"] == gate["members_per_snapshot"]
        and reviewed["member_count_max"] == gate["members_per_snapshot"]
        and reviewed["weight_sum_min"] >= gate["weight_sum_min"]
        and reviewed["weight_sum_max"] <= gate["weight_sum_max"]
        and reviewed["first_last_overlap"] < gate["members_per_snapshot"]
        and reviewed["sample_unexplained_missing_rows"]
        == gate["sample_unexplained_missing_rows"]
        and reviewed["finite_observed_pct_change"]
        and reviewed["estimated_compressed_storage_mb"]
        <= gate["estimated_compressed_storage_mb_max"]
        and reviewed["incremental_daily_calls"] <= gate["incremental_daily_calls_max"]
    )
    _write_json(artifacts / "reviewed_data_quality.json", reviewed)

    status = "PASS" if reviewed["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX29 执行\n\n"
        f"复核数据门结果：`{status}`。完整月份缺失{len(missing_completed_months)}个，最近权重"
        f"快照距开发截止{last_snapshot_age_days}天；每期均为500只成分，抽样无法解释缺失"
        f"{reviewed['sample_unexplained_missing_rows']}条。\n\n"
        f"历史并集{reviewed['historical_constituent_union']}只、首次回填约"
        f"{reviewed['estimated_initial_daily_calls']}次均作为一次性诊断信息；持续维护为每日"
        f"{reviewed['incremental_daily_calls']}次日线请求、每月一次权重更新，估算压缩存储"
        f"{reviewed['estimated_compressed_storage_mb']:.1f}MB。\n",
        encoding="utf-8",
    )
    conclusion = (
        "历史时点成分宽度数据满足OPC持续维护要求，可以进入完整数据集建设。"
        if reviewed["passed"]
        else "修正后仍未通过数据门，S003应暂停。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX29 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮没有计算宽度、条件收益或创建候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": protocol["dataset"]["development_cutoff"],
            "status": status,
            "breadth_feature_generation": False,
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
