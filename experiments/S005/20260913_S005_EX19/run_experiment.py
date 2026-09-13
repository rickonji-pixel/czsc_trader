from __future__ import annotations

import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX19"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    source_spec = protocol["source"]
    source = repo / "experiments/S005" / str(source_spec["experiment_id"])
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["manifest_sha256"],
        source / "artifacts/data_quality.json": source_spec["quality_sha256"],
        source / "artifacts/constituent_panel.csv.gz": source_spec["panel_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    quality = _read(source / "artifacts/data_quality.json")
    gate = protocol["quality_gate"]
    data_pass = bool(
        quality["member_count_min"] == gate["members_per_session"]
        and quality["member_count_max"] == gate["members_per_session"]
        and quality["weight_sum_min"] >= gate["weight_sum_min"]
        and quality["weight_sum_max"] <= gate["weight_sum_max"]
        and quality["daily_coverage_ratio"] >= gate["minimum_daily_coverage_ratio"]
        and quality["moneyflow_coverage_ratio"] >= gate["minimum_moneyflow_coverage_ratio"]
        and quality["moneyflow_daily_coverage_p10"] >= gate["minimum_moneyflow_daily_p10"]
        and quality["daily_duplicate_rows"] == 0
        and quality["moneyflow_duplicate_rows"] == 0
        and quality["causal_membership"]
    )
    incremental_calls = int(protocol["assessed_incremental_calls_per_session"])
    cost_pass = incremental_calls <= int(gate["maximum_incremental_calls_per_session"])
    passed = data_pass and cost_pass
    review = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "source_experiment": source_spec["experiment_id"],
        "source_reported_pass": quality["passed"],
        "source_historical_backfill_calls": quality["api_calls"],
        "historical_backfill_calls_classification": "INFORMATIONAL",
        "data_quality_pass": data_pass,
        "assessed_incremental_calls_per_session": incremental_calls,
        "incremental_cost_pass": cost_pass,
        "passed": passed,
    }
    (artifacts / "reviewed_data_quality.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    status = "PASS" if passed else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S005 EX19 执行\n\n"
        f"纠偏复核：`{status}`。EX18的{quality['api_calls']}次历史回填调用改列披露项；"
        f"数据质量门为`{'PASS' if data_pass else 'FAIL'}`，日常增量最多{incremental_calls}次，"
        f"运维成本门为`{'PASS' if cost_pass else 'FAIL'}`。没有重新请求或修改源数据。\n",
        encoding="utf-8",
    )
    decision = (
        "PROCEED_TO_CONSTITUENT_MECHANISM_PREREGISTRATION"
        if passed
        else "STOP_CONSTITUENT_ROUTE_ON_REVIEWED_DATA"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX19 结论\n\n"
        f"裁决：`{decision}`。一次性回填量不构成持续运维负担；该结论只恢复新机制研究资格，"
        "不构成Alpha证据，也没有创建候选、冻结策略或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": "2026-09-02",
            "status": status,
            "reads_post_event_prices": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
