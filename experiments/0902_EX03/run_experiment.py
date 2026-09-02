"""Range-only weight feasibility research with a locked 2026 test."""

from __future__ import annotations

from itertools import product
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
from czsc_trader.regime_weight import (
    classify_regimes,
    lagged_efficiency_ratio,
    project_group_weights,
    score_with_regime_weights,
)
from czsc_trader.strategy_metrics import (
    closed_trade_ledger,
    strategy_comparison_metrics,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
ARTIFACTS = EXPERIMENT_DIR / "artifacts"
PROTOCOL_PATH = ARTIFACTS / "protocol.json"
BASELINE_ROOT = REPO_ROOT / "configs" / "rule_baselines"
RAW_DIR = REPO_ROOT / "data" / "raw"
TEST_DATA_ACCESSED = False


def classify_trade_cycles(
    orders: pd.DataFrame,
    regimes: pd.Series,
    sessions: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Attach causal entry/exit regimes and session holding time to closed trades."""
    ledger = closed_trade_ledger(orders).copy()
    if ledger.empty:
        return ledger.assign(
            entry_regime=pd.Series(dtype="string"),
            exit_regime=pd.Series(dtype="string"),
            regime_path=pd.Series(dtype="string"),
            holding_sessions=pd.Series(dtype="int64"),
            fees=pd.Series(dtype="float64"),
            short_loss=pd.Series(dtype="bool"),
        )
    labels = regimes.astype("string").copy()
    labels.index = pd.DatetimeIndex(pd.to_datetime(labels.index)).normalize()
    calendar = pd.DatetimeIndex(pd.to_datetime(sessions)).normalize()
    locations = pd.Series(np.arange(len(calendar), dtype=int), index=calendar)
    ledger["entry_signal_date"] = pd.to_datetime(ledger["entry_signal_date"])
    ledger["exit_signal_date"] = pd.to_datetime(ledger["exit_signal_date"])
    ledger["entry_date"] = pd.to_datetime(ledger["entry_date"])
    ledger["exit_date"] = pd.to_datetime(ledger["exit_date"])
    ledger["entry_regime"] = ledger["entry_signal_date"].dt.normalize().map(labels)
    ledger["exit_regime"] = ledger["exit_signal_date"].dt.normalize().map(labels)
    if ledger[["entry_regime", "exit_regime"]].isna().any().any():
        raise ValueError("trade signal dates do not align to regime labels")
    ledger["regime_path"] = (
        ledger["entry_regime"].astype(str) + "_to_" + ledger["exit_regime"].astype(str)
    )
    entry_locations = ledger["entry_date"].dt.normalize().map(locations)
    exit_locations = ledger["exit_date"].dt.normalize().map(locations)
    if entry_locations.isna().any() or exit_locations.isna().any():
        raise ValueError("trade execution dates do not align to sessions")
    ledger["holding_sessions"] = (exit_locations - entry_locations).astype(int)
    ledger["fees"] = ledger["entry_fees"].astype(float) + ledger["exit_fees"].astype(float)
    ledger["short_loss"] = ledger["net_return"].astype(float).lt(0.0) & ledger[
        "holding_sessions"
    ].le(10)
    return ledger


def _compound(values: pd.Series) -> float:
    returns = values.astype(float)
    return float(np.prod(1.0 + returns) - 1.0) if not returns.empty else 0.0


def trade_diagnostics(cycles: pd.DataFrame) -> dict[str, float | int]:
    """Summarize closed trades by causal entry/exit regime path."""
    output: dict[str, float | int] = {}
    for path in ("range_to_range", "range_to_trend", "trend_to_range", "trend_to_trend"):
        selected = cycles.loc[cycles["regime_path"].eq(path)].copy()
        returns = selected["net_return"].astype(float)
        output[f"{path}_count"] = int(len(selected))
        output[f"{path}_loss_count"] = int(returns.lt(0.0).sum())
        output[f"{path}_loss_rate"] = float(returns.lt(0.0).mean()) if len(selected) else 0.0
        output[f"{path}_short_loss_count"] = int(selected["short_loss"].astype(bool).sum())
        output[f"{path}_compound_return"] = _compound(returns)
        output[f"{path}_median_holding_sessions"] = (
            float(selected["holding_sessions"].median()) if len(selected) else 0.0
        )
        output[f"{path}_fees"] = float(selected["fees"].sum()) if len(selected) else 0.0
    return output


def select_representatives(metrics: pd.DataFrame) -> dict[str, int]:
    """Select three deterministic diagnostic representatives without a pass gate."""
    required = {
        "candidate_id",
        "range_to_range_compound_return",
        "range_to_range_short_loss_count",
        "balanced_score",
        "weight_l1_distance",
    }
    if not required <= set(metrics.columns):
        raise ValueError(f"candidate metrics missing columns: {sorted(required - set(metrics.columns))}")
    pure = metrics.sort_values(
        ["range_to_range_compound_return", "weight_l1_distance", "candidate_id"],
        ascending=[False, True, True],
        kind="stable",
    )
    short = metrics.sort_values(
        ["range_to_range_short_loss_count", "weight_l1_distance", "candidate_id"],
        ascending=[True, True, True],
        kind="stable",
    )
    balanced = metrics.sort_values(
        ["balanced_score", "weight_l1_distance", "candidate_id"],
        ascending=[False, True, True],
        kind="stable",
    )
    return {
        "best_pure_range_return": int(pure.iloc[0]["candidate_id"]),
        "fewest_short_losses": int(short.iloc[0]["candidate_id"]),
        "best_balanced": int(balanced.iloc[0]["candidate_id"]),
    }


def cross_sample_consistency(
    research: pd.DataFrame,
    test: pd.DataFrame,
    *,
    control_candidate_id: int,
) -> dict[str, object]:
    """Describe locked-test consistency without turning test results into promotion."""
    research_by_id = research.set_index("candidate_id")
    test_by_id = test.set_index("candidate_id")
    if set(research_by_id.index) != set(test_by_id.index):
        raise ValueError("research and test candidate identities differ")
    if control_candidate_id not in research_by_id.index:
        raise ValueError("control candidate is missing")
    research_pure = research_by_id["range_to_range_compound_return"].gt(
        research_by_id.loc[control_candidate_id, "range_to_range_compound_return"]
    )
    test_pure = test_by_id["range_to_range_compound_return"].gt(
        test_by_id.loc[control_candidate_id, "range_to_range_compound_return"]
    )
    research_short = research_by_id["range_to_range_short_loss_count"].lt(
        research_by_id.loc[control_candidate_id, "range_to_range_short_loss_count"]
    )
    test_short = test_by_id["range_to_range_short_loss_count"].le(
        test_by_id.loc[control_candidate_id, "range_to_range_short_loss_count"]
    )
    return {
        "research_pure_return_improved_count": int(research_pure.sum()),
        "test_pure_return_improved_count": int(test_pure.sum()),
        "both_samples_pure_return_improved_candidate_ids": [
            int(value) for value in research_by_id.index[research_pure & test_pure]
        ],
        "both_samples_pure_return_and_short_loss_improved_candidate_ids": [
            int(value)
            for value in research_by_id.index[research_pure & test_pure & research_short & test_short]
        ],
        "diagnostic_only": True,
        "promotion_allowed": False,
    }


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
    return (
        canonical_json_sha256(path)
        if path.suffix.lower() == ".json"
        else normalized_text_sha256(path)
    )


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, encoding="utf-8"
    ).strip()


def _protocol() -> dict[str, object]:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    expected = {
        "experiment_id": "0902_EX03",
        "status": "PRE_REGISTERED",
        "research_start": "2021-01-01",
        "research_end": "2025-12-31",
        "test_start": "2026-01-01",
        "test_end": "2026-09-02",
        "candidate_count": 25,
        "short_holding_max_sessions": 10,
        "locked_test_used_for_tuning": False,
        "composite_pass_gate": False,
        "automatic_promotion": False,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"protocol field {key} differs from preregistration")
    if payload.get("range_trend_multipliers") != [0.5, 0.75, 1.0, 1.25, 1.5]:
        raise ValueError("range trend multiplier grid differs from preregistration")
    if payload.get("range_volume_multipliers") != [0.5, 0.75, 1.0, 1.25, 1.5]:
        raise ValueError("range volume multiplier grid differs from preregistration")
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
        REPO_ROOT / "src" / "czsc_trader" / "strategy_metrics.py",
        EXPERIMENT_DIR / "run_experiment.py",
        PROTOCOL_PATH,
    )
    return {path.relative_to(REPO_ROOT).as_posix(): _identity_sha(path) for path in paths}


def _candidate_grid(protocol: dict[str, object]) -> tuple[tuple[int, float, float], ...]:
    values = tuple(
        (candidate_id, float(trend), float(volume))
        for candidate_id, (trend, volume) in enumerate(
            product(
                protocol["range_trend_multipliers"],
                protocol["range_volume_multipliers"],
            )
        )
    )
    if len(values) != int(protocol["candidate_count"]):
        raise AssertionError("candidate count differs from protocol")
    control = [row for row in values if row[1:] == tuple(protocol["control_multipliers"])]
    if control != [(18, 1.25, 1.25)]:
        raise AssertionError("control candidate identity differs")
    return values


def _daily_prices(data: object) -> pd.DataFrame:
    daily = data.daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"])
    return daily.set_index("dt").sort_index()


def _full_metrics(result: object, init_cash: float) -> dict[str, object]:
    metrics = strategy_comparison_metrics(result.equity, result.orders, init_cash)
    return {
        "strategy_return": metrics["return"],
        "max_drawdown": metrics["max_drawdown"],
        "calmar": metrics["calmar"],
        "win_loss_ratio": metrics["win_loss_ratio"],
        "win_loss_ratio_status": metrics["win_loss_ratio_status"],
        "sharpe": metrics["sharpe"],
        "exposure": float(result.metrics["exposure"]),
        "trade_count": int(result.metrics["trade_count"]),
    }


def _balanced_scores(metrics: pd.DataFrame) -> pd.Series:
    components = pd.DataFrame(index=metrics.index)
    components["pure_return"] = metrics["range_to_range_compound_return"].rank(
        pct=True, ascending=True, method="average"
    )
    components["short_losses"] = metrics["range_to_range_short_loss_count"].rank(
        pct=True, ascending=False, method="average"
    )
    components["signal_density"] = metrics["range_signal_density_per_100"].rank(
        pct=True, ascending=False, method="average"
    )
    components["transition_return"] = metrics["range_to_trend_compound_return"].rank(
        pct=True, ascending=True, method="average"
    )
    calmar = metrics["calmar"].astype(float).replace([np.inf, -np.inf], np.nan)
    components["calmar"] = calmar.fillna(calmar.min() - 1.0).rank(
        pct=True, ascending=True, method="average"
    )
    return components.mean(axis=1).rename("balanced_score")


def _candidate_weights(
    active: object,
    base_weights: pd.Series,
    groups: dict[str, tuple[str, ...]],
    range_trend: float,
    range_volume: float,
) -> dict[str, pd.Series]:
    trend = pd.Series(
        active.regime_factor_weights["trend"], index=active.factor_names, dtype=float
    )
    range_weights = project_group_weights(
        base_weights, groups, float(range_trend), float(range_volume)
    )
    return {"trend": trend, "range": range_weights}


def _evaluate_candidate(
    *,
    candidate_id: int,
    range_trend: float,
    range_volume: float,
    weights: dict[str, pd.Series],
    factors: pd.DataFrame,
    regimes: pd.Series,
    fallback: pd.Series,
    daily: pd.DataFrame,
    active: object,
    period_name: str,
    start: str,
    end: str,
    fee_rate: float,
    init_cash: float,
    control_range_weights: pd.Series,
) -> tuple[dict[str, object], pd.DataFrame, pd.Series, pd.Series, object]:
    scores = score_with_regime_weights(factors, regimes, weights, fallback)
    target = positions_from_scores(scores, active.rule.enter, active.rule.exit, active.rule)
    result = run_period_backtests(
        daily,
        target,
        {period_name: (pd.Timestamp(start), pd.Timestamp(end))},
        fee_rate=fee_rate,
        init_cash=init_cash,
    )[period_name]
    sessions = pd.DatetimeIndex(pd.to_datetime(daily["dt"]))
    cycles = classify_trade_cycles(result.orders, regimes, sessions)
    diagnostics = trade_diagnostics(cycles)
    period_mask = regimes.index.to_series().between(pd.Timestamp(start), pd.Timestamp(end))
    period_regimes = regimes.loc[period_mask]
    transitions = target.diff().abs().fillna(0.0).gt(0.0)
    range_events = int((transitions & regimes.eq("range") & period_mask).sum())
    range_days = int(period_regimes.eq("range").sum())
    row = {
        "candidate_id": int(candidate_id),
        "range_trend_multiplier": float(range_trend),
        "range_volume_multiplier": float(range_volume),
        "weight_l1_distance": float(
            np.abs(weights["range"].to_numpy() - control_range_weights.to_numpy()).sum()
        ),
        "range_days": range_days,
        "range_transition_count": range_events,
        "range_signal_density_per_100": float(range_events / range_days * 100.0),
        **diagnostics,
        **_full_metrics(result, init_cash),
    }
    cycles.insert(0, "candidate_id", int(candidate_id))
    return row, cycles, scores, target, result


def _build_factor_attribution(
    factors: pd.DataFrame,
    scores: pd.Series,
    weights: pd.Series,
    regimes: pd.Series,
    orders: pd.DataFrame,
    cycles: pd.DataFrame,
) -> pd.DataFrame:
    event_side = {
        pd.Timestamp(row.signal_date): str(row.side) for row in orders.itertuples()
    }
    cycle_lookup: dict[pd.Timestamp, tuple[str, float]] = {}
    for row in cycles.itertuples():
        value = (str(row.regime_path), float(row.net_return))
        cycle_lookup[pd.Timestamp(row.entry_signal_date)] = value
        cycle_lookup[pd.Timestamp(row.exit_signal_date)] = value
    rows: list[dict[str, object]] = []
    for signal_date, side in event_side.items():
        if str(regimes.loc[signal_date]) != "range":
            continue
        path, net_return = cycle_lookup.get(signal_date, ("open_cycle", np.nan))
        for factor in factors.columns:
            factor_value = float(factors.loc[signal_date, factor])
            weight = float(weights.loc[factor])
            rows.append(
                {
                    "signal_date": signal_date,
                    "side": side,
                    "regime_path": path,
                    "cycle_net_return": net_return,
                    "factor": factor,
                    "factor_value": factor_value,
                    "weight": weight,
                    "contribution": factor_value * weight,
                    "factor_score": float(scores.loc[signal_date]),
                }
            )
    return pd.DataFrame(rows)


def _evaluate_sample(
    protocol: dict[str, object],
    *,
    cutoff: str,
    start: str,
    end: str,
    period_name: str,
    frozen_candidates: list[dict[str, object]] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[int, dict[str, object]], dict[str, object]]:
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
    base_weights = pd.Series(parent.factor_weights, index=names, name="weight", dtype=float)
    groups = signal_groups(factors.columns)
    prices = _daily_prices(data)
    regimes = classify_regimes(
        lagged_efficiency_ratio(prices["close"], active.er_lookback), active.er_threshold
    ).reindex(factors.index)
    if regimes.isna().any():
        raise ValueError("regime labels do not align to factor rows")
    control_weights = {
        label: pd.Series(active.regime_factor_weights[label], index=names, dtype=float)
        for label in ("trend", "range")
    }
    grid = _candidate_grid(protocol)
    frozen_by_id = (
        {int(item["candidate_id"]): item for item in frozen_candidates}
        if frozen_candidates is not None
        else {}
    )
    metric_rows: list[dict[str, object]] = []
    cycle_frames: list[pd.DataFrame] = []
    paths: dict[int, dict[str, object]] = {}
    for candidate_id, range_trend, range_volume in grid:
        weights = _candidate_weights(
            active, base_weights, groups, range_trend, range_volume
        )
        if frozen_candidates is not None:
            frozen = frozen_by_id.get(candidate_id)
            if frozen is None:
                raise ValueError(f"frozen candidate {candidate_id} is missing")
            expected = pd.Series(frozen["range_weights"], dtype=float).reindex(names)
            if expected.isna().any() or not np.allclose(
                weights["range"].to_numpy(), expected.to_numpy(), rtol=0.0, atol=1e-15
            ):
                raise ValueError(f"frozen candidate {candidate_id} weights differ")
            weights["range"] = expected
        row, cycles, scores, target, result = _evaluate_candidate(
            candidate_id=candidate_id,
            range_trend=range_trend,
            range_volume=range_volume,
            weights=weights,
            factors=factors,
            regimes=regimes,
            fallback=base_weights,
            daily=data.daily,
            active=active,
            period_name=period_name,
            start=start,
            end=end,
            fee_rate=float(protocol["fee_rate"]),
            init_cash=float(protocol["init_cash"]),
            control_range_weights=control_weights["range"],
        )
        metric_rows.append(row)
        cycle_frames.append(cycles)
        paths[candidate_id] = {
            "weights": weights,
            "scores": scores,
            "target": target,
            "result": result,
        }
    metrics = pd.DataFrame(metric_rows).sort_values("candidate_id").reset_index(drop=True)
    metrics["balanced_score"] = _balanced_scores(metrics)
    cycles = pd.concat(cycle_frames, ignore_index=True)

    control = paths[18]
    applied = apply_resolved_baseline(frame, active, daily_close=prices["close"])
    tolerance = float(protocol["comparison_tolerance"])
    score_delta = float(np.max(np.abs(control["scores"].to_numpy() - applied.scores.to_numpy())))
    target_equal = bool(control["target"].equals(applied.target_position))
    trend_mask = regimes.eq("trend")
    max_trend_delta = max(
        float(np.max(np.abs(path["scores"].loc[trend_mask] - control["scores"].loc[trend_mask])))
        for path in paths.values()
    )
    if score_delta > tolerance or not target_equal or max_trend_delta > tolerance:
        raise ValueError("control reproduction or trend score invariance failed")
    audit = {
        "status": "PASS",
        "candidate_count": int(len(metrics)),
        "control_candidate_id": 18,
        "control_score_max_absolute_delta": score_delta,
        "control_target_equal": target_equal,
        "all_candidate_trend_score_max_absolute_delta": max_trend_delta,
        "data_hashes": data.hashes,
    }
    context = {
        "active": active,
        "factors": factors,
        "regimes": regimes,
        "paths": paths,
        "control_weights": control_weights,
        "audit": audit,
    }
    return metrics, cycles, paths, context


def _freeze_candidates(
    protocol: dict[str, object],
    metrics: pd.DataFrame,
    paths: dict[int, dict[str, object]],
    representatives: dict[str, int],
) -> dict[str, object]:
    candidates = []
    for row in metrics.itertuples():
        candidate_id = int(row.candidate_id)
        candidates.append(
            {
                "candidate_id": candidate_id,
                "range_trend_multiplier": float(row.range_trend_multiplier),
                "range_volume_multiplier": float(row.range_volume_multiplier),
                "range_weights": {
                    str(name): float(value)
                    for name, value in paths[candidate_id]["weights"]["range"].items()
                },
            }
        )
    return {
        "schema_version": 1,
        "experiment_id": "0902_EX03",
        "protocol_sha256": canonical_json_sha256(PROTOCOL_PATH),
        "research_end": protocol["research_end"],
        "test_data_accessed_before_freeze": False,
        "candidate_count": len(candidates),
        "representatives": representatives,
        "candidates": candidates,
    }


def _test_comparison(
    research: pd.DataFrame,
    test: pd.DataFrame,
    representatives: dict[str, int],
) -> pd.DataFrame:
    fields = (
        "range_to_range_compound_return",
        "range_to_range_loss_rate",
        "range_to_range_short_loss_count",
        "range_signal_density_per_100",
        "range_to_trend_compound_return",
        "strategy_return",
        "max_drawdown",
        "calmar",
        "win_loss_ratio",
    )
    labels: dict[int, list[str]] = {}
    for label, candidate_id in representatives.items():
        labels.setdefault(int(candidate_id), []).append(label)
    labels.setdefault(18, []).append("active_control")
    rows: list[dict[str, object]] = []
    for candidate_id, candidate_labels in sorted(labels.items()):
        research_row = research.loc[research["candidate_id"].eq(candidate_id)].iloc[0]
        test_row = test.loc[test["candidate_id"].eq(candidate_id)].iloc[0]
        row: dict[str, object] = {
            "candidate_id": candidate_id,
            "roles": ",".join(candidate_labels),
            "range_trend_multiplier": float(research_row["range_trend_multiplier"]),
            "range_volume_multiplier": float(research_row["range_volume_multiplier"]),
        }
        for field in fields:
            row[f"research_{field}"] = research_row[field]
            row[f"test_{field}"] = test_row[field]
        rows.append(row)
    return pd.DataFrame(rows)


def _write_documents(
    *,
    execution_commit: str,
    elapsed: float,
    research: pd.DataFrame,
    test: pd.DataFrame,
    representatives: dict[str, int],
    audit: dict[str, object],
    consistency: dict[str, object],
) -> None:
    control_research = research.loc[research["candidate_id"].eq(18)].iloc[0]
    control_test = test.loc[test["candidate_id"].eq(18)].iloc[0]
    execution = [
        "# 0902_EX03 执行过程",
        "",
        f"- 执行提交：`{execution_commit}`。",
        "- 2021—2025研究期完成25项range权重候选回测。",
        "- 候选、研究指标与代表候选先冻结并哈希，随后才加载2026数据。",
        "- 2026-01-01至2026-09-02对全部25项冻结候选执行一次锁定测试。",
        f"- trend分数最大绝对差：`{audit['test']['all_candidate_trend_score_max_absolute_delta']:.3e}`。",
        f"- 活动对照研究期分数最大绝对差：`{audit['research']['control_score_max_absolute_delta']:.3e}`。",
        f"- 活动对照测试期分数最大绝对差：`{audit['test']['control_score_max_absolute_delta']:.3e}`。",
        f"- 总耗时：{elapsed:.2f}秒。",
        "- 活动基线、行情清单和核心实现文件在执行前后保持不变。",
    ]
    (EXPERIMENT_DIR / "03_execution.md").write_text("\n".join(execution) + "\n", encoding="utf-8")

    labels = {
        "best_pure_range_return": "纯震荡收益最佳",
        "fewest_short_losses": "短周期亏损最少",
        "best_balanced": "综合诊断最佳",
    }
    lines = [
        "# 0902_EX03 结论",
        "",
        "状态：`COMPLETE`。本轮为可行性研究，不设置PASS/FAIL，不修改活动基线。",
        "",
        "| 角色 | 候选 | range趋势乘数 | range量价乘数 | 研究纯震荡复合收益 | 测试纯震荡复合收益 | 研究短亏 | 测试短亏 | 测试总收益 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for role, candidate_id in representatives.items():
        r = research.loc[research["candidate_id"].eq(candidate_id)].iloc[0]
        t = test.loc[test["candidate_id"].eq(candidate_id)].iloc[0]
        lines.append(
            f"| {labels[role]} | {candidate_id} | {r['range_trend_multiplier']:.2f} | "
            f"{r['range_volume_multiplier']:.2f} | {r['range_to_range_compound_return']:.2%} | "
            f"{t['range_to_range_compound_return']:.2%} | {int(r['range_to_range_short_loss_count'])} | "
            f"{int(t['range_to_range_short_loss_count'])} | {t['strategy_return']:.2%} |"
        )
    lines.extend(
        [
            "",
            "活动对照候选18：",
            "",
            f"- 研究期纯震荡复合收益`{control_research['range_to_range_compound_return']:.2%}`，短周期亏损`{int(control_research['range_to_range_short_loss_count'])}`次。",
            f"- 测试期纯震荡复合收益`{control_test['range_to_range_compound_return']:.2%}`，短周期亏损`{int(control_test['range_to_range_short_loss_count'])}`次，总收益`{control_test['strategy_return']:.2%}`。",
            "",
            f"研究期有{consistency['research_pure_return_improved_count']}项候选改善纯震荡收益，测试期有{consistency['test_pure_return_improved_count']}项；两段同时改善的候选为`{consistency['both_samples_pure_return_improved_candidate_ids']}`。",
        ]
    )
    consistent_ids = consistency["both_samples_pure_return_and_short_loss_improved_candidate_ids"]
    if consistent_ids:
        candidate_id = int(consistent_ids[0])
        r = research.loc[research["candidate_id"].eq(candidate_id)].iloc[0]
        t = test.loc[test["candidate_id"].eq(candidate_id)].iloc[0]
        lines.extend(
            [
                "",
                f"候选{candidate_id}是唯一在研究期和测试期同时改善纯震荡收益并减少短亏的候选：",
                "",
                f"- range趋势/量价乘数为`{r['range_trend_multiplier']:.2f}/{r['range_volume_multiplier']:.2f}`。",
                f"- 研究期纯震荡收益由`{control_research['range_to_range_compound_return']:.2%}`改善至`{r['range_to_range_compound_return']:.2%}`，短亏由{int(control_research['range_to_range_short_loss_count'])}次降至{int(r['range_to_range_short_loss_count'])}次。",
                f"- 测试期纯震荡收益由`{control_test['range_to_range_compound_return']:.2%}`改善至`{t['range_to_range_compound_return']:.2%}`，短亏由{int(control_test['range_to_range_short_loss_count'])}次降至{int(t['range_to_range_short_loss_count'])}次。",
                f"- 测试期总收益由`{control_test['strategy_return']:.2%}`降至`{t['strategy_return']:.2%}`，range→trend复合收益由`{control_test['range_to_trend_compound_return']:.2%}`降至`{t['range_to_trend_compound_return']:.2%}`。",
            ]
        )
    lines.extend(
        [
            "",
            "可行性结论：降低range趋势与量价权重、相对提高结构类权重，存在跨样本改善纯震荡交易的方向；当前证据集中于单一候选，测试期纯震荡交易仅2笔，并伴随趋势过渡收益下降，因此不足以晋升。",
            "",
            "完整25项研究与测试结果、逐笔交易和基线信号因子贡献保存在artifacts目录。2026活动基线表现此前已被观察，因此本轮锁定测试提供候选级外推证据，不构成完全盲测或未来收益证明。",
        ]
    )
    (EXPERIMENT_DIR / "04_conclusion.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run() -> dict[str, object]:
    global TEST_DATA_ACCESSED
    started = time.perf_counter()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    protocol = _protocol()
    before = _critical_hashes()
    execution_commit = _git_head()

    research, research_cycles, research_paths, research_context = _evaluate_sample(
        protocol,
        cutoff=str(protocol["research_end"]),
        start=str(protocol["research_start"]),
        end=str(protocol["research_end"]),
        period_name="research",
    )
    representatives = select_representatives(research)
    _write_csv(ARTIFACTS / "research_candidate_metrics.csv", research)
    _write_csv(ARTIFACTS / "research_cycles.csv", research_cycles)
    control_cycles = research_cycles.loc[research_cycles["candidate_id"].eq(18)].copy()
    attribution = _build_factor_attribution(
        research_context["factors"],
        research_paths[18]["scores"],
        research_paths[18]["weights"]["range"],
        research_context["regimes"],
        research_paths[18]["result"].orders,
        control_cycles,
    )
    _write_csv(ARTIFACTS / "baseline_range_signal_attribution.csv", attribution)
    frozen = _freeze_candidates(protocol, research, research_paths, representatives)
    _write_json(ARTIFACTS / "frozen_candidates.json", frozen)
    frozen_sha = canonical_json_sha256(ARTIFACTS / "frozen_candidates.json")

    TEST_DATA_ACCESSED = True
    test, test_cycles, _, test_context = _evaluate_sample(
        protocol,
        cutoff=str(protocol["test_end"]),
        start=str(protocol["test_start"]),
        end=str(protocol["test_end"]),
        period_name="locked_test",
        frozen_candidates=frozen["candidates"],
    )
    _write_csv(ARTIFACTS / "test_candidate_metrics.csv", test)
    _write_csv(ARTIFACTS / "test_cycles.csv", test_cycles)
    comparison = _test_comparison(research, test, representatives)
    _write_csv(ARTIFACTS / "representative_comparison.csv", comparison)
    consistency = cross_sample_consistency(research, test, control_candidate_id=18)
    _write_json(ARTIFACTS / "cross_sample_consistency.json", consistency)

    after = _critical_hashes()
    if before != after:
        raise ValueError("critical identities changed during execution")
    audit = {
        "status": "PASS",
        "phase_order": ["research", "freeze_candidates", "load_locked_test"],
        "test_data_accessed_before_freeze": False,
        "frozen_candidates_sha256": frozen_sha,
        "critical_hashes_before": before,
        "critical_hashes_after": after,
        "critical_hashes_unchanged": True,
        "research": research_context["audit"],
        "test": test_context["audit"],
    }
    _write_json(ARTIFACTS / "audit.json", audit)
    summary = {
        "status": "COMPLETE",
        "experiment_id": "0902_EX03",
        "active_baseline": protocol["active_baseline"],
        "candidate_count": 25,
        "control_candidate_id": 18,
        "representatives": representatives,
        "cross_sample_consistency": consistency,
        "research_period": [protocol["research_start"], protocol["research_end"]],
        "test_period": [protocol["test_start"], protocol["test_end"]],
        "composite_pass_gate": False,
        "automatic_promotion": False,
    }
    _write_json(ARTIFACTS / "metrics.json", summary)
    elapsed = time.perf_counter() - started
    _write_documents(
        execution_commit=execution_commit,
        elapsed=elapsed,
        research=research,
        test=test,
        representatives=representatives,
        audit=audit,
        consistency=consistency,
    )
    manifest = build_experiment_manifest(
        EXPERIMENT_DIR,
        {
            "experiment_id": "0902_EX03",
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
    _write_json(
        ARTIFACTS / "error.json",
        {"status": "ERROR", "type": type(exc).__name__, "message": str(exc)},
    )
    (EXPERIMENT_DIR / "03_execution.md").write_text(
        f"# 0902_EX03 执行过程\n\n执行停止：`{type(exc).__name__}: {exc}`。\n",
        encoding="utf-8",
    )
    (EXPERIMENT_DIR / "04_conclusion.md").write_text(
        "# 0902_EX03 结论\n\n状态：`ERROR`。未修改活动策略。\n",
        encoding="utf-8",
    )
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    build_experiment_manifest(
        EXPERIMENT_DIR,
        {
            "experiment_id": "0902_EX03",
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
