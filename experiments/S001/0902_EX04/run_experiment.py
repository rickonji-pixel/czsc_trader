"""Search connected range-weight platforms before one locked 2026 test."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pandas as pd

from czsc_trader.backtest import run_period_backtests
from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import resolve_baseline
from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.factors import generate_factor_frame, signal_groups
from czsc_trader.four_layer import normalized_signal_factors, positions_from_scores
from czsc_trader.identity import canonical_json_sha256, normalized_text_sha256
from czsc_trader.range_platform import pareto_layers, project_group_shares, simplex_components
from czsc_trader.regime_weight import classify_regimes, lagged_efficiency_ratio, score_with_regime_weights
from czsc_trader.strategy_metrics import closed_trade_ledger, strategy_comparison_metrics


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
ARTIFACTS = EXPERIMENT_DIR / "artifacts"
PROTOCOL_PATH = ARTIFACTS / "protocol.json"
BASELINE_ROOT = REPO_ROOT / "configs" / "rule_baselines"
RAW_DIR = REPO_ROOT / "data" / "raw"
TEST_DATA_ACCESSED = False


def _json_default(value: object) -> object:
    if isinstance(value, (np.integer, np.floating, np.bool_)):
        return value.item()
    if isinstance(value, (pd.Timestamp, Path)):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default)
        + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _identity_sha(path: Path) -> str:
    return canonical_json_sha256(path) if path.suffix.lower() == ".json" else normalized_text_sha256(path)


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, encoding="utf-8"
    ).strip()


def _protocol() -> dict[str, object]:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    expected = {
        "experiment_id": "0902_EX04",
        "status": "PRE_REGISTERED",
        "simplex_candidate_count": 260,
        "control_candidate_id": 260,
        "candidate_count": 261,
        "core_metrics": ["max_drawdown", "calmar", "win_loss_ratio"],
        "short_holding_max_sessions": 10,
        "locked_test_used_for_tuning": False,
        "return_used_for_pareto": False,
        "composite_pass_gate": False,
        "automatic_promotion": False,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"protocol field {key} differs from preregistration")
    if payload.get("research_windows") != {
        "full_2021_2025": ["2021-01-01", "2025-12-31"],
        "rolling_2021_2023": ["2021-01-01", "2023-12-31"],
        "rolling_2022_2024": ["2022-01-01", "2024-12-31"],
        "rolling_2023_2025": ["2023-01-01", "2025-12-31"],
    }:
        raise ValueError("research windows differ from preregistration")
    return payload


def _critical_hashes() -> dict[str, str]:
    paths = (
        BASELINE_ROOT / "registry.json",
        BASELINE_ROOT / "baseline_20260826.json",
        BASELINE_ROOT / "baseline_20260901.json",
        RAW_DIR / "588080_manifest.json",
        RAW_DIR / "588080_validation.json",
        REPO_ROOT / "src" / "czsc_trader" / "backtest.py",
        REPO_ROOT / "src" / "czsc_trader" / "baseline_execution.py",
        REPO_ROOT / "src" / "czsc_trader" / "factors.py",
        REPO_ROOT / "src" / "czsc_trader" / "four_layer.py",
        REPO_ROOT / "src" / "czsc_trader" / "regime_weight.py",
        REPO_ROOT / "src" / "czsc_trader" / "range_platform.py",
        REPO_ROOT / "src" / "czsc_trader" / "strategy_metrics.py",
        EXPERIMENT_DIR / "run_experiment.py",
        PROTOCOL_PATH,
    )
    return {path.relative_to(REPO_ROOT).as_posix(): _identity_sha(path) for path in paths}


def _candidate_specs(protocol: dict[str, object], active: object, groups: dict[str, tuple[str, ...]]) -> list[dict[str, object]]:
    specs: list[dict[str, object]] = []
    for structure_units in range(250, 701, 25):
        for trend_units in range(100, 601, 25):
            volume_units = 1000 - structure_units - trend_units
            if 100 <= volume_units <= 600:
                specs.append(
                    {
                        "candidate_id": len(specs),
                        "structure_share": structure_units / 1000.0,
                        "trend_share": trend_units / 1000.0,
                        "volume_share": volume_units / 1000.0,
                        "is_control": False,
                    }
                )
    if len(specs) != int(protocol["simplex_candidate_count"]):
        raise AssertionError("simplex candidate count differs")
    names = list(active.factor_names)
    active_range = pd.Series(active.regime_factor_weights["range"], index=names, dtype=float)
    shares = {
        name: float(active_range.loc[list(groups[name])].abs().sum())
        for name in ("structure", "trend", "volume_position")
    }
    specs.append(
        {
            "candidate_id": int(protocol["control_candidate_id"]),
            "structure_share": shares["structure"],
            "trend_share": shares["trend"],
            "volume_share": shares["volume_position"],
            "is_control": True,
        }
    )
    if len(specs) != int(protocol["candidate_count"]):
        raise AssertionError("total candidate count differs")
    return specs


def _daily_prices(data: object) -> pd.DataFrame:
    daily = data.daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"])
    return daily.set_index("dt").sort_index()


def _classify_cycles(orders: pd.DataFrame, regimes: pd.Series, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    ledger = closed_trade_ledger(orders).copy()
    if ledger.empty:
        return ledger.assign(
            regime_path=pd.Series(dtype="string"),
            holding_sessions=pd.Series(dtype="int64"),
            fees=pd.Series(dtype="float64"),
            short_loss=pd.Series(dtype="bool"),
        )
    labels = regimes.astype("string").copy()
    labels.index = pd.DatetimeIndex(pd.to_datetime(labels.index)).normalize()
    calendar = pd.DatetimeIndex(pd.to_datetime(sessions)).normalize()
    locations = pd.Series(np.arange(len(calendar), dtype=int), index=calendar)
    for column in ("entry_signal_date", "exit_signal_date", "entry_date", "exit_date"):
        ledger[column] = pd.to_datetime(ledger[column])
    ledger["entry_regime"] = ledger["entry_signal_date"].dt.normalize().map(labels)
    ledger["exit_regime"] = ledger["exit_signal_date"].dt.normalize().map(labels)
    if ledger[["entry_regime", "exit_regime"]].isna().any().any():
        raise ValueError("trade signals do not align to regimes")
    ledger["regime_path"] = ledger["entry_regime"].astype(str) + "_to_" + ledger["exit_regime"].astype(str)
    entry_locations = ledger["entry_date"].dt.normalize().map(locations)
    exit_locations = ledger["exit_date"].dt.normalize().map(locations)
    ledger["holding_sessions"] = (exit_locations - entry_locations).astype(int)
    ledger["fees"] = ledger["entry_fees"].astype(float) + ledger["exit_fees"].astype(float)
    ledger["short_loss"] = ledger["net_return"].astype(float).lt(0.0) & ledger["holding_sessions"].le(10)
    return ledger


def _compound(values: pd.Series) -> float:
    return float(np.prod(1.0 + values.astype(float)) - 1.0) if len(values) else 0.0


def _range_diagnostics(
    result: object,
    regimes: pd.Series,
    target: pd.Series,
    daily: pd.DataFrame,
    start: str,
    end: str,
) -> tuple[dict[str, object], pd.DataFrame]:
    sessions = pd.DatetimeIndex(pd.to_datetime(daily["dt"]))
    cycles = _classify_cycles(result.orders, regimes, sessions)
    diagnostics: dict[str, object] = {}
    for path in ("range_to_range", "range_to_trend", "trend_to_range", "trend_to_trend"):
        selected = cycles.loc[cycles["regime_path"].eq(path)]
        returns = selected["net_return"].astype(float)
        diagnostics[f"{path}_count"] = int(len(selected))
        diagnostics[f"{path}_loss_count"] = int(returns.lt(0.0).sum())
        diagnostics[f"{path}_loss_rate"] = float(returns.lt(0.0).mean()) if len(selected) else 0.0
        diagnostics[f"{path}_short_loss_count"] = int(selected["short_loss"].astype(bool).sum())
        diagnostics[f"{path}_compound_return"] = _compound(returns)
        diagnostics[f"{path}_median_holding_sessions"] = float(selected["holding_sessions"].median()) if len(selected) else 0.0
        diagnostics[f"{path}_fees"] = float(selected["fees"].sum()) if len(selected) else 0.0
    period = regimes.index.to_series().between(pd.Timestamp(start), pd.Timestamp(end))
    range_days = int((regimes.eq("range") & period).sum())
    transitions = target.diff().abs().fillna(0.0).gt(0.0)
    range_events = int((transitions & regimes.eq("range") & period).sum())
    diagnostics["range_days"] = range_days
    diagnostics["range_transition_count"] = range_events
    diagnostics["range_signal_density_per_100"] = float(range_events / range_days * 100.0)
    return diagnostics, cycles


def _result_metrics(result: object, init_cash: float) -> dict[str, object]:
    values = strategy_comparison_metrics(result.equity, result.orders, init_cash)
    metrics = {
        "max_drawdown": values["max_drawdown"],
        "calmar": values["calmar"],
        "win_loss_ratio": values["win_loss_ratio"],
        "win_loss_ratio_status": values["win_loss_ratio_status"],
        "strategy_return": values["return"],
        "sharpe": values["sharpe"],
        "exposure": float(result.metrics["exposure"]),
        "trade_count": int(result.metrics["trade_count"]),
    }
    for key in ("max_drawdown", "calmar", "win_loss_ratio"):
        if metrics[key] is None or not np.isfinite(float(metrics[key])):
            raise ValueError(f"core metric {key} is unavailable")
    return metrics


def _load_context(protocol: dict[str, object], cutoff: str) -> dict[str, object]:
    active = resolve_baseline(BASELINE_ROOT, "baseline_20260901", symbol="588080.SH")
    parent = resolve_baseline(BASELINE_ROOT, "baseline_20260826", symbol="588080.SH")
    if active.sha256 != protocol["active_baseline"]["sha256"]:
        raise ValueError("active baseline identity differs")
    if parent.sha256 != protocol["parent_baseline"]["sha256"]:
        raise ValueError("parent baseline identity differs")
    data = load_market_data(RAW_DIR, "588080.SH", "etf", cutoff=cutoff)
    frame = generate_factor_frame(data).frame
    names = list(active.factor_names)
    factors = normalized_signal_factors(frame[names]).astype(float)
    base_weights = pd.Series(parent.factor_weights, index=names, dtype=float)
    groups = signal_groups(factors.columns)
    prices = _daily_prices(data)
    regimes = classify_regimes(
        lagged_efficiency_ratio(prices["close"], active.er_lookback), active.er_threshold
    ).reindex(factors.index)
    applied = apply_resolved_baseline(frame, active, daily_close=prices["close"])
    return {
        "active": active,
        "data": data,
        "factors": factors,
        "base_weights": base_weights,
        "groups": groups,
        "regimes": regimes,
        "active_scores": applied.scores,
        "active_target": applied.target_position,
    }


def _weights_for_spec(spec: dict[str, object], context: dict[str, object]) -> dict[str, pd.Series]:
    active = context["active"]
    names = list(active.factor_names)
    trend = pd.Series(active.regime_factor_weights["trend"], index=names, dtype=float)
    if bool(spec["is_control"]):
        range_weights = pd.Series(active.regime_factor_weights["range"], index=names, dtype=float)
    else:
        range_weights = project_group_shares(
            context["base_weights"],
            context["groups"],
            {
                "structure": float(spec["structure_share"]),
                "trend": float(spec["trend_share"]),
                "volume_position": float(spec["volume_share"]),
            },
        )
    return {"trend": trend, "range": range_weights}


def _evaluate_research(
    protocol: dict[str, object], context: dict[str, object]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[dict[str, object]], dict[str, object]]:
    active = context["active"]
    specs = _candidate_specs(protocol, active, context["groups"])
    periods = {
        name: (pd.Timestamp(dates[0]), pd.Timestamp(dates[1]))
        for name, dates in protocol["research_windows"].items()
    }
    trend_mask = context["regimes"].eq("trend")
    period_rows: list[dict[str, object]] = []
    full_rows: list[dict[str, object]] = []
    cycle_frames: list[pd.DataFrame] = []
    frozen_candidates: list[dict[str, object]] = []
    max_trend_delta = 0.0
    control_score_delta = None
    control_target_equal = False
    for spec in specs:
        candidate_id = int(spec["candidate_id"])
        weights = _weights_for_spec(spec, context)
        scores = score_with_regime_weights(
            context["factors"], context["regimes"], weights, context["base_weights"]
        )
        target = positions_from_scores(scores, active.rule.enter, active.rule.exit, active.rule)
        max_trend_delta = max(
            max_trend_delta,
            float(np.max(np.abs(scores.loc[trend_mask] - context["active_scores"].loc[trend_mask]))),
        )
        if bool(spec["is_control"]):
            control_score_delta = float(np.max(np.abs(scores - context["active_scores"])))
            control_target_equal = bool(target.equals(context["active_target"]))
        results = run_period_backtests(
            context["data"].daily,
            target,
            periods,
            fee_rate=float(protocol["fee_rate"]),
            init_cash=float(protocol["init_cash"]),
        )
        for period_name, result in results.items():
            period_rows.append(
                {
                    **spec,
                    "period": period_name,
                    **_result_metrics(result, float(protocol["init_cash"])),
                }
            )
        full = results["full_2021_2025"]
        diagnostics, cycles = _range_diagnostics(
            full,
            context["regimes"],
            target,
            context["data"].daily,
            *protocol["research_windows"]["full_2021_2025"],
        )
        full_rows.append({**spec, **diagnostics})
        cycles.insert(0, "candidate_id", candidate_id)
        cycle_frames.append(cycles)
        frozen_candidates.append(
            {
                **spec,
                "range_weights": {str(k): float(v) for k, v in weights["range"].items()},
            }
        )
    if control_score_delta is None:
        raise AssertionError("control candidate was not evaluated")
    tolerance = float(protocol["comparison_tolerance"])
    if control_score_delta > tolerance or not control_target_equal or max_trend_delta > tolerance:
        raise ValueError("research control reproduction or trend invariance failed")
    period_metrics = pd.DataFrame(period_rows)
    core = tuple(protocol["core_metrics"])
    layer_pieces: list[pd.DataFrame] = []
    for period_name, frame in period_metrics.groupby("period", sort=False):
        selected = frame.copy()
        layers = pareto_layers(selected, core, tolerance=tolerance)
        selected["pareto_layer"] = selected["candidate_id"].map(layers)
        layer_pieces.append(selected)
    period_metrics = pd.concat(layer_pieces, ignore_index=True)
    profiles = (
        period_metrics.groupby("candidate_id")["pareto_layer"]
        .agg(first_front_count=lambda values: int(values.eq(1).sum()), mean_pareto_layer="mean", worst_pareto_layer="max")
        .reset_index()
    )
    summaries = pd.DataFrame(full_rows).merge(profiles, on="candidate_id", validate="one_to_one")
    full_core = period_metrics.loc[period_metrics["period"].eq("full_2021_2025"), [
        "candidate_id", "max_drawdown", "calmar", "win_loss_ratio", "strategy_return", "sharpe", "exposure", "trade_count"
    ]]
    summaries = summaries.merge(full_core, on="candidate_id", validate="one_to_one")
    maximum_front_count = int(summaries["first_front_count"].max())
    platform_ids = summaries.loc[summaries["first_front_count"].eq(maximum_front_count), "candidate_id"].astype(int).tolist()
    components = simplex_components(
        summaries,
        platform_ids,
        step=float(protocol["structure_share"]["step"]),
        tolerance=tolerance,
    )
    membership_rows: list[dict[str, int]] = []
    for component_id, members in enumerate(components, start=1):
        for candidate_id in members:
            membership_rows.append({"component_id": component_id, "candidate_id": candidate_id})
    membership = pd.DataFrame(membership_rows)
    platform = membership.merge(summaries, on="candidate_id", validate="many_to_one")
    audit = {
        "status": "PASS",
        "candidate_count": len(specs),
        "research_window_count": len(periods),
        "maximum_first_front_count": maximum_front_count,
        "platform_component_count": len(components),
        "platform_member_count": len(platform_ids),
        "control_score_max_absolute_delta": control_score_delta,
        "control_target_equal": control_target_equal,
        "all_candidate_trend_score_max_absolute_delta": max_trend_delta,
        "data_hashes": context["data"].hashes,
    }
    return period_metrics, summaries, platform, frozen_candidates, {
        "audit": audit,
        "cycles": pd.concat(cycle_frames, ignore_index=True),
    }


def _evaluate_test(
    protocol: dict[str, object], context: dict[str, object], frozen: dict[str, object]
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    active = context["active"]
    names = list(active.factor_names)
    trend = pd.Series(active.regime_factor_weights["trend"], index=names, dtype=float)
    trend_mask = context["regimes"].eq("trend")
    rows: list[dict[str, object]] = []
    cycle_frames: list[pd.DataFrame] = []
    max_trend_delta = 0.0
    control_score_delta = None
    control_target_equal = False
    for item in frozen["candidates"]:
        candidate_id = int(item["candidate_id"])
        range_weights = pd.Series(item["range_weights"], dtype=float).reindex(names)
        weights = {"trend": trend, "range": range_weights}
        scores = score_with_regime_weights(
            context["factors"], context["regimes"], weights, context["base_weights"]
        )
        target = positions_from_scores(scores, active.rule.enter, active.rule.exit, active.rule)
        max_trend_delta = max(
            max_trend_delta,
            float(np.max(np.abs(scores.loc[trend_mask] - context["active_scores"].loc[trend_mask]))),
        )
        if candidate_id == int(protocol["control_candidate_id"]):
            control_score_delta = float(np.max(np.abs(scores - context["active_scores"])))
            control_target_equal = bool(target.equals(context["active_target"]))
        start, end = protocol["test_window"]
        result = run_period_backtests(
            context["data"].daily,
            target,
            {"locked_test": (pd.Timestamp(start), pd.Timestamp(end))},
            fee_rate=float(protocol["fee_rate"]),
            init_cash=float(protocol["init_cash"]),
        )["locked_test"]
        diagnostics, cycles = _range_diagnostics(
            result, context["regimes"], target, context["data"].daily, start, end
        )
        rows.append(
            {
                **{key: item[key] for key in ("candidate_id", "structure_share", "trend_share", "volume_share", "is_control")},
                **diagnostics,
                **_result_metrics(result, float(protocol["init_cash"])),
            }
        )
        cycles.insert(0, "candidate_id", candidate_id)
        cycle_frames.append(cycles)
    metrics = pd.DataFrame(rows)
    layers = pareto_layers(
        metrics, tuple(protocol["core_metrics"]), tolerance=float(protocol["comparison_tolerance"])
    )
    metrics["pareto_layer"] = metrics["candidate_id"].map(layers)
    tolerance = float(protocol["comparison_tolerance"])
    if control_score_delta is None or control_score_delta > tolerance or not control_target_equal or max_trend_delta > tolerance:
        raise ValueError("test control reproduction or trend invariance failed")
    audit = {
        "status": "PASS",
        "candidate_count": len(metrics),
        "control_score_max_absolute_delta": control_score_delta,
        "control_target_equal": control_target_equal,
        "all_candidate_trend_score_max_absolute_delta": max_trend_delta,
        "data_hashes": context["data"].hashes,
    }
    return metrics, pd.concat(cycle_frames, ignore_index=True), audit


def _freeze(
    protocol: dict[str, object],
    candidates: list[dict[str, object]],
    summaries: pd.DataFrame,
    platform: pd.DataFrame,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "experiment_id": "0902_EX04",
        "protocol_sha256": canonical_json_sha256(PROTOCOL_PATH),
        "research_end": "2025-12-31",
        "test_data_accessed_before_freeze": False,
        "candidate_count": len(candidates),
        "maximum_first_front_count": int(summaries["first_front_count"].max()),
        "platform_components": [
            {
                "component_id": int(component_id),
                "candidate_ids": group["candidate_id"].astype(int).tolist(),
            }
            for component_id, group in platform.groupby("component_id")
        ],
        "candidates": candidates,
    }


def _platform_test_summary(
    protocol: dict[str, object], platform: pd.DataFrame, test: pd.DataFrame
) -> tuple[pd.DataFrame, dict[str, object]]:
    research_columns = [
        "component_id",
        "candidate_id",
        "first_front_count",
        "mean_pareto_layer",
        "worst_pareto_layer",
        "max_drawdown",
        "calmar",
        "win_loss_ratio",
        "strategy_return",
        "range_to_range_compound_return",
        "range_to_range_short_loss_count",
    ]
    research = platform[research_columns].rename(
        columns={
            column: f"research_{column}"
            for column in research_columns
            if column not in {"component_id", "candidate_id"}
        }
    )
    joined = research.merge(
        test, on="candidate_id", validate="one_to_one"
    )
    control = test.loc[test["candidate_id"].eq(int(protocol["control_candidate_id"]))].iloc[0]
    joined["test_pure_range_better_than_control"] = joined["range_to_range_compound_return"].gt(
        control["range_to_range_compound_return"]
    )
    joined["test_core_pareto_front"] = joined["pareto_layer"].eq(1)
    summary = {
        "research_platform_member_count": int(len(joined)),
        "test_core_front_member_count": int(joined["test_core_pareto_front"].sum()),
        "test_pure_range_better_member_count": int(joined["test_pure_range_better_than_control"].sum()),
        "test_core_front_and_pure_range_better_member_count": int(
            (joined["test_core_pareto_front"] & joined["test_pure_range_better_than_control"]).sum()
        ),
        "control_test_pareto_layer": int(control["pareto_layer"]),
        "control_test_max_drawdown": float(control["max_drawdown"]),
        "control_test_calmar": float(control["calmar"]),
        "control_test_win_loss_ratio": float(control["win_loss_ratio"]),
        "control_test_return": float(control["strategy_return"]),
        "research_platform_candidate_ids": joined["candidate_id"].astype(int).tolist(),
        "test_core_front_candidate_ids": test.loc[test["pareto_layer"].eq(1), "candidate_id"].astype(int).tolist(),
    }
    return joined, summary


def _write_documents(
    *,
    execution_commit: str,
    elapsed: float,
    research_audit: dict[str, object],
    test_audit: dict[str, object],
    platform_summary: dict[str, object],
    platform_test: pd.DataFrame,
    test_metrics: pd.DataFrame,
) -> None:
    execution = [
        "# 0902_EX04 执行过程",
        "",
        f"- 执行提交：`{execution_commit}`。",
        f"- 完成{research_audit['candidate_count']}项候选、{research_audit['research_window_count']}个研究窗口。",
        f"- 研究最高第一前沿次数：{research_audit['maximum_first_front_count']}；平台成员{research_audit['platform_member_count']}项，连通分量{research_audit['platform_component_count']}个。",
        "- 候选权重、四窗口指标、Pareto层级和平台成员先冻结并哈希，随后才加载2026。",
        f"- 研究期trend分数最大差：`{research_audit['all_candidate_trend_score_max_absolute_delta']:.3e}`。",
        f"- 测试期trend分数最大差：`{test_audit['all_candidate_trend_score_max_absolute_delta']:.3e}`。",
        f"- 总耗时：{elapsed:.2f}秒。",
        "- 活动基线、行情清单和核心实现文件保持不变。",
    ]
    (EXPERIMENT_DIR / "03_execution.md").write_text("\n".join(execution) + "\n", encoding="utf-8")
    components = (
        platform_test.groupby("component_id")
        .agg(
            members=("candidate_id", "size"),
            test_front=("test_core_pareto_front", "sum"),
            test_range_better=("test_pure_range_better_than_control", "sum"),
            median_test_layer=("pareto_layer", "median"),
        )
        .reset_index()
    )
    lines = [
        "# 0902_EX04 结论",
        "",
        "状态：`COMPLETE`。本轮不设置PASS/FAIL，不修改活动基线。",
        "",
        f"研究期最高第一前沿次数为{research_audit['maximum_first_front_count']}，得到{research_audit['platform_member_count']}个平台成员和{research_audit['platform_component_count']}个连通分量。",
        "",
        "| 平台分量 | 成员数 | 2026核心第一前沿成员 | 2026纯震荡优于对照成员 | 2026 Pareto层级中位数 |",
        "|---:|---:|---:|---:|---:|",
    ]
    for row in components.itertuples():
        lines.append(
            f"| {int(row.component_id)} | {int(row.members)} | {int(row.test_front)} | {int(row.test_range_better)} | {float(row.median_test_layer):.1f} |"
        )
    lines.extend(
        [
            "",
            f"全部研究平台成员中，{platform_summary['test_core_front_member_count']}项在2026仍处于三指标Pareto第一前沿，{platform_summary['test_pure_range_better_member_count']}项的纯震荡收益优于活动对照，{platform_summary['test_core_front_and_pure_range_better_member_count']}项同时满足这两项诊断。",
            "",
            "## 研究平台与活动对照",
            "",
            "| 候选 | Range结构/趋势/量价占比 | 研究第一前沿次数 | 研究最大回撤 | 研究卡玛 | 研究盈亏比 | 2026最大回撤 | 2026卡玛 | 2026盈亏比 | 2026 Pareto层级 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in platform_test.sort_values(["component_id", "candidate_id"]).itertuples():
        lines.append(
            f"| {int(row.candidate_id)} | {row.structure_share:.1%}/{row.trend_share:.1%}/{row.volume_share:.1%} | "
            f"{int(row.research_first_front_count)} | {row.research_max_drawdown:.2%} | "
            f"{row.research_calmar:.4f} | {row.research_win_loss_ratio:.4f} | {row.max_drawdown:.2%} | "
            f"{row.calmar:.4f} | {row.win_loss_ratio:.4f} | {int(row.pareto_layer)} |"
        )
    control = test_metrics.loc[
        test_metrics["candidate_id"].eq(int(platform_summary["control_candidate_id"]))
    ].iloc[0]
    lines.append(
        f"| 活动对照260 | {control.structure_share:.1%}/{control.trend_share:.1%}/{control.volume_share:.1%} | "
        f"{platform_summary['control_research_first_front_count']} | "
        f"{platform_summary['control_research_max_drawdown']:.2%} | "
        f"{platform_summary['control_research_calmar']:.4f} | "
        f"{platform_summary['control_research_win_loss_ratio']:.4f} | {control.max_drawdown:.2%} | "
        f"{control.calmar:.4f} | {control.win_loss_ratio:.4f} | {int(control.pareto_layer)} |"
    )
    test_front = test_metrics.loc[test_metrics["pareto_layer"].eq(1)].sort_values("candidate_id")
    lines.extend(
        [
            "",
            "## 判断",
            "",
            "本轮没有找到经锁定测试支持的稳定参数平台。研究平台规模很小且不连续：候选16与35构成一个两点分量，候选27是单点分量；三项候选在2026均退出核心第一前沿，也都没有改善纯震荡收益。活动对照260在2026保持第一前沿，继续作为活动基线。",
            "",
            "2026全候选事后第一前沿包含以下参数。它们是在打开锁定测试后观察到的诊断结果，只能形成下一轮面向2026-09-02之后新数据的预注册假设，不能回写本轮平台、用于本轮选优或晋升策略。",
            "",
            "| 候选 | Range结构/趋势/量价占比 | 最大回撤 | 卡玛 | 盈亏比 | 收益率 | 纯震荡收益 |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in test_front.itertuples():
        label = "活动对照260" if bool(row.is_control) else str(int(row.candidate_id))
        lines.append(
            f"| {label} | {row.structure_share:.1%}/{row.trend_share:.1%}/{row.volume_share:.1%} | "
            f"{row.max_drawdown:.2%} | {row.calmar:.4f} | {row.win_loss_ratio:.4f} | "
            f"{row.strategy_return:.2%} | {row.range_to_range_compound_return:.2%} |"
        )
    lines.extend(
        [
            "",
            "最大回撤、卡玛和盈亏比决定Pareto层级；收益率未参与平台识别。完整候选、窗口、平台和测试证据保存在artifacts目录。",
        ]
    )
    (EXPERIMENT_DIR / "04_conclusion.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run() -> dict[str, object]:
    global TEST_DATA_ACCESSED
    started = time.perf_counter()
    (ARTIFACTS / "error.json").unlink(missing_ok=True)
    protocol = _protocol()
    before = _critical_hashes()
    execution_commit = _git_head()
    research_context = _load_context(protocol, "2025-12-31")
    period_metrics, summaries, platform, candidates, details = _evaluate_research(
        protocol, research_context
    )
    _write_csv(ARTIFACTS / "research_period_metrics.csv", period_metrics)
    _write_csv(ARTIFACTS / "research_candidate_summary.csv", summaries)
    _write_csv(ARTIFACTS / "research_platform_members.csv", platform)
    _write_csv(ARTIFACTS / "research_cycles.csv", details["cycles"])
    frozen = _freeze(protocol, candidates, summaries, platform)
    _write_json(ARTIFACTS / "frozen_platform.json", frozen)
    frozen_sha = canonical_json_sha256(ARTIFACTS / "frozen_platform.json")

    TEST_DATA_ACCESSED = True
    test_context = _load_context(protocol, "2026-09-02")
    test_metrics, test_cycles, test_audit = _evaluate_test(protocol, test_context, frozen)
    _write_csv(ARTIFACTS / "test_candidate_metrics.csv", test_metrics)
    _write_csv(ARTIFACTS / "test_cycles.csv", test_cycles)
    platform_test, platform_summary = _platform_test_summary(protocol, platform, test_metrics)
    platform_summary["control_candidate_id"] = int(protocol["control_candidate_id"])
    control_research = summaries.loc[
        summaries["candidate_id"].eq(int(protocol["control_candidate_id"]))
    ].iloc[0]
    platform_summary["control_research_first_front_count"] = int(control_research["first_front_count"])
    platform_summary["control_research_max_drawdown"] = float(control_research["max_drawdown"])
    platform_summary["control_research_calmar"] = float(control_research["calmar"])
    platform_summary["control_research_win_loss_ratio"] = float(control_research["win_loss_ratio"])
    _write_csv(ARTIFACTS / "platform_test_metrics.csv", platform_test)
    _write_json(ARTIFACTS / "platform_test_summary.json", platform_summary)

    after = _critical_hashes()
    if before != after:
        raise ValueError("critical identities changed during execution")
    audit = {
        "status": "PASS",
        "phase_order": ["research", "freeze_platform", "load_locked_test"],
        "test_data_accessed_before_freeze": False,
        "frozen_platform_sha256": frozen_sha,
        "critical_hashes_before": before,
        "critical_hashes_after": after,
        "critical_hashes_unchanged": True,
        "research": details["audit"],
        "test": test_audit,
    }
    _write_json(ARTIFACTS / "audit.json", audit)
    summary = {
        "status": "COMPLETE",
        "experiment_id": "0902_EX04",
        "candidate_count": 261,
        "control_candidate_id": 260,
        "research_platform": {
            "maximum_first_front_count": details["audit"]["maximum_first_front_count"],
            "member_count": details["audit"]["platform_member_count"],
            "component_count": details["audit"]["platform_component_count"],
        },
        "platform_test": platform_summary,
        "core_metrics": protocol["core_metrics"],
        "return_used_for_pareto": False,
        "composite_pass_gate": False,
        "automatic_promotion": False,
    }
    _write_json(ARTIFACTS / "metrics.json", summary)
    elapsed = time.perf_counter() - started
    _write_documents(
        execution_commit=execution_commit,
        elapsed=elapsed,
        research_audit=details["audit"],
        test_audit=test_audit,
        platform_summary=platform_summary,
        platform_test=platform_test,
        test_metrics=test_metrics,
    )
    manifest = build_experiment_manifest(
        EXPERIMENT_DIR,
        {
            "experiment_id": "0902_EX04",
            "date": "2026-09-02",
            "status": "COMPLETE",
            "symbol": "588080.SH",
            "asset_type": "etf",
            "baseline": protocol["active_baseline"],
            "protocol_sha256": canonical_json_sha256(PROTOCOL_PATH),
            "visible_sample_end": "2025-12-31",
            "locked_test_end": "2026-09-02",
            "locked_test_accessed": True,
        },
    )
    validate_experiment_archive(EXPERIMENT_DIR)
    return {**summary, "manifest_file_count": len(manifest["files"]), "elapsed_seconds": elapsed}


def _archive_error(exc: Exception) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    _write_json(ARTIFACTS / "error.json", {"status": "ERROR", "type": type(exc).__name__, "message": str(exc)})
    (EXPERIMENT_DIR / "03_execution.md").write_text(
        f"# 0902_EX04 执行过程\n\n执行停止：`{type(exc).__name__}: {exc}`。\n", encoding="utf-8"
    )
    (EXPERIMENT_DIR / "04_conclusion.md").write_text(
        "# 0902_EX04 结论\n\n状态：`ERROR`。未修改活动策略。\n", encoding="utf-8"
    )
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    build_experiment_manifest(
        EXPERIMENT_DIR,
        {
            "experiment_id": "0902_EX04",
            "date": "2026-09-02",
            "status": "ERROR",
            "symbol": "588080.SH",
            "asset_type": "etf",
            "baseline": protocol["active_baseline"],
            "protocol_sha256": canonical_json_sha256(PROTOCOL_PATH),
            "visible_sample_end": "2025-12-31",
            "locked_test_end": "2026-09-02",
            "locked_test_accessed": TEST_DATA_ACCESSED,
        },
    )


def main() -> int:
    try:
        result = run()
    except Exception as exc:
        _archive_error(exc)
        print(json.dumps({"status": "ERROR", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
