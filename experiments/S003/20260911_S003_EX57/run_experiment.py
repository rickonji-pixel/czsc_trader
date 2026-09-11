from __future__ import annotations

from datetime import date
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
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.benchmarks import replay_benchmarks
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.intraday_overlay_replay import (
    build_moneyflow_breadth_signals,
    replay_intraday_overlay,
)
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260911_S003_EX57"
INITIAL_CASH = 100_000.0


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


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
    artifacts.mkdir(exist_ok=True)
    protocol = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "experiment_type": "benchmark_freeze_qualification",
        "strategy_id": "S003",
        "candidate_id": "S003-C001",
        "candidate_hash": "1d6736f816d652c59d43dd22491c567e65100eaddfa3d20575531ca5a124f741",
        "benchmark_id": "BuyHold-510500",
        "development_cutoff": "2026-09-08",
        "evaluation_start": "2021-04-15",
        "evaluation_end": "2026-09-08",
        "primary_mean_block_length": 21,
        "sensitivity_mean_block_lengths": [10, 42],
        "bootstrap_repetitions": 10_000,
        "seed": 20260911,
        "freezes_strategy": False,
        "mutates_pte": False,
    }
    _write(artifacts / "protocol.json", protocol)
    payload_path = repo / "experiments/S003/20260911_S003_EX56/candidate_payload.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    if payload["candidate_hash"] != protocol["candidate_hash"]:
        raise ValueError("candidate identity differs from benchmark protocol")
    context = RepositoryContext.discover(repo)
    data = load_replay_data(
        context, "research", "510500.SH", "etf", date(2026, 9, 8),
        include_five_minute=True,
    )
    snapshot = resolve_candidate_snapshot(
        context,
        "S003-C001",
        payload,
        canonical_json_sha256(payload),
        str(payload_path.relative_to(repo)).replace("\\", "/"),
    )
    signals = build_moneyflow_breadth_signals(
        snapshot, data, repo, pd.Timestamp("2021-04-15"), pd.Timestamp("2026-09-08")
    )
    result = replay_intraday_overlay(signals, data, INITIAL_CASH)
    candidate_metrics = calculate_metrics(result, INITIAL_CASH)
    replay_audit = audit_replay(
        build_replay_evidence(signals, data, result, INITIAL_CASH, candidate_metrics)
    )
    benchmarks = replay_benchmarks(signals, data, INITIAL_CASH)
    buyhold = benchmarks.buyhold_account_daily
    aligned = pd.concat(
        [
            _daily_returns(result.account_daily).rename("S003-C001"),
            _daily_returns(buyhold).rename("BuyHold-510500"),
        ],
        axis=1,
        join="inner",
    )
    if aligned.isna().any().any() or len(aligned) != len(result.account_daily):
        raise AssertionError("candidate and BuyHold returns are not fully aligned")
    evidence = ReturnMatrixEvidence(
        tuple(aligned.index.strftime("%Y-%m-%d")),
        ("S003-C001", "BuyHold-510500"),
        tuple(tuple(float(value) for value in row) for row in aligned.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(
        evidence.dates, evidence.candidate_ids, evidence.returns, hash_return_matrix(evidence)
    )
    comparisons = audit_pairwise_bootstrap(
        evidence,
        "S003-C001",
        "BuyHold-510500",
        (),
        repetitions=10_000,
        block_lengths=(21, 10, 42),
        seed=20260911,
    )
    primary = next(item for item in comparisons if item.mean_block_length == 21)
    candidate_performance = performance_metrics(aligned["S003-C001"].to_numpy())
    buyhold_performance = performance_metrics(aligned["BuyHold-510500"].to_numpy())
    monitoring = repo / "research/S003/candidates/S003-C001_MONITORING.md"
    monitoring_status = (
        AuditStatus.PASS
        if monitoring.is_file() and "状态：`APPROVED`" in monitoring.read_text(encoding="utf-8")
        else AuditStatus.INSUFFICIENT
    )
    request = BenchmarkChallengeRequest(
        candidate_id="S003-C001",
        candidate_hash=str(protocol["candidate_hash"]),
        benchmark_id="BuyHold-510500",
        integrity=AuditStatus.PASS,
        reproducibility=AuditStatus.PASS,
        technical_replay=replay_audit.status,
        monitoring_plan=monitoring_status,
        candidate_performance=candidate_performance,
        benchmark_performance=buyhold_performance,
        bootstrap=primary,
        statistical_evidence=RiskLabel.MIXED,
        external_validation=RiskLabel.FAVORABLE,
        execution_evidence=RiskLabel.FAVORABLE,
    )
    assessment = assess_benchmark_challenge(request)
    aligned.reset_index().to_csv(artifacts / "daily_return_matrix.csv", index=False)
    buyhold.to_csv(artifacts / "buyhold_account_daily.csv", index=False)
    _write(artifacts / "return_matrix_evidence.json", evidence.to_dict())
    _write(artifacts / "paired_bootstrap.json", {
        "comparisons": [item.to_dict() for item in comparisons]
    })
    _write(artifacts / "benchmark_assessment.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "request": request.to_dict(),
        "result": assessment.to_dict(),
        "candidate_metrics": candidate_metrics,
        "buyhold_total_return": float(buyhold["equity"].iloc[-1] / INITIAL_CASH - 1),
    })
    values = {item.metric: item for item in (primary.cagr, primary.max_drawdown, primary.calmar)}
    (experiment / "03_execution.md").write_text(
        "# S003 EX57 执行\n\n"
        f"在{len(aligned)}个共同交易日完成正式账本与BuyHold的10/21/42日区块Bootstrap。"
        f"SE技术回放审计为`{replay_audit.status.value}`，未冻结策略，未写入PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX57 结论\n\n"
        f"SE裁决：`{assessment.decision.value}`；风险标签：`{assessment.risk_label.value}`。\n\n"
        "| 对象 | 累计收益 | 年化收益 | 最大回撤 | 卡玛 | 盈亏比 |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        f"| S003-C001 | {float(candidate_metrics['return']):.2%} | {candidate_performance.cagr:.2%} | "
        f"{candidate_performance.max_drawdown:.2%} | {candidate_performance.calmar:.3f} | "
        f"{float(candidate_metrics['win_loss_ratio']):.3f} |\n"
        f"| BuyHold-510500 | {float(buyhold['equity'].iloc[-1] / INITIAL_CASH - 1):.2%} | "
        f"{buyhold_performance.cagr:.2%} | {buyhold_performance.max_drawdown:.2%} | "
        f"{buyhold_performance.calmar:.3f} | 不适用 |\n\n"
        "21日主区块胜出概率："
        f"年化收益{values['cagr'].probability_favorable:.2%}、"
        f"最大回撤{values['max_drawdown'].probability_favorable:.2%}、"
        f"卡玛{values['calmar'].probability_favorable:.2%}。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": "S003",
        "symbol": "510500.SH",
        "candidate_id": "S003-C001",
        "benchmark_id": "BuyHold-510500",
        "development_cutoff": "2026-09-08",
        "decision": assessment.decision.value,
        "risk_label": assessment.risk_label.value,
        "strategy_frozen": False,
        "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
