from __future__ import annotations

import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S004_EX48"
SOURCE_EXPERIMENTS = (
    "20260913_S004_EX41",
    "20260913_S004_EX42",
    "20260913_S004_EX43",
    "20260913_S004_EX44",
    "20260913_S004_EX45",
    "20260913_S004_EX46",
)


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")

    for source_id in SOURCE_EXPERIMENTS:
        validate_experiment_archive(repo / "experiments" / "S004" / source_id)

    candidate_path = repo / "research/S004/candidates/S004-C002.json"
    metrics_path = repo / "experiments/S004/20260913_S004_EX41/artifacts/candidate_metrics.json"
    health_path = repo / "experiments/S004/20260913_S004_EX46/artifacts/freeze_health.json"
    candidate = _read(candidate_path)
    metrics = _read(metrics_path)
    health = _read(health_path)

    if candidate.get("candidate_id") != "S004-C002":
        raise ValueError("unexpected S004 candidate identity")
    if health["source"]["health_result"]["decision"] != "KEEP_RESEARCHING":
        raise ValueError("freeze health result differs from closeout premise")
    if int(metrics["filtered_episodes"]) != 160:
        raise ValueError("candidate trade count differs from closeout premise")

    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "strategy_id": "S004",
        "candidate_id": "S004-C002",
        "decision": protocol["decision"],
        "research_line_status": "PAUSED",
        "candidate_status_unchanged": "RESEARCH_CANDIDATE",
        "retained_role": "DEFENSIVE_RESEARCH_CANDIDATE_AND_S005_FREQUENCY_BENCHMARK",
        "frequency_benchmark": {
            "closed_trades": int(metrics["filtered_episodes"]),
            "rolling_60_median": float(metrics["rolling_60_median_observation"]),
            "rolling_60_p10": float(metrics["rolling_60_p10_observation"]),
        },
        "lifecycle_effects": {
            "candidate_deleted": False,
            "candidate_created": False,
            "strategy_version_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
        "ex47_arrival_observation": "CONTINUES_AS_NON_BLOCKING_OPERATIONAL_OBSERVATION",
        "prohibited_follow_up": "NO_FURTHER_S004_PARAMETER_SEARCH_WITHOUT_EXPLICIT_REOPENING",
        "evidence": {
            "candidate": {"path": str(candidate_path.relative_to(repo)).replace("\\", "/"), "sha256": raw_file_sha256(candidate_path)},
            "metrics": {"path": str(metrics_path.relative_to(repo)).replace("\\", "/"), "sha256": raw_file_sha256(metrics_path)},
            "freeze_health": {"path": str(health_path.relative_to(repo)).replace("\\", "/"), "sha256": raw_file_sha256(health_path)},
        },
    }
    _write(artifacts / "closeout_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S004 EX48 执行\n\n状态：COMPLETE。已校验 EX41—EX46 档案，并固化 S004 阶段性收尾。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX48 结论\n\n"
        "S004 研究线进入 `PAUSED`。S004-C002 保留原研究候选身份和全部历史证据，"
        "不冻结、不进入 PTE；其后续唯一新增角色是作为 S005 的交易频率标杆："
        "滚动 60 交易日闭合交易中位数 7 笔、P10 为 3 笔。EX47 可作为非阻塞运行观察继续，"
        "不得据此恢复 S004 调参。重新启动 S004 必须由用户明确提出新的研究问题。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S004",
            "symbol": protocol["symbol"],
            "candidate_id": protocol["candidate_id"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": protocol["decision"],
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
