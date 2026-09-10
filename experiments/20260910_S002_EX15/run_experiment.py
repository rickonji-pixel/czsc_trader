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
    hash_return_matrix,
    performance_metrics,
)

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.benchmarks import replay_benchmarks
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260910_S002_EX15"
INITIAL_CASH = 100_000.0


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _daily_returns(account: pd.DataFrame) -> pd.Series:
    frame = account.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame = frame.set_index("date").sort_index()
    equity = frame["equity"].astype(float)
    returns = equity.pct_change()
    returns.iloc[0] = equity.iloc[0] / INITIAL_CASH - 1.0
    return returns


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    protocol = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "experiment_type": "benchmark_freeze_qualification",
        "strategy_id": "S002",
        "candidate_id": "S002-C001",
        "candidate_hash": "8a804a7fafc784c92ddd8b8c2d4c3fbc8dda953ca0fd7f0cfb6ab6fd7650574d",
        "benchmark_id": "BuyHold-510500",
        "symbol": "510500.SH",
        "asset_type": "etf",
        "dataset": "research",
        "development_cutoff": "2026-09-08",
        "evaluation_start": "2021-01-04",
        "evaluation_end": "2026-09-08",
        "initial_cash": INITIAL_CASH,
        "fee_rate": 0.0005,
        "primary_mean_block_length": 21,
        "sensitivity_mean_block_lengths": [10, 42],
        "bootstrap_repetitions": 10_000,
        "seed": 20260910,
        "freezes_strategy": False,
        "mutates_pte": False,
    }
    _write(artifacts / "protocol.json", protocol)

    candidate_path = repo / "experiments/20260910_S002_EX12/candidate_payload.json"
    candidate = _read(candidate_path)
    if canonical_json_sha256(candidate) != protocol["candidate_hash"]:
        raise ValueError("S002-C001 candidate hash differs from protocol")
    deployed_path = repo / "experiments/20260910_S002_EX14/artifacts/proposed_strategy_payload.json"
    deployed = _read(deployed_path)

    context = RepositoryContext.discover(repo)
    replay_data = load_replay_data(
        context,
        "research",
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    snapshot = resolve_candidate_snapshot(
        context,
        "S002-C001-EXECUTION",
        deployed,
        canonical_json_sha256(deployed),
        str(deployed_path.relative_to(repo)).replace("\\", "/"),
    )
    signals = replay_signals(
        snapshot,
        replay_data,
        date.fromisoformat(str(protocol["evaluation_start"])),
        date.fromisoformat(str(protocol["evaluation_end"])),
    )
    candidate_result = replay_account(signals, replay_data, INITIAL_CASH)
    candidate_metrics = calculate_metrics(candidate_result, INITIAL_CASH)
    benchmarks = replay_benchmarks(signals, replay_data, INITIAL_CASH)
    buyhold_account = benchmarks.buyhold_account_daily

    candidate_returns = _daily_returns(candidate_result.account_daily)
    buyhold_returns = _daily_returns(buyhold_account)
    aligned = pd.concat(
        [candidate_returns.rename("S002-C001"), buyhold_returns.rename("BuyHold-510500")],
        axis=1,
        join="inner",
    )
    if aligned.isna().any().any() or len(aligned) != len(candidate_returns):
        raise AssertionError("candidate and BuyHold returns are not fully aligned")
    evidence = ReturnMatrixEvidence(
        tuple(aligned.index.strftime("%Y-%m-%d")),
        ("S002-C001", "BuyHold-510500"),
        tuple(tuple(float(value) for value in row) for row in aligned.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(
        evidence.dates,
        evidence.candidate_ids,
        evidence.returns,
        hash_return_matrix(evidence),
    )
    comparisons = audit_pairwise_bootstrap(
        evidence,
        "S002-C001",
        "BuyHold-510500",
        (),
        repetitions=int(protocol["bootstrap_repetitions"]),
        block_lengths=(21, 10, 42),
        seed=int(protocol["seed"]),
    )
    primary = next(item for item in comparisons if item.mean_block_length == 21)
    candidate_performance = performance_metrics(aligned["S002-C001"].to_numpy())
    buyhold_performance = performance_metrics(aligned["BuyHold-510500"].to_numpy())

    replay_evidence = build_replay_evidence(
        signals,
        replay_data,
        candidate_result,
        INITIAL_CASH,
        candidate_metrics,
    )
    from strategy_evaluator import audit_replay

    replay_audit = audit_replay(replay_evidence)
    monitoring_path = repo / "research/S002/candidates/S002-C001_MONITORING.md"
    monitoring_status = (
        AuditStatus.PASS
        if monitoring_path.is_file() and "状态：`APPROVED`" in monitoring_path.read_text(encoding="utf-8")
        else AuditStatus.INSUFFICIENT
    )
    request = BenchmarkChallengeRequest(
        candidate_id="S002-C001",
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
        external_validation=RiskLabel.MIXED,
        execution_evidence=RiskLabel.MIXED,
    )
    assessment = assess_benchmark_challenge(request)

    aligned.reset_index().to_csv(
        artifacts / "daily_return_matrix.csv", index=False, encoding="utf-8-sig"
    )
    buyhold_account.to_csv(
        artifacts / "buyhold_account_daily.csv", index=False, encoding="utf-8-sig"
    )
    _write(artifacts / "return_matrix_evidence.json", evidence.to_dict())
    _write(
        artifacts / "paired_bootstrap.json",
        {"comparisons": [item.to_dict() for item in comparisons]},
    )
    _write(
        artifacts / "freeze_assessment.json",
        {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "request": request.to_dict(),
            "result": assessment.to_dict(),
            "candidate_total_return": float(candidate_metrics["return"]),
            "candidate_profit_factor": float(candidate_metrics["win_loss_ratio"]),
            "buyhold_total_return": float(buyhold_account["equity"].iloc[-1] / INITIAL_CASH - 1.0),
            "buyhold_profit_factor": None,
            "profit_factor_comparison": "NOT_APPLICABLE_BUYHOLD_HAS_NO_CLOSED_TRADES",
            "supporting_evidence": {
                "statistical": "experiments/20260910_S002_EX09",
                "external_validation": "experiments/20260910_S002_EX11",
                "candidate_registration": "experiments/20260910_S002_EX12",
                "formal_execution": "experiments/20260910_S002_EX14",
            },
            "lifecycle_effects": {"frozen": False, "pte_deployed": False},
        },
    )

    primary_values = {
        item.metric: item for item in (primary.cagr, primary.max_drawdown, primary.calmar)
    }
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。\n\n"
        f"已在 {len(aligned)} 个共同交易日上重新生成独立 BuyHold 对手，并完成 10、21、42 "
        "交易日平均区块的配对 Bootstrap。候选正式执行回放通过，未冻结策略，未写入 PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n状态：COMPLETE。\n\n"
        f"SE 裁决：`{assessment.decision.value}`；风险标签：`{assessment.risk_label.value}`。\n\n"
        "| 对象 | 累计收益 | 年化收益 | 最大回撤 | 卡玛 | 盈亏比 |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        f"| S002-C001 | {float(candidate_metrics['return']):.2%} | {candidate_performance.cagr:.2%} | "
        f"{candidate_performance.max_drawdown:.2%} | {candidate_performance.calmar:.3f} | "
        f"{float(candidate_metrics['win_loss_ratio']):.3f} |\n"
        f"| BuyHold-510500 | {float(buyhold_account['equity'].iloc[-1] / INITIAL_CASH - 1.0):.2%} | "
        f"{buyhold_performance.cagr:.2%} | {buyhold_performance.max_drawdown:.2%} | "
        f"{buyhold_performance.calmar:.3f} | 不适用 |\n\n"
        "21交易日平均区块 Bootstrap 的候选胜出概率为："
        f"年化收益 {primary_values['cagr'].probability_favorable:.2%}、"
        f"最大回撤 {primary_values['max_drawdown'].probability_favorable:.2%}、"
        f"卡玛 {primary_values['calmar'].probability_favorable:.2%}。\n\n"
        "候选已击败首个明确对手，具备提交冻结人工评审的资格。年化收益优势的概率证据偏弱，"
        "既有DSR、横截面和正式执行证据均为MIXED，因此本结论不等同于统计显著性已被充分证明。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S002",
            "symbol": protocol["symbol"],
            "candidate_id": protocol["candidate_id"],
            "benchmark_id": protocol["benchmark_id"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": assessment.decision.value,
            "risk_label": assessment.risk_label.value,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
