from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
from strategy_evaluator import (
    AuditStatus,
    BenchmarkChallengeDecision,
    FreezeHealthRequest,
    RiskLabel,
    assess_freeze_health,
    audit_replay,
)

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_candidate_snapshot
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256, raw_file_sha256


EXPERIMENT_ID = "20260913_S001_EX11"


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
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol["experiment_id"] != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")

    evidence_paths = {
        key: repo / str(protocol[key])
        for key in (
            "candidate_payload",
            "candidate_readiness",
            "challenge_result",
            "risk_budget_result",
            "statistical_audit",
            "monitoring_plan",
        )
    }
    if any(not path.is_file() for path in evidence_paths.values()):
        raise FileNotFoundError("freeze health evidence is incomplete")

    payload = _read(evidence_paths["candidate_payload"])
    candidate_hash = canonical_json_sha256(payload)
    if candidate_hash != protocol["candidate_hash"]:
        raise ValueError("candidate identity differs from frozen protocol")
    readiness = _read(evidence_paths["candidate_readiness"])
    challenge = _read(evidence_paths["challenge_result"])
    statistical = _read(evidence_paths["statistical_audit"])

    stress_payload = deepcopy(payload)
    stress_payload["rule"]["execution"]["capital"]["fee_rate"] = float(
        protocol["stress_fee_rate"]
    )
    context = RepositoryContext.discover(repo)
    stress_snapshot = resolve_candidate_snapshot(
        context,
        str(protocol["candidate_id"]),
        stress_payload,
        canonical_json_sha256(stress_payload),
        f"experiments/S001/{EXPERIMENT_ID}/cost_stress",
    )
    replay_data = load_replay_data(
        context,
        str(protocol["dataset"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        pd.Timestamp(str(protocol["development_cutoff"])).date(),
    )
    start = pd.Timestamp(str(protocol["evaluation_start"])).date()
    end = pd.Timestamp(str(protocol["evaluation_end"])).date()
    signals = replay_signals(stress_snapshot, replay_data, start, end)
    account = replay_account(signals, replay_data, float(protocol["initial_cash"]))
    metrics = calculate_metrics(account, float(protocol["initial_cash"]))
    replay_audit = audit_replay(
        build_replay_evidence(
            signals, replay_data, account, float(protocol["initial_cash"]), metrics
        )
    )
    sessions = len(account.account_daily)
    cagr = float((1.0 + float(metrics["return"])) ** (252.0 / sessions) - 1.0)
    stress_pass = (
        replay_audit.status is AuditStatus.PASS
        and cagr >= float(protocol["minimum_cagr"])
        and float(metrics["max_drawdown"]) >= float(protocol["maximum_drawdown_floor"])
    )

    risk_budget = pd.read_csv(evidence_paths["risk_budget_result"])
    robust_passes = risk_budget.loc[
        risk_budget["risk_budget_fraction"].isin([0.5, 0.6, 0.65]), "primary_gate"
    ].eq("PASS").all()
    monitoring = evidence_paths["monitoring_plan"].read_text(encoding="utf-8")
    monitoring_complete = all(
        phrase in monitoring
        for phrase in ("最大回撤达到 15%", "至少 10 笔干净闭合交易", "任何参数")
    )
    challenge_pass = all(
        challenge[name]["assessment"]["decision"] == "RECOMMEND_HEALTH_CHECK"
        for name in ("buyhold_challenge", "incumbent_mandate_challenge")
    )
    request = FreezeHealthRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=candidate_hash,
        candidate_readiness=(
            AuditStatus.PASS
            if readiness["assessment"]["decision"] == "RECOMMEND_REGISTRATION"
            else AuditStatus.FAIL
        ),
        benchmark_challenge=(
            BenchmarkChallengeDecision.RECOMMEND_HEALTH_CHECK
            if challenge_pass
            else BenchmarkChallengeDecision.KEEP_BENCHMARK
        ),
        evidence_integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        technical_replay=replay_audit.status,
        cost_stress=AuditStatus.PASS if stress_pass else AuditStatus.FAIL,
        monitoring_plan=AuditStatus.PASS if monitoring_complete else AuditStatus.FAIL,
        mechanism_evidence=RiskLabel.FAVORABLE,
        statistical_evidence=RiskLabel(str(statistical["risk_label"])),
        parameter_robustness=RiskLabel.FAVORABLE if robust_passes else RiskLabel.WEAK,
        external_validation=RiskLabel.MIXED,
        execution_evidence=RiskLabel.FAVORABLE,
    )
    health = assess_freeze_health(request)
    evidence_index = {
        name: {
            "path": str(path.relative_to(repo)).replace("\\", "/"),
            "sha256": raw_file_sha256(path),
        }
        for name, path in evidence_paths.items()
    }
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": protocol["candidate_id"],
        "candidate_hash": candidate_hash,
        "cost_stress": {
            "fee_rate": protocol["stress_fee_rate"],
            "cagr": cagr,
            "total_return": metrics["return"],
            "max_drawdown": metrics["max_drawdown"],
            "calmar": metrics["calmar"],
            "win_loss_ratio": metrics["win_loss_ratio"],
            "closed_trades": metrics["closed_trades"],
            "replay_audit": replay_audit.status.value,
            "mandate_gate": "PASS" if stress_pass else "FAIL",
        },
        "request": request.to_dict(),
        "assessment": health.to_dict(),
        "evidence_index": evidence_index,
        "risk_disclosures": [
            "候选与S001-v2信号相同，改进来自60%资金上限",
            "候选CAGR低于S001-v2约10.53个百分点",
            "候选卡玛略低于S001-v2约0.041",
            "统计稳健性与外部验证继续标记为MIXED",
            "年度和滚动窗口存在亏损阶段",
        ],
        "lifecycle_effects": {
            "strategy_version_created": False,
            "strategy_frozen": False,
            "pte_deployed": False,
        },
    }
    _write(artifacts / "freeze_health.json", result)
    (experiment / "03_execution.md").write_text(
        "# S001 EX11 执行\n\n"
        f"状态：COMPLETE。双倍费率回放审计 `{replay_audit.status.value}`，"
        f"收益回撤目标 `{result['cost_stress']['mandate_gate']}`。未冻结、未部署。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX11 结论\n\n"
        f"SE 冻结前体检裁决：`{health.decision.value}`；风险标签："
        f"`{health.risk_label.value}`。双倍费率下 CAGR {cagr:.2%}，"
        f"最大回撤 {float(metrics['max_drawdown']):.2%}，"
        f"卡玛 {float(metrics['calmar']):.3f}。\n\n"
        "该裁决只允许提交人工冻结确认；当前没有创建 S001-v3，也没有写入 PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S001",
            "candidate_id": protocol["candidate_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": health.decision.value,
            "risk_label": health.risk_label.value,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
