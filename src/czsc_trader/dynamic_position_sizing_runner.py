"""Preregistered dynamic score-zone position-sizing challenge for 588080.SH."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import platform
from typing import Any

import numpy as np
import pandas as pd

from .audit import audit_no_lookahead
from .backtest import run_period_backtests
from .baselines import ResolvedBaseline, resolve_baseline
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .four_layer_runner import _PeriodEvaluator, _target_digest, _write_json
from .objectives import TARGET_PERIODS
from .position_sizing import (
    build_dynamic_sizing_events,
    positions_from_dynamic_score_zones,
)
from .position_sizing_runner import (
    EXPECTED_CHAMPION,
    SELECTION_CUTOFF,
    VALIDATION_WINDOWS,
    _apply_champion,
    _finalize_error,
    _half_year_periods,
    _identity_audit,
)


CHALLENGER_ID = "dynamic_original_score_zones"


def validate_dynamic_position_sizing_protocol(
    protocol: Mapping[str, object],
) -> None:
    """Reject drift from the committed 0829_EX02 protocol."""
    expected_scalars = {
        "schema_version": 1,
        "experiment_id": "0829_EX02",
        "handler": "dynamic_score_zone_position_sizing",
        "experiment_type": "dynamic_score_zone_position_sizing",
        "status": "PRE_REGISTERED",
        "symbol": "588080.SH",
        "asset_type": "etf",
        "selection_sample_end": "2025-12-31",
        "factor_count": 12,
        "factor_policy": "exact_active_baseline_signals_weights_and_normalization",
        "position_policy": "full_entry_then_dynamic_original_score_zones_after_min_hold",
        "challenger_id": CHALLENGER_ID,
        "full_position": 1.0,
        "partial_position": 0.5,
        "entry_threshold": 0.175,
        "exit_threshold": 0.025,
        "confirm_days": 1,
        "min_hold_days": 3,
        "exit_confirm_days": 1,
        "entry_gate": "none",
        "selection_policy": "single_fixed_challenger_no_parameter_selection",
        "holdout_cutoff": "2026-08-21",
        "fee_rate": 0.0005,
        "init_cash": 1_000_000.0,
        "selection_metric": "strategy_return_only",
        "risk_metric_policy": "diagnostics_only",
        "holdout_access_before_freeze": False,
        "pass_rule": "strictly_higher_return_than_champion_in_every_original_2026_window",
        "observed_data_disclosure": (
            "2026 data through 2026-08-28 was already observed before this "
            "experiment; unchanged holdout windows are retained only for "
            "protocol comparability"
        ),
    }
    for key, expected in expected_scalars.items():
        if protocol.get(key) != expected:
            raise ValueError(f"dynamic position-sizing protocol differs for {key}")
    if protocol.get("champion") != EXPECTED_CHAMPION:
        raise ValueError("dynamic position-sizing champion identity differs")
    if tuple(protocol.get("validation_windows", ())) != VALIDATION_WINDOWS:
        raise ValueError("dynamic position-sizing validation windows differ")
    if tuple(protocol.get("holdout_windows", ())) != tuple(TARGET_PERIODS):
        raise ValueError("dynamic position-sizing holdout windows differ")


def dynamic_position_sizing_holdout_pass(
    windows: Mapping[str, Mapping[str, object]],
) -> bool:
    """Keep the original strict return leadership rule in all three windows."""
    return set(windows) == set(TARGET_PERIODS) and all(
        float(payload["challenger_return"]) > float(payload["champion_return"])
        for payload in windows.values()
    )


def _dynamic_target(
    scores: pd.Series,
    baseline: ResolvedBaseline,
    protocol: Mapping[str, object],
) -> pd.Series:
    return positions_from_dynamic_score_zones(
        scores,
        enter=float(protocol["entry_threshold"]),
        exit_=float(protocol["exit_threshold"]),
        state_rule=baseline.rule,
        partial_position=float(protocol["partial_position"]),
    )


def _visible_diagnostics(
    data: Any,
    baseline: ResolvedBaseline,
    applied: Any,
    protocol: Mapping[str, object],
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> tuple[pd.Series, dict[str, object]]:
    evaluator = _PeriodEvaluator(data.daily, _half_year_periods(), fee_rate, init_cash)
    champion_metrics = evaluator.evaluate(applied.target_position)
    target = _dynamic_target(applied.scores, baseline, protocol)
    challenger_metrics = evaluator.evaluate(target)
    rows: list[dict[str, object]] = []
    deltas: list[float] = []
    for name in VALIDATION_WINDOWS:
        champion = champion_metrics[name]
        challenger = challenger_metrics[name]
        delta = float(challenger["strategy_return"]) - float(
            champion["strategy_return"]
        )
        deltas.append(delta)
        rows.append(
            {
                "window": name,
                "champion_return": champion["strategy_return"],
                "challenger_return": challenger["strategy_return"],
                "return_delta": delta,
                "champion_sharpe": champion["sharpe"],
                "challenger_sharpe": challenger["sharpe"],
                "champion_max_drawdown": champion["max_drawdown"],
                "challenger_max_drawdown": challenger["max_drawdown"],
                "champion_exposure": champion["exposure"],
                "challenger_exposure": challenger["exposure"],
                "champion_trade_count": champion["trade_count"],
                "challenger_trade_count": challenger["trade_count"],
                "win": delta > 0.0,
            }
        )
    pd.DataFrame(rows).to_csv(
        artifacts / "visible_metrics.csv", index=False, encoding="utf-8-sig"
    )
    summary = {
        "challenger_id": CHALLENGER_ID,
        "candidate_count": 1,
        "selection_policy": protocol["selection_policy"],
        "win_count": int(sum(delta > 0.0 for delta in deltas)),
        "loss_count": int(sum(delta < 0.0 for delta in deltas)),
        "flat_count": int(sum(delta == 0.0 for delta in deltas)),
        "minimum_return_delta": min(deltas),
        "median_return_delta": float(np.median(deltas)),
        "mean_return_delta": float(np.mean(deltas)),
        "selection_target_sha256": _target_digest(target),
        "selection_cutoff": str(SELECTION_CUTOFF.date()),
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
    }
    _write_json(artifacts / "selection_metrics.json", summary)
    return target, summary


def _freeze_challenger(
    experiment_id: str,
    baseline: ResolvedBaseline,
    selection: Mapping[str, object],
    protocol: Mapping[str, object],
    artifacts: Path,
) -> tuple[dict[str, object], str]:
    frozen = {
        "schema_version": 1,
        "experiment": experiment_id,
        "sample_end": "2025-12-31",
        "champion": EXPECTED_CHAMPION,
        "factor_policy": protocol["factor_policy"],
        "factor_names": list(baseline.factor_names),
        "weights": dict(
            zip(baseline.factor_names, baseline.factor_weights, strict=True)
        ),
        "entry_threshold": float(protocol["entry_threshold"]),
        "exit_threshold": float(protocol["exit_threshold"]),
        "full_position": float(protocol["full_position"]),
        "partial_position": float(protocol["partial_position"]),
        "position_policy": protocol["position_policy"],
        "confirm_days": baseline.rule.confirm_days,
        "min_hold_days": baseline.rule.min_hold_days,
        "exit_confirm_days": baseline.rule.exit_confirm_days,
        "entry_gate": baseline.rule.entry_gate,
        "fee_rate": float(protocol["fee_rate"]),
        "selection_metric": protocol["selection_metric"],
        "candidate_id": CHALLENGER_ID,
        "selection_target_sha256": selection["selection_target_sha256"],
    }
    path = artifacts / "frozen_challenger.json"
    _write_json(path, frozen)
    return frozen, sha256(path.read_bytes()).hexdigest()


def _holdout(
    raw_dir: Path,
    baseline: ResolvedBaseline,
    protocol: Mapping[str, object],
    frozen_digest: str,
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> dict[str, object]:
    cutoff = pd.Timestamp(str(protocol["holdout_cutoff"]))
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=cutoff)
    _, applied = _apply_champion(data, baseline)
    target = _dynamic_target(applied.scores, baseline, protocol)
    events = build_dynamic_sizing_events(
        target,
        applied.scores,
        float(protocol["entry_threshold"]),
        float(protocol["exit_threshold"]),
    )
    audit_frame = pd.DataFrame(
        {
            "factor_score": applied.scores,
            "enter_threshold": float(protocol["entry_threshold"]),
            "full_threshold": float(protocol["entry_threshold"]),
            "exit_threshold": float(protocol["exit_threshold"]),
        },
        index=applied.scores.index,
    )
    champion_results = run_period_backtests(
        data.daily,
        applied.target_position,
        TARGET_PERIODS,
        fee_rate=fee_rate,
        init_cash=init_cash,
    )
    challenger_results = run_period_backtests(
        data.daily,
        target,
        TARGET_PERIODS,
        fee_rate=fee_rate,
        init_cash=init_cash,
        factor_events=events,
        factor_frame=audit_frame,
    )
    windows: dict[str, dict[str, object]] = {}
    for name in TARGET_PERIODS:
        champion = champion_results[name].metrics
        challenger = challenger_results[name].metrics
        champion_return = float(champion["strategy_return"])
        challenger_return = float(challenger["strategy_return"])
        windows[name] = {
            "champion_return": champion_return,
            "challenger_return": challenger_return,
            "return_delta": challenger_return - champion_return,
            "champion_sharpe": float(champion["sharpe"]),
            "challenger_sharpe": float(challenger["sharpe"]),
            "sharpe_delta": float(challenger["sharpe"])
            - float(champion["sharpe"]),
            "champion_max_drawdown": float(champion["max_drawdown"]),
            "challenger_max_drawdown": float(challenger["max_drawdown"]),
            "champion_exposure": float(champion["exposure"]),
            "challenger_exposure": float(challenger["exposure"]),
            "champion_trade_count": int(champion["trade_count"]),
            "challenger_trade_count": int(challenger["trade_count"]),
            "pass": challenger_return > champion_return,
        }
    overall = dynamic_position_sizing_holdout_pass(windows)
    orders = pd.concat(
        [result.orders for result in challenger_results.values()], ignore_index=True
    )
    used_events = pd.concat(
        [events, *[result.factor_events for result in challenger_results.values()]],
        ignore_index=True,
    ).drop_duplicates(subset=["event_id"])
    audit = audit_no_lookahead(orders, used_events, target, audit_frame)
    orders.to_csv(artifacts / "orders.csv", index=False, encoding="utf-8-sig")
    used_events.to_csv(
        artifacts / "factor_events.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(
        {
            "signal_date": target.index,
            "factor_score": applied.scores.to_numpy(),
            "champion_target_position": applied.target_position.to_numpy(),
            "challenger_target_position": target.to_numpy(),
            "champion_execution_position": applied.target_position.shift(1).to_numpy(),
            "challenger_execution_position": target.shift(1).to_numpy(),
        }
    ).to_csv(artifacts / "position_path.csv", index=False, encoding="utf-8-sig")
    transition_counts = {
        str(name): int(count)
        for name, count in events["event_type"].value_counts().items()
    }
    payload = {
        "status": "PASS" if overall else "FAIL",
        "windows": windows,
        "transition_counts": transition_counts,
        "audit": audit,
        "frozen_challenger_sha256": frozen_digest,
        "frozen_before_holdout": True,
        "holdout_cutoff": str(cutoff.date()),
        "holdout_data_hashes": data.hashes,
        "holdout_accessed": True,
        "observed_data_disclosure": protocol["observed_data_disclosure"],
    }
    _write_json(artifacts / "holdout_metrics.json", payload)
    return payload


def _write_result_docs(
    experiment_dir: Path,
    selection: Mapping[str, object],
    holdout: Mapping[str, object],
    execution_commit: str,
) -> None:
    executed_at = datetime.now().astimezone().isoformat()
    transitions = holdout["transition_counts"]
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        f"- 状态：正式执行完成（`{holdout['status']}`）\n"
        f"- 执行时间：{executed_at}\n"
        f"- 执行提交：`{execution_commit}`\n"
        f"- Python：{platform.python_version()}\n"
        f"- pandas：{pd.__version__}\n"
        f"- NumPy：{np.__version__}\n"
        "- 当前冠军：`baseline_20260826`\n"
        "- 固定挑战者数：1\n"
        f"- 可见半年收益胜出：{selection['win_count']}/8\n"
        "- 参数选择：无\n"
        "- 冻结前访问2026：否\n"
        "- 冻结后访问2026：是\n"
        f"- 变仓事件计数：{json.dumps(transitions, ensure_ascii=False, sort_keys=True)}\n"
        f"- 因果审计：`{holdout['audit']['status']}`\n",
        encoding="utf-8",
    )
    lines = [
        "# 研究结论",
        "",
        f"0829_EX02动态仓位挑战结果为 **{holdout['status']}**。",
        "",
        "| 窗口 | 冠军收益 | 挑战者收益 | 收益增量 | 冠军夏普 | 挑战者夏普 | 冠军最大回撤 | 挑战者最大回撤 | 判定 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name, row in holdout["windows"].items():
        lines.append(
            f"| {name} | {float(row['champion_return']):.4%} | "
            f"{float(row['challenger_return']):.4%} | {float(row['return_delta']):+.4%} | "
            f"{float(row['champion_sharpe']):.4f} | {float(row['challenger_sharpe']):.4f} | "
            f"{float(row['champion_max_drawdown']):.4%} | "
            f"{float(row['challenger_max_drawdown']):.4%} | "
            f"{'PASS' if row['pass'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "## 固定方案",
            "",
            "- 入场全仓；达到原最短持有期后，原入场阈值以上全仓、中间分数区间半仓、原退出阈值以下清仓。",
            f"- 可见样本收益胜出：{selection['win_count']}/8；只有一个固定挑战者，没有参数选择。",
            "- 收益率是唯一PASS指标；风险指标和变仓次数仅作诊断。",
            "- 2026截至2026-08-28已经被观察，本轮不构成新的独立前向证据。",
            "- 本轮不自动修改活动基线；当前正式基线仍为`baseline_20260826`。",
            "",
        ]
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def run_dynamic_position_sizing_experiment(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> dict[str, object]:
    """Execute and freeze the committed 0829_EX02 protocol exactly once."""
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    holdout_accessed = False
    try:
        validate_dynamic_position_sizing_protocol(protocol)
        if fee_rate != float(protocol["fee_rate"]) or init_cash != float(
            protocol["init_cash"]
        ):
            raise ValueError("runtime fee or initial cash differs from preregistration")
        baseline = resolve_baseline(
            Path(baseline_root), str(protocol["champion"]["version"]), symbol="588080.SH"
        )
        selection_data = load_market_data(
            raw_dir, "588080.SH", "etf", cutoff=SELECTION_CUTOFF
        )
        _, applied = _apply_champion(selection_data, baseline)
        identity = _identity_audit(baseline, applied, protocol)
        _write_json(artifacts / "identity_audit.json", identity)
        _, selection = _visible_diagnostics(
            selection_data,
            baseline,
            applied,
            protocol,
            artifacts,
            fee_rate,
            init_cash,
        )
        frozen, frozen_digest = _freeze_challenger(
            experiment_dir.name, baseline, selection, protocol, artifacts
        )
        holdout_accessed = True
        holdout = _holdout(
            raw_dir,
            baseline,
            protocol,
            frozen_digest,
            artifacts,
            fee_rate,
            init_cash,
        )
        _write_result_docs(experiment_dir, selection, holdout, execution_commit)
        build_experiment_manifest(
            experiment_dir,
            {
                "experiment_id": experiment_dir.name,
                "date": "2026-08-29",
                "status": holdout["status"],
                "symbol": "588080.SH",
                "asset_type": "etf",
                "champion": EXPECTED_CHAMPION,
                "visible_sample_end": "2025-12-31",
                "holdout_accessed": True,
                "frozen_challenger_sha256": frozen_digest,
            },
        )
        validate_experiment_archive(experiment_dir)
        return {
            "status": holdout["status"],
            "challenger_id": CHALLENGER_ID,
            "frozen_challenger": frozen,
            "selection": selection,
            "holdout": holdout,
            "experiment_dir": str(experiment_dir),
        }
    except Exception as exc:
        _finalize_error(
            experiment_dir,
            protocol,
            exc,
            execution_commit,
            holdout_accessed=holdout_accessed,
        )
        raise
