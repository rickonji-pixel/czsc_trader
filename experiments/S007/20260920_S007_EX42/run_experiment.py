from __future__ import annotations

from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any
from collections.abc import Mapping

import numpy as np
import pandas as pd
from strategy_evaluator import AuditStatus, audit_replay
from strategy_runtime import effective_target_order_type

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.signal_replay import SignalReplay
from czsc_trader.backtesting.srt_bridge import load_srt_strategy, replay_srt_account
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260920_S007_EX42"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_hash(path: Path, expected: str) -> None:
    observed = _sha256(path)
    if observed != expected:
        raise ValueError(f"source hash differs: {path}: {observed} != {expected}")


def _plain_json(value):
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _load_frozen_baseline_signals(
    snapshot,
    strategy,
    corrected_backtest: Path,
) -> SignalReplay:
    manifest = _read_json(corrected_backtest / "manifest.json")
    decisions = pd.read_csv(
        corrected_backtest / "decisions.csv",
        parse_dates=["signal_date", "valid_session"],
    )
    execution = strategy.definition.execution
    signal_support = manifest["signal_support"]
    support = {
        "mode": "srt_input_contract",
        "release_id": strategy.definition.release_id,
        "runtime_sha256": strategy.definition.runtime_sha256,
        "execution_policy": {
            "policy_type": execution.policy_type,
            "settings": _plain_json(execution.settings),
        },
        "entry_order_type": effective_target_order_type(execution.settings, "BUY"),
        "exit_order_type": effective_target_order_type(execution.settings, "SELL"),
        "requested_cutoff": signal_support["requested_cutoff"],
        "publication_sha256": signal_support["publication_sha256"],
        "frozen_decision_source": "outputs/588080_0918_BT04/decisions.csv",
    }
    return SignalReplay(
        snapshot=snapshot,
        decisions=decisions,
        calculation_start=pd.Timestamp(manifest["ranges"]["calculation"][0]),
        calculation_end=pd.Timestamp(manifest["ranges"]["calculation"][1]),
        evaluation_start=pd.Timestamp(manifest["ranges"]["evaluation"][0]),
        evaluation_end=pd.Timestamp(manifest["ranges"]["evaluation"][1]),
        support_data=support,
        chart_data=None,
    )


def _behavior_hash(signals: SignalReplay, sessions: pd.DatetimeIndex) -> str:
    target = pd.Series(0, index=sessions, dtype=np.int8)
    valid = signals.decisions.dropna(subset=["valid_session"])
    execution_dates = pd.DatetimeIndex(pd.to_datetime(valid["valid_session"]).dt.normalize())
    target.loc[execution_dates] = valid["target_position"].to_numpy(dtype=np.int8)
    return hashlib.sha256(target.to_numpy(dtype=np.int8).tobytes()).hexdigest()


def _decision_id(signal_date: pd.Timestamp, target: int) -> str:
    digest = hashlib.sha256(
        f"{EXPERIMENT_ID}|{signal_date.date().isoformat()}|{target}".encode()
    ).hexdigest()[:20].upper()
    return f"DEC-{digest}"


def _build_overlay_signals(
    baseline: SignalReplay,
    baseline_result,
    fee_rate: float,
) -> tuple[SignalReplay, pd.DataFrame]:
    decisions = baseline.decisions.sort_values("valid_session").reset_index(drop=True).copy()
    decisions["signal_date"] = pd.to_datetime(decisions["signal_date"]).dt.normalize()
    decisions["valid_session"] = pd.to_datetime(decisions["valid_session"]).dt.normalize()
    base_target = decisions["target_position"].astype(np.int8)
    entry_mask = base_target.eq(1) & base_target.shift(1, fill_value=0).eq(0)
    exit_mask = base_target.eq(0) & base_target.shift(1, fill_value=0).eq(1)

    closed = baseline_result.trades.loc[baseline_result.trades["status"].eq("CLOSED")].copy()
    closed["entry_date"] = pd.to_datetime(closed["entry_date"]).dt.normalize()
    closed["exit_date"] = pd.to_datetime(closed["exit_date"]).dt.normalize()
    if closed["entry_date"].duplicated().any():
        raise ValueError("baseline entry dates must be unique")
    trades_by_entry = closed.set_index("entry_date")
    close = (
        baseline_result.account_daily.assign(
            date=pd.to_datetime(baseline_result.account_daily["date"]).dt.normalize()
        )
        .drop_duplicates("date", keep="last")
        .set_index("date")["close"]
        .astype(float)
        .sort_index()
    )

    overlay_target = base_target.copy()
    actions = pd.Series("BASE_V1_TARGET", index=decisions.index, dtype="string")
    trigger_rows: list[dict[str, object]] = []
    entries = list(decisions.index[entry_mask])
    exits = list(decisions.index[exit_mask])
    if len(entries) != len(exits):
        raise ValueError("baseline target cycles are not closed")

    for entry_index, exit_index in zip(entries, exits, strict=True):
        if exit_index <= entry_index:
            raise ValueError("baseline cycle exit precedes entry")
        entry_date = pd.Timestamp(decisions.at[entry_index, "valid_session"])
        if entry_date not in trades_by_entry.index:
            raise ValueError(f"baseline trade missing for entry session: {entry_date.date()}")
        trade = trades_by_entry.loc[entry_date]
        checkpoint_index = entry_index + 1
        if checkpoint_index > exit_index:
            raise ValueError("baseline cycle has no entry-session close decision")
        base_continues = bool(base_target.iloc[checkpoint_index] == 1)
        entry_price = float(trade["entry_price"])
        checkpoint_close = float(close.loc[entry_date])
        mark_to_market_net = (
            (checkpoint_close * (1.0 - fee_rate))
            / (entry_price * (1.0 + fee_rate))
            - 1.0
        )
        triggered = base_continues and mark_to_market_net <= 0.0
        if triggered:
            overlay_target.iloc[checkpoint_index:exit_index] = 0
            actions.iloc[checkpoint_index] = "EX42_PRICE_RISK_EXIT"
            if checkpoint_index + 1 < exit_index:
                actions.iloc[checkpoint_index + 1 : exit_index] = "EX42_BLOCKED_UNTIL_BASE_FLAT"
        trigger_rows.append(
            {
                "baseline_cycle_id": str(trade["cycle_id"]),
                "entry_date": entry_date,
                "baseline_exit_date": pd.Timestamp(trade["exit_date"]),
                "entry_price": entry_price,
                "checkpoint_close": checkpoint_close,
                "cost_adjusted_mark_to_market_net": mark_to_market_net,
                "base_continues_holding": base_continues,
                "overlay_triggered": triggered,
                "overlay_exit_date": (
                    pd.Timestamp(decisions.at[checkpoint_index, "valid_session"])
                    if triggered
                    else pd.Timestamp(trade["exit_date"])
                ),
            }
        )

    decisions["target_position"] = overlay_target.astype(int)
    decisions["action"] = actions
    decisions["decision_id"] = [
        _decision_id(pd.Timestamp(row.signal_date), int(row.target_position))
        for row in decisions.itertuples(index=False)
    ]
    if decisions["decision_id"].duplicated().any():
        raise ValueError("overlay decision IDs must be unique")

    support = dict(baseline.support_data or {})
    support["research_counterfactual"] = {
        "experiment_id": EXPERIMENT_ID,
        "identity": "EXPERIMENT_LOCAL_PRECOMPUTED_TARGET_OVERLAY",
        "base_release": baseline.snapshot.identity.reference,
        "checkpoint": "ENTRY_SESSION_CLOSE",
        "trigger": "COST_ADJUSTED_MARK_TO_MARKET_NET_LE_ZERO",
        "reentry": "BLOCK_UNTIL_BASE_CYCLE_FLAT",
    }
    chart_data = None if baseline.chart_data is None else baseline.chart_data.copy()
    if chart_data is not None and "target_position" in chart_data:
        overlay_by_signal = decisions.set_index("signal_date")["target_position"]
        chart_dates = pd.to_datetime(chart_data["date"]).dt.normalize()
        chart_data["target_position"] = chart_dates.map(overlay_by_signal).fillna(
            chart_data["target_position"]
        )
    return (
        replace(
            baseline,
            decisions=decisions,
            support_data=support,
            chart_data=chart_data,
        ),
        pd.DataFrame(trigger_rows),
    )


def _holding_sessions(trades: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.Series:
    positions = pd.Series(np.arange(len(sessions)), index=sessions)
    entry = pd.to_datetime(trades["entry_date"]).dt.normalize().map(positions)
    exit_ = pd.to_datetime(trades["exit_date"]).dt.normalize().map(positions)
    if entry.isna().any() or exit_.isna().any():
        raise ValueError("trade date is outside evaluation sessions")
    return (exit_ - entry).astype(int)


def _diagnostics(result, sessions: pd.DatetimeIndex, window: int) -> dict[str, object]:
    closed = result.trades.loc[result.trades["status"].eq("CLOSED")].copy()
    closed["entry_date"] = pd.to_datetime(closed["entry_date"]).dt.normalize()
    closed["exit_date"] = pd.to_datetime(closed["exit_date"]).dt.normalize()
    holding = _holding_sessions(closed, sessions)
    exits = pd.Series(0, index=sessions, dtype=int)
    exit_counts = closed["exit_date"].value_counts()
    exits.loc[exit_counts.index] = exit_counts.to_numpy(dtype=int)
    rolling = exits.rolling(window, min_periods=window).sum().dropna()
    yearly = closed.groupby(closed["exit_date"].dt.year).size()
    evaluation_years = sorted(set(sessions.year))
    yearly_counts = {str(year): int(yearly.get(year, 0)) for year in evaluation_years}
    fills = result.fills.copy()
    turnover = float((fills["quantity"].astype(float) * fills["price"].astype(float)).sum())
    fee_total = float(fills["fees"].astype(float).sum())
    exposure = result.account_daily["quantity"].astype(float).gt(0)
    returns = closed["net_return"].astype(float)
    return {
        "closed_trades": int(len(closed)),
        "yearly_closed_trades": yearly_counts,
        "zero_trade_years": int(sum(count == 0 for count in yearly_counts.values())),
        "rolling_window_sessions": int(window),
        "rolling_closed_trades_median": float(rolling.median()) if len(rolling) else 0.0,
        "rolling_closed_trades_p10": float(rolling.quantile(0.10)) if len(rolling) else 0.0,
        "rolling_zero_trade_windows": int(rolling.eq(0).sum()),
        "rolling_zero_trade_window_rate": float(rolling.eq(0).mean()) if len(rolling) else 1.0,
        "exposure_ratio": float(exposure.mean()),
        "one_session_trade_count": int(holding.eq(1).sum()),
        "one_session_trade_rate": float(holding.eq(1).mean()) if len(holding) else None,
        "median_holding_sessions": float(holding.median()) if len(holding) else None,
        "mean_holding_sessions": float(holding.mean()) if len(holding) else None,
        "orders": int(len(result.orders)),
        "fills": int(len(fills)),
        "turnover": turnover,
        "fee_total": fee_total,
        "win_rate": float(returns.gt(0).mean()) if len(returns) else None,
        "mean_trade_return": float(returns.mean()) if len(returns) else None,
        "median_trade_return": float(returns.median()) if len(returns) else None,
    }


def _assert_baseline_matches_formal(result, formal_trades_path: Path) -> None:
    formal = pd.read_csv(formal_trades_path)
    current = result.trades.reset_index(drop=True)
    columns = [
        "status",
        "entry_date",
        "exit_date",
        "quantity",
        "entry_price",
        "exit_price",
        "net_return",
    ]
    if len(formal) != len(current):
        raise ValueError("current SRT baseline trade count differs from formal replay")
    for column in ("entry_date", "exit_date"):
        formal[column] = pd.to_datetime(formal[column]).dt.normalize()
        current[column] = pd.to_datetime(current[column]).dt.normalize()
    if not formal[columns[:-3]].equals(current[columns[:-3]]):
        raise ValueError("current SRT baseline trade identity differs from formal replay")
    for column in ("entry_price", "exit_price", "net_return"):
        if not np.allclose(
            formal[column].to_numpy(dtype=float),
            current[column].to_numpy(dtype=float),
            rtol=0.0,
            atol=1e-12,
        ):
            raise ValueError(f"current SRT baseline {column} differs from formal replay")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    protocol = _read_json(experiment / "02_protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("EX42 protocol identity differs")
    forbidden = (
        "reads_forward_data",
        "searches_parameters",
        "creates_candidate",
        "changes_strategy_parameters",
        "mutates_pte",
    )
    if any(protocol.get(field) for field in forbidden):
        raise ValueError("EX42 must remain a fixed research counterfactual")

    sources = protocol["sources"]
    formal_archive = repo / str(sources["formal_replay_archive"])
    overlap_archive = repo / str(sources["action_overlap_archive"])
    execution_contract_archive = repo / str(sources["execution_contract_archive"])
    corrected_backtest = repo / str(sources["corrected_backtest"])
    validate_experiment_archive(formal_archive)
    validate_experiment_archive(overlap_archive)
    validate_experiment_archive(execution_contract_archive)
    _require_hash(
        formal_archive / "experiment_manifest.json",
        str(sources["formal_replay_manifest_sha256"]),
    )
    _require_hash(
        formal_archive / "artifacts/trades.csv", str(sources["formal_trades_sha256"])
    )
    _require_hash(
        formal_archive / "artifacts/decisions.csv", str(sources["formal_decisions_sha256"])
    )
    _require_hash(
        overlap_archive / "experiment_manifest.json",
        str(sources["action_overlap_manifest_sha256"]),
    )
    _require_hash(
        overlap_archive / "artifacts/trade_action_overlap.csv",
        str(sources["action_overlap_sha256"]),
    )
    _require_hash(
        execution_contract_archive / "experiment_manifest.json",
        str(sources["execution_contract_manifest_sha256"]),
    )
    _require_hash(
        corrected_backtest / "decisions.csv", str(sources["corrected_decisions_sha256"])
    )
    _require_hash(
        corrected_backtest / "manifest.json", str(sources["corrected_manifest_sha256"])
    )
    _require_hash(
        repo / str(sources["strategy_version"]), str(sources["strategy_version_sha256"])
    )

    context = RepositoryContext.discover(repo)
    snapshot = resolve_registered_strategy(context, "S007", "v1")
    replay_data = load_replay_data(
        context,
        "research",
        str(protocol["symbol"]),
        "etf",
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    _, strategy = load_srt_strategy(
        repo, snapshot.identity.reference, deployment_symbol=str(protocol["symbol"])
    )
    baseline_signals = _load_frozen_baseline_signals(
        snapshot, strategy, corrected_backtest
    )
    initial_cash = float(protocol["initial_cash"])
    baseline_result = replay_srt_account(
        strategy=strategy,
        signals=baseline_signals,
        replay_data=replay_data,
        initial_cash=initial_cash,
    )
    baseline_metrics = calculate_metrics(baseline_result, initial_cash)
    baseline_audit = audit_replay(
        build_replay_evidence(
            baseline_signals, replay_data, baseline_result, initial_cash, baseline_metrics
        )
    )
    if baseline_audit.status is not AuditStatus.PASS:
        raise ValueError(f"baseline SE audit failed: {baseline_audit.reason_codes}")
    _assert_baseline_matches_formal(
        baseline_result, formal_archive / "artifacts/trades.csv"
    )

    sessions = pd.DatetimeIndex(
        pd.to_datetime(
            replay_data.adjusted.daily.loc[
                replay_data.adjusted.daily["dt"].between(
                    baseline_signals.evaluation_start, baseline_signals.evaluation_end
                ),
                "dt",
            ]
        ).dt.normalize(),
        name="date",
    )
    behavior_sessions = pd.DatetimeIndex(
        pd.to_datetime(
            replay_data.adjusted.daily.loc[
                replay_data.adjusted.daily["dt"].between(
                    pd.Timestamp("2021-01-04"), baseline_signals.evaluation_end
                ),
                "dt",
            ]
        ).dt.normalize(),
        name="date",
    )
    baseline_behavior_hash = _behavior_hash(baseline_signals, behavior_sessions)
    if baseline_behavior_hash != str(sources["expected_behavior_hash"]):
        raise ValueError("current SRT baseline behavior differs from frozen selection")

    overlay_signals, trigger_frame = _build_overlay_signals(
        baseline_signals,
        baseline_result,
        float(protocol["fee_rate_per_side"]),
    )
    expected_incremental = pd.read_csv(
        overlap_archive / "artifacts/trade_action_overlap.csv",
        parse_dates=["entry_date"],
    ).query("overlap_group == 'PRICE_RISK_ONLY'")
    triggered = trigger_frame.loc[trigger_frame["overlay_triggered"]]
    if len(triggered) != len(expected_incremental):
        expected_dates = set(pd.to_datetime(expected_incremental["entry_date"]).dt.normalize())
        triggered_dates = set(pd.to_datetime(triggered["entry_date"]).dt.normalize())
        missing = sorted(item.date().isoformat() for item in expected_dates - triggered_dates)
        extra = sorted(item.date().isoformat() for item in triggered_dates - expected_dates)
        raise ValueError(
            "overlay trigger count differs from EX41: "
            f"{len(triggered)} != {len(expected_incremental)}; missing={missing}; extra={extra}"
        )
    expected_dates = set(pd.to_datetime(expected_incremental["entry_date"]).dt.normalize())
    triggered_dates = set(pd.to_datetime(triggered["entry_date"]).dt.normalize())
    if triggered_dates != expected_dates:
        raise ValueError("overlay triggered entry dates differ from EX41 fixed rule")

    overlay_result = replay_srt_account(
        strategy=strategy,
        signals=overlay_signals,
        replay_data=replay_data,
        initial_cash=initial_cash,
    )
    overlay_metrics = calculate_metrics(overlay_result, initial_cash)
    overlay_audit = audit_replay(
        build_replay_evidence(
            overlay_signals, replay_data, overlay_result, initial_cash, overlay_metrics
        )
    )
    if overlay_audit.status is not AuditStatus.PASS:
        raise ValueError(f"overlay SE audit failed: {overlay_audit.reason_codes}")

    baseline_entries = pd.to_datetime(
        baseline_result.trades.loc[
            baseline_result.trades["status"].eq("CLOSED"), "entry_date"
        ]
    ).dt.normalize()
    overlay_entries = pd.to_datetime(
        overlay_result.trades.loc[
            overlay_result.trades["status"].eq("CLOSED"), "entry_date"
        ]
    ).dt.normalize()
    if list(baseline_entries) != list(overlay_entries):
        raise ValueError("overlay changed the frozen base-cycle entry schedule")

    window = int(protocol["rolling_frequency_sessions"])
    baseline_diagnostics = _diagnostics(baseline_result, sessions, window)
    overlay_diagnostics = _diagnostics(overlay_result, sessions, window)
    baseline_closed = baseline_result.trades.loc[
        baseline_result.trades["status"].eq("CLOSED")
    ].copy()
    overlay_closed = overlay_result.trades.loc[
        overlay_result.trades["status"].eq("CLOSED")
    ].copy()
    comparison = pd.DataFrame(
        {
            "entry_date": baseline_entries,
            "baseline_cycle_id": baseline_closed["cycle_id"].to_numpy(),
            "overlay_cycle_id": overlay_closed["cycle_id"].to_numpy(),
            "baseline_exit_date": pd.to_datetime(baseline_closed["exit_date"]).dt.normalize(),
            "overlay_exit_date": pd.to_datetime(overlay_closed["exit_date"]).dt.normalize(),
            "baseline_net_return": baseline_closed["net_return"].astype(float).to_numpy(),
            "overlay_net_return": overlay_closed["net_return"].astype(float).to_numpy(),
        }
    )
    comparison["return_delta"] = (
        comparison["overlay_net_return"] - comparison["baseline_net_return"]
    )
    trigger_lookup = trigger_frame.set_index("entry_date")["overlay_triggered"]
    comparison["overlay_triggered"] = comparison["entry_date"].map(trigger_lookup).astype(bool)
    triggered_comparison = comparison.loc[comparison["overlay_triggered"]].copy()
    top_harm = triggered_comparison.nsmallest(5, "return_delta")
    triggered_mechanism = {
        "baseline_mean_return": float(triggered_comparison["baseline_net_return"].mean()),
        "overlay_mean_return": float(triggered_comparison["overlay_net_return"].mean()),
        "baseline_win_rate": float(triggered_comparison["baseline_net_return"].gt(0).mean()),
        "overlay_win_rate": float(triggered_comparison["overlay_net_return"].gt(0).mean()),
        "baseline_winner_count": int(triggered_comparison["baseline_net_return"].gt(0).sum()),
        "baseline_winners_harmed_count": int(
            (
                triggered_comparison["baseline_net_return"].gt(0)
                & triggered_comparison["return_delta"].lt(0)
            ).sum()
        ),
        "baseline_nonwinners_improved_count": int(
            (
                triggered_comparison["baseline_net_return"].le(0)
                & triggered_comparison["return_delta"].gt(0)
            ).sum()
        ),
        "positive_return_delta_sum": float(
            triggered_comparison.loc[
                triggered_comparison["return_delta"].gt(0), "return_delta"
            ].sum()
        ),
        "negative_return_delta_sum": float(
            triggered_comparison.loc[
                triggered_comparison["return_delta"].lt(0), "return_delta"
            ].sum()
        ),
        "largest_harm": [
            {
                "entry_date": pd.Timestamp(row.entry_date).date().isoformat(),
                "baseline_net_return": float(row.baseline_net_return),
                "overlay_net_return": float(row.overlay_net_return),
                "return_delta": float(row.return_delta),
            }
            for row in top_harm.itertuples(index=False)
        ],
    }

    metric_delta = {
        key: (
            None
            if baseline_metrics[key] is None or overlay_metrics[key] is None
            else float(overlay_metrics[key]) - float(baseline_metrics[key])
        )
        for key in ("return", "max_drawdown", "calmar", "sharpe", "win_loss_ratio")
    }
    degeneration = {
        "closed_trade_count_preserved": (
            overlay_diagnostics["closed_trades"] == baseline_diagnostics["closed_trades"]
        ),
        "entry_schedule_preserved": True,
        "zero_trade_years": overlay_diagnostics["zero_trade_years"],
        "rolling_zero_trade_window_rate": overlay_diagnostics[
            "rolling_zero_trade_window_rate"
        ],
        "exposure_ratio": overlay_diagnostics["exposure_ratio"],
        "exposure_ratio_change": float(overlay_diagnostics["exposure_ratio"])
        - float(baseline_diagnostics["exposure_ratio"]),
        "one_session_trade_rate": overlay_diagnostics["one_session_trade_rate"],
        "observed_as_no_trade": bool(
            overlay_diagnostics["closed_trades"] == 0
            or overlay_diagnostics["zero_trade_years"] > 0
            or overlay_diagnostics["rolling_zero_trade_window_rate"] == 1.0
        ),
        "interpretation": "DESCRIPTIVE_DIAGNOSTIC_ONLY",
    }
    evidence = {
        "schema_version": 1,
        "status": "PASS",
        "experiment_id": EXPERIMENT_ID,
        "credential_id": protocol["credential_id"],
        "strategy_release": "S007-v1",
        "counterfactual_identity": "EXPERIMENT_LOCAL_PRECOMPUTED_TARGET_OVERLAY",
        "development_cutoff": protocol["development_cutoff"],
        "baseline_behavior_hash": baseline_behavior_hash,
        "overlay_behavior_hash": _behavior_hash(overlay_signals, behavior_sessions),
        "overlay_trigger_count": int(trigger_frame["overlay_triggered"].sum()),
        "baseline": {
            "metrics": baseline_metrics,
            "diagnostics": baseline_diagnostics,
            "audit": baseline_audit.to_dict(),
        },
        "overlay": {
            "metrics": overlay_metrics,
            "diagnostics": overlay_diagnostics,
            "audit": overlay_audit.to_dict(),
        },
        "metric_delta_overlay_minus_baseline": metric_delta,
        "triggered_trade_return_delta": {
            "mean": float(triggered_comparison["return_delta"].mean()),
            "median": float(triggered_comparison["return_delta"].median()),
            "improved_count": int(triggered_comparison["return_delta"].gt(0).sum()),
            "worsened_count": int(triggered_comparison["return_delta"].lt(0).sum()),
        },
        "triggered_trade_mechanism": triggered_mechanism,
        "no_trade_degeneration": degeneration,
        "forward_data_read": False,
        "parameters_searched": False,
        "strategy_parameters_changed": False,
        "candidate_created": False,
        "pte_mutated": False,
        "review_required_before_next_experiment": True,
    }

    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    for prefix, result in (("baseline", baseline_result), ("overlay", overlay_result)):
        for name, frame in (
            ("decisions", result.decisions),
            ("orders", result.orders),
            ("fills", result.fills),
            ("account_daily", result.account_daily),
            ("trades", result.trades),
        ):
            frame.to_csv(
                artifacts / f"{prefix}_{name}.csv",
                index=False,
                encoding="utf-8-sig",
                lineterminator="\n",
            )
    trigger_frame.to_csv(
        artifacts / "overlay_triggers.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    comparison.to_csv(
        artifacts / "trade_comparison.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    yearly_rows = []
    for year in sorted(baseline_diagnostics["yearly_closed_trades"]):
        yearly_rows.append(
            {
                "year": int(year),
                "baseline_closed_trades": baseline_diagnostics["yearly_closed_trades"][year],
                "overlay_closed_trades": overlay_diagnostics["yearly_closed_trades"][year],
            }
        )
    pd.DataFrame(yearly_rows).to_csv(
        artifacts / "yearly_trade_frequency.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    _write_json(artifacts / "counterfactual_evidence.json", evidence)

    base_diag = baseline_diagnostics
    over_diag = overlay_diagnostics
    experiment.joinpath("03_execution.md").write_text(
        "# S007 EX42 执行\n\n"
        "状态：`COMPLETE`。冻结基线与EX31正式账本及目标行为哈希一致，基线和覆盖层"
        f"的SE账本审计均为`PASS`。固定规则触发{int(trigger_frame['overlay_triggered'].sum())}笔，"
        f"覆盖层生成{int(overlay_metrics['closed_trades'])}笔闭合交易。\n\n"
        f"基线/覆盖层总收益为{float(baseline_metrics['return']):.2%}/"
        f"{float(overlay_metrics['return']):.2%}，最大回撤为"
        f"{float(baseline_metrics['max_drawdown']):.2%}/"
        f"{float(overlay_metrics['max_drawdown']):.2%}，卡玛为"
        f"{float(baseline_metrics['calmar']):.2f}/{float(overlay_metrics['calmar']):.2f}。\n\n"
        f"不交易诊断：闭合交易{base_diag['closed_trades']}/{over_diag['closed_trades']}笔，"
        f"滚动60日中位数/P10为{base_diag['rolling_closed_trades_median']:.1f}/"
        f"{base_diag['rolling_closed_trades_p10']:.1f}与"
        f"{over_diag['rolling_closed_trades_median']:.1f}/"
        f"{over_diag['rolling_closed_trades_p10']:.1f}，"
        f"持仓暴露为{base_diag['exposure_ratio']:.2%}/{over_diag['exposure_ratio']:.2%}，"
        f"1日交易占比为{base_diag['one_session_trade_rate']:.2%}/"
        f"{over_diag['one_session_trade_rate']:.2%}。\n",
        encoding="utf-8",
    )
    no_trade_text = (
        "未观察到事实上的不交易"
        if not degeneration["observed_as_no_trade"]
        else "观察到事实上的不交易风险"
    )
    return_delta = float(metric_delta["return"])
    drawdown_delta = float(metric_delta["max_drawdown"])
    calmar_delta = float(metric_delta["calmar"])
    experiment.joinpath("04_conclusion.md").write_text(
        "# S007 EX42 结论\n\n"
        "状态：`COMPLETE`；裁决：`AWAITING_HUMAN_REVIEW`。\n\n"
        f"固定覆盖层相对基线的总收益变化为{return_delta * 100:+.2f}个百分点，最大回撤变化为"
        f"{drawdown_delta * 100:+.2f}个百分点，卡玛变化为{calmar_delta:+.2f}。"
        f"43笔触发交易中，{evidence['triggered_trade_return_delta']['improved_count']}笔改善、"
        f"{evidence['triggered_trade_return_delta']['worsened_count']}笔恶化。\n\n"
        f"触发组的平均收益从{triggered_mechanism['baseline_mean_return']:.2%}降至"
        f"{triggered_mechanism['overlay_mean_return']:.2%}，胜率从"
        f"{triggered_mechanism['baseline_win_rate']:.2%}降至"
        f"{triggered_mechanism['overlay_win_rate']:.2%}。基础路径最终盈利的"
        f"{triggered_mechanism['baseline_winner_count']}笔中，有"
        f"{triggered_mechanism['baseline_winners_harmed_count']}笔被提前退出削弱；"
        "首日收盘弱势并不能单独区分随后修复与继续走弱。\n\n"
        f"{no_trade_text}：入口计划和{over_diag['closed_trades']}笔闭合交易全部保留，"
        f"零交易年份为{over_diag['zero_trade_years']}，滚动60日零交易窗口占比"
        f"{over_diag['rolling_zero_trade_window_rate']:.2%}。但持仓暴露从"
        f"{base_diag['exposure_ratio']:.2%}降至{over_diag['exposure_ratio']:.2%}，"
        f"1日交易占比从{base_diag['one_session_trade_rate']:.2%}升至"
        f"{over_diag['one_session_trade_rate']:.2%}，需要在人工评审中判断这种短持仓结构"
        "是否符合S007策略族定位。\n\n"
        "本轮观察项不构成新硬门槛。没有搜索参数、创建候选、修改冻结策略或改变PTE。"
        "下一实验须经人工评审后再启动。\n",
        encoding="utf-8",
    )

    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "strategy_version": protocol["strategy_version"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": "AWAITING_HUMAN_REVIEW",
            "overlay_trigger_count": int(trigger_frame["overlay_triggered"].sum()),
            "observed_as_no_trade": degeneration["observed_as_no_trade"],
            "strategy_parameters_changed": False,
            "candidate_created": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
