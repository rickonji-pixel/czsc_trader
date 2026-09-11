from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from strategy_evaluator import AuditStatus, audit_replay

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260910_S002_EX14"


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    candidate_path = repo / "experiments/20260910_S002_EX12/candidate_payload.json"
    candidate = _read(candidate_path)
    if canonical_json_sha256(candidate) != protocol["candidate_hash"]:
        raise ValueError("S002-C001 candidate hash differs from frozen protocol")
    proposed = _read(artifacts / "proposed_strategy_payload.json")

    context = RepositoryContext.discover(repo)
    replay_data = load_replay_data(
        context,
        "research",
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    original = resolve_candidate_snapshot(
        context,
        "S002-C001",
        candidate,
        str(protocol["candidate_hash"]),
        str(candidate_path.relative_to(repo)).replace("\\", "/"),
    )
    proposed_hash = canonical_json_sha256(proposed)
    deployed = resolve_candidate_snapshot(
        context,
        "S002-C001-EXECUTION",
        proposed,
        proposed_hash,
        "experiments/20260910_S002_EX14/artifacts/proposed_strategy_payload.json",
    )
    start = date.fromisoformat(str(protocol["evaluation_start"]))
    end = date.fromisoformat(str(protocol["evaluation_end"]))
    original_signals = replay_signals(original, replay_data, start, end)
    deployed_signals = replay_signals(deployed, replay_data, start, end)
    target_match = original_signals.decisions["target_position"].equals(
        deployed_signals.decisions["target_position"]
    )
    if protocol["require_identical_target_positions"] and not target_match:
        raise AssertionError("proposed runtime target positions differ from S002-C001")

    result = replay_account(deployed_signals, replay_data, 100_000.0)
    metrics = calculate_metrics(result, 100_000.0)
    evidence = build_replay_evidence(
        deployed_signals, replay_data, result, 100_000.0, metrics
    )
    audit = audit_replay(evidence)
    if audit.status is not AuditStatus.PASS:
        raise AssertionError(f"SE replay audit failed: {audit.reason_codes}")

    entries = result.orders.loc[result.orders["side"].eq("BUY")]
    exits = result.orders.loc[result.orders["side"].eq("SELL")]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": "S002-C001",
        "candidate_hash": protocol["candidate_hash"],
        "proposed_strategy_payload_hash": proposed_hash,
        "execution_policy_hash": canonical_json_sha256(proposed["rule"]["execution"]),
        "target_positions_identical": target_match,
        "entry_signals": int(
            deployed_signals.decisions["target_position"].diff().eq(1).sum()
        ),
        "exit_signals": int(
            deployed_signals.decisions["target_position"].diff().eq(-1).sum()
        ),
        "buy_orders": len(entries),
        "buy_orders_filled": int(entries["status"].eq("FILLED").sum()),
        "buy_orders_unfilled": int(entries["status"].eq("UNFILLED").sum()),
        "sell_orders": len(exits),
        "sell_orders_filled": int(exits["status"].eq("FILLED").sum()),
        "formal_execution_metrics": metrics,
        "research_next_open_reference": {
            "closed_trades": 25,
            "maximum_drawdown": -0.0363661389838702,
            "calmar_ratio": 1.5177090960302675,
            "total_return": 0.3414832610103573,
            "source": "experiments/20260910_S002_EX12/artifacts/candidate_readiness.json",
        },
        "se_replay_audit": audit.to_dict(),
        "candidate_changed": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    }
    _write(artifacts / "execution_compatibility.json", summary)
    _write(
        artifacts / "version_input.json",
        {
            "schema_version": 1,
            "strategy_id": "S002",
            "version": "v1",
            "release_id": "S002-v1",
            "parent_version": None,
            "change_summary": "冻结三连跌五日修复候选及项目唯一正式执行规则",
            "source_experiment": "experiments/20260910_S002_EX14",
            "source_candidate": "S002-C001",
            "selection_data_cutoff": protocol["development_cutoff"],
            "forward_start": "2026-09-09",
            "strategy_payload": proposed,
            "release_hash": None,
        },
    )
    _write(
        artifacts / "freeze_evidence_input.json",
        {
            "schema_version": 1,
            "evidence_id": "EVD-S002-V1-RESEARCH",
            "strategy_id": "S002",
            "version": "v1",
            "release_hash": "0" * 64,
            "phase": "RESEARCH_BACKTEST",
            "period_start": protocol["evaluation_start"],
            "period_end": protocol["evaluation_end"],
            "data_identity": {
                "dataset": protocol["dataset"],
                "symbol": protocol["symbol"],
                "cutoff": protocol["development_cutoff"],
                "fingerprint": replay_data.fingerprint,
            },
            "initial_capital": 100000.0,
            "fee_rate": 0.0005,
            "maximum_drawdown": metrics["max_drawdown"],
            "calmar_ratio": metrics["calmar"],
            "win_loss_ratio": metrics["win_loss_ratio"],
            "win_loss_ratio_status": metrics["win_loss_ratio_status"],
            "total_return": metrics["return"],
            "sharpe_ratio": metrics["sharpe"],
            "closed_trades": metrics["closed_trades"],
            "source_path": (
                "experiments/20260910_S002_EX14/artifacts/execution_compatibility.json"
            ),
            "source_hash": canonical_json_sha256(summary),
            "recorded_at": "2026-09-10T12:00:00+08:00",
            "recorded_by": "codex",
        },
    )
    result.decisions.to_csv(artifacts / "decisions.csv", index=False, encoding="utf-8-sig")
    result.orders.to_csv(artifacts / "orders.csv", index=False, encoding="utf-8-sig")
    result.fills.to_csv(artifacts / "fills.csv", index=False, encoding="utf-8-sig")
    result.trades.to_csv(artifacts / "trades.csv", index=False, encoding="utf-8-sig")
    result.account_daily.to_csv(
        artifacts / "account_daily.csv", index=False, encoding="utf-8-sig"
    )

    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。\n\n"
        "共享运行时与S002-C001逐日目标仓位一致；已按项目正式执行规则完成账户回放，"
        "并通过SE replay audit。未修改候选、未冻结策略、未写入PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n状态：COMPLETE。\n\n"
        f"共{summary['entry_signals']}次买入信号；正式规则生成{summary['buy_orders']}张买单，"
        f"成交{summary['buy_orders_filled']}张、未成交{summary['buy_orders_unfilled']}张。"
        f"正式口径累计收益{float(metrics['return']):.2%}，最大回撤"
        f"{float(metrics['max_drawdown']):.2%}，卡玛比率"
        f"{float(metrics['calmar']):.3f}。\n\n"
        "该结果是S002冻结前的正式执行证据；研究口径成绩不再直接充当部署绩效。\n",
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
            "candidate_id": "S002-C001",
            "development_cutoff": protocol["development_cutoff"],
            "target_positions_identical": target_match,
            "se_replay_audit": audit.status.value,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
