from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
from strategy_evaluator import (
    AuditStatus,
    BenchmarkChallengeRequest,
    ReturnMatrixEvidence,
    RiskLabel,
    assess_benchmark_challenge,
    audit_pairwise_bootstrap,
    audit_replay,
    hash_return_matrix,
    performance_metrics,
)

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.benchmarks import replay_benchmarks
from czsc_trader.backtesting.causal_feature_gate_replay import build_causal_feature_gate_signals
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260915_S007_EX32"


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


def _daily_returns(account: pd.DataFrame, initial_cash: float) -> pd.Series:
    equity = account.copy()
    equity["date"] = pd.to_datetime(equity["date"], errors="raise").dt.normalize()
    values = equity.set_index("date").sort_index()["equity"].astype(float)
    returns = values.pct_change()
    returns.iloc[0] = values.iloc[0] / initial_cash - 1.0
    return returns


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("EX32 protocol identity differs")
    if any(protocol.get(key) for key in ("strategy_frozen", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX32 cannot freeze or mutate SM/PTE")
    sources = protocol["sources"]
    ex31 = repo / str(sources["ex31_archive"])
    validate_experiment_archive(ex31)
    frozen = {
        ex31 / "experiment_manifest.json": sources["ex31_manifest_sha256"],
        ex31 / "candidate_payload.json": sources["candidate_payload_sha256"],
        ex31 / "candidate_manifest.json": sources["candidate_manifest_sha256"],
        ex31 / "artifacts/candidate_readiness.json": sources["candidate_readiness_sha256"],
        ex31 / "artifacts/tdr_replay_summary.json": sources["tdr_replay_summary_sha256"],
    }
    if any(_sha256(path) != expected for path, expected in frozen.items()):
        raise ValueError("EX31 candidate evidence differs")
    readiness = _read(ex31 / "artifacts/candidate_readiness.json")
    if readiness["result"]["decision"] != "RECOMMEND_REGISTRATION":
        raise ValueError("candidate was not recommended for registration")
    candidate_path = ex31 / "candidate_payload.json"
    candidate = _read(candidate_path)
    candidate_hash = canonical_json_sha256(candidate)
    if candidate_hash != protocol["candidate_hash"]:
        raise ValueError("candidate identity differs from PK protocol")

    context = RepositoryContext.discover(repo)
    snapshot = resolve_candidate_snapshot(
        context,
        str(protocol["candidate_id"]),
        candidate,
        candidate_hash,
        str(candidate_path.relative_to(repo)).replace("\\", "/"),
    )
    end = date.fromisoformat(str(protocol["development_cutoff"]))
    replay_data = load_replay_data(context, "research", str(protocol["symbol"]), "etf", end)
    signals = build_causal_feature_gate_signals(
        snapshot,
        replay_data,
        pd.Timestamp(protocol["evaluation_start"]),
        pd.Timestamp(protocol["development_cutoff"]),
        repo,
    )
    initial_cash = float(protocol["initial_cash"])
    result = replay_account(signals, replay_data, initial_cash)
    candidate_metrics = calculate_metrics(result, initial_cash)
    replay_audit = audit_replay(
        build_replay_evidence(signals, replay_data, result, initial_cash, candidate_metrics)
    )
    benchmarks = replay_benchmarks(signals, replay_data, initial_cash)
    buyhold = benchmarks.buyhold_account_daily
    aligned = pd.concat(
        [
            _daily_returns(result.account_daily, initial_cash).rename(str(protocol["candidate_id"])),
            _daily_returns(buyhold, initial_cash).rename(str(protocol["benchmark_id"])),
        ],
        axis=1,
        join="inner",
    )
    if aligned.isna().any().any() or len(aligned) != len(result.account_daily):
        raise ValueError("candidate and BuyHold daily returns are not fully aligned")
    evidence = ReturnMatrixEvidence(
        tuple(aligned.index.strftime("%Y-%m-%d")),
        tuple(aligned.columns),
        tuple(tuple(float(value) for value in row) for row in aligned.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(
        evidence.dates,
        evidence.candidate_ids,
        evidence.returns,
        hash_return_matrix(evidence),
    )
    block_lengths = (
        int(protocol["primary_mean_block_length"]),
        *tuple(map(int, protocol["sensitivity_mean_block_lengths"])),
    )
    comparisons = audit_pairwise_bootstrap(
        evidence,
        str(protocol["candidate_id"]),
        str(protocol["benchmark_id"]),
        (),
        repetitions=int(protocol["bootstrap_repetitions"]),
        block_lengths=block_lengths,
        seed=int(protocol["seed"]),
    )
    primary = next(
        item for item in comparisons if item.mean_block_length == protocol["primary_mean_block_length"]
    )
    candidate_performance = performance_metrics(aligned[str(protocol["candidate_id"])].to_numpy())
    benchmark_performance = performance_metrics(aligned[str(protocol["benchmark_id"])].to_numpy())
    monitoring = repo / "research/S007/candidates/S007-C001_MONITORING.md"
    monitoring_status = (
        AuditStatus.PASS
        if monitoring.is_file() and "状态：`APPROVED`" in monitoring.read_text(encoding="utf-8")
        else AuditStatus.INSUFFICIENT
    )
    request = BenchmarkChallengeRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=candidate_hash,
        benchmark_id=str(protocol["benchmark_id"]),
        integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        technical_replay=replay_audit.status,
        monitoring_plan=monitoring_status,
        candidate_performance=candidate_performance,
        benchmark_performance=benchmark_performance,
        bootstrap=primary,
        statistical_evidence=RiskLabel.MIXED,
        external_validation=RiskLabel.MIXED,
        execution_evidence=RiskLabel.FAVORABLE,
    )
    assessment = assess_benchmark_challenge(request)
    aligned.reset_index(names="date").to_csv(
        artifacts / "daily_return_matrix.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    result.account_daily.to_csv(
        artifacts / "candidate_account_daily.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    buyhold.to_csv(
        artifacts / "buyhold_account_daily.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    _write(artifacts / "return_matrix_evidence.json", evidence.to_dict())
    _write(artifacts / "paired_bootstrap.json", {"comparisons": [item.to_dict() for item in comparisons]})
    _write(
        artifacts / "benchmark_assessment.json",
        {
            "schema_version": 1,
            "request": request.to_dict(),
            "result": assessment.to_dict(),
            "candidate_account_metrics": candidate_metrics,
            "buyhold_total_return": float(buyhold["equity"].iloc[-1] / initial_cash - 1.0),
        },
    )
    values = {item.metric: item for item in (primary.cagr, primary.max_drawdown, primary.calmar)}
    (experiment / "03_execution.md").write_text(
        "# S007 EX32 执行\n\n"
        f"状态：`COMPLETE`。TDR在{len(aligned)}个共同交易日重建候选与BuyHold独立账户，"
        f"SE技术账本审计为`{replay_audit.status.value}`，完成10/21/42日配对区块Bootstrap。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX32 结论\n\n"
        f"SE裁决：`{assessment.decision.value}`；风险标签：`{assessment.risk_label.value}`。\n\n"
        "| 对象 | 累计收益 | 年化收益 | 最大回撤 | 卡玛 | 盈亏比 |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        f"| S007-C001 | {float(candidate_metrics['return']):.2%} | {candidate_performance.cagr:.2%} | "
        f"{candidate_performance.max_drawdown:.2%} | {candidate_performance.calmar:.3f} | "
        f"{float(candidate_metrics['win_loss_ratio']):.3f} |\n"
        f"| BuyHold-588080 | {float(buyhold['equity'].iloc[-1] / initial_cash - 1.0):.2%} | "
        f"{benchmark_performance.cagr:.2%} | {benchmark_performance.max_drawdown:.2%} | "
        f"{benchmark_performance.calmar:.3f} | 不适用 |\n\n"
        f"21日主区块年化/回撤/卡玛胜出概率为{values['cagr'].probability_favorable:.2%}/"
        f"{values['max_drawdown'].probability_favorable:.2%}/"
        f"{values['calmar'].probability_favorable:.2%}。本轮只决定是否进入完整体检，未冻结策略或修改SM/PTE。\n",
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
            "benchmark_id": protocol["benchmark_id"],
            "decision": assessment.decision.value,
            "risk_label": assessment.risk_label.value,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
