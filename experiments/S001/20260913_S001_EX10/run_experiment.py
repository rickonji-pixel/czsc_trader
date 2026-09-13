from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
from strategy_evaluator import (
    AuditStatus,
    BenchmarkChallengeDecision,
    BenchmarkChallengeRequest,
    MandateChallengeRequest,
    ReturnMatrixEvidence,
    RiskLabel,
    assess_benchmark_challenge,
    assess_mandate_challenge,
    audit_pairwise_bootstrap,
    audit_replay,
    hash_return_matrix,
    performance_metrics,
)

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_candidate_snapshot, resolve_registered_strategy
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.benchmarks import replay_benchmarks
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260913_S001_EX10"


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
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _daily_returns(account: pd.DataFrame, initial_cash: float) -> pd.Series:
    frame = account.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    equity = frame.set_index("date").sort_index()["equity"].astype(float)
    returns = equity.pct_change()
    returns.iloc[0] = equity.iloc[0] / initial_cash - 1.0
    return returns


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    readiness_source = repo / str(protocol["readiness_experiment"])
    validate_experiment_archive(readiness_source)
    readiness_path = readiness_source / "artifacts/candidate_readiness.json"
    payload_path = repo / str(protocol["candidate_payload"])
    if _sha256(readiness_path) != protocol["readiness_sha256"]:
        raise ValueError("candidate readiness differs from protocol")
    if _sha256(payload_path) != protocol["candidate_payload_sha256"]:
        raise ValueError("candidate payload file differs from protocol")
    readiness = _read(readiness_path)
    if readiness["assessment"]["decision"] != "RECOMMEND_REGISTRATION":
        raise ValueError("candidate did not receive research registration recommendation")
    payload = _read(payload_path)
    if canonical_json_sha256(payload) != protocol["candidate_hash"]:
        raise ValueError("candidate canonical identity differs from protocol")
    monitoring = (repo / str(protocol["monitoring_plan"])).read_text(encoding="utf-8")
    if "状态：`PREPARED`" not in monitoring:
        raise ValueError("candidate monitoring plan is not prepared")

    context = RepositoryContext.discover(repo)
    candidate = resolve_candidate_snapshot(
        context,
        str(protocol["candidate_id"]),
        payload,
        str(protocol["candidate_hash"]),
        str(protocol["candidate_payload"]),
    )
    incumbent = resolve_registered_strategy(context, "S001", "v2")
    cutoff = pd.Timestamp(str(protocol["development_cutoff"])).date()
    data = load_replay_data(
        context,
        str(protocol["dataset"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        cutoff,
    )
    start = pd.Timestamp(str(protocol["evaluation_start"])).date()
    end = pd.Timestamp(str(protocol["evaluation_end"])).date()
    results: dict[str, tuple[object, dict[str, object]]] = {}
    signals_by_id = {}
    for identity, snapshot in (
        (str(protocol["candidate_id"]), candidate),
        (str(protocol["incumbent_id"]), incumbent),
    ):
        signals = replay_signals(snapshot, data, start, end)
        replay = replay_account(signals, data, float(protocol["initial_cash"]))
        metrics = calculate_metrics(replay, float(protocol["initial_cash"]))
        audit = audit_replay(
            build_replay_evidence(
                signals, data, replay, float(protocol["initial_cash"]), metrics
            )
        )
        if audit.status is not AuditStatus.PASS:
            raise AssertionError(f"{identity}: SE replay audit failed")
        results[identity] = (replay, metrics)
        signals_by_id[identity] = signals
    benchmark = replay_benchmarks(
        signals_by_id[str(protocol["candidate_id"])],
        data,
        float(protocol["initial_cash"]),
    )
    candidate_id = str(protocol["candidate_id"])
    incumbent_id = str(protocol["incumbent_id"])
    benchmark_id = str(protocol["benchmark_id"])
    aligned = pd.concat(
        [
            _daily_returns(results[candidate_id][0].account_daily, float(protocol["initial_cash"])).rename(candidate_id),
            _daily_returns(results[incumbent_id][0].account_daily, float(protocol["initial_cash"])).rename(incumbent_id),
            _daily_returns(benchmark.buyhold_account_daily, float(protocol["initial_cash"])).rename(benchmark_id),
        ],
        axis=1,
        join="inner",
    )
    if aligned.isna().any().any():
        raise AssertionError("PK returns are not aligned")
    evidence = ReturnMatrixEvidence(
        tuple(aligned.index.strftime("%Y-%m-%d")),
        tuple(aligned.columns),
        tuple(tuple(float(value) for value in row) for row in aligned.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(
        evidence.dates, evidence.candidate_ids, evidence.returns, hash_return_matrix(evidence)
    )
    block_lengths = (
        int(protocol["primary_mean_block_length"]),
        *tuple(map(int, protocol["sensitivity_mean_block_lengths"])),
    )
    buyhold_bootstraps = audit_pairwise_bootstrap(
        evidence,
        candidate_id,
        benchmark_id,
        (),
        repetitions=int(protocol["bootstrap_repetitions"]),
        block_lengths=block_lengths,
        seed=int(protocol["seed"]),
    )
    primary = next(
        item
        for item in buyhold_bootstraps
        if item.mean_block_length == int(protocol["primary_mean_block_length"])
    )
    candidate_performance = performance_metrics(aligned[candidate_id].to_numpy())
    incumbent_performance = performance_metrics(aligned[incumbent_id].to_numpy())
    buyhold_performance = performance_metrics(aligned[benchmark_id].to_numpy())
    buyhold_request = BenchmarkChallengeRequest(
        candidate_id=candidate_id,
        candidate_hash=str(protocol["candidate_hash"]),
        benchmark_id=benchmark_id,
        integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        technical_replay=AuditStatus.PASS,
        monitoring_plan=AuditStatus.PASS,
        candidate_performance=candidate_performance,
        benchmark_performance=buyhold_performance,
        bootstrap=primary,
        statistical_evidence=RiskLabel.MIXED,
        external_validation=RiskLabel.MIXED,
        execution_evidence=RiskLabel.FAVORABLE,
    )
    buyhold_assessment = assess_benchmark_challenge(buyhold_request)
    mandate_request = MandateChallengeRequest(
        candidate_id=candidate_id,
        candidate_hash=str(protocol["candidate_hash"]),
        incumbent_id=incumbent_id,
        integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        technical_replay=AuditStatus.PASS,
        candidate_performance=candidate_performance,
        incumbent_performance=incumbent_performance,
        minimum_cagr=float(protocol["minimum_cagr"]),
        maximum_drawdown_floor=float(protocol["maximum_drawdown_floor"]),
    )
    mandate_assessment = assess_mandate_challenge(mandate_request)
    passed = (
        buyhold_assessment.decision is BenchmarkChallengeDecision.RECOMMEND_HEALTH_CHECK
        and mandate_assessment.decision
        is BenchmarkChallengeDecision.RECOMMEND_HEALTH_CHECK
    )
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "candidate_id": candidate_id,
        "buyhold_challenge": {
            "request": buyhold_request.to_dict(),
            "assessment": buyhold_assessment.to_dict(),
            "bootstrap": [item.to_dict() for item in buyhold_bootstraps],
        },
        "incumbent_mandate_challenge": {
            "request": mandate_request.to_dict(),
            "assessment": mandate_assessment.to_dict(),
        },
        "route_decision": "PROCEED_TO_FREEZE_HEALTH_CHECK" if passed else "KEEP_RESEARCHING",
        "strategy_frozen": False,
        "pte_mutated": False,
    }
    aligned.reset_index(names="date").to_csv(
        artifacts / "daily_return_matrix.csv", index=False, lineterminator="\n"
    )
    _write(artifacts / "challenge_result.json", result)
    (experiment / "03_execution.md").write_text(
        "# S001 EX10 执行\n\n"
        "状态：`COMPLETE`。候选、在位策略账本通过 SE 审计；完成 BuyHold 区块 Bootstrap"
        "和在位策略目标约束挑战。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX10 结论\n\n"
        f"对 BuyHold：`{buyhold_assessment.decision.value}`；对 S001-v2："
        f"`{mandate_assessment.decision.value}`。候选以 CAGR 代价"
        f" {mandate_assessment.cagr_cost:.2%} 换取最大回撤改善"
        f" {mandate_assessment.drawdown_improvement:.2%}，卡玛变化"
        f" {mandate_assessment.calmar_change:.3f}。裁决：`{result['route_decision']}`。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S001",
            "candidate_id": candidate_id,
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": result["route_decision"],
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
