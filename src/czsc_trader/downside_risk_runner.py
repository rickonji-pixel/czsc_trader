"""Preregistered downside-risk position optimization for 588080.SH."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from itertools import product
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
from .downside_risk import (
    DownsideRiskSpec,
    build_downside_risk_events,
    downside_risk_target,
    downside_stress,
    downside_stress_threshold,
    downside_volatility,
)
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .four_layer_runner import _PeriodEvaluator, _target_digest, _write_json
from .objectives import TARGET_PERIODS
from .position_sizing_runner import (
    EXPECTED_CHAMPION,
    _apply_champion,
    _identity_audit,
)


LOOKBACKS = (10, 20, 40)
STRESS_QUANTILES = (0.7, 0.8, 0.9)
PRESSURE_POSITIONS = (0.25, 0.5, 0.75)
ANNUAL_WINDOWS = ("2021", "2022", "2023", "2024", "2025")
DIAGNOSTIC_WINDOWS = ("2026Q1", "2026H1", "2026M1-M8")
EXPECTED_ELIGIBILITY = (
    "full_period_challenger_return_greater_than_or_equal_to_champion",
    "full_period_challenger_max_drawdown_strictly_greater_than_champion",
)
EXPECTED_RANKING = (
    "full_period_max_drawdown_improvement_desc",
    "worst_annual_return_delta_desc",
    "full_period_trade_count_asc",
    "candidate_id_asc",
)


def build_downside_risk_specs(
    protocol: Mapping[str, object],
) -> tuple[DownsideRiskSpec, ...]:
    """Build the exact preregistered 27-candidate grid."""
    return tuple(
        DownsideRiskSpec(int(lookback), float(quantile), float(position))
        for lookback, quantile, position in product(
            protocol["downside_lookbacks"],  # type: ignore[arg-type]
            protocol["stress_quantiles"],  # type: ignore[arg-type]
            protocol["pressure_positions"],  # type: ignore[arg-type]
        )
    )


def validate_downside_risk_protocol(protocol: Mapping[str, object]) -> None:
    """Reject any drift from the committed 0830_EX01 protocol."""
    expected_scalars = {
        "schema_version": 1,
        "handler": "downside_risk_position_optimization",
        "experiment_type": "downside_risk_position_optimization",
        "status": "PRE_REGISTERED",
        "symbol": "588080.SH",
        "asset_type": "etf",
        "factor_policy": "exact_active_baseline_signals_weights_normalization_and_state_machine",
        "position_policy": "champion_binary_gate_times_downside_risk_multiplier",
        "warmup_start": "2020-01-01",
        "selection_start": "2021-01-01",
        "selection_end": "2025-12-31",
        "selection_full_window": "2021-2025FULL",
        "downside_formula": "sqrt(252 * rolling_mean(min(log_return, 0)^2, lookback))",
        "threshold_policy": "strictly_greater_than_quantile_of_prior_252_valid_downside_volatility_values",
        "stress_history": 252,
        "candidate_count": 27,
        "no_eligible_policy": "fail_without_freeze_or_2026_access",
        "holdout_main_window": "2026FULL",
        "holdout_cutoff": "2026-08-28",
        "fee_rate": 0.0005,
        "init_cash": 1_000_000.0,
        "holdout_access_before_freeze": False,
        "pass_rule": "2026FULL_challenger_return_greater_than_or_equal_to_champion_and_max_drawdown_strictly_greater_than_champion",
        "observed_data_disclosure": (
            "2026 data through 2026-08-28 was already observed before this "
            "experiment; it is used only after 2021-2025 selection and freeze "
            "as the final historical backtest"
        ),
    }
    for key, expected in expected_scalars.items():
        if protocol.get(key) != expected:
            raise ValueError(f"downside-risk protocol differs for {key}")
    experiment_id = protocol.get("experiment_id")
    if experiment_id == "0830_EX01":
        if protocol.get("retry_of") is not None:
            raise ValueError("original downside-risk protocol cannot declare retry_of")
    elif experiment_id == "0830_EX02":
        if protocol.get("retry_of") != "0830_EX01":
            raise ValueError("downside-risk retry identity differs")
    else:
        raise ValueError("downside-risk experiment identity differs")
    if protocol.get("champion") != EXPECTED_CHAMPION:
        raise ValueError("downside-risk champion identity differs")
    expected_sequences = {
        "annual_windows": ANNUAL_WINDOWS,
        "downside_lookbacks": LOOKBACKS,
        "stress_quantiles": STRESS_QUANTILES,
        "pressure_positions": PRESSURE_POSITIONS,
        "selection_eligibility": EXPECTED_ELIGIBILITY,
        "candidate_ranking": EXPECTED_RANKING,
        "holdout_diagnostic_windows": DIAGNOSTIC_WINDOWS,
    }
    for key, expected in expected_sequences.items():
        if tuple(protocol.get(key, ())) != expected:
            raise ValueError(f"downside-risk protocol differs for {key}")
    specs = build_downside_risk_specs(protocol)
    if len(specs) != 27 or len({spec.candidate_id for spec in specs}) != 27:
        raise ValueError("downside-risk protocol must build 27 unique candidates")


def rank_eligible_candidates(rows: pd.DataFrame) -> pd.DataFrame:
    """Filter hard constraints before applying the preregistered stable ranking."""
    eligible = rows.loc[
        rows["full_challenger_return"].ge(rows["full_champion_return"])
        & rows["full_challenger_max_drawdown"].gt(
            rows["full_champion_max_drawdown"]
        )
    ].copy()
    return eligible.sort_values(
        [
            "max_drawdown_improvement",
            "worst_annual_return_delta",
            "full_trade_count",
            "candidate_id",
        ],
        ascending=[False, False, True, True],
        kind="stable",
    ).reset_index(drop=True)


def downside_risk_2026_pass(
    champion: Mapping[str, object],
    challenger: Mapping[str, object],
) -> bool:
    """Apply the preregistered 2026FULL return constraint and drawdown objective."""
    return (
        float(challenger["strategy_return"]) >= float(champion["strategy_return"])
        and float(challenger["max_drawdown"]) > float(champion["max_drawdown"])
    )


def _selection_periods() -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    periods = {
        "2021-2025FULL": (pd.Timestamp("2021-01-01"), pd.Timestamp("2025-12-31"))
    }
    periods.update(
        {
            str(year): (pd.Timestamp(year, 1, 1), pd.Timestamp(year, 12, 31))
            for year in range(2021, 2026)
        }
    )
    return periods


def _holdout_periods(cutoff: pd.Timestamp) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    return {
        "2026FULL": (pd.Timestamp("2026-01-01"), cutoff),
        **TARGET_PERIODS,
    }


def _candidate_path(
    daily: pd.DataFrame,
    baseline_target: pd.Series,
    spec: DownsideRiskSpec,
    history: int,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    prices = daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="dt")
    prices = prices.sort_index()
    risk = downside_volatility(
        prices["close"].reindex(baseline_target.index), spec.lookback
    )
    threshold = downside_stress_threshold(
        risk, spec.stress_quantile, history=history
    )
    stress = downside_stress(risk, spec.stress_quantile, history=history)
    target = downside_risk_target(
        baseline_target, stress, pressure_position=spec.pressure_position
    )
    return target, risk, threshold, stress


def _selection(
    data: Any,
    baseline: ResolvedBaseline,
    applied: Any,
    protocol: Mapping[str, object],
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> tuple[DownsideRiskSpec | None, pd.Series | None, dict[str, object]]:
    periods = _selection_periods()
    evaluator = _PeriodEvaluator(data.daily, periods, fee_rate, init_cash)
    champion_metrics = evaluator.evaluate(applied.target_position)
    rows: list[dict[str, object]] = []
    targets: dict[str, pd.Series] = {}
    for spec in build_downside_risk_specs(protocol):
        target, _, _, _ = _candidate_path(
            data.daily,
            applied.target_position,
            spec,
            int(protocol["stress_history"]),
        )
        targets[spec.candidate_id] = target
        metrics = evaluator.evaluate(target)
        full_champion = champion_metrics["2021-2025FULL"]
        full_challenger = metrics["2021-2025FULL"]
        annual_deltas = [
            float(metrics[name]["strategy_return"])
            - float(champion_metrics[name]["strategy_return"])
            for name in ANNUAL_WINDOWS
        ]
        row: dict[str, object] = {
            "candidate_id": spec.candidate_id,
            "lookback": spec.lookback,
            "stress_quantile": spec.stress_quantile,
            "pressure_position": spec.pressure_position,
            "full_champion_return": full_champion["strategy_return"],
            "full_challenger_return": full_challenger["strategy_return"],
            "full_return_delta": float(full_challenger["strategy_return"])
            - float(full_champion["strategy_return"]),
            "full_champion_max_drawdown": full_champion["max_drawdown"],
            "full_challenger_max_drawdown": full_challenger["max_drawdown"],
            "max_drawdown_improvement": float(full_challenger["max_drawdown"])
            - float(full_champion["max_drawdown"]),
            "worst_annual_return_delta": min(annual_deltas),
            "median_annual_return_delta": float(np.median(annual_deltas)),
            "full_champion_sharpe": full_champion["sharpe"],
            "full_challenger_sharpe": full_challenger["sharpe"],
            "full_champion_exposure": full_champion["exposure"],
            "full_challenger_exposure": full_challenger["exposure"],
            "full_trade_count": int(full_challenger["trade_count"]),
        }
        for name, delta in zip(ANNUAL_WINDOWS, annual_deltas, strict=True):
            row[f"{name}_champion_return"] = champion_metrics[name]["strategy_return"]
            row[f"{name}_challenger_return"] = metrics[name]["strategy_return"]
            row[f"{name}_return_delta"] = delta
            row[f"{name}_challenger_max_drawdown"] = metrics[name]["max_drawdown"]
            row[f"{name}_challenger_sharpe"] = metrics[name]["sharpe"]
        row["eligible"] = (
            float(row["full_challenger_return"])
            >= float(row["full_champion_return"])
            and float(row["full_challenger_max_drawdown"])
            > float(row["full_champion_max_drawdown"])
        )
        rows.append(row)
    results = pd.DataFrame(rows).sort_values("candidate_id", kind="stable")
    ranked = rank_eligible_candidates(results)
    eligible_rank = {
        str(candidate_id): rank
        for rank, candidate_id in enumerate(ranked["candidate_id"], start=1)
    }
    results.insert(
        0,
        "eligible_rank",
        results["candidate_id"].map(eligible_rank).astype("Int64"),
    )
    results.to_csv(
        artifacts / "candidate_results.csv", index=False, encoding="utf-8-sig"
    )
    selection: dict[str, object] = {
        "candidate_count": len(results),
        "eligible_count": len(ranked),
        "selection_start": str(protocol["selection_start"]),
        "selection_end": str(protocol["selection_end"]),
        "selection_full_window": str(protocol["selection_full_window"]),
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
    }
    if ranked.empty:
        selection.update(
            {
                "status": "FAIL",
                "reason": "no candidate satisfied return non-decline and strict drawdown improvement",
                "selected_candidate": None,
            }
        )
        _write_json(artifacts / "selection_metrics.json", selection)
        return None, None, selection
    best_id = str(ranked.iloc[0]["candidate_id"])
    best = next(
        spec
        for spec in build_downside_risk_specs(protocol)
        if spec.candidate_id == best_id
    )
    best_target = targets[best_id]
    selection.update(
        {
            "status": "PASS",
            "selected_candidate": best_id,
            "max_drawdown_improvement": float(
                ranked.iloc[0]["max_drawdown_improvement"]
            ),
            "full_return_delta": float(ranked.iloc[0]["full_return_delta"]),
            "worst_annual_return_delta": float(
                ranked.iloc[0]["worst_annual_return_delta"]
            ),
            "selection_target_sha256": _target_digest(best_target),
        }
    )
    _write_json(artifacts / "selection_metrics.json", selection)
    return best, best_target, selection


def _freeze_challenger(
    experiment_id: str,
    baseline: ResolvedBaseline,
    spec: DownsideRiskSpec,
    selection: Mapping[str, object],
    protocol: Mapping[str, object],
    artifacts: Path,
) -> tuple[dict[str, object], str]:
    frozen = {
        "schema_version": 1,
        "experiment": experiment_id,
        "sample_end": str(protocol["selection_end"]),
        "champion": EXPECTED_CHAMPION,
        "factor_policy": protocol["factor_policy"],
        "position_policy": protocol["position_policy"],
        "factor_names": list(baseline.factor_names),
        "weights": dict(
            zip(baseline.factor_names, baseline.factor_weights, strict=True)
        ),
        "lookback": spec.lookback,
        "stress_quantile": spec.stress_quantile,
        "pressure_position": spec.pressure_position,
        "stress_history": int(protocol["stress_history"]),
        "downside_formula": protocol["downside_formula"],
        "threshold_policy": protocol["threshold_policy"],
        "candidate_id": spec.candidate_id,
        "fee_rate": float(protocol["fee_rate"]),
        "selection_target_sha256": selection["selection_target_sha256"],
    }
    path = artifacts / "frozen_challenger.json"
    _write_json(path, frozen)
    return frozen, sha256(path.read_bytes()).hexdigest()


def _holdout(
    raw_dir: Path,
    baseline: ResolvedBaseline,
    spec: DownsideRiskSpec,
    protocol: Mapping[str, object],
    frozen_digest: str,
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> dict[str, object]:
    cutoff = pd.Timestamp(str(protocol["holdout_cutoff"]))
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=cutoff)
    _, applied = _apply_champion(data, baseline)
    target, risk, threshold, stress = _candidate_path(
        data.daily,
        applied.target_position,
        spec,
        int(protocol["stress_history"]),
    )
    events = build_downside_risk_events(
        target,
        applied.target_position,
        applied.scores,
        risk,
        threshold,
        stress,
        spec,
    )
    audit_frame = pd.DataFrame(
        {
            "factor_score": applied.scores,
            "downside_volatility": risk,
            "stress_threshold": threshold,
            "stress": stress,
        },
        index=applied.scores.index,
    )
    periods = _holdout_periods(cutoff)
    champion_results = run_period_backtests(
        data.daily,
        applied.target_position,
        periods,
        fee_rate=fee_rate,
        init_cash=init_cash,
    )
    challenger_results = run_period_backtests(
        data.daily,
        target,
        periods,
        fee_rate=fee_rate,
        init_cash=init_cash,
        factor_events=events,
        factor_frame=audit_frame,
    )
    windows: dict[str, dict[str, object]] = {}
    for name in periods:
        champion = champion_results[name].metrics
        challenger = challenger_results[name].metrics
        windows[name] = {
            "champion_return": float(champion["strategy_return"]),
            "challenger_return": float(challenger["strategy_return"]),
            "return_delta": float(challenger["strategy_return"])
            - float(champion["strategy_return"]),
            "champion_max_drawdown": float(champion["max_drawdown"]),
            "challenger_max_drawdown": float(challenger["max_drawdown"]),
            "max_drawdown_improvement": float(challenger["max_drawdown"])
            - float(champion["max_drawdown"]),
            "champion_sharpe": float(champion["sharpe"]),
            "challenger_sharpe": float(challenger["sharpe"]),
            "champion_exposure": float(champion["exposure"]),
            "challenger_exposure": float(challenger["exposure"]),
            "champion_trade_count": int(champion["trade_count"]),
            "challenger_trade_count": int(challenger["trade_count"]),
        }
    main_pass = downside_risk_2026_pass(
        champion_results["2026FULL"].metrics,
        challenger_results["2026FULL"].metrics,
    )
    windows["2026FULL"]["return_constraint_pass"] = (
        float(windows["2026FULL"]["challenger_return"])
        >= float(windows["2026FULL"]["champion_return"])
    )
    windows["2026FULL"]["drawdown_objective_pass"] = (
        float(windows["2026FULL"]["challenger_max_drawdown"])
        > float(windows["2026FULL"]["champion_max_drawdown"])
    )
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
            "downside_volatility": risk.to_numpy(),
            "stress_threshold": threshold.to_numpy(),
            "stress": stress.to_numpy(),
            "baseline_target_position": applied.target_position.to_numpy(),
            "challenger_target_position": target.to_numpy(),
            "baseline_execution_position": applied.target_position.shift(1).to_numpy(),
            "challenger_execution_position": target.shift(1).to_numpy(),
        }
    ).to_csv(artifacts / "position_path.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        {
            "signal_date": target.index,
            "downside_volatility": risk.to_numpy(),
            "stress_threshold": threshold.to_numpy(),
            "stress": stress.to_numpy(),
        }
    ).to_csv(artifacts / "risk_state_path.csv", index=False, encoding="utf-8-sig")
    payload = {
        "status": "PASS" if main_pass else "FAIL",
        "main_window": "2026FULL",
        "windows": windows,
        "transition_counts": {
            str(name): int(count)
            for name, count in events["event_type"].value_counts().items()
        },
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
    spec: DownsideRiskSpec,
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
        f"- 候选数：{selection['candidate_count']}\n"
        f"- 合格候选数：{selection['eligible_count']}\n"
        f"- 冻结候选：`{spec.candidate_id}`\n"
        f"- 冻结参数：L={spec.lookback}，Q={spec.stress_quantile:.2f}，P={spec.pressure_position:.2f}\n"
        "- 冻结前访问2026：否\n"
        "- 冻结后访问2026：是\n"
        f"- 因果审计：`{holdout['audit']['status']}`\n",
        encoding="utf-8",
    )
    main = holdout["windows"]["2026FULL"]
    lines = [
        "# 研究结论",
        "",
        f"0830_EX01下行风险仓位优化结果为 **{holdout['status']}**。",
        "",
        "| 窗口 | 冠军收益 | 挑战者收益 | 收益增量 | 冠军最大回撤 | 挑战者最大回撤 | 回撤改善 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in holdout["windows"].items():
        lines.append(
            f"| {name} | {float(row['champion_return']):.4%} | "
            f"{float(row['challenger_return']):.4%} | {float(row['return_delta']):+.4%} | "
            f"{float(row['champion_max_drawdown']):.4%} | "
            f"{float(row['challenger_max_drawdown']):.4%} | "
            f"{float(row['max_drawdown_improvement']):+.4%} |"
        )
    lines.extend(
        [
            "",
            "## 判定",
            "",
            f"- 冻结候选：`{spec.candidate_id}`（L={spec.lookback}，Q={spec.stress_quantile:.2f}，P={spec.pressure_position:.2f}）。",
            f"- 2026FULL收益约束：{'PASS' if main['return_constraint_pass'] else 'FAIL'}。",
            f"- 2026FULL回撤目标：{'PASS' if main['drawdown_objective_pass'] else 'FAIL'}。",
            "- 两项必须同时满足；本轮不自动修改活动基线。",
            "",
        ]
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def _finalize_no_eligible(
    experiment_dir: Path,
    protocol: Mapping[str, object],
    selection: Mapping[str, object],
    *,
    execution_commit: str,
) -> None:
    artifacts = experiment_dir / "artifacts"
    if not (artifacts / "selection_metrics.json").exists():
        _write_json(artifacts / "selection_metrics.json", dict(selection))
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        "- 状态：正式选择完成（`FAIL`）\n"
        f"- 执行提交：`{execution_commit}`\n"
        f"- 候选数：{selection['candidate_count']}\n"
        "- 合格候选数：0\n"
        "- 冻结挑战者：无\n"
        "- 访问2026：否\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "# 研究结论\n\n"
        f"{experiment_dir.name}结果为 **FAIL**。27项候选中没有方案同时满足选择期收益不下降和最大回撤严格改善，因此没有冻结挑战者，也没有访问2026。活动基线未改变。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": "2026-08-30",
            "status": "FAIL",
            "symbol": str(protocol["symbol"]),
            "asset_type": str(protocol["asset_type"]),
            "champion": protocol["champion"],
            "visible_sample_end": str(protocol["selection_end"]),
            "holdout_accessed": False,
            "failure_stage": "selection_no_eligible_candidate",
        },
    )
    validate_experiment_archive(experiment_dir)


def _finalize_error(
    experiment_dir: Path,
    protocol: Mapping[str, object],
    exc: Exception,
    execution_commit: str,
    *,
    holdout_accessed: bool,
) -> None:
    artifacts = experiment_dir / "artifacts"
    _write_json(
        artifacts / "error.json",
        {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "holdout_accessed": holdout_accessed,
        },
    )
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        "- 状态：正式执行异常（`ERROR`）\n"
        f"- 执行提交：`{execution_commit}`\n"
        f"- 异常类型：`{type(exc).__name__}`\n"
        f"- 异常信息：{str(exc)}\n"
        f"- 访问2026：{'是' if holdout_accessed else '否'}\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "# 研究结论\n\n"
        "本轮状态为 **ERROR**，没有产生可用于仓位目标判断的PASS或FAIL结果。活动基线未改变。错误详情见`artifacts/error.json`。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": "2026-08-30",
            "status": "ERROR",
            "symbol": str(protocol.get("symbol", "588080.SH")),
            "asset_type": str(protocol.get("asset_type", "etf")),
            "champion": protocol.get("champion", EXPECTED_CHAMPION),
            "visible_sample_end": str(protocol.get("selection_end", "2025-12-31")),
            "holdout_accessed": holdout_accessed,
        },
    )
    validate_experiment_archive(experiment_dir)


def run_downside_risk_experiment(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> dict[str, object]:
    """Execute a committed downside-risk selection, freeze, and final backtest."""
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    holdout_accessed = False
    try:
        validate_downside_risk_protocol(protocol)
        if fee_rate != float(protocol["fee_rate"]) or init_cash != float(
            protocol["init_cash"]
        ):
            raise ValueError("runtime fee or initial cash differs from preregistration")
        baseline = resolve_baseline(
            Path(baseline_root), str(protocol["champion"]["version"]), symbol="588080.SH"
        )
        selection_data = load_market_data(
            raw_dir,
            "588080.SH",
            "etf",
            cutoff=pd.Timestamp(str(protocol["selection_end"])),
        )
        _, applied = _apply_champion(selection_data, baseline)
        identity = _identity_audit(baseline, applied, protocol)
        _write_json(artifacts / "identity_audit.json", identity)
        best, _, selection = _selection(
            selection_data,
            baseline,
            applied,
            protocol,
            artifacts,
            fee_rate,
            init_cash,
        )
        if best is None:
            _finalize_no_eligible(
                experiment_dir,
                protocol,
                selection,
                execution_commit=execution_commit,
            )
            return {
                "status": "FAIL",
                "stage": "selection",
                "selection": selection,
                "holdout_accessed": False,
                "experiment_dir": str(experiment_dir),
            }
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
                "date": "2026-08-30",
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
            "stage": "holdout",
            "selected_candidate": best.candidate_id,
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
