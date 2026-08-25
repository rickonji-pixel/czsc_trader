"""EX04 return-only optimization over the fixed champion factors."""

from __future__ import annotations

from collections.abc import Mapping
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
from .factors import generate_factor_frame
from .four_layer import (
    normalized_signal_factors,
    positions_from_scores,
    score_four_layer,
    validate_fixed_factor_weights,
)
from .four_layer_runner import (
    _PeriodEvaluator,
    _equivalence,
    _events,
    _metrics,
    _write_json,
    coordinate_optimize,
)
from .objectives import TARGET_PERIODS
from .rules import FACTOR_COLUMNS, positions_for_rule


SELECTION_CUTOFF = pd.Timestamp("2025-12-31")
VALIDATION_WINDOWS = (
    "2022H1", "2022H2", "2023H1", "2023H2",
    "2024H1", "2024H2", "2025H1", "2025H2",
)


@dataclass(frozen=True)
class ReturnSpec:
    step: float
    rounds: int
    allow_sign_flip: bool
    enter: float
    exit: float

    @property
    def spec_id(self) -> str:
        flip = "flip" if self.allow_sign_flip else "keep"
        return f"step{self.step:.4f}_r{self.rounds}_{flip}_en{self.enter:+.3f}_ex{self.exit:+.3f}"


def build_return_specs(protocol: Mapping[str, object]) -> tuple[ReturnSpec, ...]:
    return tuple(
        ReturnSpec(float(step), int(rounds), bool(flip), float(enter), float(exit_))
        for step in protocol["steps"]
        for rounds in protocol["rounds"]
        for flip in protocol["allow_sign_flip"]
        for enter in protocol["enter_thresholds"]
        for exit_ in protocol["exit_thresholds"]
        if float(exit_) < float(enter)
    )


def half_year_periods(start_year: int, end_year: int) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for year in range(start_year, end_year + 1):
        periods[f"{year}H1"] = (pd.Timestamp(year, 1, 1), pd.Timestamp(year, 6, 30))
        periods[f"{year}H2"] = (pd.Timestamp(year, 7, 1), pd.Timestamp(year, 12, 31))
    return periods


def training_windows_before(
    periods: Mapping[str, tuple[pd.Timestamp, pd.Timestamp]], validation: str
) -> tuple[str, ...]:
    start = periods[validation][0]
    return tuple(name for name, (_, end) in periods.items() if end < start)


def return_objective(
    champion: Mapping[str, Mapping[str, object]],
    challenger: Mapping[str, Mapping[str, object]],
    weights: pd.Series,
    origin: pd.Series,
) -> tuple[float, ...]:
    deltas = [
        float(challenger[name]["strategy_return"])
        - float(champion[name]["strategy_return"])
        for name in champion
    ]
    return (
        float(sum(delta > 0.0 for delta in deltas)),
        min(deltas),
        float(np.median(deltas)),
        float(np.mean(deltas)),
        -float((weights - origin).abs().sum()),
    )


def rank_return_results(rows: pd.DataFrame) -> pd.DataFrame:
    return rows.sort_values(
        [
            "win_count",
            "min_return_delta",
            "median_return_delta",
            "mean_return_delta",
            "weight_shift",
            "spec_id",
        ],
        ascending=[False, False, False, False, True, True],
        kind="stable",
    ).reset_index(drop=True)


def return_holdout_pass(windows: Mapping[str, Mapping[str, object]]) -> bool:
    return set(windows) == set(TARGET_PERIODS) and all(
        float(payload["challenger_return"]) > float(payload["champion_return"])
        for payload in windows.values()
    )


def _validate_protocol(protocol: Mapping[str, object]) -> None:
    if protocol.get("experiment_type") != "return_only_fixed_factor_challenger":
        raise ValueError("not an EX04 return-only protocol")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("protocol must remain PRE_REGISTERED")
    if protocol.get("selection_sample_end") != "2025-12-31":
        raise ValueError("selection must stop at 2025-12-31")
    if protocol.get("factor_count") != 12:
        raise ValueError("EX04 must keep 12 factors")
    if protocol.get("factor_policy") != "exact_champion_signals_no_add_drop_or_reselection":
        raise ValueError("EX04 protocol permits factor changes")
    if protocol.get("selection_metric") != "strategy_return_only":
        raise ValueError("EX04 selection must use return only")
    if protocol.get("holdout_access_before_freeze") is not False:
        raise ValueError("holdout access must be forbidden before freeze")
    if tuple(protocol.get("validation_windows", [])) != VALIDATION_WINDOWS:
        raise ValueError("EX04 validation windows differ from protocol")
    if tuple(protocol.get("holdout_windows", [])) != tuple(TARGET_PERIODS):
        raise ValueError("EX04 holdout windows differ from objectives")
    if len(build_return_specs(protocol)) != 144:
        raise ValueError("EX04 must contain 144 configurations")


def _fit_weights(
    factors: pd.DataFrame,
    origin: pd.Series,
    state_rule: Any,
    evaluator: _PeriodEvaluator,
    step: float,
    rounds: int,
    allow_sign_flip: bool,
    minimum: float,
) -> pd.Series:
    champion_target = positions_from_scores(
        score_four_layer(factors, origin), state_rule.enter, state_rule.exit, state_rule
    )
    champion = evaluator.evaluate(champion_target)

    def evaluate(weights: pd.Series) -> tuple[float, ...]:
        target = positions_from_scores(
            score_four_layer(factors, weights), state_rule.enter, state_rule.exit, state_rule
        )
        return return_objective(champion, evaluator.evaluate(target), weights, origin)

    fitted = coordinate_optimize(
        origin,
        step=step,
        rounds=rounds,
        allow_sign_flip=allow_sign_flip,
        minimum_absolute_weight=minimum,
        evaluator=evaluate,
    )
    validate_fixed_factor_weights(fitted, factors.columns, minimum)
    return fitted


def _selection(
    data: Any,
    factors: pd.DataFrame,
    origin: pd.Series,
    state_rule: Any,
    protocol: Mapping[str, object],
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> tuple[ReturnSpec, pd.Series, dict[str, object]]:
    periods = half_year_periods(2021, 2025)
    validation_periods = {name: periods[name] for name in VALIDATION_WINDOWS}
    validation_evaluator = _PeriodEvaluator(data.daily, validation_periods, fee_rate, init_cash)
    champion_target = positions_from_scores(
        score_four_layer(factors, origin), state_rule.enter, state_rule.exit, state_rule
    )
    champion_validation = validation_evaluator.evaluate(champion_target)
    specs = build_return_specs(protocol)
    minimum = float(protocol["minimum_absolute_weight"])
    fitted_cache: dict[tuple[float, int, bool, str], pd.Series] = {}
    rows: list[dict[str, object]] = []
    for spec in specs:
        metrics: dict[str, dict[str, object]] = {}
        shifts: list[float] = []
        for validation in VALIDATION_WINDOWS:
            key = (spec.step, spec.rounds, spec.allow_sign_flip, validation)
            weights = fitted_cache.get(key)
            if weights is None:
                training_names = training_windows_before(periods, validation)
                training_periods = {name: periods[name] for name in training_names}
                weights = _fit_weights(
                    factors,
                    origin,
                    state_rule,
                    _PeriodEvaluator(data.daily, training_periods, fee_rate, init_cash),
                    spec.step,
                    spec.rounds,
                    spec.allow_sign_flip,
                    minimum,
                )
                fitted_cache[key] = weights
            target = positions_from_scores(
                score_four_layer(factors, weights), spec.enter, spec.exit, state_rule
            )
            metrics[validation] = validation_evaluator.evaluate(target)[validation]
            shifts.append(float((weights - origin).abs().sum()))
        deltas = {
            name: float(metrics[name]["strategy_return"])
            - float(champion_validation[name]["strategy_return"])
            for name in VALIDATION_WINDOWS
        }
        row: dict[str, object] = {
            **asdict(spec),
            "spec_id": spec.spec_id,
            "factor_count": 12,
            "win_count": sum(delta > 0.0 for delta in deltas.values()),
            "min_return_delta": min(deltas.values()),
            "median_return_delta": float(np.median(list(deltas.values()))),
            "mean_return_delta": float(np.mean(list(deltas.values()))),
            "weight_shift": float(np.mean(shifts)),
        }
        for name in VALIDATION_WINDOWS:
            row[f"{name}_return"] = metrics[name]["strategy_return"]
            row[f"{name}_return_delta"] = deltas[name]
            row[f"{name}_sharpe"] = metrics[name]["sharpe"]
            row[f"{name}_win"] = deltas[name] > 0.0
        rows.append(row)
    ranked = rank_return_results(pd.DataFrame(rows))
    ranked.to_csv(artifacts / "candidate_results.csv", index=False, encoding="utf-8-sig")
    best_id = str(ranked.iloc[0]["spec_id"])
    best = next(spec for spec in specs if spec.spec_id == best_id)
    final_weights = _fit_weights(
        factors,
        origin,
        state_rule,
        _PeriodEvaluator(data.daily, periods, fee_rate, init_cash),
        best.step,
        best.rounds,
        best.allow_sign_flip,
        minimum,
    )
    final_weights.rename_axis("factor").reset_index().to_csv(
        artifacts / "factor_weights.csv", index=False, encoding="utf-8-sig"
    )
    summary = {
        "best_spec": best_id,
        "candidate_count": len(ranked),
        "best_win_count": int(ranked.iloc[0]["win_count"]),
        "best_min_return_delta": float(ranked.iloc[0]["min_return_delta"]),
        "best_median_return_delta": float(ranked.iloc[0]["median_return_delta"]),
        "best_mean_return_delta": float(ranked.iloc[0]["mean_return_delta"]),
        "final_weight_shift": float((final_weights - origin).abs().sum()),
        "selection_cutoff": "2025-12-31",
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
    }
    _write_json(artifacts / "selection_metrics.json", summary)
    return best, final_weights, summary


def _component_attribution(
    data: Any,
    factors: pd.DataFrame,
    origin: pd.Series,
    weights: pd.Series,
    spec: ReturnSpec,
    state_rule: Any,
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> None:
    periods = half_year_periods(2021, 2025)
    evaluator = _PeriodEvaluator(data.daily, periods, fee_rate, init_cash)
    variants = {
        "champion": (origin, state_rule.enter, state_rule.exit),
        "weights_only": (weights, state_rule.enter, state_rule.exit),
        "thresholds_only": (origin, spec.enter, spec.exit),
        "combined": (weights, spec.enter, spec.exit),
    }
    rows: list[dict[str, object]] = []
    for variant, (variant_weights, enter, exit_) in variants.items():
        target = positions_from_scores(
            score_four_layer(factors, variant_weights), enter, exit_, state_rule
        )
        for window, metrics in evaluator.evaluate(target).items():
            rows.append(
                {
                    "variant": variant,
                    "window": window,
                    "strategy_return": metrics["strategy_return"],
                    "sharpe": metrics["sharpe"],
                    "max_drawdown": metrics["max_drawdown"],
                    "exposure": metrics["exposure"],
                    "trade_count": metrics["trade_count"],
                }
            )
    pd.DataFrame(rows).to_csv(
        artifacts / "component_attribution.csv", index=False, encoding="utf-8-sig"
    )


def _holdout(
    raw_dir: Path,
    baseline: Any,
    weights: pd.Series,
    spec: ReturnSpec,
    frozen_digest: str,
    artifacts: Path,
    fee_rate: float,
    init_cash: float,
) -> dict[str, object]:
    cutoff = max(end for _, end in TARGET_PERIODS.values())
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=cutoff)
    frame = generate_factor_frame(data).frame
    factors = normalized_signal_factors(frame.filter(like="raw__"))
    validate_fixed_factor_weights(weights, factors.columns, 0.005)
    scores = score_four_layer(factors, weights)
    target = positions_from_scores(scores, spec.enter, spec.exit, baseline.rule)
    champion_target, _ = positions_for_rule(frame.loc[:, FACTOR_COLUMNS], baseline.rule)
    champion_results = run_period_backtests(
        data.daily, champion_target, TARGET_PERIODS, fee_rate=fee_rate, init_cash=init_cash
    )
    events = _events(target, scores, spec.enter, spec.exit)
    audit_frame = pd.DataFrame(
        {"factor_score": scores, "enter_threshold": spec.enter, "exit_threshold": spec.exit},
        index=scores.index,
    )
    challenger_results = run_period_backtests(
        data.daily, target, TARGET_PERIODS, fee_rate=fee_rate, init_cash=init_cash,
        factor_events=events, factor_frame=audit_frame,
    )
    champion_metrics, challenger_metrics = _metrics(champion_results), _metrics(challenger_results)
    windows: dict[str, dict[str, object]] = {}
    for name in TARGET_PERIODS:
        champion, challenger = champion_metrics[name], challenger_metrics[name]
        windows[name] = {
            "champion_return": champion["strategy_return"],
            "challenger_return": challenger["strategy_return"],
            "return_delta": float(challenger["strategy_return"]) - float(champion["strategy_return"]),
            "champion_sharpe": champion["sharpe"],
            "challenger_sharpe": challenger["sharpe"],
            "sharpe_delta": float(challenger["sharpe"]) - float(champion["sharpe"]),
            "champion_exposure": champion["exposure"],
            "challenger_exposure": challenger["exposure"],
            "pass": float(challenger["strategy_return"]) > float(champion["strategy_return"]),
        }
    overall = return_holdout_pass(windows)
    orders = pd.concat([result.orders for result in challenger_results.values()], ignore_index=True)
    used_events = pd.concat(
        [events, *[result.factor_events for result in challenger_results.values()]], ignore_index=True
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
    spec: ReturnSpec,
) -> None:
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n"
        f"- 状态：正式执行完成（`{holdout['status']}`）\n"
        f"- 预注册配置数：{selection['candidate_count']}\n"
        f"- 冻结配置：`{selection['best_spec']}`\n"
        f"- 半年验证收益胜出：{selection['best_win_count']}/8\n"
        "- 选优指标：仅收益率\n"
        "- 冻结前访问2026：否\n"
        "- 冻结后访问2026：是\n"
        f"- 因果审计：`{holdout['audit']['status']}`\n",
        encoding="utf-8",
    )
    lines = [
        "# 研究结论", "", f"EX04收益率单目标挑战结果为 **{holdout['status']}**。", "",
        "| 窗口 | 冠军收益 | 挑战者收益 | 收益增量 | 冠军夏普 | 挑战者夏普 | 判定 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for name in TARGET_PERIODS:
        row = holdout["windows"][name]
        lines.append(
            f"| {name} | {float(row['champion_return']):.4%} | "
            f"{float(row['challenger_return']):.4%} | {float(row['return_delta']):+.4%} | "
            f"{float(row['champion_sharpe']):.4f} | {float(row['challenger_sharpe']):.4f} | "
            f"{'PASS' if row['pass'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "", "## 边界", "",
            "- 因子集合、归一化和状态机保持冠军一致。",
            f"- 冻结阈值：入场{spec.enter:.3f}，离场{spec.exit:.3f}。",
            "- 夏普率仅展示，未参与选优或PASS。",
            "- 留出结果未参与二次调参。",
            "- 权重与阈值的独立贡献见`artifacts/component_attribution.csv`。",
        ]
    )
    (experiment_dir / "04_conclusion.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_return_only_experiment(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    _validate_protocol(protocol)
    baseline = resolve_baseline(Path(baseline_root), str(protocol["champion"]["version"]))
    if baseline.sha256 != str(protocol["champion"]["sha256"]):
        raise ValueError("champion hash differs from preregistration")
    selection_data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=SELECTION_CUTOFF)
    frame = generate_factor_frame(selection_data).frame
    factors, origin, proof = _equivalence(frame, baseline.rule, 1e-12)
    _write_json(artifacts / "equivalence_proof.json", proof)
    best, weights, selection = _selection(
        selection_data, factors, origin, baseline.rule, protocol, artifacts, fee_rate, init_cash
    )
    _component_attribution(
        selection_data, factors, origin, weights, best, baseline.rule, artifacts, fee_rate, init_cash
    )
    frozen = {
        "schema_version": 1,
        "experiment": experiment_dir.name,
        "sample_end": "2025-12-31",
        "champion": {"version": baseline.version, "sha256": baseline.sha256},
        "factor_policy": protocol["factor_policy"],
        "factor_names": list(factors.columns),
        "spec": asdict(best),
        "spec_id": best.spec_id,
        "weights": weights.to_dict(),
        "selection_metric": "strategy_return_only",
    }
    frozen_path = artifacts / "frozen_challenger.json"
    _write_json(frozen_path, frozen)
    frozen_digest = sha256(frozen_path.read_bytes()).hexdigest()
    holdout = _holdout(raw_dir, baseline, weights, best, frozen_digest, artifacts, fee_rate, init_cash)
    _write_docs(experiment_dir, selection, holdout, best)
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
        "best_spec": best.spec_id,
        "selection": selection,
        "holdout": holdout,
        "experiment_dir": str(experiment_dir),
    }



