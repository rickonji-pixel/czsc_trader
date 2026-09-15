from __future__ import annotations

from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from strategy_evaluator import performance_metrics

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.causal_feature_gate_replay import build_causal_feature_gate_signals
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260916_S007_EX36"


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


def _profit_factor(returns: pd.Series) -> float | None:
    positive = float(returns.clip(lower=0.0).sum())
    negative = float(-returns.clip(upper=0.0).sum())
    return None if negative == 0.0 else positive / negative


def _metrics(returns: pd.Series) -> dict[str, float]:
    result = performance_metrics(returns.to_numpy(dtype=float))
    return {
        "total_return": float(np.prod(1.0 + returns.to_numpy(dtype=float)) - 1.0),
        "net_cagr": result.cagr,
        "max_drawdown": result.max_drawdown,
        "calmar": result.calmar,
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("EX36 protocol identity differs")
    if any(
        protocol.get(key)
        for key in (
            "creates_strategy_version", "strategy_frozen",
            "mutates_strategy_manager", "mutates_pte",
        )
    ):
        raise ValueError("EX36 cannot freeze or mutate SM/PTE")
    for relative, expected in protocol["sources"].items():
        if _sha256(repo / relative) != expected:
            raise ValueError(f"frozen source differs: {relative}")

    source_experiment = repo / "experiments/S007/20260915_S007_EX31"
    source = source_experiment / "artifacts"
    trades = pd.read_csv(source / "trades.csv")
    account = pd.read_csv(source / "account_daily.csv")
    trades = trades.loc[trades["status"].eq("CLOSED")].copy()
    if len(trades) != 139:
        raise ValueError(f"expected 139 closed trades, received {len(trades)}")
    if int(account.iloc[-1]["quantity"]) != 0:
        raise ValueError("formal account is not flat at the development cutoff")

    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="raise").dt.normalize()
    trades["exit_date"] = pd.to_datetime(trades["exit_date"], errors="raise").dt.normalize()
    trades["net_return"] = pd.to_numeric(trades["net_return"], errors="raise")
    ranked = trades.sort_values(["net_return", "entry_date"], ascending=[False, True]).reset_index(drop=True)
    positive_sum = float(trades["net_return"].clip(lower=0.0).sum())
    if positive_sum <= 0.0:
        raise ValueError("positive trade return sum must be positive")

    account["date"] = pd.to_datetime(account["date"], errors="raise").dt.normalize()
    candidate_path = source_experiment / "candidate_payload.json"
    candidate = _read(candidate_path)
    candidate_hash = canonical_json_sha256(candidate)
    if candidate_hash != protocol["candidate_hash"]:
        raise ValueError("candidate hash differs from concentration protocol")
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
        pd.Timestamp(account["date"].min()),
        pd.Timestamp(protocol["development_cutoff"]),
        repo,
    )
    original_replay = replay_account(signals, replay_data, float(protocol["initial_cash"]))
    expected_equity = account["equity"].to_numpy(dtype=float)
    replayed_equity = original_replay.account_daily["equity"].to_numpy(dtype=float)
    if not np.allclose(replayed_equity, expected_equity, rtol=0.0, atol=1e-8):
        raise ValueError("current TDR replay differs from frozen EX31 account")

    equity = account.set_index("date")["equity"].astype(float)
    daily_returns = equity.pct_change()
    daily_returns.iloc[0] = equity.iloc[0] / float(protocol["initial_cash"]) - 1.0
    baseline = _metrics(daily_returns)
    baseline["profit_factor"] = float(_profit_factor(trades["net_return"]))
    baseline["closed_trades"] = int(len(trades))

    rows: list[dict[str, object]] = []
    counterfactuals: list[dict[str, object]] = []
    for count in map(int, protocol["top_trade_counts"]):
        removed = ranked.head(count)
        concentration = float(removed["net_return"].sum() / positive_sum)
        decisions = signals.decisions.copy()
        valid_sessions = pd.to_datetime(decisions["valid_session"], errors="coerce").dt.normalize()
        for trade in removed.itertuples(index=False):
            mask = (
                (valid_sessions >= trade.entry_date)
                & (valid_sessions <= trade.exit_date)
            )
            decisions.loc[mask, "target_position"] = 0
        counterfactual = replay_account(
            replace(signals, decisions=decisions),
            replay_data,
            float(protocol["initial_cash"]),
        )
        counterfactual_equity = counterfactual.account_daily["equity"].astype(float)
        counterfactual_returns = counterfactual_equity.pct_change()
        counterfactual_returns.iloc[0] = (
            counterfactual_equity.iloc[0] / float(protocol["initial_cash"]) - 1.0
        )
        remaining = counterfactual.trades.loc[counterfactual.trades["status"].eq("CLOSED")]
        metrics = _metrics(counterfactual_returns)
        metrics["profit_factor"] = _profit_factor(remaining["net_return"])
        metrics["closed_trades"] = int(len(remaining))
        counterfactuals.append({
            "removed_top_profitable_trades": count,
            "share_of_sum_positive_trade_returns": concentration,
            **metrics,
        })
        for rank, trade in enumerate(removed.itertuples(index=False), start=1):
            rows.append({
                "top_count": count,
                "rank": rank,
                "cycle_id": trade.cycle_id,
                "entry_date": trade.entry_date.date().isoformat(),
                "exit_date": trade.exit_date.date().isoformat(),
                "net_return": float(trade.net_return),
            })

    pd.DataFrame(rows).to_csv(
        artifacts / "top_profitable_trades.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": protocol["candidate_id"],
        "role": protocol["decision_role"],
        "baseline": baseline,
        "counterfactuals": counterfactuals,
    }
    _write(artifacts / "concentration_diagnostic.json", result)
    largest = counterfactuals[0]
    top_three = counterfactuals[1]
    top_five = counterfactuals[2]
    (experiment / "03_execution.md").write_text(
        "# S007 EX36 执行\n\n"
        "状态：`COMPLETE`。已核对139笔闭合交易及开发截止日空仓状态，并完成最大1/3/5笔"
        "正收益集中度和反事实删除计算。未生成新信号或参数。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX36 结论\n\n"
        f"最大1笔、前3笔、前5笔分别占全部正收益之和的"
        f"{largest['share_of_sum_positive_trade_returns']:.2%}、"
        f"{top_three['share_of_sum_positive_trade_returns']:.2%}、"
        f"{top_five['share_of_sum_positive_trade_returns']:.2%}。\n\n"
        f"删除最大1笔后：年化{largest['net_cagr']:.2%}、最大回撤"
        f"{largest['max_drawdown']:.2%}、卡玛{largest['calmar']:.3f}；"
        f"删除前3笔后：年化{top_three['net_cagr']:.2%}、最大回撤"
        f"{top_three['max_drawdown']:.2%}、卡玛{top_three['calmar']:.3f}；"
        f"删除前5笔后：年化{top_five['net_cagr']:.2%}、最大回撤"
        f"{top_five['max_drawdown']:.2%}、卡玛{top_five['calmar']:.3f}。\n\n"
        "本轮是事后风险诊断，不新增机器门槛，不直接改变EX35的冻结评审资格。\n",
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
            "candidate_hash": protocol["candidate_hash"],
            "decision_role": protocol["decision_role"],
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
