from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from strategy_evaluator import AuditStatus, audit_replay

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.intraday_overlay_replay import (
    build_moneyflow_breadth_signals,
    replay_intraday_overlay,
)
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260911_S003_EX56"


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    payload = json.loads((experiment / "candidate_payload.json").read_text(encoding="utf-8"))
    context = RepositoryContext.discover(repo)
    snapshot = resolve_candidate_snapshot(
        context,
        "S003-C001",
        payload,
        canonical_json_sha256(payload),
        str((experiment / "candidate_payload.json").relative_to(repo)),
    )
    data = load_replay_data(
        context,
        "research",
        "510500.SH",
        "etf",
        pd.Timestamp("2026-09-08").date(),
        include_five_minute=True,
    )
    signals = build_moneyflow_breadth_signals(
        snapshot,
        data,
        repo,
        pd.Timestamp("2021-04-15"),
        pd.Timestamp("2026-09-08"),
    )
    result = replay_intraday_overlay(signals, data, 100_000)
    metrics = calculate_metrics(result, 100_000)
    audit = audit_replay(build_replay_evidence(signals, data, result, 100_000, metrics))

    expected_events = pd.read_csv(
        repo / "experiments/S003/20260911_S003_EX44/artifacts/mechanism_events.csv"
    )
    expected_dates = pd.to_datetime(expected_events["event_date"]).dt.normalize().tolist()
    actual_dates = pd.to_datetime(signals.decisions["valid_session"]).dt.normalize().tolist()
    research = pd.read_csv(
        repo / "experiments/S003/20260911_S003_EX45/artifacts/episodes.csv.gz"
    )
    research = research.loc[research["variant"].eq("PRIMARY_LONG")].sort_values("event_date")
    formal_returns = result.trades.sort_values("entry_date")["net_return"].to_numpy(dtype=float)
    research_returns = research["baseline_return"].to_numpy(dtype=float)
    checks = {
        "candidate_identity_preserved": payload["candidate_hash"]
        == "1d6736f816d652c59d43dd22491c567e65100eaddfa3d20575531ca5a124f741",
        "event_dates_exact": actual_dates == expected_dates,
        "event_count_276": len(actual_dates) == 276,
        "episode_returns_exact": bool(
            formal_returns.shape == research_returns.shape
            and np.allclose(formal_returns, research_returns, rtol=0.0, atol=1e-12)
        ),
        "two_fills_per_event": len(result.fills) == 2 * len(actual_dates),
        "round_lots_only": bool(result.orders["quantity"].mod(100).eq(0).all()),
        "end_of_day_core_quantity_constant": bool(
            result.account_daily["quantity"].eq(result.account_daily["quantity"].iloc[0]).all()
        ),
        "no_negative_cash": bool(result.account_daily["cash"].ge(-1e-8).all()),
        "all_cycles_closed": bool(result.trades["status"].eq("CLOSED").all()),
        "se_independent_audit_pass": audit.status is AuditStatus.PASS,
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    result.decisions.to_csv(artifacts / "decisions.csv", index=False, lineterminator="\n")
    result.orders.to_csv(artifacts / "orders.csv", index=False, lineterminator="\n")
    result.fills.to_csv(artifacts / "fills.csv", index=False, lineterminator="\n")
    result.account_daily.to_csv(artifacts / "account_daily.csv", index=False, lineterminator="\n")
    result.trades.to_csv(artifacts / "trades.csv", index=False, lineterminator="\n")
    _write_json(artifacts / "audit.json", audit.to_dict())
    _write_json(artifacts / "formal_replay_summary.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": status,
        "checks": checks,
        "metrics": metrics,
        "dataset_fingerprint": data.fingerprint,
        "candidate_payload_sha256": canonical_json_sha256(payload),
    })
    (experiment / "03_execution.md").write_text(
        "# S003 EX56 执行\n\n"
        f"正式回放：`{status}`。事件{len(actual_dates)}笔，订单{len(result.orders)}笔，"
        f"闭合交易{metrics['closed_trades']}笔，收益率{metrics['return']:.2%}，"
        f"最大回撤{metrics['max_drawdown']:.2%}，卡玛{metrics['calmar']:.2f}。\n",
        encoding="utf-8",
    )
    conclusion = (
        "TDR已能无损复现冻结信号和逐笔执行收益，正式账户账本满足T+1库存轮换，且SE独立审计通过。"
        if status == "PASS"
        else "正式执行与冻结研究证据不一致，停止后续兼容建设。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX56 结论\n\n"
        f"结论：`{status}`。{conclusion}研究候选已通过TDR正式发布链的端到端测试；本轮仍未完成PTE计划退出，"
        "不得冻结或进入模拟盘。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "strategy_id": "S003",
        "candidate_id": "S003-C001",
        "symbol": "510500.SH",
        "development_cutoff": "2026-09-08",
        "status": status,
        "mutates_strategy_manager": False,
        "mutates_pte": False,
        "freeze_allowed": False,
    })


if __name__ == "__main__":
    main()
