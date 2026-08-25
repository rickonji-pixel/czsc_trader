"""Mechanism and stability diagnostics for the frozen EX04 strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
import json

import numpy as np
import pandas as pd

from .attribution import all_coalitions, shapley_interactions, shapley_values
from .backtest import run_period_backtests
from .baselines import resolve_baseline
from .data import load_market_data
from .factors import generate_factor_frame, signal_groups
from .four_layer import (
    positions_from_scores,
    score_four_layer,
    validate_fixed_factor_weights,
)
from .four_layer_runner import ANNUAL_PERIODS, _PeriodEvaluator, _equivalence
from .return_only_runner import half_year_periods


_COMPONENT_VARIANTS = ("baseline", "weights_only", "thresholds_only", "combined")


def validate_protocol(protocol: Mapping[str, object]) -> None:
    """Reject any drift from the preregistered diagnostic boundary."""
    if protocol.get("experiment_type") != "ex04_mechanism_attribution":
        raise ValueError("not an EX04 mechanism attribution protocol")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("protocol status must remain PRE_REGISTERED")
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise ValueError("visible sample must stop at 2025-12-31")
    if protocol.get("holdout_access_allowed") is not False:
        raise ValueError("holdout access must remain disabled")
    promotion = protocol.get("promotion")
    if not isinstance(promotion, Mapping) or any(bool(value) for value in promotion.values()):
        raise ValueError("every promotion and optimization action must remain disabled")
    local = protocol.get("local_geometry")
    if not isinstance(local, Mapping) or int(local.get("total_perturbations", -1)) != 32:
        raise ValueError("local geometry must contain exactly 32 perturbations")
    threshold = protocol.get("threshold_perturbations")
    if not isinstance(threshold, Mapping):
        raise ValueError("threshold perturbations are missing")
    if len(tuple(threshold.get("enter", ()))) != 4 or len(tuple(threshold.get("exit", ()))) != 4:
        raise ValueError("threshold protocol must contain 8 one-family perturbations")
    if threshold.get("combine_families") is not False:
        raise ValueError("threshold perturbation families must not be combined")


def component_attribution(rows: pd.DataFrame) -> pd.DataFrame:
    """Compute exact two-factor Shapley effects for weights and thresholds."""
    required = {"window", "variant", "strategy_return", "sharpe"}
    if not required <= set(rows.columns):
        raise ValueError(f"component rows missing columns: {sorted(required - set(rows.columns))}")
    output: list[dict[str, object]] = []
    for window, group in rows.groupby("window", sort=False):
        if set(group["variant"].astype(str)) != set(_COMPONENT_VARIANTS) or len(group) != 4:
            raise ValueError(f"{window}: component variants must occur exactly once")
        indexed = group.set_index("variant")
        for metric, prefix in (("strategy_return", "return"), ("sharpe", "sharpe")):
            baseline = float(indexed.loc["baseline", metric])
            weights = float(indexed.loc["weights_only", metric])
            thresholds = float(indexed.loc["thresholds_only", metric])
            combined = float(indexed.loc["combined", metric])
            values = {
                "weights": 0.5 * ((weights - baseline) + (combined - thresholds)),
                "thresholds": 0.5 * ((thresholds - baseline) + (combined - weights)),
                "interaction": combined - weights - thresholds + baseline,
            }
            for component, value in values.items():
                existing = next(
                    (
                        row
                        for row in output
                        if row["window"] == window and row["component"] == component
                    ),
                    None,
                )
                if existing is None:
                    existing = {"window": window, "component": component}
                    output.append(existing)
                existing[f"{prefix}_contribution"] = value
    return pd.DataFrame(output)


def perturb_weight(weights: pd.Series, factor: str, delta: float) -> pd.Series:
    """Move one positive weight and redistribute the opposite mass proportionally."""
    values = weights.astype(float).copy()
    if factor not in values.index:
        raise KeyError(factor)
    if not np.isfinite(values.to_numpy()).all() or not values.gt(0.0).all():
        raise ValueError("local perturbation requires finite positive weights")
    if not np.isclose(float(values.sum()), 1.0, rtol=0.0, atol=1e-12):
        raise ValueError("local perturbation requires weights summing to one")
    changed = float(values.loc[factor]) + float(delta)
    if changed <= 0.0 or changed >= 1.0:
        raise ValueError("perturbed weight must remain strictly between zero and one")
    others = values.index[values.index != factor]
    other_total = float(values.loc[others].sum())
    values.loc[others] *= (1.0 - changed) / other_total
    values.loc[factor] = changed
    if not values.gt(0.0).all():
        raise ValueError("weight redistribution produced a non-positive weight")
    return values.rename(weights.name)


def classify_local_geometry(
    rows: pd.DataFrame, rules: Mapping[str, object]
) -> dict[str, object]:
    """Apply the preregistered peak/plateau rules to one row per perturbation."""
    required = {"variant_id", "median_return_delta", "min_return_delta"}
    if not required <= set(rows.columns):
        raise ValueError(f"local rows missing columns: {sorted(required - set(rows.columns))}")
    total = int(rules["total_perturbations"])
    if len(rows) != total or rows["variant_id"].astype(str).nunique() != total:
        raise ValueError(f"local geometry requires exactly {total} unique perturbations")
    medians = rows["median_return_delta"].astype(float)
    minima = rows["min_return_delta"].astype(float)
    nearby = (
        medians.abs().le(float(rules["nearby_median_absolute_return_delta"]))
        & minima.ge(float(rules["nearby_min_return_delta"]))
    )
    negative_median = medians.lt(float(rules["peak_negative_median_threshold"]))
    bad_min = minima.le(float(rules["peak_bad_min_threshold"]))
    nearby_count = int(nearby.sum())
    negative_count = int(negative_median.sum())
    bad_count = int(bad_min.sum())
    if (
        negative_count >= int(rules["peak_negative_median_min_count"])
        and bad_count >= int(rules["peak_bad_min_count"])
    ):
        classification = "sharp_local_peak"
    elif nearby_count >= int(rules["plateau_min_count"]):
        classification = "broad_plateau"
    else:
        classification = "mixed_or_asymmetric"
    return {
        "classification": classification,
        "total_perturbations": total,
        "nearby_stable_count": nearby_count,
        "negative_median_count": negative_count,
        "bad_min_count": bad_count,
    }


def _validate_aligned(series: Sequence[pd.Series]) -> pd.Index:
    if not series:
        raise ValueError("at least one series is required")
    index = series[0].index
    if any(not item.index.equals(index) for item in series[1:]):
        raise ValueError("series indices must match exactly")
    return index


def difference_intervals(
    baseline_target: pd.Series,
    ex04_target: pd.Series,
    baseline_returns: pd.Series,
    ex04_returns: pd.Series,
) -> pd.DataFrame:
    """Summarize contiguous execution-position disagreements and their contributions."""
    index = _validate_aligned(
        (baseline_target, ex04_target, baseline_returns, ex04_returns)
    )
    differs = baseline_target.astype(float).ne(ex04_target.astype(float))
    columns = [
        "interval_id",
        "start",
        "end",
        "trading_days",
        "return_delta_contribution",
        "absolute_contribution",
        "absolute_contribution_share",
    ]
    if not differs.any():
        return pd.DataFrame(columns=columns)
    group_ids = differs.ne(differs.shift(fill_value=False)).cumsum()
    delta = ex04_returns.astype(float) - baseline_returns.astype(float)
    raw: list[dict[str, object]] = []
    for interval_id, positions in differs.loc[differs].groupby(group_ids.loc[differs]):
        dates = positions.index
        contribution = float(delta.loc[positions.index].sum())
        raw.append(
            {
                "interval_id": int(interval_id),
                "start": pd.Timestamp(dates[0]),
                "end": pd.Timestamp(dates[-1]),
                "trading_days": len(dates),
                "return_delta_contribution": contribution,
                "absolute_contribution": abs(contribution),
            }
        )
    result = pd.DataFrame(raw)
    total = float(result["absolute_contribution"].sum())
    result["absolute_contribution_share"] = (
        result["absolute_contribution"] / total if total > 0.0 else 0.0
    )
    return result.loc[:, columns]


def market_regimes(
    close: pd.Series, lookback: int, up: float, down: float, lag: int
) -> pd.Series:
    """Classify each day from a strictly lagged trailing close return."""
    if lookback < 1 or lag < 1:
        raise ValueError("lookback and lag must be positive")
    if float(down) >= float(up):
        raise ValueError("down threshold must be below up threshold")
    trailing = close.astype(float).pct_change(periods=lookback, fill_method=None).shift(lag)
    labels = pd.Series("sideways", index=close.index, dtype="string")
    labels.loc[trailing.isna()] = "warmup"
    labels.loc[trailing.ge(float(up))] = "uptrend"
    labels.loc[trailing.le(float(down))] = "downtrend"
    return labels.rename("regime")


def paired_circular_block_bootstrap(
    baseline_returns: pd.Series,
    ex04_returns: pd.Series,
    *,
    block_length: int,
    replications: int,
    seed: int,
    quantiles: Sequence[float],
) -> dict[str, object]:
    """Bootstrap the paired cumulative-return difference with circular blocks."""
    _validate_aligned((baseline_returns, ex04_returns))
    left = baseline_returns.astype(float).to_numpy()
    right = ex04_returns.astype(float).to_numpy()
    if len(left) == 0:
        raise ValueError("bootstrap paths must not be empty")
    if not np.isfinite(left).all() or not np.isfinite(right).all():
        raise ValueError("bootstrap paths must be finite")
    if block_length < 1 or block_length > len(left) or replications < 1:
        raise ValueError("invalid bootstrap block length or replication count")
    requested = tuple(float(value) for value in quantiles)
    if not requested or any(value < 0.0 or value > 1.0 for value in requested):
        raise ValueError("bootstrap quantiles must lie in [0, 1]")
    rng = np.random.default_rng(int(seed))
    block_count = int(np.ceil(len(left) / block_length))
    offsets = np.arange(block_length)
    deltas = np.empty(replications, dtype=float)
    for replication in range(replications):
        starts = rng.integers(0, len(left), size=block_count)
        sampled = ((starts[:, None] + offsets[None, :]) % len(left)).ravel()[: len(left)]
        left_total = float(np.prod(1.0 + left[sampled]) - 1.0)
        right_total = float(np.prod(1.0 + right[sampled]) - 1.0)
        deltas[replication] = right_total - left_total
    return {
        "method": "paired_circular_moving_block",
        "sample_days": len(left),
        "block_length_trading_days": int(block_length),
        "replications": int(replications),
        "seed": int(seed),
        "quantiles": {
            str(value): float(np.quantile(deltas, value)) for value in requested
        },
        "probability_delta_above_zero": float(np.mean(deltas > 0.0)),
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _window_rows(
    variant: str, metrics: Mapping[str, Mapping[str, object]]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for window, values in metrics.items():
        rows.append(
            {
                "variant": variant,
                "window": window,
                "strategy_return": float(values["strategy_return"]),
                "sharpe": float(values["sharpe"]),
                "max_drawdown": float(values["max_drawdown"]),
                "exposure": float(values["exposure"]),
                "trade_count": int(values["trade_count"]),
            }
        )
    return rows


def normalize_cash_sharpe(metrics: Mapping[str, object]) -> dict[str, object]:
    """Give an all-cash payoff zero utility and reject other non-finite Sharpes."""
    result = dict(metrics)
    sharpe = float(result["sharpe"])
    if np.isfinite(sharpe):
        return result
    is_cash = (
        abs(float(result["strategy_return"])) <= 1e-15
        and abs(float(result["exposure"])) <= 1e-15
        and int(result["trade_count"]) == 0
    )
    if not is_cash:
        raise ValueError("active coalition produced a non-finite Sharpe utility")
    result["sharpe"] = 0.0
    return result


def _classify_contribution_rows(
    rows: pd.DataFrame,
    *,
    object_type: str,
    object_column: str,
    primary_windows: Sequence[str],
    return_epsilon: float,
    sharpe_epsilon: float,
    stable_years: int,
    dominated: bool,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    primary = rows.loc[rows["window"].astype(str).isin(tuple(primary_windows))]
    for object_id, group in primary.groupby(object_column, sort=False):
        returns = group["return_contribution"].astype(float)
        sharpes = group["sharpe_contribution"].astype(float)
        positive = int(((returns > return_epsilon) & (sharpes > sharpe_epsilon)).sum())
        negative = int(((returns < -return_epsilon) & (sharpes < -sharpe_epsilon)).sum())
        if positive >= stable_years:
            classification = "stable_positive"
        elif negative >= stable_years:
            classification = "stable_negative"
        elif positive > 0 and negative > 0:
            classification = "regime_dependent"
        else:
            classification = "inconclusive"
        if dominated and classification in {"stable_positive", "stable_negative"}:
            classification = "inconclusive_dominated"
        output.append(
            {
                "object_type": object_type,
                "object_id": str(object_id),
                "classification": classification,
                "positive_years": positive,
                "negative_years": negative,
                "median_return_contribution": float(returns.median()),
                "median_sharpe_contribution": float(sharpes.median()),
                "single_interval_dominated": dominated,
            }
        )
    return output


def _continuous_paths(
    daily: pd.DataFrame,
    targets: Mapping[str, pd.Series],
    start: pd.Timestamp,
    end: pd.Timestamp,
    fee_rate: float,
    init_cash: float,
) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    prices = daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="dt")
    prices = prices.sort_index()
    period = {"continuous": (start, end)}
    returns: dict[str, pd.Series] = {}
    execution_targets: dict[str, pd.Series] = {}
    period_index = prices.index[(prices.index >= start) & (prices.index <= end)]
    prior = prices.index[prices.index < period_index[0]][-1]
    for name, target in targets.items():
        result = run_period_backtests(
            prices,
            target,
            period,
            fee_rate=fee_rate,
            init_cash=init_cash,
        )["continuous"]
        equity = result.equity.astype(float)
        daily_returns = equity.pct_change(fill_method=None)
        daily_returns.iloc[0] = float(equity.iloc[0] / init_cash - 1.0)
        returns[name] = daily_returns.rename(name)
        execution = target.reindex(period_index).shift(1)
        execution.iloc[0] = float(target.loc[prior])
        execution_targets[name] = execution.astype(float).rename(name)
    return returns, execution_targets


def run_ex04_attribution(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Run the frozen EX04 diagnosis without loading or producing a holdout."""
    validate_protocol(protocol)
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    repository_root = experiment_dir.parent.parent

    general = protocol["general_baseline"]
    if not isinstance(general, Mapping):
        raise ValueError("general baseline identity is missing")
    baseline = resolve_baseline(Path(baseline_root), str(general["version"]))
    if baseline.sha256 != str(general["sha256"]):
        raise ValueError("general baseline hash differs from preregistration")

    research = protocol["research_object"]
    if not isinstance(research, Mapping):
        raise ValueError("research object identity is missing")
    frozen_path = repository_root / str(research["path"])
    frozen_bytes = frozen_path.read_bytes()
    frozen_digest = sha256(frozen_bytes).hexdigest()
    if frozen_digest != str(research["sha256"]):
        raise ValueError("EX04 frozen challenger hash differs from preregistration")
    frozen = json.loads(frozen_bytes.decode("utf-8-sig"))

    cutoff = pd.Timestamp(str(protocol["visible_sample_end"]))
    data = load_market_data(Path(raw_dir), "588080.SH", "etf", cutoff=cutoff)
    if any("2026" in str(name) for name in data.hashes):
        raise AssertionError("2026 file entered EX04 attribution inputs")
    factor_frame = generate_factor_frame(data).frame
    factors, origin, proof = _equivalence(
        factor_frame, baseline.rule, float(protocol["equivalence_tolerance"])
    )
    factor_names = tuple(str(name) for name in frozen["factor_names"])
    if factor_names != tuple(factors.columns):
        raise ValueError("EX04 factor identities differ from baseline four-layer factors")
    ex04_weights = pd.Series(
        [float(frozen["weights"][name]) for name in factor_names],
        index=factors.columns,
        name="weight",
    )
    validate_fixed_factor_weights(ex04_weights, factors.columns, 0.0)
    ex04_enter = float(frozen["spec"]["enter"])
    ex04_exit = float(frozen["spec"]["exit"])

    primary_windows = tuple(str(name) for name in protocol["primary_windows"])
    diagnostic_windows = tuple(str(name) for name in protocol["diagnostic_windows"])
    annual = {name: ANNUAL_PERIODS[name] for name in primary_windows}
    halves = half_year_periods(2021, 2025)
    if tuple(halves) != diagnostic_windows:
        raise ValueError("diagnostic windows differ from preregistration")
    periods = {**annual, **halves}
    fee_rate = float(protocol["fee_rate"])
    init_cash = float(protocol["init_cash"])
    evaluator = _PeriodEvaluator(data.daily, periods, fee_rate, init_cash)

    component_specs = {
        "baseline": (origin, float(baseline.rule.enter), float(baseline.rule.exit)),
        "weights_only": (ex04_weights, float(baseline.rule.enter), float(baseline.rule.exit)),
        "thresholds_only": (origin, ex04_enter, ex04_exit),
        "combined": (ex04_weights, ex04_enter, ex04_exit),
    }
    component_rows: list[dict[str, object]] = []
    targets: dict[str, pd.Series] = {}
    metrics_by_variant: dict[str, dict[str, dict[str, object]]] = {}
    for variant, (weights, enter, exit_) in component_specs.items():
        target = positions_from_scores(
            score_four_layer(factors, weights), enter, exit_, baseline.rule
        )
        targets[variant] = target
        metrics = evaluator.evaluate(target)
        metrics_by_variant[variant] = metrics
        component_rows.extend(_window_rows(variant, metrics))
    component_metrics = pd.DataFrame(component_rows)
    component_effects = component_attribution(component_metrics)
    _write_csv(artifacts / "component_metrics.csv", component_metrics)
    _write_csv(artifacts / "component_attribution.csv", component_effects)

    comparison_rows: list[dict[str, object]] = []
    for window in periods:
        left = metrics_by_variant["baseline"][window]
        right = metrics_by_variant["combined"][window]
        comparison_rows.append(
            {
                "window": window,
                "baseline_return": float(left["strategy_return"]),
                "ex04_return": float(right["strategy_return"]),
                "return_delta": float(right["strategy_return"]) - float(left["strategy_return"]),
                "baseline_sharpe": float(left["sharpe"]),
                "ex04_sharpe": float(right["sharpe"]),
                "sharpe_delta": float(right["sharpe"]) - float(left["sharpe"]),
                "baseline_max_drawdown": float(left["max_drawdown"]),
                "ex04_max_drawdown": float(right["max_drawdown"]),
                "baseline_exposure": float(left["exposure"]),
                "ex04_exposure": float(right["exposure"]),
                "baseline_trade_count": int(left["trade_count"]),
                "ex04_trade_count": int(right["trade_count"]),
            }
        )
    window_comparison = pd.DataFrame(comparison_rows)
    _write_csv(artifacts / "window_comparison.csv", window_comparison)

    groups = signal_groups(factors.columns)
    declared_groups = tuple(str(name) for name in protocol["factor_groups"])
    if tuple(groups) != declared_groups:
        raise ValueError("factor groups differ from preregistration")
    coalition_rows: list[dict[str, object]] = []
    coalition_metrics: dict[frozenset[str], dict[str, dict[str, object]]] = {}
    for coalition in all_coalitions(declared_groups):
        weights = ex04_weights.copy()
        absent = set(declared_groups) - set(coalition)
        for group_name in absent:
            weights.loc[list(groups[group_name])] = 0.0
        target = positions_from_scores(
            score_four_layer(factors, weights), ex04_enter, ex04_exit, baseline.rule
        )
        values = {
            window: normalize_cash_sharpe(metrics)
            for window, metrics in evaluator.evaluate(target).items()
        }
        if not coalition:
            values = {
                window: {**metrics, "strategy_return": 0.0, "sharpe": 0.0}
                for window, metrics in values.items()
            }
        coalition_metrics[coalition] = values
        coalition_id = "+".join(name for name in declared_groups if name in coalition) or "empty"
        for row in _window_rows(coalition_id, values):
            row["coalition"] = row.pop("variant")
            row["coalition_size"] = len(coalition)
            coalition_rows.append(row)
    coalition_frame = pd.DataFrame(coalition_rows)
    _write_csv(artifacts / "group_coalitions.csv", coalition_frame)

    shapley_rows: list[dict[str, object]] = []
    interaction_rows: list[dict[str, object]] = []
    for window in periods:
        return_utilities = {
            coalition: float(values[window]["strategy_return"])
            for coalition, values in coalition_metrics.items()
        }
        sharpe_utilities = {
            coalition: float(values[window]["sharpe"])
            for coalition, values in coalition_metrics.items()
        }
        return_values = shapley_values(return_utilities, declared_groups)
        sharpe_values = shapley_values(sharpe_utilities, declared_groups)
        for group_name in declared_groups:
            shapley_rows.append(
                {
                    "window": window,
                    "group": group_name,
                    "return_contribution": return_values[group_name],
                    "sharpe_contribution": sharpe_values[group_name],
                }
            )
        return_interactions = shapley_interactions(return_utilities, declared_groups)
        sharpe_interactions = shapley_interactions(sharpe_utilities, declared_groups)
        for pair, value in return_interactions.items():
            interaction_rows.append(
                {
                    "window": window,
                    "left_group": pair[0],
                    "right_group": pair[1],
                    "return_interaction": value,
                    "sharpe_interaction": sharpe_interactions[pair],
                }
            )
    group_shapley = pd.DataFrame(shapley_rows)
    group_interactions = pd.DataFrame(interaction_rows)
    _write_csv(artifacts / "group_shapley.csv", group_shapley)
    _write_csv(artifacts / "group_interactions.csv", group_interactions)

    ex04_metrics = metrics_by_variant["combined"]
    perturbation_rows: list[dict[str, object]] = []
    perturbation_specs: list[tuple[str, str, pd.Series, float, float]] = []
    step = float(protocol["weight_perturbation_step"])
    for factor in factors.columns:
        for delta in (step, -step):
            perturbation_specs.append(
                (
                    f"weight::{factor}::{delta:+.4f}",
                    "weight",
                    perturb_weight(ex04_weights, str(factor), delta),
                    ex04_enter,
                    ex04_exit,
                )
            )
    thresholds = protocol["threshold_perturbations"]
    if not isinstance(thresholds, Mapping):
        raise ValueError("threshold perturbations are missing")
    for enter in thresholds["enter"]:
        perturbation_specs.append(
            (f"enter::{float(enter):.4f}", "enter", ex04_weights, float(enter), ex04_exit)
        )
    for exit_ in thresholds["exit"]:
        perturbation_specs.append(
            (f"exit::{float(exit_):.4f}", "exit", ex04_weights, ex04_enter, float(exit_))
        )
    if len(perturbation_specs) != 32:
        raise AssertionError("formal local perturbation count differs from protocol")
    for variant_id, family, weights, enter, exit_ in perturbation_specs:
        target = positions_from_scores(
            score_four_layer(factors, weights), enter, exit_, baseline.rule
        )
        values = evaluator.evaluate(target)
        deltas = [
            float(values[window]["strategy_return"])
            - float(ex04_metrics[window]["strategy_return"])
            for window in diagnostic_windows
        ]
        sharpe_deltas = [
            float(values[window]["sharpe"]) - float(ex04_metrics[window]["sharpe"])
            for window in diagnostic_windows
        ]
        perturbation_rows.append(
            {
                "variant_id": variant_id,
                "family": family,
                "enter_threshold": enter,
                "exit_threshold": exit_,
                "position_disagreement_rate": float(target.ne(targets["combined"]).mean()),
                "median_return_delta": float(np.median(deltas)),
                "min_return_delta": float(np.min(deltas)),
                "mean_return_delta": float(np.mean(deltas)),
                "median_sharpe_delta": float(np.median(sharpe_deltas)),
                **{
                    f"{window}_return_delta": delta
                    for window, delta in zip(diagnostic_windows, deltas, strict=True)
                },
            }
        )
    local_frame = pd.DataFrame(perturbation_rows)
    local_rules = protocol["local_geometry"]
    if not isinstance(local_rules, Mapping):
        raise ValueError("local geometry rules are missing")
    local_summary = classify_local_geometry(local_frame, local_rules)
    _write_csv(artifacts / "local_perturbations.csv", local_frame)
    _write_json(artifacts / "local_geometry.json", local_summary)

    continuous = protocol["continuous_window"]
    if not isinstance(continuous, Mapping):
        raise ValueError("continuous window is missing")
    continuous_start = pd.Timestamp(str(continuous["start"]))
    continuous_end = pd.Timestamp(str(continuous["end"]))
    return_paths, execution_targets = _continuous_paths(
        data.daily,
        {"baseline": targets["baseline"], "ex04": targets["combined"]},
        continuous_start,
        continuous_end,
        fee_rate,
        init_cash,
    )
    intervals = difference_intervals(
        execution_targets["baseline"],
        execution_targets["ex04"],
        return_paths["baseline"],
        return_paths["ex04"],
    )
    _write_csv(artifacts / "difference_intervals.csv", intervals)
    max_share = (
        float(intervals["absolute_contribution_share"].max()) if not intervals.empty else 0.0
    )
    top3_share = (
        float(intervals["absolute_contribution_share"].nlargest(3).sum())
        if not intervals.empty
        else 0.0
    )
    materiality = protocol["materiality"]
    if not isinstance(materiality, Mapping):
        raise ValueError("materiality rules are missing")
    dominated = max_share > float(materiality["single_interval_dominance_share"])

    prices = data.daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="dt")
    regime_rules = protocol["regime"]
    if not isinstance(regime_rules, Mapping):
        raise ValueError("regime rules are missing")
    labels = market_regimes(
        prices["close"],
        int(regime_rules["lookback_trading_days"]),
        float(regime_rules["uptrend_threshold"]),
        float(regime_rules["downtrend_threshold"]),
        int(regime_rules["information_lag_days"]),
    ).reindex(return_paths["baseline"].index)
    regime_rows: list[dict[str, object]] = []
    for regime in ("uptrend", "downtrend", "sideways", "warmup"):
        mask = labels.eq(regime)
        regime_rows.append(
            {
                "regime": regime,
                "trading_days": int(mask.sum()),
                "baseline_exposure": float(execution_targets["baseline"].loc[mask].mean()) if mask.any() else 0.0,
                "ex04_exposure": float(execution_targets["ex04"].loc[mask].mean()) if mask.any() else 0.0,
                "baseline_daily_return_sum": float(return_paths["baseline"].loc[mask].sum()),
                "ex04_daily_return_sum": float(return_paths["ex04"].loc[mask].sum()),
                "daily_return_delta_sum": float(
                    (return_paths["ex04"] - return_paths["baseline"]).loc[mask].sum()
                ),
            }
        )
    regime_frame = pd.DataFrame(regime_rows)
    _write_csv(artifacts / "regime_attribution.csv", regime_frame)

    bootstrap_rules = protocol["bootstrap"]
    if not isinstance(bootstrap_rules, Mapping):
        raise ValueError("bootstrap rules are missing")
    bootstrap = paired_circular_block_bootstrap(
        return_paths["baseline"],
        return_paths["ex04"],
        block_length=int(bootstrap_rules["block_length_trading_days"]),
        replications=int(bootstrap_rules["replications"]),
        seed=int(bootstrap_rules["seed"]),
        quantiles=tuple(float(value) for value in bootstrap_rules["quantiles"]),
    )
    _write_json(artifacts / "bootstrap_summary.json", bootstrap)

    component_for_classification = component_effects.loc[
        component_effects["component"].isin(("weights", "thresholds"))
    ]
    classifications = _classify_contribution_rows(
        component_for_classification,
        object_type="component",
        object_column="component",
        primary_windows=primary_windows,
        return_epsilon=float(materiality["return_contribution"]),
        sharpe_epsilon=float(materiality["sharpe_contribution"]),
        stable_years=int(materiality["stable_year_count"]),
        dominated=dominated,
    )
    classifications.extend(
        _classify_contribution_rows(
            group_shapley,
            object_type="factor_group",
            object_column="group",
            primary_windows=primary_windows,
            return_epsilon=float(materiality["return_contribution"]),
            sharpe_epsilon=float(materiality["sharpe_contribution"]),
            stable_years=int(materiality["stable_year_count"]),
            dominated=dominated,
        )
    )
    classification_frame = pd.DataFrame(classifications)
    _write_csv(artifacts / "classification.csv", classification_frame)

    identity = {
        "status": "PASS",
        "general_baseline": {"version": baseline.version, "sha256": baseline.sha256},
        "research_object_path": str(research["path"]),
        "research_object_sha256": frozen_digest,
        "equivalence": proof,
        "factor_groups": {name: list(values) for name, values in groups.items()},
        "visible_data_hashes": data.hashes,
    }
    _write_json(artifacts / "identity_audit.json", identity)
    metrics = {
        "status": "COMPLETE",
        "experiment_id": protocol["experiment_id"],
        "visible_sample_end": str(cutoff.date()),
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
        "frozen_challenger": None,
        "component_variant_count": len(component_specs),
        "group_coalition_count": len(coalition_metrics),
        "local_perturbation_count": len(local_frame),
        "local_geometry": local_summary,
        "difference_interval_count": len(intervals),
        "max_interval_absolute_contribution_share": max_share,
        "top3_interval_absolute_contribution_share": top3_share,
        "single_interval_dominated": dominated,
        "bootstrap": bootstrap,
        "classification_counts": classification_frame["classification"].value_counts().sort_index().to_dict(),
        "evaluator_cache": evaluator.cache_stats,
    }
    _write_json(artifacts / "metrics.json", metrics)
    return metrics
