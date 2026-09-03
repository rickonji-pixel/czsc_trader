"""Search stable 12-factor range weights on the frozen development pool."""

from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
import subprocess
import time

import numpy as np
import pandas as pd

from strategy_manager import StrategyRegistry

from czsc_trader.backtest import run_period_backtests
from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import resolve_baseline
from czsc_trader.data import load_market_data
from czsc_trader.execution_policy import entry_limit_series, policy_metrics, simulate_limit_policy
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.factors import generate_factor_frame, signal_groups
from czsc_trader.four_layer import normalized_signal_factors, positions_from_scores
from czsc_trader.identity import canonical_json_sha256, normalized_text_sha256
from czsc_trader.range_platform import (
    dirichlet_weight_candidates,
    project_group_shares,
    robust_pareto_profiles,
    select_robust_seeds,
)
from czsc_trader.regime_weight import classify_regimes, lagged_efficiency_ratio, score_with_regime_weights
from czsc_trader.strategy_metrics import closed_trade_ledger, strategy_comparison_metrics


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
ARTIFACTS = EXPERIMENT_DIR / "artifacts"
PROTOCOL_PATH = ARTIFACTS / "protocol.json"
FACTOR_PREFIX = "weight__"


def shortlist_candidate_ids(
    profiles: pd.DataFrame, *, control_id: int, limit: int
) -> list[int]:
    """Select metric-only robust candidates while retaining the formal control."""
    if limit < 1:
        raise ValueError("shortlist limit must be positive")
    ranked = select_robust_seeds(profiles, limit=limit)
    control = int(control_id)
    if control in ranked:
        return ranked
    return [*ranked[: max(0, limit - 1)], control]


def nearest_neighborhood(
    candidates: pd.DataFrame,
    *,
    representative_id: int,
    factor_names: Sequence[str],
    member_count: int,
) -> pd.DataFrame:
    """Return the nearest factor-weight vectors under L1 distance."""
    if member_count < 1:
        raise ValueError("neighborhood member count must be positive")
    indexed = candidates.set_index("candidate_id", drop=False)
    representative = indexed.loc[int(representative_id), list(factor_names)].to_numpy(float)
    result = candidates.copy()
    result["weight_l1_distance"] = np.abs(
        result.loc[:, list(factor_names)].to_numpy(float) - representative
    ).sum(axis=1)
    return result.sort_values(
        ["weight_l1_distance", "candidate_id"], kind="stable"
    ).head(member_count).reset_index(drop=True)


def _clean(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_clean(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        "experiment_id": "0903_EX02",
        "status": "PRE_REGISTERED",
        "development_end": "2026-09-02",
        "core_metrics": ["max_drawdown", "calmar", "win_loss_ratio"],
        "return_used_for_selection": False,
        "automatic_promotion": False,
        "hard_pass_gate": False,
        "stage1_candidate_count": 768,
        "stage1_seed_count": 12,
        "stage2_candidate_count": 372,
        "formal_shortlist_count": 24,
    }
    for key, expected_value in expected.items():
        if payload.get(key) != expected_value:
            raise ValueError(f"protocol field {key} differs from preregistration")
    return payload


def _critical_hashes() -> dict[str, str]:
    paths = (
        REPO_ROOT / "configs" / "strategies" / "registry.json",
        REPO_ROOT / "configs" / "strategies" / "S001" / "versions" / "v1.json",
        REPO_ROOT / "configs" / "rule_baselines" / "baseline_20260903.json",
        REPO_ROOT / "data" / "raw" / "588080_manifest.json",
        REPO_ROOT / "src" / "czsc_trader" / "range_platform.py",
        REPO_ROOT / "src" / "czsc_trader" / "execution_policy.py",
        EXPERIMENT_DIR / "run_experiment.py",
        PROTOCOL_PATH,
    )
    return {path.relative_to(REPO_ROOT).as_posix(): _identity_sha(path) for path in paths}


def _load_context(protocol: dict[str, object]) -> dict[str, object]:
    registry = StrategyRegistry(REPO_ROOT / "configs" / "strategies")
    release = registry.get_version("S001", "v1")
    if release.release_hash != protocol["strategy_release"]["release_hash"]:
        raise ValueError("S001-v1 release hash differs from preregistration")
    baseline = resolve_baseline(
        REPO_ROOT / "configs" / "rule_baselines", "baseline_20260903", symbol="588080.SH"
    )
    if baseline.sha256 != protocol["strategy_release"]["legacy_baseline_sha256"]:
        raise ValueError("legacy baseline identity differs from preregistration")
    data = load_market_data(
        REPO_ROOT / "data" / "raw", "588080.SH", "etf", cutoff=str(protocol["development_end"])
    )
    frame = generate_factor_frame(data).frame
    names = list(baseline.factor_names)
    factors = normalized_signal_factors(frame[names]).astype(float)
    daily = data.daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"])
    daily = daily.set_index("dt").sort_index().reindex(factors.index)
    regimes = classify_regimes(
        lagged_efficiency_ratio(daily["close"], baseline.er_lookback), baseline.er_threshold
    ).reindex(factors.index)
    applied = apply_resolved_baseline(frame, baseline, daily_close=daily["close"])
    groups = signal_groups(factors.columns)
    execution = baseline.execution
    if execution is None:
        raise ValueError("S001-v1 complete execution is missing")
    return {
        "release": release,
        "baseline": baseline,
        "data": data,
        "frame": frame,
        "factors": factors,
        "factor_names": names,
        "daily": daily,
        "regimes": regimes,
        "groups": groups,
        "base_weights": pd.Series(baseline.factor_weights, index=names, dtype=float),
        "trend_weights": pd.Series(baseline.regime_factor_weights["trend"], index=names, dtype=float),
        "control_range_weights": pd.Series(
            baseline.regime_factor_weights["range"], index=names, dtype=float
        ),
        "active_scores": applied.scores,
        "active_target": applied.target_position,
        "execution": execution,
    }


def _candidate_table(protocol: dict[str, object], context: dict[str, object]) -> pd.DataFrame:
    names = context["factor_names"]
    control = context["control_range_weights"]
    balanced = pd.Series(np.repeat(1.0 / len(names), len(names)), index=names)
    high_structure = project_group_shares(
        control,
        context["groups"],
        protocol["stage1"]["high_structure_group_shares"],
    )
    generated = dirichlet_weight_candidates(
        {"control": control, "balanced": balanced, "high_structure": high_structure},
        samples_per_anchor=int(protocol["stage1"]["samples_per_anchor"]),
        concentrations=protocol["stage1"]["concentrations"],
        seed=int(protocol["stage1"]["seed"]),
    )
    generated = generated.rename(columns={name: f"{FACTOR_PREFIX}{name}" for name in names})
    generated.insert(1, "stage", 1)
    generated["parent_seed_id"] = pd.NA
    return _attach_group_shares(generated, context)


def _attach_group_shares(candidates: pd.DataFrame, context: dict[str, object]) -> pd.DataFrame:
    result = candidates.copy()
    for group_name, members in context["groups"].items():
        columns = [f"{FACTOR_PREFIX}{name}" for name in members]
        result[f"{group_name}_share"] = result[columns].abs().sum(axis=1)
    return result


def _weights(row: pd.Series, names: Sequence[str]) -> pd.Series:
    return pd.Series({name: float(row[f"{FACTOR_PREFIX}{name}"]) for name in names})


def _target_for_weights(weights: pd.Series, context: dict[str, object]) -> pd.Series:
    baseline = context["baseline"]
    scores = score_with_regime_weights(
        context["factors"],
        context["regimes"],
        {"trend": context["trend_weights"], "range": weights},
        context["base_weights"],
    )
    target = positions_from_scores(scores, baseline.rule.enter, baseline.rule.exit, baseline.rule)
    trend_mask = context["regimes"].eq("trend")
    delta = float(np.max(np.abs(scores.loc[trend_mask] - context["active_scores"].loc[trend_mask])))
    if delta > 1e-12:
        raise ValueError("range candidate changed trend scores")
    return target


def _comparison_metrics(result: object, init_cash: float) -> dict[str, object]:
    metrics = strategy_comparison_metrics(result.equity, result.orders, init_cash)
    values = {
        "max_drawdown": metrics["max_drawdown"],
        "calmar": metrics["calmar"],
        "win_loss_ratio": metrics["win_loss_ratio"],
        "win_loss_ratio_status": metrics["win_loss_ratio_status"],
        "strategy_return": metrics["return"],
        "sharpe": metrics["sharpe"],
        "trade_count": int(len(result.orders)),
    }
    for key in ("max_drawdown", "calmar", "win_loss_ratio"):
        if values[key] is None or not np.isfinite(float(values[key])):
            raise ValueError(f"core metric {key} is unavailable")
    return values


def _proxy_evaluate(
    candidates: pd.DataFrame, protocol: dict[str, object], context: dict[str, object]
) -> tuple[pd.DataFrame, dict[int, pd.Series]]:
    periods = {
        name: (pd.Timestamp(bounds[0]), pd.Timestamp(bounds[1]))
        for name, bounds in protocol["development_windows"].items()
    }
    rows: list[dict[str, object]] = []
    targets: dict[int, pd.Series] = {}
    for candidate in candidates.itertuples(index=False):
        row = pd.Series(candidate._asdict())
        candidate_id = int(row["candidate_id"])
        target = _target_for_weights(_weights(row, context["factor_names"]), context)
        targets[candidate_id] = target
        results = run_period_backtests(
            context["data"].daily,
            target,
            periods,
            fee_rate=float(protocol["fee_rate"]),
            init_cash=float(protocol["init_cash"]),
        )
        for period, result in results.items():
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "period": period,
                    **_comparison_metrics(result, float(protocol["init_cash"])),
                }
            )
    return pd.DataFrame(rows), targets


def _stage2_candidates(
    stage1: pd.DataFrame,
    seed_ids: Sequence[int],
    protocol: dict[str, object],
    context: dict[str, object],
) -> pd.DataFrame:
    indexed = stage1.set_index("candidate_id")
    anchors = {
        f"seed_{candidate_id}": _weights(indexed.loc[int(candidate_id)], context["factor_names"])
        for candidate_id in seed_ids
    }
    generated = dirichlet_weight_candidates(
        anchors,
        samples_per_anchor=int(protocol["stage2"]["samples_per_seed"]),
        concentrations={name: float(protocol["stage2"]["concentration"]) for name in anchors},
        seed=int(protocol["stage2"]["seed"]),
    )
    generated = generated.loc[~generated["is_anchor"]].reset_index(drop=True)
    generated["candidate_id"] = np.arange(len(stage1), len(stage1) + len(generated))
    generated = generated.rename(
        columns={name: f"{FACTOR_PREFIX}{name}" for name in context["factor_names"]}
    )
    generated.insert(1, "stage", 2)
    generated["parent_seed_id"] = generated["anchor_name"].str.removeprefix("seed_").astype(int)
    return _attach_group_shares(generated, context)


def _period_slice(index: pd.DatetimeIndex, start: str, end: str) -> pd.DatetimeIndex:
    selected = index[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]
    prior = index[index < selected[0]]
    if selected.empty or prior.empty:
        raise ValueError(f"formal period {start}..{end} lacks prices or prior signal")
    return prior[-1:].append(selected)


def _cycle_diagnostics(
    orders: pd.DataFrame, regimes: pd.Series, sessions: pd.DatetimeIndex
) -> tuple[dict[str, object], pd.DataFrame]:
    cycles = closed_trade_ledger(orders).copy()
    if cycles.empty:
        return {"range_to_range_count": 0, "range_to_range_return": 0.0,
                "range_to_range_short_loss_count": 0, "range_to_trend_count": 0,
                "range_to_trend_return": 0.0}, cycles
    labels = regimes.astype("string").copy()
    labels.index = pd.DatetimeIndex(labels.index).normalize()
    locations = pd.Series(np.arange(len(sessions)), index=pd.DatetimeIndex(sessions).normalize())
    for column in ("entry_signal_date", "exit_signal_date", "entry_date", "exit_date"):
        cycles[column] = pd.to_datetime(cycles[column])
    cycles["entry_regime"] = cycles["entry_signal_date"].dt.normalize().map(labels)
    cycles["exit_regime"] = cycles["exit_signal_date"].dt.normalize().map(labels)
    cycles["regime_path"] = cycles["entry_regime"].astype(str) + "_to_" + cycles["exit_regime"].astype(str)
    cycles["holding_sessions"] = (
        cycles["exit_date"].dt.normalize().map(locations)
        - cycles["entry_date"].dt.normalize().map(locations)
    ).astype(int)
    cycles["short_loss"] = cycles["net_return"].astype(float).lt(0.0) & cycles["holding_sessions"].le(10)

    def compound(path: str) -> float:
        values = cycles.loc[cycles["regime_path"].eq(path), "net_return"].astype(float)
        return float(np.prod(1.0 + values) - 1.0) if len(values) else 0.0

    pure = cycles.loc[cycles["regime_path"].eq("range_to_range")]
    transition = cycles.loc[cycles["regime_path"].eq("range_to_trend")]
    return {
        "range_to_range_count": int(len(pure)),
        "range_to_range_return": compound("range_to_range"),
        "range_to_range_short_loss_count": int(pure["short_loss"].sum()),
        "range_to_trend_count": int(len(transition)),
        "range_to_trend_return": compound("range_to_trend"),
    }, cycles


def _formal_evaluate(
    candidate_ids: Sequence[int],
    candidates: pd.DataFrame,
    targets: dict[int, pd.Series],
    protocol: dict[str, object],
    context: dict[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    execution = context["execution"]
    daily = context["daily"]
    entry_limits = entry_limit_series(
        daily,
        execution.entry_limit_family,
        execution.entry_limit_parameter,
        tick=execution.instrument.price_tick,
    )
    rows: list[dict[str, object]] = []
    cycle_frames: list[pd.DataFrame] = []
    minimum_cash = float("inf")
    lot_invariant = True
    for candidate_id in candidate_ids:
        for period, bounds in protocol["development_windows"].items():
            selected = _period_slice(daily.index, bounds[0], bounds[1])
            intraday = context["data"].intraday.copy()
            intraday_dates = pd.to_datetime(intraday["dt"]).dt.normalize()
            intraday = intraday.loc[intraday_dates.isin(selected.normalize())]
            simulation = simulate_limit_policy(
                daily=daily.loc[selected],
                intraday=intraday,
                target_position=targets[int(candidate_id)].loc[selected],
                entry_limits=entry_limits.loc[selected],
                fee_rate=execution.capital.fee_rate,
                init_cash=float(protocol["init_cash"]),
                lot_size=execution.instrument.lot_size,
                fill_on_equal_touch=False,
            )
            metrics = policy_metrics(simulation, float(protocol["init_cash"]))
            values = {
                "max_drawdown": metrics["max_drawdown"],
                "calmar": metrics["calmar"],
                "win_loss_ratio": metrics["win_loss_ratio"],
                "win_loss_ratio_status": metrics["win_loss_ratio_status"],
                "strategy_return": metrics["return"],
                "sharpe": metrics["sharpe"],
                "trade_count": metrics["trade_count"],
                "t1_fill_rate": metrics["t1_fill_rate"],
                "final_fill_rate": metrics["final_fill_rate"],
            }
            for key in ("max_drawdown", "calmar", "win_loss_ratio"):
                if values[key] is None or not np.isfinite(float(values[key])):
                    raise ValueError(f"formal core metric {key} is unavailable")
            diagnostics, cycles = _cycle_diagnostics(
                simulation.orders, context["regimes"], daily.index
            )
            rows.append(
                {"candidate_id": int(candidate_id), "period": period, **values, **diagnostics}
            )
            if not cycles.empty:
                cycles.insert(0, "period", period)
                cycles.insert(0, "candidate_id", int(candidate_id))
                cycle_frames.append(cycles)
            minimum_cash = min(minimum_cash, float(simulation.daily_state["cash"].min()))
            sizes = simulation.orders["size"].astype(float) if not simulation.orders.empty else pd.Series(dtype=float)
            lot_invariant = lot_invariant and bool(sizes.empty or sizes.mod(execution.instrument.lot_size).eq(0).all())
    audit = {
        "minimum_cash": minimum_cash,
        "cash_non_negative": minimum_cash >= -1e-8,
        "all_orders_in_lots": lot_invariant,
        "fill_on_equal_touch": False,
        "lot_size": execution.instrument.lot_size,
        "entry_limit_family": execution.entry_limit_family,
        "entry_limit_parameter": execution.entry_limit_parameter,
    }
    if not audit["cash_non_negative"] or not audit["all_orders_in_lots"]:
        raise AssertionError("formal execution invariants failed")
    cycles = pd.concat(cycle_frames, ignore_index=True) if cycle_frames else pd.DataFrame()
    return pd.DataFrame(rows), cycles, audit


def _write_documents(
    result: dict[str, object], formal_metrics: pd.DataFrame, formal_profiles: pd.DataFrame
) -> None:
    (EXPERIMENT_DIR / "03_execution.md").write_text(
        "# 0903_EX02 执行\n\n"
        f"- 执行提交：`{result['execution_commit']}`。\n"
        f"- 全局候选：{result['stage1_candidate_count']}；局部候选：{result['stage2_candidate_count']}。\n"
        f"- 正式执行复核：{result['formal_shortlist_count']}项候选、{result['window_count']}个窗口。\n"
        f"- 总耗时：{result['elapsed_seconds']:.2f}秒。\n"
        "- S001-v1、行情数据和既有实验均未修改。\n",
        encoding="utf-8",
    )
    ids = [int(result["control_candidate_id"]), int(result["best_challenger_id"])]
    selected = formal_metrics.loc[formal_metrics["candidate_id"].isin(ids)].copy()
    profiles = formal_profiles.set_index("candidate_id")
    lines = [
        "# 0903_EX02 结论",
        "",
        "状态：`COMPLETE`。本轮没有硬性PASS/FAIL门槛，不自动冻结或晋升策略。",
        "",
        "## S001-v1与最佳挑战者",
        "",
        "| 窗口 | 方案 | 最大回撤 | 卡玛 | 盈亏比 | 收益率 | 纯range收益 | range→trend收益 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in selected.sort_values(["period", "candidate_id"]).itertuples():
        label = "S001-v1" if int(row.candidate_id) == ids[0] else f"挑战者{ids[1]}"
        lines.append(
            f"| {row.period} | {label} | {row.max_drawdown:.2%} | {row.calmar:.4f} | "
            f"{row.win_loss_ratio:.4f} | {row.strategy_return:.2%} | "
            f"{row.range_to_range_return:.2%} | {row.range_to_trend_return:.2%} |"
        )
    challenger = profiles.loc[ids[1]]
    control = profiles.loc[ids[0]]
    lines.extend(
        [
            "",
            "## 稳健性摘要",
            "",
            f"最佳挑战者{ids[1]}在{int(challenger.first_front_count)}个窗口位于三指标Pareto第一前沿，最差层级{int(challenger.worst_pareto_layer)}，平均层级{challenger.mean_pareto_layer:.2f}。",
            f"S001-v1在{int(control.first_front_count)}个窗口位于第一前沿，最差层级{int(control.worst_pareto_layer)}，平均层级{control.mean_pareto_layer:.2f}。",
            "",
            "收益率没有参与候选排序；最大回撤、卡玛和盈亏比共同决定Pareto层级。全部结果来自截至2026-09-02的开发池，不构成新的样本外证据。是否冻结为S001-v2，应在审阅邻域稳定性和交易诊断后单独决定。",
        ]
    )
    (EXPERIMENT_DIR / "04_conclusion.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run() -> dict[str, object]:
    started = time.perf_counter()
    protocol = _protocol()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    before = _critical_hashes()
    context = _load_context(protocol)
    stage1 = _candidate_table(protocol, context)
    if len(stage1) != int(protocol["stage1_candidate_count"]):
        raise AssertionError("stage1 candidate count differs")
    stage1_metrics, stage1_targets = _proxy_evaluate(stage1, protocol, context)
    stage1_metrics, stage1_profiles = robust_pareto_profiles(
        stage1_metrics, protocol["core_metrics"], tolerance=float(protocol["comparison_tolerance"])
    )
    seed_ids = select_robust_seeds(stage1_profiles, limit=int(protocol["stage1_seed_count"]))
    stage2 = _stage2_candidates(stage1, seed_ids, protocol, context)
    if len(stage2) != int(protocol["stage2_candidate_count"]):
        raise AssertionError("stage2 candidate count differs")
    stage2_metrics, stage2_targets = _proxy_evaluate(stage2, protocol, context)
    combined = pd.concat([stage1, stage2], ignore_index=True)
    combined_metrics, combined_profiles = robust_pareto_profiles(
        pd.concat([stage1_metrics.drop(columns="pareto_layer"), stage2_metrics], ignore_index=True),
        protocol["core_metrics"],
        tolerance=float(protocol["comparison_tolerance"]),
    )
    control_id = int(stage1.loc[stage1["anchor_name"].eq("control") & stage1["is_anchor"], "candidate_id"].iloc[0])
    shortlist_ids = shortlist_candidate_ids(
        combined_profiles, control_id=control_id, limit=int(protocol["formal_shortlist_count"])
    )
    all_targets = {**stage1_targets, **stage2_targets}
    formal_metrics, formal_cycles, execution_audit = _formal_evaluate(
        shortlist_ids, combined, all_targets, protocol, context
    )
    formal_metrics, formal_profiles = robust_pareto_profiles(
        formal_metrics, protocol["core_metrics"], tolerance=float(protocol["comparison_tolerance"])
    )
    challengers = formal_profiles.loc[formal_profiles["candidate_id"].ne(control_id)]
    best_challenger_id = select_robust_seeds(challengers, limit=1)[0]
    formal_candidates = combined.loc[combined["candidate_id"].isin(shortlist_ids)].copy()
    neighborhood = nearest_neighborhood(
        formal_candidates,
        representative_id=best_challenger_id,
        factor_names=[f"{FACTOR_PREFIX}{name}" for name in context["factor_names"]],
        member_count=min(7, len(formal_candidates)),
    )
    after = _critical_hashes()
    if before != after:
        raise AssertionError("critical inputs changed during experiment")
    result = {
        "status": "COMPLETE",
        "experiment_id": "0903_EX02",
        "execution_commit": _git_head(),
        "strategy_release": protocol["strategy_release"],
        "development_end": protocol["development_end"],
        "stage1_candidate_count": len(stage1),
        "stage1_seed_ids": seed_ids,
        "stage2_candidate_count": len(stage2),
        "formal_shortlist_count": len(shortlist_ids),
        "formal_shortlist_ids": shortlist_ids,
        "control_candidate_id": control_id,
        "best_challenger_id": best_challenger_id,
        "window_count": len(protocol["development_windows"]),
        "core_metrics": protocol["core_metrics"],
        "return_used_for_selection": False,
        "automatic_promotion": False,
        "execution_audit": execution_audit,
        "data_hashes": context["data"].hashes,
        "critical_hashes": before,
        "elapsed_seconds": time.perf_counter() - started,
    }
    _write_csv(ARTIFACTS / "stage1_candidates.csv", stage1)
    _write_csv(ARTIFACTS / "stage1_period_metrics.csv", stage1_metrics)
    _write_csv(ARTIFACTS / "stage1_profiles.csv", stage1_profiles)
    _write_csv(ARTIFACTS / "stage2_candidates.csv", stage2)
    _write_csv(ARTIFACTS / "proxy_period_metrics.csv", combined_metrics)
    _write_csv(ARTIFACTS / "proxy_profiles.csv", combined_profiles)
    _write_csv(ARTIFACTS / "formal_shortlist_candidates.csv", formal_candidates)
    _write_csv(ARTIFACTS / "formal_period_metrics.csv", formal_metrics)
    _write_csv(ARTIFACTS / "formal_profiles.csv", formal_profiles)
    _write_csv(ARTIFACTS / "representative_neighborhood.csv", neighborhood)
    _write_csv(ARTIFACTS / "formal_cycles.csv", formal_cycles)
    _write_json(ARTIFACTS / "metrics.json", result)
    _write_documents(result, formal_metrics, formal_profiles)
    build_experiment_manifest(
        EXPERIMENT_DIR,
        {
            "schema_version": 1,
            "experiment_id": "0903_EX02",
            "date": "2026-09-03",
            "status": "COMPLETE",
            "symbol": "588080.SH",
            "strategy_release": protocol["strategy_release"],
            "visible_sample_end": protocol["development_end"],
            "automatic_promotion": False,
        },
    )
    validate_experiment_archive(EXPERIMENT_DIR)
    return result


if __name__ == "__main__":
    print(json.dumps(_clean(run()), ensure_ascii=False))
