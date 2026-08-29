"""Preregistered entry-fixed position-sizing challenge for 588080.SH."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
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
from .baseline_execution import apply_resolved_baseline
from .baselines import ResolvedBaseline, resolve_baseline
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factors import generate_factor_frame
from .four_layer_runner import _PeriodEvaluator, _target_digest, _write_json
from .objectives import TARGET_PERIODS
from .position_sizing import (
    build_entry_sizing_events,
    positions_from_entry_sizing,
)


SELECTION_CUTOFF = pd.Timestamp("2025-12-31")
VALIDATION_WINDOWS = (
    "2022H1",
    "2022H2",
    "2023H1",
    "2023H2",
    "2024H1",
    "2024H2",
    "2025H1",
    "2025H2",
)
FULL_THRESHOLDS = (0.2, 0.225, 0.25)
EXPECTED_CHAMPION = {
    "version": "baseline_20260826",
    "sha256": "fc22ca5a973f77faf528cdb3efba4900163e18fe0c08cf79234d79ef22f5c822",
    "source_path": "experiments/0824_EX04/artifacts/frozen_challenger.json",
    "source_sha256": "c6fa86c0f87743564231dec4fb6e66971bde0fa4107829d077cb5cf31c53885f",
}
EXPECTED_RANKING = (
    "strict_return_win_count_desc",
    "minimum_return_delta_desc",
    "median_return_delta_desc",
    "mean_return_delta_desc",
    "candidate_id_asc",
)


@dataclass(frozen=True)
class PositionSizingSpec:
    full_threshold: float

    @property
    def candidate_id(self) -> str:
        return f"full_{self.full_threshold:.3f}"


def build_position_sizing_specs(
    protocol: Mapping[str, object],
) -> tuple[PositionSizingSpec, ...]:
    return tuple(
        PositionSizingSpec(float(value))
        for value in protocol["full_thresholds"]  # type: ignore[index]
    )


def validate_position_sizing_protocol(protocol: Mapping[str, object]) -> None:
    """Reject any drift from the committed 0829_EX01 protocol."""
    if protocol.get("experiment_id") != "0829_EX01":
        raise ValueError("position-sizing experiment identity differs")
    if (
        protocol.get("handler") != "entry_fixed_position_sizing"
        or protocol.get("experiment_type") != "entry_fixed_position_sizing"
    ):
        raise ValueError("position-sizing handler identity differs")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("position-sizing protocol must remain PRE_REGISTERED")
    if protocol.get("symbol") != "588080.SH" or protocol.get("asset_type") != "etf":
        raise ValueError("position-sizing instrument identity differs")
    if protocol.get("champion") != EXPECTED_CHAMPION:
        raise ValueError("position-sizing champion identity differs")
    expected_scalars = {
        "selection_sample_end": "2025-12-31",
        "factor_count": 12,
        "factor_policy": "exact_active_baseline_signals_weights_and_normalization",
        "position_policy": "entry_fixed_no_rebalance_until_original_exit",
        "partial_position": 0.5,
        "entry_threshold": 0.175,
        "exit_threshold": 0.025,
        "confirm_days": 1,
        "min_hold_days": 3,
        "exit_confirm_days": 1,
        "entry_gate": "none",
        "holdout_cutoff": "2026-08-21",
        "fee_rate": 0.0005,
        "init_cash": 1_000_000.0,
        "selection_metric": "strategy_return_only",
        "risk_metric_policy": "diagnostics_only",
        "holdout_access_before_freeze": False,
        "pass_rule": "strictly_higher_return_than_champion_in_every_original_2026_window",
    }
    for key, expected in expected_scalars.items():
        if protocol.get(key) != expected:
            raise ValueError(f"position-sizing protocol differs for {key}")
    if tuple(protocol.get("full_thresholds", ())) != FULL_THRESHOLDS:
        raise ValueError("position-sizing full thresholds differ")
    if tuple(protocol.get("validation_windows", ())) != VALIDATION_WINDOWS:
        raise ValueError("position-sizing validation windows differ")
    if tuple(protocol.get("holdout_windows", ())) != tuple(TARGET_PERIODS):
        raise ValueError("position-sizing holdout windows differ")
    if tuple(protocol.get("candidate_ranking", ())) != EXPECTED_RANKING:
        raise ValueError("position-sizing candidate ranking differs")
    if len(build_position_sizing_specs(protocol)) != 3:
        raise ValueError("position-sizing protocol must contain three candidates")


def rank_position_sizing_results(rows: pd.DataFrame) -> pd.DataFrame:
    """Apply the preregistered return-only ranking in stable order."""
    return rows.sort_values(
        [
            "win_count",
            "min_return_delta",
            "median_return_delta",
            "mean_return_delta",
            "candidate_id",
        ],
        ascending=[False, False, False, False, True],
        kind="stable",
    ).reset_index(drop=True)


def position_sizing_holdout_pass(
    windows: Mapping[str, Mapping[str, object]],
) -> bool:
    """Require strict return leadership in exactly the original windows."""
    return set(windows) == set(TARGET_PERIODS) and all(
        float(payload["challenger_return"]) > float(payload["champion_return"])
        for payload in windows.values()
    )


def _half_year_periods() -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for year in range(2022, 2026):
        periods[f"{year}H1"] = (
            pd.Timestamp(year, 1, 1),
            pd.Timestamp(year, 6, 30),
        )
        periods[f"{year}H2"] = (
            pd.Timestamp(year, 7, 1),
            pd.Timestamp(year, 12, 31),
        )
    return periods


def _apply_champion(data: Any, baseline: ResolvedBaseline) -> tuple[pd.DataFrame, Any]:
    frame = generate_factor_frame(data).frame
    applied = apply_resolved_baseline(frame, baseline)
    if len(baseline.factor_names) != 12 or len(baseline.factor_weights) != 12:
        raise AssertionError("active champion lost its frozen 12-factor identity")
    if not applied.target_position.isin([0.0, 1.0]).all():
        raise AssertionError("active champion is no longer a binary position path")
    if not applied.target_position.index.equals(applied.scores.index):
        raise AssertionError("active champion score and target indices differ")
    return frame, applied


def _identity_audit(
    baseline: ResolvedBaseline,
    applied: Any,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    target_values = sorted(map(float, applied.target_position.unique()))
    payload = {
        "status": "PASS",
        "champion_version": baseline.version,
        "champion_sha256": baseline.sha256,
        "source_path": baseline.source_path,
        "source_sha256": baseline.source_sha256,
        "factor_count": len(baseline.factor_names),
        "factor_names": list(baseline.factor_names),
        "weight_l1_norm": float(sum(map(abs, baseline.factor_weights))),
        "champion_position_values": target_values,
        "entry_threshold": float(baseline.rule.enter),
        "exit_threshold": float(baseline.rule.exit),
        "confirm_days": int(baseline.rule.confirm_days),
        "min_hold_days": int(baseline.rule.min_hold_days),
        "exit_confirm_days": int(baseline.rule.exit_confirm_days),
        "entry_gate": baseline.rule.entry_gate,
        "protocol_champion": protocol["champion"],
    }
    if baseline.version != EXPECTED_CHAMPION["version"]:
        raise AssertionError("resolved champion version differs from preregistration")
    if baseline.sha256 != EXPECTED_CHAMPION["sha256"]:
        raise AssertionError("resolved champion SHA-256 differs from preregistration")
    if baseline.source_path != EXPECTED_CHAMPION["source_path"]:
        raise AssertionError("resolved champion source path differs from preregistration")
    if baseline.source_sha256 != EXPECTED_CHAMPION["source_sha256"]:
        raise AssertionError("resolved champion source SHA-256 differs from preregistration")
    if abs(float(payload["weight_l1_norm"]) - 1.0) > 1e-12:
        raise AssertionError("active champion factor weights lost L1 normalization")
    return payload


def _sized_target(
    scores: pd.Series,
    baseline: ResolvedBaseline,
    spec: PositionSizingSpec,
    protocol: Mapping[str, object],
) -> pd.Series:
    return positions_from_entry_sizing(
        scores,
        enter=float(protocol["entry_threshold"]),
        full=spec.full_threshold,
        exit_=float(protocol["exit_threshold"]),
        state_rule=baseline.rule,
        partial_position=float(protocol["partial_position"]),
    )


def _selection(
    data: Any,
    baseline: ResolvedBaseline,
    applied: Any,
    protocol: Mapping[str, object],
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> tuple[PositionSizingSpec, pd.Series, dict[str, object]]:
    periods = _half_year_periods()
    evaluator = _PeriodEvaluator(data.daily, periods, fee_rate, init_cash)
    champion_metrics = evaluator.evaluate(applied.target_position)
    specs = build_position_sizing_specs(protocol)
    targets: dict[str, pd.Series] = {}
    rows: list[dict[str, object]] = []
    for spec in specs:
        target = _sized_target(applied.scores, baseline, spec, protocol)
        targets[spec.candidate_id] = target
        metrics = evaluator.evaluate(target)
        deltas = {
            name: float(metrics[name]["strategy_return"])
            - float(champion_metrics[name]["strategy_return"])
            for name in VALIDATION_WINDOWS
        }
        row: dict[str, object] = {
            **asdict(spec),
            "candidate_id": spec.candidate_id,
            "win_count": int(sum(delta > 0.0 for delta in deltas.values())),
            "min_return_delta": min(deltas.values()),
            "median_return_delta": float(np.median(list(deltas.values()))),
            "mean_return_delta": float(np.mean(list(deltas.values()))),
            "mean_sharpe_delta": float(
                np.mean(
                    [
                        float(metrics[name]["sharpe"])
                        - float(champion_metrics[name]["sharpe"])
                        for name in VALIDATION_WINDOWS
                    ]
                )
            ),
        }
        for name in VALIDATION_WINDOWS:
            row[f"{name}_champion_return"] = champion_metrics[name]["strategy_return"]
            row[f"{name}_challenger_return"] = metrics[name]["strategy_return"]
            row[f"{name}_return_delta"] = deltas[name]
            row[f"{name}_challenger_sharpe"] = metrics[name]["sharpe"]
            row[f"{name}_challenger_max_drawdown"] = metrics[name]["max_drawdown"]
            row[f"{name}_challenger_exposure"] = metrics[name]["exposure"]
            row[f"{name}_challenger_trade_count"] = metrics[name]["trade_count"]
            row[f"{name}_win"] = deltas[name] > 0.0
        rows.append(row)
    ranked = rank_position_sizing_results(pd.DataFrame(rows))
    ranked.insert(0, "rank", range(1, len(ranked) + 1))
    ranked.to_csv(
        artifacts / "candidate_results.csv", index=False, encoding="utf-8-sig"
    )
    best_id = str(ranked.iloc[0]["candidate_id"])
    best = next(spec for spec in specs if spec.candidate_id == best_id)
    best_target = targets[best_id]
    summary = {
        "best_candidate": best_id,
        "best_full_threshold": best.full_threshold,
        "candidate_count": len(ranked),
        "best_win_count": int(ranked.iloc[0]["win_count"]),
        "best_min_return_delta": float(ranked.iloc[0]["min_return_delta"]),
        "best_median_return_delta": float(ranked.iloc[0]["median_return_delta"]),
        "best_mean_return_delta": float(ranked.iloc[0]["mean_return_delta"]),
        "selection_target_sha256": _target_digest(best_target),
        "selection_cutoff": str(SELECTION_CUTOFF.date()),
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
    }
    _write_json(artifacts / "selection_metrics.json", summary)
    return best, best_target, summary


def _freeze_challenger(
    experiment_id: str,
    baseline: ResolvedBaseline,
    spec: PositionSizingSpec,
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
        "weights": dict(zip(baseline.factor_names, baseline.factor_weights, strict=True)),
        "entry_threshold": float(protocol["entry_threshold"]),
        "full_threshold": spec.full_threshold,
        "exit_threshold": float(protocol["exit_threshold"]),
        "partial_position": float(protocol["partial_position"]),
        "position_policy": protocol["position_policy"],
        "confirm_days": baseline.rule.confirm_days,
        "min_hold_days": baseline.rule.min_hold_days,
        "exit_confirm_days": baseline.rule.exit_confirm_days,
        "entry_gate": baseline.rule.entry_gate,
        "fee_rate": float(protocol["fee_rate"]),
        "selection_metric": "strategy_return_only",
        "candidate_id": spec.candidate_id,
        "selection_target_sha256": selection["selection_target_sha256"],
    }
    frozen_path = artifacts / "frozen_challenger.json"
    _write_json(frozen_path, frozen)
    return frozen, sha256(frozen_path.read_bytes()).hexdigest()


def _holdout(
    raw_dir: Path,
    baseline: ResolvedBaseline,
    spec: PositionSizingSpec,
    protocol: Mapping[str, object],
    frozen_digest: str,
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> dict[str, object]:
    cutoff = pd.Timestamp(str(protocol["holdout_cutoff"]))
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=cutoff)
    _, applied = _apply_champion(data, baseline)
    target = _sized_target(applied.scores, baseline, spec, protocol)
    events = build_entry_sizing_events(
        target,
        applied.scores,
        float(protocol["entry_threshold"]),
        spec.full_threshold,
        float(protocol["exit_threshold"]),
    )
    audit_frame = pd.DataFrame(
        {
            "factor_score": applied.scores,
            "enter_threshold": float(protocol["entry_threshold"]),
            "full_threshold": spec.full_threshold,
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
    overall = position_sizing_holdout_pass(windows)
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
    payload = {
        "status": "PASS" if overall else "FAIL",
        "windows": windows,
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
    spec: PositionSizingSpec,
    execution_commit: str,
) -> None:
    executed_at = datetime.now().astimezone().isoformat()
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        f"- 状态：正式执行完成（`{holdout['status']}`）\n"
        f"- 执行时间：{executed_at}\n"
        f"- 执行提交：`{execution_commit}`\n"
        f"- Python：{platform.python_version()}\n"
        f"- pandas：{pd.__version__}\n"
        f"- NumPy：{np.__version__}\n"
        "- 当前冠军：`baseline_20260826`\n"
        f"- 固定候选数：{selection['candidate_count']}\n"
        f"- 冻结候选：`{selection['best_candidate']}`\n"
        f"- 冻结满仓阈值：{float(spec.full_threshold):.3f}\n"
        f"- 半年验证收益胜出：{selection['best_win_count']}/8\n"
        "- 选优指标：仅收益率\n"
        "- 冻结前访问2026：否\n"
        "- 冻结后访问2026：是\n"
        f"- 因果审计：`{holdout['audit']['status']}`\n",
        encoding="utf-8",
    )
    lines = [
        "# 研究结论",
        "",
        f"0829_EX01入场定仓挑战结果为 **{holdout['status']}**。",
        "",
        "| 窗口 | 冠军收益 | 挑战者收益 | 收益增量 | 冠军夏普 | 挑战者夏普 | 冠军最大回撤 | 挑战者最大回撤 | 判定 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name in TARGET_PERIODS:
        row = holdout["windows"][name]
        lines.append(
            f"| {name} | {float(row['champion_return']):.4%} | "
            f"{float(row['challenger_return']):.4%} | "
            f"{float(row['return_delta']):+.4%} | "
            f"{float(row['champion_sharpe']):.4f} | "
            f"{float(row['challenger_sharpe']):.4f} | "
            f"{float(row['champion_max_drawdown']):.4%} | "
            f"{float(row['challenger_max_drawdown']):.4%} | "
            f"{'PASS' if row['pass'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "## 冻结方案",
            "",
            f"- 满仓阈值：`{spec.full_threshold:.3f}`；半仓：`0.5`。",
            "- 入场时定仓，持有期间不升降仓，原退出信号清仓。",
            f"- 选择阶段收益胜出：{selection['best_win_count']}/8。",
            "- 因子、权重、原进出场阈值、状态机和次日开盘执行均未改变。",
            "- 夏普、最大回撤、持仓率和交易次数仅展示，未参与选优或PASS。",
            "- 2026窗口已经被观察，本轮只保持原协议可比性，未据结果二次调参。",
            "- 本轮不自动修改活动基线；当前正式基线仍为`baseline_20260826`。",
        ]
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _finalize_error(
    experiment_dir: Path,
    protocol: Mapping[str, object],
    exc: Exception,
    execution_commit: str,
    *,
    holdout_accessed: bool,
) -> None:
    if (experiment_dir / "experiment_manifest.json").exists():
        return
    artifacts = experiment_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    _write_json(
        artifacts / "error.json",
        {
            "status": "ERROR",
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "execution_commit": execution_commit,
            "holdout_accessed": holdout_accessed,
        },
    )
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        "- 状态：正式执行异常（`ERROR`）\n"
        f"- 执行提交：`{execution_commit}`\n"
        f"- 异常类型：`{type(exc).__name__}`\n"
        f"- 异常信息：{str(exc)}\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "# 研究结论\n\n"
        "本轮状态为 **ERROR**，没有产生可用于冠军挑战判定的PASS或FAIL结果。\n"
        "活动基线未改变。错误详情见`artifacts/error.json`。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": "2026-08-29",
            "status": "ERROR",
            "symbol": str(protocol.get("symbol", "588080.SH")),
            "asset_type": str(protocol.get("asset_type", "etf")),
            "champion": protocol.get("champion", EXPECTED_CHAMPION),
            "visible_sample_end": "2025-12-31",
            "holdout_accessed": holdout_accessed,
        },
    )
    validate_experiment_archive(experiment_dir)


def run_position_sizing_experiment(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> dict[str, object]:
    """Execute and freeze the committed 0829_EX01 protocol exactly once."""
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    holdout_accessed = False
    try:
        validate_position_sizing_protocol(protocol)
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
        _, selection_applied = _apply_champion(selection_data, baseline)
        identity = _identity_audit(baseline, selection_applied, protocol)
        _write_json(artifacts / "identity_audit.json", identity)
        best, _, selection = _selection(
            selection_data,
            baseline,
            selection_applied,
            protocol,
            artifacts,
            fee_rate,
            init_cash,
        )
        frozen, frozen_digest = _freeze_challenger(
            experiment_dir.name,
            baseline,
            best,
            selection,
            protocol,
            artifacts,
        )
        holdout_accessed = True
        holdout = _holdout(
            raw_dir,
            baseline,
            best,
            protocol,
            frozen_digest,
            artifacts,
            fee_rate,
            init_cash,
        )
        _write_result_docs(experiment_dir, selection, holdout, best, execution_commit)
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
            "best_candidate": best.candidate_id,
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
