from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest
from strategy_manager import canonical_sha256


ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT = Path(__file__).resolve().parent
ARTIFACTS = EXPERIMENT / "artifacts"
CANDIDATE_PATH = ROOT / "experiments/S007/20260915_S007_EX31/candidate_payload.json"
BENCHMARK_PATH = ROOT / "experiments/S007/20260915_S007_EX32/artifacts/benchmark_assessment.json"
MACHINE_REPORT_PATH = ROOT / "experiments/S007/20260916_S007_EX35/artifacts/machine_evaluation_report.json"
CONCENTRATION_PATH = ROOT / "experiments/S007/20260916_S007_EX36/artifacts/concentration_diagnostic.json"
RATIONALE = "S007-C001完成机器体检并获人工冻结确认"


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    candidate = _read(CANDIDATE_PATH)
    benchmark = _read(BENCHMARK_PATH)
    machine_report = _read(MACHINE_REPORT_PATH)
    concentration = _read(CONCENTRATION_PATH)
    if candidate["candidate_id"] != machine_report["candidate_id"]:
        raise ValueError("candidate and machine report differ")
    candidate_hash = str(machine_report["candidate_hash"])
    if candidate_hash != "eef64c7c2e18c4158a4d23f66e63efee9553948defc060f69a79eda0bbd2b5cd":
        raise ValueError("candidate identity changed")
    fee_rate = float(candidate["rule"]["execution"]["capital"]["fee_rate"])
    if fee_rate != 0.001:
        raise ValueError("S007 frozen execution cost must be one-way 10bp")
    if machine_report["machine_verdict"] != "ELIGIBLE_FOR_FREEZE_REVIEW":
        raise ValueError("SE machine report is not eligible for freeze review")
    if concentration["role"] != "POSTHOC_DIAGNOSTIC_NOT_A_PREREGISTERED_GATE":
        raise ValueError("primary concentration evidence changed role")

    strategy_input = ARTIFACTS / "strategy_create.json"
    now = (
        str(_read(strategy_input)["created_at"])
        if strategy_input.is_file()
        else datetime.now().astimezone().isoformat(timespec="seconds")
    )
    strategy_payload = {
        "candidate_source": {
            "candidate_hash": candidate_hash,
            "candidate_id": "S007-C001",
            "definition": "experiments/S007/20260915_S007_EX31/candidate_payload.json",
            "machine_evaluation": "experiments/S007/20260916_S007_EX35/artifacts/machine_evaluation_report.json",
        },
        "strategy_kind": candidate["strategy_kind"],
        "symbol": candidate["symbol"],
        "rule": candidate["rule"],
    }
    strategy = {
        "schema_version": 1,
        "strategy_id": "S007",
        "name": "多源机会风险门控",
        "objective": "在控制最大回撤的前提下，以多源机会、风险与确认信息择时交易588080.SH",
        "responsibility": "基于完整收盘数据生成目标仓位；执行模块负责账户、订单、成交与运行状态",
        "scope": ["588080.SH"],
        "created_at": now,
        "created_by": "tomxiao",
    }
    version = {
        "schema_version": 1,
        "strategy_id": "S007",
        "version": "v1",
        "release_id": "S007-v1",
        "parent_version": None,
        "change_summary": "冻结S007-C001多源机会风险门控机制及唯一正式执行规则",
        "source_experiment": "experiments/S007/20260916_S007_EX37",
        "source_candidate": "S007-C001",
        "selection_data_cutoff": "2026-09-02",
        "forward_start": "2026-09-03",
        "strategy_payload": strategy_payload,
        "release_hash": None,
    }
    metrics = benchmark["candidate_account_metrics"]
    evidence = {
        "schema_version": 1,
        "evidence_id": "EVD-S007-V1-RESEARCH",
        "strategy_id": "S007",
        "version": "v1",
        "release_hash": "0" * 64,
        "phase": "RESEARCH_BACKTEST",
        "period_start": "2021-01-05",
        "period_end": "2026-09-02",
        "data_identity": {
            "candidate_hash": candidate_hash,
            "cutoff": "2026-09-02",
            "dataset": "research",
            "symbol": "588080.SH",
        },
        "initial_capital": 100000.0,
        "fee_rate": fee_rate,
        "maximum_drawdown": float(metrics["max_drawdown"]),
        "calmar_ratio": float(metrics["calmar"]),
        "win_loss_ratio": float(metrics["win_loss_ratio"]),
        "win_loss_ratio_status": str(metrics["win_loss_ratio_status"]),
        "total_return": float(metrics["return"]),
        "sharpe_ratio": float(metrics["sharpe"]),
        "closed_trades": int(metrics["closed_trades"]),
        "source_path": "experiments/S007/20260915_S007_EX32/artifacts/benchmark_assessment.json",
        "source_hash": canonical_sha256(benchmark),
        "recorded_at": now,
        "recorded_by": "tomxiao",
    }
    review = {
        "schema_version": 1,
        "strategy_id": "S007",
        "candidate_id": "S007-C001",
        "candidate_hash": candidate_hash,
        "decision": "APPROVE_FREEZE",
        "reviewed_by": "tomxiao",
        "rationale": RATIONALE,
        "mechanism_review": "APPROVED",
        "external_relevance_review": "APPROVED",
        "deployment_review": "APPROVED",
        "monitoring_plan_review": "APPROVED",
        "reviewed_at": now,
    }
    health_check = {"machine_report": machine_report, "review": review}
    cost_alignment = {
        "schema_version": 1,
        "candidate_id": "S007-C001",
        "fee_semantics": "ONE_WAY_TOTAL_COST_ON_EXECUTED_NOTIONAL",
        "fee_rate": fee_rate,
        "fee_rate_bp": fee_rate * 10000,
        "checks": {
            "research": {
                "status": "PASS",
                "evidence": "candidate rule and EX27-EX32 formal evaluation use fee_rate_one_way=0.001",
            },
            "tdr_backtest": {
                "status": "PASS",
                "evidence": "execution replay reads frozen capital.fee_rate and deducts gross*fee_rate once per fill side",
            },
            "pte_estimate": {
                "status": "PASS",
                "evidence": "advice.v4 carries frozen fee_rate; PTE reserves and estimates with that value, then reconciles only the broker-fee difference",
            },
        },
        "channel_actual_cost_policy": "Futu aggregate cash reconciliation supersedes modeled fee by difference without double charging",
        "principal_dependent_fixed_platform_fee_in_search": False,
        "sources": {
            "candidate": str(CANDIDATE_PATH.relative_to(ROOT)).replace("\\", "/"),
            "machine_report": str(MACHINE_REPORT_PATH.relative_to(ROOT)).replace("\\", "/"),
            "primary_concentration_diagnostic": str(CONCENTRATION_PATH.relative_to(ROOT)).replace("\\", "/"),
        },
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    _write(ARTIFACTS / "strategy_create.json", strategy)
    _write(ARTIFACTS / "version_create.json", version)
    _write(ARTIFACTS / "research_evidence.json", evidence)
    _write(ARTIFACTS / "freeze_health.json", health_check)
    _write(ARTIFACTS / "machine_evaluation.json", machine_report)
    _write(ARTIFACTS / "cost_alignment.json", cost_alignment)

    frozen_path = ROOT / "strategies/S007/versions/v1.json"
    frozen = _read(frozen_path) if frozen_path.is_file() else None
    strategy_frozen = bool(frozen and frozen.get("release_hash"))
    metadata = {
        "experiment_id": "20260916_S007_EX37",
        "status": "COMPLETE" if strategy_frozen else "PREPARED",
        "experiment_type": "human_freeze_acceptance_and_cost_alignment",
        "strategy_id": "S007",
        "symbol": "588080.SH",
        "development_cutoff": "2026-09-02",
        "candidate_id": "S007-C001",
        "candidate_hash": candidate_hash,
        "machine_verdict": machine_report["machine_verdict"],
        "risk_label": machine_report["risk_label"],
        "strategy_frozen": strategy_frozen,
        "release_hash": frozen.get("release_hash") if frozen else None,
        "pte_mutated": False,
    }
    build_experiment_manifest(EXPERIMENT, metadata)


if __name__ == "__main__":
    main()
