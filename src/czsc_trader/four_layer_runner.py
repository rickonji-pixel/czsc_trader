"""Fixed-factor four-layer optimizer and formal experiment runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .audit import audit_no_lookahead
from .backtest import run_period_backtests
from .baselines import resolve_baseline
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factors import aggregate_signal_groups, generate_factor_frame, signal_groups
from .four_layer import (
    flatten_champion_weights,
    normalized_signal_factors,
    positions_from_scores,
    score_four_layer,
    validate_fixed_factor_weights,
)
from .objectives import TARGET_PERIODS
from .rules import FACTOR_COLUMNS, positions_for_rule


SELECTION_CUTOFF = pd.Timestamp("2025-12-31")
ANNUAL_PERIODS = {
    str(year): (pd.Timestamp(year, 1, 1), pd.Timestamp(year, 12, 31))
    for year in range(2021, 2026)
}
VALIDATION_YEARS = ("2023", "2024", "2025")


@dataclass(frozen=True)
class OptimizerSpec:
    step: float
    rounds: int
    allow_sign_flip: bool
    enter: float
    exit: float

    @property
    def optimizer_id(self) -> str:
        flip = "flip" if self.allow_sign_flip else "keep"
        return f"step{self.step:.3f}_r{self.rounds}_{flip}_en{self.enter:+.2f}_ex{self.exit:+.2f}"


def build_optimizer_specs(protocol: dict[str, object]) -> tuple[OptimizerSpec, ...]:
    return tuple(
        OptimizerSpec(float(step), int(rounds), bool(flip), float(enter), float(exit_))
        for step in protocol["steps"]
        for rounds in protocol["rounds"]
        for flip in protocol["allow_sign_flip"]
        for enter in protocol["enter_thresholds"]
        for exit_ in protocol["exit_thresholds"]
        if float(exit_) < float(enter)
    )


def coordinate_optimize(
    start: pd.Series,
    *,
    step: float,
    rounds: int,
    allow_sign_flip: bool,
    minimum_absolute_weight: float,
    evaluator: Callable[[pd.Series], tuple[float, ...]],
) -> pd.Series:
    """Greedily optimize weights while preserving every fixed factor identity."""
    current = start.astype(float).copy()
    current /= float(current.abs().sum())
    current_score = evaluator(current)
    for _ in range(rounds):
        for name in current.index:
            raw_values = [float(current[name]) + step, float(current[name]) - step]
            if allow_sign_flip:
                raw_values.append(-float(current[name]))
            best, best_score = current, current_score
            for value in raw_values:
                candidate = current.copy()
                candidate.loc[name] = value
                norm = float(candidate.abs().sum())
                if norm <= 0.0:
                    continue
                candidate /= norm
                if candidate.abs().lt(minimum_absolute_weight - 1e-15).any():
                    continue
                score = evaluator(candidate)
                if score > best_score:
                    best, best_score = candidate, score
            current, current_score = best, best_score
    return current.rename("weight")


def window_passes(champion: dict[str, object], challenger: dict[str, object]) -> bool:
    return (
        float(challenger["strategy_return"]) > float(champion["strategy_return"])
        and float(challenger["sharpe"]) > float(champion["sharpe"])
    )


def all_holdout_windows_pass(windows: dict[str, dict[str, dict[str, object]]]) -> bool:
    return set(windows) == set(TARGET_PERIODS) and all(
        window_passes(payload["champion"], payload["challenger"])
        for payload in windows.values()
    )


def rank_optimizer_results(rows: pd.DataFrame) -> pd.DataFrame:
    return rows.sort_values(
        [
            "pass_count",
            "min_return_delta",
            "min_sharpe_delta",
            "mean_return_delta",
            "mean_sharpe_delta",
            "weight_shift",
            "optimizer_id",
        ],
        ascending=[False, False, False, False, False, True, True],
        kind="stable",
    ).reset_index(drop=True)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _metrics(results: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {name: result.metrics for name, result in results.items()}


def _target_digest(target: pd.Series) -> str:
    hashed = pd.util.hash_pandas_object(target.astype(float), index=True).to_numpy()
    return sha256(hashed.tobytes()).hexdigest()


class _PeriodEvaluator:
    def __init__(
        self,
        daily: pd.DataFrame,
        periods: Mapping[str, tuple[pd.Timestamp, pd.Timestamp]],
        fee_rate: float,
        init_cash: float,
    ) -> None:
        self.daily = daily
        self.periods = dict(periods)
        self.fee_rate = fee_rate
        self.init_cash = init_cash
        self.cache: dict[str, dict[str, dict[str, object]]] = {}
        self._cache_hits = 0
        self._cache_misses = 0

    @property
    def cache_stats(self) -> dict[str, int]:
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "entries": len(self.cache),
        }

    def evaluate(self, target: pd.Series) -> dict[str, dict[str, object]]:
        digest = _target_digest(target)
        if digest in self.cache:
            self._cache_hits += 1
        else:
            self._cache_misses += 1
            self.cache[digest] = _metrics(
                run_period_backtests(
                    self.daily,
                    target,
                    self.periods,
                    fee_rate=self.fee_rate,
                    init_cash=self.init_cash,
                )
            )
        return self.cache[digest]


def _objective(
    champion: Mapping[str, Mapping[str, object]],
    challenger: Mapping[str, Mapping[str, object]],
    weights: pd.Series,
    origin: pd.Series,
) -> tuple[float, ...]:
    return_deltas = [
        float(challenger[name]["strategy_return"])
        - float(champion[name]["strategy_return"])
        for name in champion
    ]
    sharpe_deltas = [
        float(challenger[name]["sharpe"]) - float(champion[name]["sharpe"])
        for name in champion
    ]
    passes = [window_passes(dict(champion[name]), dict(challenger[name])) for name in champion]
    return (
        float(sum(passes)),
        min(return_deltas),
        min(sharpe_deltas),
        float(np.mean(return_deltas)),
        float(np.mean(sharpe_deltas)),
        -float((weights - origin).abs().sum()),
    )


def _fit_weights(
    factors: pd.DataFrame,
    origin: pd.Series,
    state_rule: Any,
    periods: Mapping[str, tuple[pd.Timestamp, pd.Timestamp]],
    evaluator: _PeriodEvaluator,
    spec: OptimizerSpec,
    minimum_absolute_weight: float,
) -> pd.Series:
    champion_metrics = evaluator.evaluate(
        positions_from_scores(
            score_four_layer(factors, origin), state_rule.enter, state_rule.exit, state_rule
        )
    )

    def evaluate_weights(weights: pd.Series) -> tuple[float, ...]:
        scores = score_four_layer(factors, weights)
        target = positions_from_scores(scores, state_rule.enter, state_rule.exit, state_rule)
        metrics = evaluator.evaluate(target)
        return _objective(champion_metrics, metrics, weights, origin)

    fitted = coordinate_optimize(
        origin,
        step=spec.step,
        rounds=spec.rounds,
        allow_sign_flip=spec.allow_sign_flip,
        minimum_absolute_weight=minimum_absolute_weight,
        evaluator=evaluate_weights,
    )
    validate_fixed_factor_weights(fitted, factors.columns, minimum_absolute_weight)
    return fitted


def _validate_protocol(protocol: Mapping[str, object]) -> None:
    if protocol.get("experiment_type") != "fixed_factor_four_layer_challenger":
        raise ValueError("not a fixed-factor four-layer protocol")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("protocol must remain PRE_REGISTERED before execution")
    if protocol.get("selection_sample_end") != "2025-12-31":
        raise ValueError("selection must stop at 2025-12-31")
    if protocol.get("factor_count") != 12:
        raise ValueError("protocol must keep exactly 12 champion factors")
    if protocol.get("factor_policy") != "exact_champion_signals_no_add_drop_or_reselection":
        raise ValueError("protocol permits factor selection")
    if protocol.get("holdout_access_before_freeze") is not False:
        raise ValueError("protocol must forbid holdout access before freeze")
    if tuple(protocol.get("selection_windows", [])) != VALIDATION_YEARS:
        raise ValueError("selection windows must be 2023 through 2025")
    if tuple(protocol.get("holdout_windows", [])) != tuple(TARGET_PERIODS):
        raise ValueError("holdout windows differ from registered objectives")
    if len(build_optimizer_specs(dict(protocol))) != 72:
        raise ValueError("optimizer grid must contain 72 configurations")


def _equivalence(
    factor_frame: pd.DataFrame,
    state_rule: Any,
    tolerance: float,
) -> tuple[pd.DataFrame, pd.Series, dict[str, object]]:
    raw = factor_frame.filter(like="raw__")
    normalized = normalized_signal_factors(raw)
    groups = signal_groups(normalized.columns)
    flat_weights = flatten_champion_weights(
        list(normalized.columns), groups, state_rule.weights
    )
    validate_fixed_factor_weights(flat_weights, normalized.columns, 0.0)
    grouped = aggregate_signal_groups(normalized, groups)
    champion_target, champion_scores = positions_for_rule(grouped, state_rule)
    flat_scores = score_four_layer(normalized, flat_weights)
    flat_target = positions_from_scores(
        flat_scores, state_rule.enter, state_rule.exit, state_rule
    )
    max_error = float((flat_scores - champion_scores).abs().max())
    positions_equal = bool(flat_target.equals(champion_target))
    if max_error > tolerance or not positions_equal:
        raise AssertionError(
            f"four-layer equivalence failed: score_error={max_error}, positions_equal={positions_equal}"
        )
    frequency_counts = {
        "30m": sum("raw__30m__" in name for name in normalized.columns),
        "daily": sum("raw__daily__" in name for name in normalized.columns),
        "weekly": sum("raw__weekly__" in name for name in normalized.columns),
    }
    proof = {
        "status": "PASS",
        "factor_count": len(normalized.columns),
        "factor_names": list(normalized.columns),
        "frequency_counts": frequency_counts,
        "max_score_absolute_error": max_error,
        "positions_equal": positions_equal,
        "weight_l1_norm": float(flat_weights.abs().sum()),
    }
    if len(normalized.columns) != 12 or any(value == 0 for value in frequency_counts.values()):
        raise AssertionError("fixed factor set lost a signal or frequency")
    return normalized, flat_weights, proof


def _select(
    data: Any,
    factors: pd.DataFrame,
    origin: pd.Series,
    state_rule: Any,
    protocol: Mapping[str, object],
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> tuple[OptimizerSpec, pd.Series, dict[str, object]]:
    specs = build_optimizer_specs(dict(protocol))
    minimum = float(protocol["minimum_absolute_weight"])
    fitted_cache: dict[tuple[float, int, bool, str], pd.Series] = {}
    rows: list[dict[str, object]] = []
    champion_all_target = positions_from_scores(
        score_four_layer(factors, origin), state_rule.enter, state_rule.exit, state_rule
    )
    validation_evaluator = _PeriodEvaluator(
        data.daily,
        {year: ANNUAL_PERIODS[year] for year in VALIDATION_YEARS},
        fee_rate,
        init_cash,
    )
    champion_validation = validation_evaluator.evaluate(champion_all_target)
    for spec in specs:
        fold_metrics: dict[str, dict[str, object]] = {}
        shifts: list[float] = []
        for validation_year in VALIDATION_YEARS:
            train_years = [
                str(year) for year in range(2021, int(validation_year))
            ]
            cache_key = (spec.step, spec.rounds, spec.allow_sign_flip, validation_year)
            weights = fitted_cache.get(cache_key)
            if weights is None:
                train_periods = {year: ANNUAL_PERIODS[year] for year in train_years}
                train_evaluator = _PeriodEvaluator(
                    data.daily, train_periods, fee_rate, init_cash
                )
                weights = _fit_weights(
                    factors,
                    origin,
                    state_rule,
                    train_periods,
                    train_evaluator,
                    spec,
                    minimum,
                )
                fitted_cache[cache_key] = weights
            scores = score_four_layer(factors, weights)
            target = positions_from_scores(scores, spec.enter, spec.exit, state_rule)
            fold_metrics[validation_year] = validation_evaluator.evaluate(target)[
                validation_year
            ]
            shifts.append(float((weights - origin).abs().sum()))
        return_deltas = {
            year: float(fold_metrics[year]["strategy_return"])
            - float(champion_validation[year]["strategy_return"])
            for year in VALIDATION_YEARS
        }
        sharpe_deltas = {
            year: float(fold_metrics[year]["sharpe"])
            - float(champion_validation[year]["sharpe"])
            for year in VALIDATION_YEARS
        }
        passes = {
            year: window_passes(champion_validation[year], fold_metrics[year])
            for year in VALIDATION_YEARS
        }
        row: dict[str, object] = {
            **asdict(spec),
            "optimizer_id": spec.optimizer_id,
            "factor_count": 12,
            "pass_count": sum(passes.values()),
            "min_return_delta": min(return_deltas.values()),
            "min_sharpe_delta": min(sharpe_deltas.values()),
            "mean_return_delta": float(np.mean(list(return_deltas.values()))),
            "mean_sharpe_delta": float(np.mean(list(sharpe_deltas.values()))),
            "weight_shift": float(np.mean(shifts)),
        }
        for year in VALIDATION_YEARS:
            row[f"{year}_return"] = fold_metrics[year]["strategy_return"]
            row[f"{year}_sharpe"] = fold_metrics[year]["sharpe"]
            row[f"{year}_return_delta"] = return_deltas[year]
            row[f"{year}_sharpe_delta"] = sharpe_deltas[year]
            row[f"{year}_pass"] = passes[year]
        rows.append(row)
    ranked = rank_optimizer_results(pd.DataFrame(rows))
    ranked.to_csv(artifacts / "candidate_results.csv", index=False, encoding="utf-8-sig")
    best_id = str(ranked.iloc[0]["optimizer_id"])
    best = next(spec for spec in specs if spec.optimizer_id == best_id)
    final_evaluator = _PeriodEvaluator(data.daily, ANNUAL_PERIODS, fee_rate, init_cash)
    final_weights = _fit_weights(
        factors,
        origin,
        state_rule,
        ANNUAL_PERIODS,
        final_evaluator,
        best,
        minimum,
    )
    validate_fixed_factor_weights(final_weights, factors.columns, minimum)
    final_weights.rename_axis("factor").reset_index().to_csv(
        artifacts / "factor_weights.csv", index=False, encoding="utf-8-sig"
    )
    summary = {
        "best_optimizer": best_id,
        "candidate_count": len(ranked),
        "best_pass_count": int(ranked.iloc[0]["pass_count"]),
        "best_min_return_delta": float(ranked.iloc[0]["min_return_delta"]),
        "best_min_sharpe_delta": float(ranked.iloc[0]["min_sharpe_delta"]),
        "final_weight_shift": float((final_weights - origin).abs().sum()),
        "selection_cutoff": str(SELECTION_CUTOFF.date()),
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
    }
    _write_json(artifacts / "selection_metrics.json", summary)
    return best, final_weights, summary


def _events(target: pd.Series, scores: pd.Series, enter: float, exit_: float) -> pd.DataFrame:
    previous = target.shift(1, fill_value=0.0)
    rows: list[dict[str, object]] = []
    for date in target.index[target.ne(previous)]:
        after = float(target.loc[date])
        event_type = "Entry" if after == 1.0 else "Exit"
        rows.append(
            {
                "event_id": f"FourLayer:{pd.Timestamp(date):%Y%m%d}:{event_type}",
                "signal_date": pd.Timestamp(date),
                "event_type": event_type,
                "factor_score": float(scores.loc[date]),
                "enter_threshold": enter,
                "exit_threshold": exit_,
                "before_position": float(previous.loc[date]),
                "after_position": after,
            }
        )
    return pd.DataFrame(rows)


def _holdout(
    raw_dir: Path,
    baseline: Any,
    weights: pd.Series,
    spec: OptimizerSpec,
    frozen_digest: str,
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> dict[str, object]:
    cutoff = max(end for _, end in TARGET_PERIODS.values())
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=cutoff)
    factor_frame = generate_factor_frame(data).frame
    factors = normalized_signal_factors(factor_frame.filter(like="raw__"))
    validate_fixed_factor_weights(
        weights, factors.columns, minimum_absolute_weight=0.005
    )
    scores = score_four_layer(factors, weights)
    target = positions_from_scores(scores, spec.enter, spec.exit, baseline.rule)
    champion_target, _ = positions_for_rule(
        factor_frame.loc[:, FACTOR_COLUMNS], baseline.rule
    )
    champion_results = run_period_backtests(
        data.daily, champion_target, TARGET_PERIODS, fee_rate=fee_rate, init_cash=init_cash
    )
    events = _events(target, scores, spec.enter, spec.exit)
    audit_frame = pd.DataFrame(
        {
            "factor_score": scores,
            "enter_threshold": spec.enter,
            "exit_threshold": spec.exit,
        },
        index=scores.index,
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
    champion_metrics, challenger_metrics = _metrics(champion_results), _metrics(challenger_results)
    windows: dict[str, dict[str, object]] = {}
    for name in TARGET_PERIODS:
        champion, challenger = champion_metrics[name], challenger_metrics[name]
        windows[name] = {
            "champion": champion,
            "challenger": challenger,
            "return_delta": float(challenger["strategy_return"])
            - float(champion["strategy_return"]),
            "sharpe_delta": float(challenger["sharpe"])
            - float(champion["sharpe"]),
            "pass": window_passes(champion, challenger),
        }
    overall = all_holdout_windows_pass(windows)  # type: ignore[arg-type]
    orders = pd.concat(
        [result.orders for result in challenger_results.values()], ignore_index=True
    )
    used_events = pd.concat(
        [events, *[result.factor_events for result in challenger_results.values()]],
        ignore_index=True,
    ).drop_duplicates(subset=["event_id"])
    audit = audit_no_lookahead(orders, used_events, target, audit_frame)
    orders.to_csv(artifacts / "orders.csv", index=False, encoding="utf-8-sig")
    payload = {
        "status": "PASS" if overall else "FAIL",
        "windows": windows,
        "audit": audit,
        "frozen_challenger_sha256": frozen_digest,
        "frozen_before_holdout": True,
        "holdout_cutoff": str(cutoff.date()),
        "holdout_data_hashes": data.hashes,
        "holdout_accessed": True,
    }
    _write_json(artifacts / "holdout_metrics.json", payload)
    return payload


def _write_docs(
    experiment_dir: Path,
    selection: Mapping[str, object],
    holdout: Mapping[str, object],
    proof: Mapping[str, object],
    weights: pd.Series,
    origin: pd.Series,
    spec: OptimizerSpec,
) -> None:
    execution = (
        "# 执行过程\n\n"
        f"- 状态：正式执行完成（`{holdout['status']}`）\n"
        f"- 等价证明：`{proof['status']}`，最大得分误差 `{proof['max_score_absolute_error']}`，仓位完全一致\n"
        f"- 固定因子：{proof['factor_count']}项，频率分布 `{proof['frequency_counts']}`\n"
        f"- 预注册配置数：{selection['candidate_count']}\n"
        f"- 冻结配置：`{selection['best_optimizer']}`\n"
        f"- 扩展验证通过：{selection['best_pass_count']}/3\n"
        "- 冻结前访问2026：否\n"
        "- 冻结后访问2026：是\n"
        f"- 因果审计：`{holdout['audit']['status']}`\n"
    )
    (experiment_dir / "03_execution.md").write_text(execution, encoding="utf-8")
    lines = [
        "# 研究结论",
        "",
        f"本轮固定因子四层挑战结果为 **{holdout['status']}**。",
        "",
        "| 窗口 | 冠军收益 | 挑战者收益 | 收益增量 | 冠军夏普 | 挑战者夏普 | 夏普增量 | 判定 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name in TARGET_PERIODS:
        row = holdout["windows"][name]
        champion, challenger = row["champion"], row["challenger"]
        lines.append(
            f"| {name} | {float(champion['strategy_return']):.4%} | "
            f"{float(challenger['strategy_return']):.4%} | {float(row['return_delta']):+.4%} | "
            f"{float(champion['sharpe']):.4f} | {float(challenger['sharpe']):.4f} | "
            f"{float(row['sharpe_delta']):+.4f} | {'PASS' if row['pass'] else 'FAIL'} |"
        )
    changed_signs = int(((weights * origin) < 0.0).sum())
    lines.extend(
        [
            "",
            "## 实验边界",
            "",
            "- 因子集合始终固定为冠军原有12项，未新增、删除或筛选。",
            f"- 权重L1变化：{float((weights - origin).abs().sum()):.6f}；符号翻转：{changed_signs}项。",
            f"- 冻结阈值：入场{spec.enter:.2f}，离场{spec.exit:.2f}。",
            "- 留出结果未参与二次调参。",
        ]
    )
    (experiment_dir / "04_conclusion.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_four_layer_experiment(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> dict[str, object]:
    """Prove equivalence, select on pre-2026 data, freeze, then test once."""
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    _validate_protocol(protocol)
    baseline = resolve_baseline(Path(baseline_root), str(protocol["champion"]["version"]))
    if baseline.sha256 != str(protocol["champion"]["sha256"]):
        raise ValueError("champion hash differs from preregistration")
    selection_data = load_market_data(
        Path(raw_dir), "588080.SH", "etf", cutoff=SELECTION_CUTOFF
    )
    factor_frame = generate_factor_frame(selection_data).frame
    factors, origin, proof = _equivalence(
        factor_frame, baseline.rule, float(protocol["equivalence_tolerance"])
    )
    _write_json(artifacts / "equivalence_proof.json", proof)
    best, weights, selection = _select(
        selection_data,
        factors,
        origin,
        baseline.rule,
        protocol,
        artifacts,
        fee_rate,
        init_cash,
    )
    frozen = {
        "schema_version": 1,
        "experiment": experiment_dir.name,
        "sample_end": "2025-12-31",
        "champion": {"version": baseline.version, "sha256": baseline.sha256},
        "factor_policy": protocol["factor_policy"],
        "factor_names": list(factors.columns),
        "optimizer": asdict(best),
        "optimizer_id": best.optimizer_id,
        "weights": weights.to_dict(),
        "state_machine": {
            "confirm_days": baseline.rule.confirm_days,
            "min_hold_days": baseline.rule.min_hold_days,
            "exit_confirm_days": baseline.rule.exit_confirm_days,
            "entry_gate": baseline.rule.entry_gate,
        },
    }
    frozen_path = artifacts / "frozen_challenger.json"
    _write_json(frozen_path, frozen)
    frozen_digest = sha256(frozen_path.read_bytes()).hexdigest()
    holdout = _holdout(
        Path(raw_dir), baseline, weights, best, frozen_digest, artifacts, fee_rate, init_cash
    )
    _write_docs(experiment_dir, selection, holdout, proof, weights, origin, best)
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": "2026-08-24",
            "status": holdout["status"],
            "symbol": "588080.SH",
            "asset_type": "etf",
            "champion": frozen["champion"],
            "visible_sample_end": "2025-12-31",
            "holdout_accessed": True,
            "frozen_challenger_sha256": frozen_digest,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {
        "status": holdout["status"],
        "best_optimizer": best.optimizer_id,
        "selection": selection,
        "holdout": holdout,
        "experiment_dir": str(experiment_dir),
    }
# CLI dispatch lives in czsc_trader.cli.
