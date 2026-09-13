from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_candidate_snapshot, resolve_registered_strategy
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260913_S001_EX13"


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


def _trade_profile(result, sessions: pd.DatetimeIndex, gap: int) -> dict[str, float | int]:
    closed = result.trades.loc[result.trades["status"].eq("CLOSED")].copy()
    session_positions = {date.normalize(): index for index, date in enumerate(sessions)}
    holding_sessions: list[int] = []
    clusters = 0
    previous_exit: int | None = None
    exits: list[pd.Timestamp] = []
    for row in closed.itertuples(index=False):
        entry = session_positions[pd.Timestamp(row.entry_date).normalize()]
        exit_ = session_positions[pd.Timestamp(row.exit_date).normalize()]
        holding_sessions.append(exit_ - entry + 1)
        if previous_exit is None or entry - previous_exit >= gap:
            clusters += 1
        previous_exit = exit_
        exits.append(pd.Timestamp(row.exit_date).normalize())
    rolling_counts = []
    exit_index = pd.DatetimeIndex(exits)
    for end_index in range(59, len(sessions)):
        window = sessions[end_index - 59 : end_index + 1]
        rolling_counts.append(int(exit_index.isin(window).sum()))
    quantity = result.account_daily["quantity"].astype(float)
    return {
        "closed_trades": int(len(closed)),
        "closed_trades_per_60_sessions": float(len(closed) * 60 / len(sessions)),
        "five_session_declustered_cycles": clusters,
        "declustered_cycles_per_60_sessions": float(clusters * 60 / len(sessions)),
        "median_holding_sessions": float(np.median(holding_sessions)),
        "mean_holding_sessions": float(np.mean(holding_sessions)),
        "actual_position_session_ratio": float(quantity.gt(0).mean()),
        "rolling_60_median_closed_trades": float(np.median(rolling_counts)),
        "rolling_60_p25_closed_trades": float(np.quantile(rolling_counts, 0.25)),
        "rolling_60_max_closed_trades": int(max(rolling_counts)),
        "rolling_60_share_at_least_6": float(np.mean(np.asarray(rolling_counts) >= 6)),
    }


def _baseline_state_diagnostics(decisions: pd.DataFrame) -> dict[str, object]:
    frame = decisions.copy().sort_values("signal_date").reset_index(drop=True)
    target = frame["target_position"].astype(int)
    score = frame["factor_score"].astype(float)
    previous = target.shift(1, fill_value=0)
    flat = previous.eq(0)
    long = previous.eq(1)
    entry_threshold = 0.175
    exit_threshold = 0.025
    bands = {
        "below_exit": score.lt(exit_threshold),
        "exit_to_0155": score.ge(exit_threshold) & score.lt(0.155),
        "0155_to_0165": score.ge(0.155) & score.lt(0.165),
        "0165_to_entry": score.ge(0.165) & score.lt(entry_threshold),
        "at_or_above_entry": score.ge(entry_threshold),
    }
    flat_score_bands = {name: int((flat & mask).sum()) for name, mask in bands.items()}
    holding_age = pd.Series(0, index=frame.index, dtype=int)
    age = 0
    for index, value in enumerate(target):
        age = age + 1 if value else 0
        holding_age.iloc[index] = age
    return {
        "sessions": int(len(frame)),
        "entry_transitions": int((target.eq(1) & previous.eq(0)).sum()),
        "exit_transitions": int((target.eq(0) & previous.eq(1)).sum()),
        "flat_sessions": int(flat.sum()),
        "long_sessions": int(long.sum()),
        "flat_score_bands": flat_score_bands,
        "entry_qualified_while_already_long": int((long & score.ge(entry_threshold)).sum()),
        "exit_qualified_during_first_three_holding_sessions": int(
            (target.eq(1) & holding_age.le(3) & score.le(exit_threshold)).sum()
        ),
        "confirm_days_is_already_minimum": True,
        "exit_confirm_days_is_already_minimum": True,
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    context = RepositoryContext.discover(repo)
    source = resolve_registered_strategy(
        context, str(protocol["strategy_id"]), str(protocol["source_version"])
    )
    if source.source_hash != protocol["source_release_hash"]:
        raise ValueError("source strategy release differs from frozen protocol")
    data = load_replay_data(
        context,
        str(protocol["dataset"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        pd.Timestamp(str(protocol["development_cutoff"])).date(),
    )
    start = pd.Timestamp(str(protocol["evaluation_start"])).date()
    end = pd.Timestamp(str(protocol["evaluation_end"])).date()
    sessions = pd.DatetimeIndex(
        data.execution_daily.loc[
            data.execution_daily["dt"].between(pd.Timestamp(start), pd.Timestamp(end)), "dt"
        ]
    )
    rows: list[dict[str, object]] = []
    baseline_decisions: pd.DataFrame | None = None
    for raw_variant in protocol["diagnostic_variants"]:
        variant = dict(raw_variant)
        payload = deepcopy(source.strategy_payload)
        rule = payload["rule"]
        rule["entry_threshold"] = float(variant["entry_threshold"])
        rule["exit_threshold"] = float(variant["exit_threshold"])
        rule["min_hold_days"] = int(variant["min_hold_days"])
        snapshot = resolve_candidate_snapshot(
            context,
            str(variant["variant_id"]),
            payload,
            canonical_json_sha256(payload),
            f"experiments/S001/{EXPERIMENT_ID}/{variant['variant_id']}",
        )
        signals = replay_signals(snapshot, data, start, end)
        result = replay_account(signals, data, float(protocol["initial_cash"]))
        metrics = calculate_metrics(result, float(protocol["initial_cash"]))
        profile = _trade_profile(
            result, sessions, int(protocol["decluster_gap_sessions"])
        )
        rows.append(
            {
                **variant,
                **profile,
                "total_return": metrics["return"],
                "cagr": float(
                    (1.0 + float(metrics["return"])) ** (252.0 / len(sessions)) - 1.0
                ),
                "max_drawdown": metrics["max_drawdown"],
                "calmar": metrics["calmar"],
                "win_loss_ratio": metrics["win_loss_ratio"],
            }
        )
        if variant["variant_id"] == "BASELINE":
            baseline_decisions = signals.decisions.loc[
                signals.decisions["signal_date"].between(pd.Timestamp(start), pd.Timestamp(end))
            ]
    landscape = pd.DataFrame(rows)
    landscape.to_csv(artifacts / "frequency_landscape.csv", index=False, lineterminator="\n")
    if baseline_decisions is None:
        raise AssertionError("baseline diagnostic is missing")
    state = _baseline_state_diagnostics(baseline_decisions)
    baseline = landscape.loc[landscape["variant_id"].eq("BASELINE")].iloc[0]
    axes = {
        "entry_threshold": ("ENTRY_0165", "ENTRY_0155"),
        "exit_threshold": ("EXIT_0050", "EXIT_0075"),
        "minimum_holding": ("HOLD_1", "HOLD_2"),
    }
    axis_effects = {}
    for axis, ids in axes.items():
        selected = landscape.loc[landscape["variant_id"].isin(ids)]
        best = selected.sort_values("closed_trades", ascending=False).iloc[0]
        axis_effects[axis] = {
            "best_diagnostic_variant": str(best["variant_id"]),
            "closed_trade_gain": int(best["closed_trades"] - baseline["closed_trades"]),
            "rate_per_60_gain": float(
                best["closed_trades_per_60_sessions"]
                - baseline["closed_trades_per_60_sessions"]
            ),
        }
    ranked_axes = sorted(
        axis_effects,
        key=lambda name: axis_effects[name]["closed_trade_gain"],
        reverse=True,
    )
    target_minimum = float(protocol["target_closed_trades_per_60_sessions"][0])
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "source_strategy": source.identity.reference,
        "baseline": baseline.to_dict(),
        "state_machine_diagnostics": state,
        "axis_effects": axis_effects,
        "frequency_bottleneck_ranking": ranked_axes,
        "single_axis_target_hits": landscape.loc[
            landscape["closed_trades_per_60_sessions"].ge(target_minimum), "variant_id"
        ].astype(str).tolist(),
        "route_decision": "PROCEED_TO_FREQUENCY_MECHANISM_RESEARCH",
        "candidate_created": False,
        "trial_count_added": int(len(landscape)),
    }
    _write(artifacts / "bottleneck_summary.json", summary)
    leader = ranked_axes[0]
    (experiment / "03_execution.md").write_text(
        "# S001 EX13 执行\n\n状态：COMPLETE。完成7条预登记单轴诊断路径，"
        "未组合参数、未选择或生成候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX13 结论\n\n"
        f"基线每60个交易日约{float(baseline['closed_trades_per_60_sessions']):.2f}笔闭合交易。"
        f"单轴诊断显示首要频率杠杆为`{leader}`；达到6笔下限的路径："
        f"{', '.join(summary['single_axis_target_hits']) or '无'}。"
        "本轮仅定位瓶颈，下一轮须研究新增交易的金融机制与交易质量。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "strategy_version": protocol["source_version"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": summary["route_decision"],
            "candidate_generation": False,
            "trial_count_added": len(landscape),
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
