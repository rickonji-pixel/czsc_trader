from __future__ import annotations

import json
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
from czsc_trader.backtesting import (
    build_closing_dislocation_signals,
    load_replay_data,
    resolve_candidate_snapshot,
)
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.benchmarks import replay_benchmarks
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260912_S004_EX16"
INITIAL_CASH = 100_000.0


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _daily_returns(account: pd.DataFrame) -> pd.Series:
    frame = account.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    equity = frame.set_index("date").sort_index()["equity"].astype(float)
    returns = equity.pct_change()
    returns.iloc[0] = equity.iloc[0] / INITIAL_CASH - 1.0
    return returns


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    for source_id in ("20260912_S004_EX14", "20260912_S004_EX15"):
        validate_experiment_archive(repo / "experiments" / "S004" / source_id)
    payload_path = repo / str(protocol["candidate_payload"])
    payload = _read(payload_path)
    if payload.get("candidate_hash") != protocol["candidate_hash"]:
        raise ValueError("candidate identity differs from benchmark protocol")
    statistical = _read(repo / "experiments/S004/20260912_S004_EX15/artifacts/statistical_summary.json")

    context = RepositoryContext.discover(repo)
    data = load_replay_data(
        context,
        "research",
        "588080.SH",
        "etf",
        pd.Timestamp(protocol["development_cutoff"]).date(),
        include_one_minute=True,
    )
    snapshot = resolve_candidate_snapshot(
        context,
        str(protocol["candidate_id"]),
        payload,
        canonical_json_sha256(payload),
        str(payload_path.relative_to(repo)),
    )
    signals = build_closing_dislocation_signals(
        snapshot,
        data,
        pd.Timestamp(protocol["evaluation_start"]),
        pd.Timestamp(protocol["evaluation_end"]),
    )
    result = replay_account(signals, data, INITIAL_CASH)
    candidate_metrics = calculate_metrics(result, INITIAL_CASH)
    replay_audit = audit_replay(
        build_replay_evidence(signals, data, result, INITIAL_CASH, candidate_metrics)
    )
    benchmarks = replay_benchmarks(signals, data, INITIAL_CASH)
    buyhold = benchmarks.buyhold_account_daily
    aligned = pd.concat(
        [
            _daily_returns(result.account_daily).rename(str(protocol["candidate_id"])),
            _daily_returns(buyhold).rename(str(protocol["benchmark_id"])),
        ],
        axis=1,
        join="inner",
    )
    if aligned.isna().any().any() or len(aligned) != len(result.account_daily):
        raise AssertionError("candidate and BuyHold returns are not fully aligned")
    evidence = ReturnMatrixEvidence(
        tuple(aligned.index.strftime("%Y-%m-%d")),
        tuple(aligned.columns),
        tuple(tuple(float(value) for value in row) for row in aligned.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(
        evidence.dates, evidence.candidate_ids, evidence.returns, hash_return_matrix(evidence)
    )
    comparisons = audit_pairwise_bootstrap(
        evidence,
        str(protocol["candidate_id"]),
        str(protocol["benchmark_id"]),
        (),
        repetitions=int(protocol["bootstrap_repetitions"]),
        block_lengths=(
            int(protocol["primary_mean_block_length"]),
            *tuple(map(int, protocol["sensitivity_mean_block_lengths"])),
        ),
        seed=int(protocol["seed"]),
    )
    primary = next(
        item for item in comparisons
        if item.mean_block_length == int(protocol["primary_mean_block_length"])
    )
    candidate_performance = performance_metrics(aligned[str(protocol["candidate_id"])].to_numpy())
    buyhold_performance = performance_metrics(aligned[str(protocol["benchmark_id"])].to_numpy())
    request = BenchmarkChallengeRequest(
        candidate_id=str(protocol["candidate_id"]),
        candidate_hash=str(protocol["candidate_hash"]),
        benchmark_id=str(protocol["benchmark_id"]),
        integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        technical_replay=replay_audit.status,
        monitoring_plan=AuditStatus.INSUFFICIENT,
        candidate_performance=candidate_performance,
        benchmark_performance=buyhold_performance,
        bootstrap=primary,
        statistical_evidence=(
            RiskLabel.WEAK if statistical["evidence_label"] == "ADVERSE" else RiskLabel.MIXED
        ),
        external_validation=RiskLabel.MIXED,
        execution_evidence=RiskLabel.FAVORABLE,
    )
    assessment = assess_benchmark_challenge(request)
    aligned.reset_index(names="date").to_csv(artifacts / "daily_return_matrix.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    buyhold.to_csv(artifacts / "buyhold_account_daily.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(artifacts / "return_matrix_evidence.json", evidence.to_dict())
    _write(artifacts / "paired_bootstrap.json", {"comparisons": [item.to_dict() for item in comparisons]})
    _write(artifacts / "benchmark_assessment.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "request": request.to_dict(),
        "result": assessment.to_dict(),
        "candidate_metrics": candidate_metrics,
        "buyhold_total_return": float(buyhold["equity"].iloc[-1] / INITIAL_CASH - 1.0),
    })
    values = {item.metric: item for item in (primary.cagr, primary.max_drawdown, primary.calmar)}
    (experiment / "03_execution.md").write_text(
        f"# 20260912_S004_EX16 执行\n\n状态：COMPLETE。{len(aligned)}个共同交易日完成10/21/42日配对区块Bootstrap，SE技术回放审计为`{replay_audit.status.value}`。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# 20260912_S004_EX16 结论\n\n"
        f"SE裁决：`{assessment.decision.value}`；风险标签：`{assessment.risk_label.value}`。\n\n"
        "| 对象 | 累计收益 | 年化收益 | 最大回撤 | 卡玛 | 盈亏比 |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        f"| S004-C001 | {float(candidate_metrics['return']):.2%} | {candidate_performance.cagr:.2%} | {candidate_performance.max_drawdown:.2%} | {candidate_performance.calmar:.3f} | {float(candidate_metrics['win_loss_ratio']):.3f} |\n"
        f"| BuyHold-588080 | {float(buyhold['equity'].iloc[-1] / INITIAL_CASH - 1):.2%} | {buyhold_performance.cagr:.2%} | {buyhold_performance.max_drawdown:.2%} | {buyhold_performance.calmar:.3f} | 不适用 |\n\n"
        f"21日主区块胜出概率：年化收益{values['cagr'].probability_favorable:.2%}、最大回撤{values['max_drawdown'].probability_favorable:.2%}、卡玛{values['calmar'].probability_favorable:.2%}。\n\n"
        "PK结果不会覆盖EX15的统计告警，本轮不冻结、不写入PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": "S004",
        "symbol": "588080.SH",
        "candidate_id": protocol["candidate_id"],
        "benchmark_id": protocol["benchmark_id"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": assessment.decision.value,
        "risk_label": assessment.risk_label.value,
        "strategy_frozen": False,
        "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
