from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date
import hashlib
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
    performance_metrics,
)

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.causal_feature_gate_replay import build_causal_feature_gate_signals
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256, raw_file_sha256
from czsc_trader.strategy_runtime import apply_resolved_strategy


EXPERIMENT_ID = "20260915_S007_EX33"
PTE_BLOCKER = "PTE_CAUSAL_FEATURE_GATE_LIVE_RUNTIME_NOT_IMPLEMENTED"


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


def _verify(path: Path, expected: object) -> None:
    actual = _sha256(path)
    if actual != str(expected):
        raise ValueError(f"frozen evidence differs: {path}: {actual} != {expected}")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("EX33 protocol identity differs")
    if any(
        protocol.get(key)
        for key in (
            "creates_strategy_version",
            "strategy_frozen",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX33 cannot create, freeze, or deploy a strategy")

    sources = protocol["sources"]
    ex31 = repo / str(sources["ex31_archive"])
    ex32 = repo / str(sources["ex32_archive"])
    validate_experiment_archive(ex31)
    validate_experiment_archive(ex32)
    evidence_paths = {
        "ex31_manifest": ex31 / "experiment_manifest.json",
        "candidate_payload": ex31 / "candidate_payload.json",
        "candidate_manifest": ex31 / "candidate_manifest.json",
        "candidate_readiness": ex31 / "artifacts/candidate_readiness.json",
        "tdr_replay": ex31 / "artifacts/tdr_replay_summary.json",
        "ex32_manifest": ex32 / "experiment_manifest.json",
        "benchmark_challenge": ex32 / "artifacts/benchmark_assessment.json",
        "statistical": repo / "experiments/S007/20260915_S007_EX29/artifacts/statistical_summary.json",
        "external": repo / "experiments/S007/20260915_S007_EX30/artifacts/external_validation_summary.json",
        "monitoring_plan": repo / "research/S007/candidates/S007-C001_MONITORING.md",
    }
    expected_hashes = {
        "ex31_manifest": sources["ex31_manifest_sha256"],
        "candidate_payload": sources["candidate_payload_sha256"],
        "candidate_manifest": sources["candidate_manifest_sha256"],
        "candidate_readiness": sources["candidate_readiness_sha256"],
        "tdr_replay": sources["tdr_replay_summary_sha256"],
        "ex32_manifest": sources["ex32_manifest_sha256"],
        "benchmark_challenge": sources["benchmark_assessment_sha256"],
        "statistical": sources["statistical_summary_sha256"],
        "external": sources["external_validation_summary_sha256"],
        "monitoring_plan": sources["monitoring_plan_sha256"],
    }
    for name, path in evidence_paths.items():
        _verify(path, expected_hashes[name])

    candidate = _read(evidence_paths["candidate_payload"])
    candidate_hash = canonical_json_sha256(candidate)
    if candidate_hash != protocol["candidate_hash"]:
        raise ValueError("candidate identity differs from EX33 protocol")
    readiness = _read(evidence_paths["candidate_readiness"])
    replay = _read(evidence_paths["tdr_replay"])
    challenge = _read(evidence_paths["benchmark_challenge"])
    statistical = _read(evidence_paths["statistical"])
    external = _read(evidence_paths["external"])
    monitoring_text = evidence_paths["monitoring_plan"].read_text(encoding="utf-8")

    context = RepositoryContext.discover(repo)
    stress_candidate = deepcopy(candidate)
    stress = protocol["cost_stress"]
    stress_candidate["rule"]["execution"]["capital"]["fee_rate"] = float(
        stress["one_way_fee_rate"]
    )
    stress_hash = canonical_json_sha256(stress_candidate)
    stress_snapshot = resolve_candidate_snapshot(
        context,
        f"{protocol['candidate_id']}-COST-STRESS",
        stress_candidate,
        stress_hash,
        str(evidence_paths["candidate_payload"].relative_to(repo)).replace("\\", "/"),
    )
    replay_data = load_replay_data(
        context,
        "research",
        str(protocol["symbol"]),
        "etf",
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    signals = build_causal_feature_gate_signals(
        stress_snapshot,
        replay_data,
        pd.Timestamp(protocol["evaluation_start"]),
        pd.Timestamp(protocol["development_cutoff"]),
        repo,
    )
    initial_cash = float(protocol["initial_cash"])
    cost_result = replay_account(signals, replay_data, initial_cash)
    cost_metrics = calculate_metrics(cost_result, initial_cash)
    cost_audit = audit_replay(
        build_replay_evidence(signals, replay_data, cost_result, initial_cash, cost_metrics)
    )
    cost_returns = cost_result.account_daily["equity"].astype(float).pct_change()
    cost_returns.iloc[0] = cost_result.account_daily["equity"].iloc[0] / initial_cash - 1.0
    cost_performance = performance_metrics(cost_returns.to_numpy())
    all_sessions = pd.DatetimeIndex(
        pd.to_datetime(replay_data.adjusted.daily["dt"], errors="raise").dt.normalize(),
        name="date",
    )
    sessions = all_sessions[
        (all_sessions >= signals.calculation_start) & (all_sessions <= signals.calculation_end)
    ]
    execution_target = pd.Series(0, index=sessions, dtype="int8")
    valid = signals.decisions.dropna(subset=["valid_session"])
    execution_target.loc[pd.DatetimeIndex(valid["valid_session"])] = valid[
        "target_position"
    ].to_numpy(dtype="int8")
    stress_behavior_hash = hashlib.sha256(
        execution_target.to_numpy(dtype="int8").tobytes()
    ).hexdigest()
    cost_checks = {
        "tdr_ledger_audit": cost_audit.status.value,
        "positive_cagr": cost_performance.cagr > float(stress["minimum_cagr"]),
        "drawdown_within_limit": cost_performance.max_drawdown >= float(
            stress["maximum_drawdown_limit"]
        ),
        "minimum_calmar_met": cost_performance.calmar >= float(stress["minimum_calmar"]),
        "closed_trade_count_unchanged": int(cost_metrics["closed_trades"])
        == int(replay["metrics"]["closed_trades"]),
        "target_behavior_unchanged": stress_behavior_hash
        == str(protocol["expected_behavior_hash"]),
    }
    cost_pass = cost_audit.status is AuditStatus.PASS and all(
        value is True for key, value in cost_checks.items() if key != "tdr_ledger_audit"
    )
    for name, frame in (
        ("cost_stress_orders.csv", cost_result.orders),
        ("cost_stress_fills.csv", cost_result.fills),
        ("cost_stress_account_daily.csv", cost_result.account_daily),
        ("cost_stress_trades.csv", cost_result.trades),
    ):
        frame.to_csv(artifacts / name, index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(
        artifacts / "cost_stress_summary.json",
        {
            "schema_version": 1,
            "one_way_fee_rate": stress["one_way_fee_rate"],
            "stress_candidate_hash": stress_hash,
            "behavior_hash": stress_behavior_hash,
            "checks": cost_checks,
            "status": "PASS" if cost_pass else "FAIL",
            "performance": cost_performance.to_dict(),
            "account_metrics": cost_metrics,
        },
    )

    # Exercise the exact generic runtime branch currently used by run_advice for every
    # strategy except the S003 intraday overlay. A supported live S007 runtime must not
    # fail here and must also have a post-cutoff feature publication contract.
    runtime_error: str | None = None
    try:
        apply_resolved_strategy(replay_data.adjusted, stress_snapshot.resolved_rule)
    except Exception as exc:  # evidence records the actual public-boundary failure
        runtime_error = f"{type(exc).__name__}: {exc}"
    source_panel = repo / str(candidate["rule"]["data_source"]["path"])
    panel_dates = pd.read_csv(source_panel, compression="gzip", usecols=["date"])
    panel_max_date = str(pd.to_datetime(panel_dates["date"], errors="raise").max().date())
    advice_source = repo / "src/czsc_trader/application/advice_service.py"
    data_source = repo / "src/czsc_trader/application/data_service.py"
    advice_text = advice_source.read_text(encoding="utf-8")
    data_text = data_source.read_text(encoding="utf-8")
    pte_checks = {
        "backtest_runtime_supported": True,
        "generic_advice_runtime_succeeds": runtime_error is None,
        "live_feature_publication_implemented": "causal_feature_gate" in data_text,
        "dedicated_advice_contract_implemented": "causal_feature_gate" in advice_text,
        "research_panel_ends_at_development_cutoff": panel_max_date
        == str(protocol["development_cutoff"]),
    }
    pte_pass = all(
        pte_checks[key]
        for key in (
            "generic_advice_runtime_succeeds",
            "live_feature_publication_implemented",
            "dedicated_advice_contract_implemented",
        )
    )
    _write(
        artifacts / "pte_compatibility.json",
        {
            "schema_version": 1,
            "status": "PASS" if pte_pass else "FAIL",
            "candidate_id": protocol["candidate_id"],
            "candidate_hash": candidate_hash,
            "checks": pte_checks,
            "runtime_error": runtime_error,
            "research_panel_max_date": panel_max_date,
            "implementation_identity": {
                str(advice_source.relative_to(repo)).replace("\\", "/"): raw_file_sha256(advice_source),
                str(data_source.relative_to(repo)).replace("\\", "/"): raw_file_sha256(data_source),
                "src/czsc_trader/strategy_runtime.py": raw_file_sha256(
                    repo / "src/czsc_trader/strategy_runtime.py"
                ),
            },
        },
    )

    identity_pass = bool(
        readiness["request"]["candidate_hash"] == candidate_hash
        and challenge["request"]["candidate_hash"] == candidate_hash
        and readiness["result"]["decision"] == "RECOMMEND_REGISTRATION"
        and statistical["status"] == "PASS"
        and external["status"] == "PASS"
    )
    reproducibility_pass = bool(
        replay["behavior_hash"] == protocol["expected_behavior_hash"]
        and replay["audit"]["status"] == "PASS"
    )
    technical_replay_pass = bool(
        replay["audit"]["status"] == "PASS"
        and int(replay["orders"]) == int(replay["fills"])
        and int(replay["metrics"]["closed_trades"]) >= 100
    )
    monitoring_pass = "状态：`APPROVED`" in monitoring_text
    conditional_request = FreezeHealthRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=candidate_hash,
        candidate_readiness=AuditStatus.PASS,
        benchmark_challenge=BenchmarkChallengeDecision(str(challenge["result"]["decision"])),
        evidence_integrity=AuditStatus.PASS if identity_pass else AuditStatus.FAIL,
        reproducibility=AuditStatus.PASS if reproducibility_pass else AuditStatus.FAIL,
        technical_replay=AuditStatus.PASS if technical_replay_pass else AuditStatus.FAIL,
        cost_stress=AuditStatus.PASS if cost_pass else AuditStatus.FAIL,
        monitoring_plan=AuditStatus.PASS if monitoring_pass else AuditStatus.INSUFFICIENT,
        mechanism_evidence=RiskLabel.FAVORABLE,
        statistical_evidence=RiskLabel.MIXED,
        parameter_robustness=RiskLabel.FAVORABLE,
        external_validation=RiskLabel.MIXED,
        execution_evidence=RiskLabel.FAVORABLE,
    )
    conditional_result = assess_freeze_health(conditional_request)
    blockers = () if pte_pass else (PTE_BLOCKER,)
    current_request = replace(conditional_request, blocking_findings=blockers)
    current_result = assess_freeze_health(current_request)
    evidence_index = {
        name: {
            "path": str(path.relative_to(repo)).replace("\\", "/"),
            "sha256": raw_file_sha256(path),
        }
        for name, path in evidence_paths.items()
    }
    source = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": protocol["candidate_id"],
        "candidate_hash": candidate_hash,
        "conditional_request": conditional_request.to_dict(),
        "conditional_result": conditional_result.to_dict(),
        "current_request": current_request.to_dict(),
        "current_result": current_result.to_dict(),
        "evidence_index": evidence_index,
        "cost_stress": _read(artifacts / "cost_stress_summary.json"),
        "pte_compatibility": _read(artifacts / "pte_compatibility.json"),
        "blocking_findings": [
            {
                "code": PTE_BLOCKER,
                "meaning": "PTE缺少S007实时特征发布和实时决策计算契约，冻结研究面板只覆盖开发截止日",
                "scope": "deployment_engineering",
                "candidate_evidence_affected": False,
            }
        ]
        if blockers
        else [],
        "risk_disclosures": [
            "PBO为46.43%，有效DSR概率为63.92%，统计证据保持MIXED",
            "588300外部验证存在单笔60.78%收益的集中风险，跨标的证据保守保持MIXED",
            "正式PK的年化收益Bootstrap 95%区间下界略低于零",
            "尚未产生S007独立前瞻模拟盘证据",
        ],
        "lifecycle_effects": {
            "strategy_version_created": False,
            "strategy_frozen": False,
            "pte_deployed": False,
        },
    }
    _write(
        artifacts / "freeze_health.json",
        {
            "source": source,
            "approval": {
                "schema_version": 1,
                "assessment_id": "FHC-20260915-S007-C001",
                "strategy_id": "S007",
                "candidate_id": protocol["candidate_id"],
                "candidate_hash": candidate_hash,
                "decision": current_result.decision.value,
                "risk_label": current_result.risk_label.value,
                "conditional_decision": conditional_result.decision.value,
                "conditional_risk_label": conditional_result.risk_label.value,
                "source_experiment": f"experiments/S007/{EXPERIMENT_ID}",
                "source_hash": canonical_json_sha256(source),
                "assessed_at": "2026-09-15T23:30:00+08:00",
            },
        },
    )
    (experiment / "03_execution.md").write_text(
        "# S007 EX33 执行\n\n"
        "状态：`COMPLETE`。SE汇总不可变候选、正式PK、统计和跨标的证据，并对15bp成本压力"
        "账户执行独立TDR回放及账本审计；同时实际探测实时策略执行边界和特征发布契约。"
        "本实验未创建版本、未冻结策略、未写入SM/PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX33 结论\n\n"
        f"15bp压力结果：年化{cost_performance.cagr:.2%}、最大回撤"
        f"{cost_performance.max_drawdown:.2%}、卡玛{cost_performance.calmar:.3f}，"
        f"成本压力审计`{'PASS' if cost_pass else 'FAIL'}`。\n\n"
        f"若只看策略与TDR证据，SE条件裁决为`{conditional_result.decision.value} / "
        f"{conditional_result.risk_label.value}`。当前生命周期裁决为"
        f"`{current_result.decision.value} / {current_result.risk_label.value}`。"
        f"{'唯一阻断项为`' + PTE_BLOCKER + '`：当前仅有确定性回测支持，实时特征发布和advice计算尚未实现。' if blockers else 'PTE兼容审计通过。'}"
        "工程阻断不改写候选研究证据；解除并复验前不得冻结或进入PTE。\n",
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
            "decision": current_result.decision.value,
            "conditional_decision": conditional_result.decision.value,
            "risk_label": current_result.risk_label.value,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
