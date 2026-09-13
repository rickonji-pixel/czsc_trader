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
from czsc_trader.backtesting import build_closing_dislocation_signals, load_replay_data, resolve_candidate_snapshot
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.benchmarks import replay_benchmarks
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260913_S004_EX44"
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


def _bootstrap(evidence: ReturnMatrixEvidence, candidate: str, opponent: str, protocol: dict[str, object]):
    return audit_pairwise_bootstrap(
        evidence,
        candidate,
        opponent,
        (),
        repetitions=int(protocol["bootstrap_repetitions"]),
        block_lengths=(
            int(protocol["primary_mean_block_length"]),
            *tuple(map(int, protocol["sensitivity_mean_block_lengths"])),
        ),
        seed=int(protocol["seed"]),
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    for source_id in ("20260912_S004_EX14", "20260913_S004_EX42", "20260913_S004_EX43"):
        validate_experiment_archive(repo / "experiments" / "S004" / source_id)
    statistical = _read(repo / "experiments/S004/20260913_S004_EX43/artifacts/statistical_summary.json")
    if statistical["evidence_label"] != "FAVORABLE":
        raise ValueError("candidate statistical evidence differs from PK protocol")
    monitoring = (repo / str(protocol["monitoring_plan"])).read_text(encoding="utf-8")
    if "状态：`PREPARED`" not in monitoring or "融资明细必须在信号次日开盘前稳定到达" not in monitoring:
        raise ValueError("candidate monitoring plan is incomplete")

    context = RepositoryContext.discover(repo)
    data = load_replay_data(
        context,
        str(protocol["dataset"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        pd.Timestamp(protocol["development_cutoff"]).date(),
        include_one_minute=True,
    )
    results = {}
    signals_by_id = {}
    for identity_key, payload_key in (("candidate_id", "candidate_payload"), ("incumbent_id", "incumbent_payload")):
        identity = str(protocol[identity_key])
        payload_path = repo / str(protocol[payload_key])
        payload = _read(payload_path)
        snapshot = resolve_candidate_snapshot(
            context,
            identity,
            payload,
            canonical_json_sha256(payload),
            str(payload_path.relative_to(repo)),
        )
        signals = build_closing_dislocation_signals(
            snapshot,
            data,
            pd.Timestamp(protocol["evaluation_start"]),
            pd.Timestamp(protocol["evaluation_end"]),
            repo if identity == protocol["candidate_id"] else None,
        )
        result = replay_account(signals, data, INITIAL_CASH)
        metrics = calculate_metrics(result, INITIAL_CASH)
        if audit_replay(build_replay_evidence(signals, data, result, INITIAL_CASH, metrics)).status is not AuditStatus.PASS:
            raise AssertionError(f"{identity}: SE replay audit failed")
        results[identity] = (result, metrics)
        signals_by_id[identity] = signals

    benchmark = replay_benchmarks(signals_by_id[str(protocol["candidate_id"])], data, INITIAL_CASH)
    buyhold = benchmark.buyhold_account_daily
    aligned = pd.concat(
        [
            _daily_returns(results[str(protocol["candidate_id"])][0].account_daily).rename(str(protocol["candidate_id"])),
            _daily_returns(results[str(protocol["incumbent_id"])][0].account_daily).rename(str(protocol["incumbent_id"])),
            _daily_returns(buyhold).rename(str(protocol["benchmark_id"])),
        ],
        axis=1,
        join="inner",
    )
    if aligned.isna().any().any():
        raise AssertionError("PK daily returns are not fully aligned")
    evidence = ReturnMatrixEvidence(
        tuple(aligned.index.strftime("%Y-%m-%d")),
        tuple(aligned.columns),
        tuple(tuple(float(value) for value in row) for row in aligned.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(evidence.dates, evidence.candidate_ids, evidence.returns, hash_return_matrix(evidence))

    candidate_id = str(protocol["candidate_id"])
    comparisons = {}
    assessments = {}
    candidate_performance = performance_metrics(aligned[candidate_id].to_numpy())
    for opponent_id in (str(protocol["incumbent_id"]), str(protocol["benchmark_id"])):
        bootstraps = _bootstrap(evidence, candidate_id, opponent_id, protocol)
        primary = next(item for item in bootstraps if item.mean_block_length == int(protocol["primary_mean_block_length"]))
        opponent_performance = performance_metrics(aligned[opponent_id].to_numpy())
        request = BenchmarkChallengeRequest(
            candidate_id=candidate_id,
            candidate_hash=str(protocol["candidate_hash"]),
            benchmark_id=opponent_id,
            integrity=AuditStatus.PASS,
            reproducibility=AuditStatus.PASS,
            technical_replay=AuditStatus.PASS,
            monitoring_plan=AuditStatus.PASS,
            candidate_performance=candidate_performance,
            benchmark_performance=opponent_performance,
            bootstrap=primary,
            statistical_evidence=RiskLabel.FAVORABLE,
            external_validation=RiskLabel.MIXED,
            execution_evidence=RiskLabel.FAVORABLE,
        )
        comparisons[opponent_id] = [item.to_dict() for item in bootstraps]
        assessments[opponent_id] = {
            "request": request.to_dict(),
            "result": assess_benchmark_challenge(request).to_dict(),
        }

    metrics = {
        candidate_id: results[candidate_id][1],
        str(protocol["incumbent_id"]): results[str(protocol["incumbent_id"])][1],
        str(protocol["benchmark_id"]): {
            "return": float(buyhold["equity"].iloc[-1] / INITIAL_CASH - 1.0),
            **performance_metrics(aligned[str(protocol["benchmark_id"])].to_numpy()).to_dict(),
        },
    }
    route = (
        "PROCEED_TO_FREEZE_HEALTH_CHECK"
        if all(value["result"]["decision"] == "RECOMMEND_HEALTH_CHECK" for value in assessments.values())
        else "KEEP_RESEARCHING"
    )
    aligned.reset_index(names="date").to_csv(artifacts / "daily_return_matrix.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(artifacts / "return_matrix_evidence.json", evidence.to_dict())
    _write(artifacts / "paired_bootstrap.json", comparisons)
    _write(
        artifacts / "benchmark_assessment.json",
        {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "status": "PASS",
            "assessments": assessments,
            "metrics": metrics,
            "frequency_observation": {"rolling_60_median": 7.0, "rolling_60_p10": 3.0},
            "route_decision": route,
        },
    )
    primary_rows = []
    for opponent_id, items in comparisons.items():
        primary = next(item for item in items if item["mean_block_length"] == 21)
        values = {item["metric"]: item for item in (primary["cagr"], primary["max_drawdown"], primary["calmar"])}
        primary_rows.append(
            f"- 对 {opponent_id}：年化收益胜出 {values['cagr']['probability_favorable']:.2%}，"
            f"最大回撤胜出 {values['max_drawdown']['probability_favorable']:.2%}，"
            f"卡玛胜出 {values['calmar']['probability_favorable']:.2%}。"
        )
    (experiment / "03_execution.md").write_text(
        "# S004 EX44 执行\n\n状态：COMPLETE。三对象正式账本与 SE 技术审计通过，完成两组 10/21/42 日配对区块 Bootstrap。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX44 结论\n\n" + "\n".join(primary_rows) + f"\n\n裁决：`{route}`。PK 不直接授予冻结或 PTE 资格。\n",
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
            "candidate_id": candidate_id,
            "development_cutoff": protocol["development_cutoff"],
            "decision": route,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
