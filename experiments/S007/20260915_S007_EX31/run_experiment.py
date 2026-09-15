from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from strategy_evaluator import AuditStatus, CandidateReadinessRequest, RiskLabel
from strategy_evaluator import assess_research_candidate, audit_replay

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.causal_feature_gate_replay import build_causal_feature_gate_signals
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260915_S007_EX31"
INITIAL_CASH = 100_000.0


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("EX31 protocol identity differs")
    if any(
        protocol.get(key)
        for key in ("creates_strategy_version", "formal_pk", "freeze_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX31 cannot run PK, freeze or mutate SM/PTE")
    sources = protocol["sources"]
    for name in ("ex28", "ex29", "ex30"):
        archive = repo / str(sources[f"{name}_archive"])
        validate_experiment_archive(archive)
        if _sha256(archive / "experiment_manifest.json") != sources[f"{name}_manifest_sha256"]:
            raise ValueError(f"{name} manifest differs")
    frozen = {
        repo / "experiments/S007/20260915_S007_EX28/artifacts/selected_configuration.json": sources["selected_configuration_sha256"],
        repo / "experiments/S007/20260915_S007_EX29/artifacts/statistical_summary.json": sources["statistical_summary_sha256"],
        repo / "experiments/S007/20260915_S007_EX30/artifacts/external_validation_summary.json": sources["external_validation_summary_sha256"],
    }
    if any(_sha256(path) != expected for path, expected in frozen.items()):
        raise ValueError("candidate evidence source differs")

    candidate_path = experiment / "candidate_payload.json"
    candidate = _read(candidate_path)
    candidate_hash = canonical_json_sha256(candidate)
    if candidate_hash != protocol["candidate_hash"]:
        raise ValueError("candidate hash differs from frozen protocol")
    context = RepositoryContext.discover(repo)
    snapshot = resolve_candidate_snapshot(
        context,
        str(protocol["candidate_id"]),
        candidate,
        candidate_hash,
        str(candidate_path.relative_to(repo)).replace("\\", "/"),
    )
    replay_data = load_replay_data(
        context,
        "research",
        str(protocol["symbol"]),
        "etf",
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    signals = build_causal_feature_gate_signals(
        snapshot,
        replay_data,
        pd.Timestamp(protocol["evaluation_start"]),
        pd.Timestamp(protocol["development_cutoff"]),
        repo,
    )
    result = replay_account(signals, replay_data, INITIAL_CASH)
    metrics = calculate_metrics(result, INITIAL_CASH)
    audited = audit_replay(build_replay_evidence(signals, replay_data, result, INITIAL_CASH, metrics))
    if audited.status is not AuditStatus.PASS:
        raise ValueError(f"SE replay audit failed: {audited.reason_codes}")

    all_sessions = pd.DatetimeIndex(pd.to_datetime(replay_data.adjusted.daily["dt"]), name="date")
    sessions = all_sessions[
        (all_sessions >= signals.calculation_start) & (all_sessions <= signals.calculation_end)
    ]
    execution_target = pd.Series(0, index=sessions, dtype=np.int8)
    valid = signals.decisions.dropna(subset=["valid_session"])
    execution_target.loc[pd.DatetimeIndex(valid["valid_session"])] = valid["target_position"].to_numpy(dtype=np.int8)
    behavior_hash = hashlib.sha256(execution_target.to_numpy(dtype=np.int8).tobytes()).hexdigest()
    if behavior_hash != protocol["expected_behavior_hash"]:
        raise ValueError(
            "TDR candidate behavior differs from EX28 selection: "
            f"{behavior_hash} != {protocol['expected_behavior_hash']}"
        )

    statistical = _read(
        repo / "experiments/S007/20260915_S007_EX29/artifacts/statistical_summary.json"
    )
    external = _read(repo / "experiments/S007/20260915_S007_EX30/artifacts/external_validation_summary.json")
    request = CandidateReadinessRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=candidate_hash,
        integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        mechanism_evidence=RiskLabel.FAVORABLE,
        statistical_evidence=RiskLabel(str(statistical["evidence_label"])),
        external_validation=RiskLabel.MIXED if external["posthoc_concentration_warning"] else RiskLabel.FAVORABLE,
        primary_closed_trades=int(metrics["closed_trades"]),
        minimum_closed_trades=int(protocol["minimum_closed_trades"]),
    )
    assessment = assess_research_candidate(request)
    if assessment.decision.value != "RECOMMEND_REGISTRATION":
        raise ValueError(f"SE did not recommend candidate registration: {assessment.to_dict()}")

    for name, frame in (
        ("decisions.csv", result.decisions),
        ("orders.csv", result.orders),
        ("fills.csv", result.fills),
        ("account_daily.csv", result.account_daily),
        ("trades.csv", result.trades),
    ):
        frame.to_csv(artifacts / name, index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(artifacts / "candidate_readiness.json", {"request": request.to_dict(), "result": assessment.to_dict()})
    _write(
        artifacts / "tdr_replay_summary.json",
        {
            "schema_version": 1,
            "audit": audited.to_dict(),
            "behavior_hash": behavior_hash,
            "metrics": metrics,
            "orders": len(result.orders),
            "fills": len(result.fills),
        },
    )
    _write(
        experiment / "candidate_manifest.json",
        {
            "schema_version": 1,
            "candidate_id": protocol["candidate_id"],
            "candidate_hash": candidate_hash,
            "payload_path": "candidate_payload.json",
            "payload_sha256": _sha256(candidate_path),
            "registration_decision": assessment.decision.value,
            "risk_label": assessment.risk_label.value,
        },
    )
    (experiment / "03_execution.md").write_text(
        "# S007 EX31 执行\n\n"
        f"状态：`COMPLETE`。TDR重建完整目标序列，行为哈希与EX28一致；正式账户生成"
        f"{len(result.orders)}笔订单、{len(result.fills)}笔成交和{int(metrics['closed_trades'])}笔闭合交易。"
        f"SE账本审计为`{audited.status.value}`。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX31 结论\n\n"
        f"SE裁决：`{assessment.decision.value}`；风险标签：`{assessment.risk_label.value}`。"
        "`S007-C001`已登记为研究候选，候选公式、来源、执行规则和身份哈希均已冻结。"
        "本轮未执行正式PK、冻结或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "candidate_id": protocol["candidate_id"],
            "candidate_hash": candidate_hash,
            "decision": assessment.decision.value,
            "risk_label": assessment.risk_label.value,
            "candidate_created": True,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
