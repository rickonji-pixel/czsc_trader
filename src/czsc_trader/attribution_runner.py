"""Execute the preregistered attribution of the frozen 588080 champion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .attribution import (
    all_coalitions,
    classify_annual_effect,
    count_material_interaction_years,
    dominant_difference_share,
    drop_signal_and_reaggregate,
    rule_with_weight_delta,
    shapley_interactions,
    shapley_values,
    zero_signal_contribution,
)
from .backtest import PeriodBacktestResult, run_period_backtests
from .baselines import resolve_baseline
from .data import load_market_data
from .factors import (
    aggregate_signal_groups,
    generate_factor_frame,
    map_signal_frame,
    signal_groups,
    signal_primary,
)
from .rules import FACTOR_COLUMNS, Rule, positions_for_rule


RESEARCH_CUTOFF = pd.Timestamp("2025-12-31")
PRIMARY_YEARS = tuple(str(year) for year in range(2021, 2026))


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _periods(protocol: Mapping[str, object]) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    names = [
        *[str(value) for value in protocol["primary_windows"]],
        *[str(value) for value in protocol["diagnostic_windows"]],
    ]
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for name in names:
        if len(name) == 4 and name.isdigit():
            periods[name] = (
                pd.Timestamp(f"{name}-01-01"),
                pd.Timestamp(f"{name}-12-31"),
            )
            continue
        year, half = int(name[:4]), name[-2:]
        if half == "H1":
            periods[name] = (pd.Timestamp(year, 1, 1), pd.Timestamp(year, 6, 30))
        elif half == "H2":
            periods[name] = (pd.Timestamp(year, 7, 1), pd.Timestamp(year, 12, 31))
        else:
            raise ValueError(f"invalid attribution window: {name}")
    return periods


def _validate_protocol(protocol: Mapping[str, object]) -> None:
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise ValueError("attribution protocol must stop at 2025-12-31")
    if protocol.get("holdout_access_allowed") is not False:
        raise ValueError("attribution protocol must forbid holdout access")
    promotion = protocol.get("promotion")
    if not isinstance(promotion, Mapping) or any(bool(value) for value in promotion.values()):
        raise ValueError("diagnostic attribution protocol must disable every promotion action")
    if tuple(str(value) for value in protocol.get("primary_windows", [])) != PRIMARY_YEARS:
        raise ValueError("attribution primary windows must be 2021 through 2025")
    _periods(protocol)


def _target_digest(target: pd.Series) -> str:
    hashes = pd.util.hash_pandas_object(target.astype(float), index=True).to_numpy()
    return sha256(hashes.tobytes()).hexdigest()


class _TargetEvaluator:
    def __init__(
        self,
        daily: pd.DataFrame,
        periods: Mapping[str, tuple[pd.Timestamp, pd.Timestamp]],
        fee_rate: float,
        init_cash: float,
    ) -> None:
        self.daily = daily
        self.periods = dict(periods)
        self.fee_rate = float(fee_rate)
        self.init_cash = float(init_cash)
        self.cache: dict[str, dict[str, PeriodBacktestResult]] = {}

    def evaluate(self, target: pd.Series) -> dict[str, PeriodBacktestResult]:
        key = _target_digest(target)
        if key not in self.cache:
            self.cache[key] = run_period_backtests(
                self.daily,
                target,
                self.periods,
                fee_rate=self.fee_rate,
                init_cash=self.init_cash,
            )
        return self.cache[key]


def _safe_float(value: object, *, default: float = 0.0) -> float:
    number = float(value)
    return number if np.isfinite(number) else default


def _comparison_rows(
    object_type: str,
    object_id: str,
    counterfactual: str,
    champion_target: pd.Series,
    counterfactual_target: pd.Series,
    champion: Mapping[str, PeriodBacktestResult],
    changed: Mapping[str, PeriodBacktestResult],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for window in champion:
        champion_result = champion[window]
        changed_result = changed[window]
        champion_return = _safe_float(champion_result.metrics["strategy_return"])
        changed_return = _safe_float(changed_result.metrics["strategy_return"])
        champion_sharpe = _safe_float(champion_result.metrics["sharpe"])
        changed_sharpe = _safe_float(changed_result.metrics["sharpe"])
        equity_index = champion_result.equity.index
        daily_delta = (
            changed_result.equity.pct_change(fill_method=None).fillna(0.0)
            - champion_result.equity.pct_change(fill_method=None).fillna(0.0)
        )
        rows.append(
            {
                "object_type": object_type,
                "object_id": object_id,
                "counterfactual": counterfactual,
                "window": window,
                "champion_return": champion_return,
                "counterfactual_return": changed_return,
                "return_delta": changed_return - champion_return,
                "champion_sharpe": champion_sharpe,
                "counterfactual_sharpe": changed_sharpe,
                "sharpe_delta": changed_sharpe - champion_sharpe,
                "dominant_share": dominant_difference_share(
                    champion_target.reindex(equity_index),
                    counterfactual_target.reindex(equity_index),
                    daily_delta,
                ),
                "trade_count": int(changed_result.metrics["trade_count"]),
                "exposure": _safe_float(changed_result.metrics["exposure"]),
                "max_drawdown": _safe_float(changed_result.metrics["max_drawdown"]),
            }
        )
    return rows


def _annual_classification(rows: Sequence[dict[str, object]], protocol: Mapping[str, object]) -> str:
    annual = pd.DataFrame(rows)
    annual = annual.loc[annual["window"].isin(PRIMARY_YEARS)]
    materiality = protocol["materiality"]
    assert isinstance(materiality, Mapping)
    return classify_annual_effect(
        annual,
        float(materiality["return_delta"]),
        float(materiality["sharpe_delta"]),
        int(materiality["stable_year_count"]),
    )


def _classification_record(
    object_type: str,
    object_id: str,
    counterfactual: str,
    rows: Sequence[dict[str, object]],
    protocol: Mapping[str, object],
    classification: str | None = None,
) -> dict[str, object]:
    annual = pd.DataFrame(rows)
    annual = annual.loc[annual["window"].isin(PRIMARY_YEARS)]
    return {
        "object_type": object_type,
        "object_id": object_id,
        "counterfactual": counterfactual,
        "classification": classification or _annual_classification(rows, protocol),
        "median_return_delta": float(annual["return_delta"].median()),
        "median_sharpe_delta": float(annual["sharpe_delta"].median()),
        "max_dominant_share": float(annual["dominant_share"].max()),
        "material_positive_years": int(
            (
                (annual["return_delta"] > float(protocol["materiality"]["return_delta"]))
                & (annual["sharpe_delta"] > float(protocol["materiality"]["sharpe_delta"]))
            ).sum()
        ),
        "material_negative_years": int(
            (
                (annual["return_delta"] < -float(protocol["materiality"]["return_delta"]))
                & (annual["sharpe_delta"] < -float(protocol["materiality"]["sharpe_delta"]))
            ).sum()
        ),
    }


def _local_label(rows: Sequence[dict[str, object]], protocol: Mapping[str, object]) -> str:
    annual = pd.DataFrame(rows)
    annual = annual.loc[annual["window"].isin(PRIMARY_YEARS)]
    ret_eps = float(protocol["materiality"]["return_delta"])
    sharpe_eps = float(protocol["materiality"]["sharpe_delta"])
    stable = int(protocol["materiality"]["stable_year_count"])
    improved = ((annual["return_delta"] > ret_eps) & (annual["sharpe_delta"] > sharpe_eps)).sum()
    harmed = ((annual["return_delta"] < -ret_eps) & (annual["sharpe_delta"] < -sharpe_eps)).sum()
    if annual["dominant_share"].gt(0.5).any():
        return "inconclusive"
    if improved >= stable:
        return "stable_local_improvement"
    if harmed >= stable:
        return "stable_local_deterioration"
    return "mixed_or_immaterial"


def _champion_trade_diagnostics(
    daily: pd.DataFrame,
    results: Mapping[str, PeriodBacktestResult],
) -> pd.DataFrame:
    prices = daily.set_index("dt").sort_index()
    rows: list[dict[str, object]] = []
    for window, result in results.items():
        orders = result.orders.sort_values("execution_date", kind="stable")
        entry: pd.Series | None = None
        for _, order in orders.iterrows():
            if str(order["side"]) == "Buy":
                entry = order
                continue
            if str(order["side"]) != "Sell" or entry is None:
                continue
            start = pd.Timestamp(entry["execution_date"])
            end = pd.Timestamp(order["execution_date"])
            path = prices.loc[start:end]
            entry_price = float(entry["price"])
            rows.append(
                {
                    "window": window,
                    "entry_date": start,
                    "exit_date": end,
                    "net_return": (
                        float(order["size"]) * float(order["price"]) - float(order["fees"])
                    )
                    / (float(entry["size"]) * entry_price + float(entry["fees"]))
                    - 1.0,
                    "mfe": float(path["high"].max() / entry_price - 1.0),
                    "mae": float(path["low"].min() / entry_price - 1.0),
                }
            )
            entry = None
    return pd.DataFrame(rows, columns=["window", "entry_date", "exit_date", "net_return", "mfe", "mae"])


def _heatmap(classifications: pd.DataFrame, output_path: Path) -> None:
    labels = (
        classifications["object_type"].astype(str)
        + ":"
        + classifications["object_id"].astype(str)
        + ":"
        + classifications["counterfactual"].astype(str)
    )
    figure = go.Figure(
        data=go.Heatmap(
            z=[
                classifications["median_return_delta"].astype(float).tolist(),
                classifications["median_sharpe_delta"].astype(float).tolist(),
            ],
            x=labels.tolist(),
            y=["median_return_delta", "median_sharpe_delta"],
            colorscale="RdYlGn",
            zmid=0.0,
        )
    )
    figure.update_layout(title="588080 Champion Attribution", template="plotly_white")
    figure.write_html(output_path, include_plotlyjs=True, full_html=True)


def run_attribution_from_frames(
    daily: pd.DataFrame,
    raw: pd.DataFrame,
    mapped: pd.DataFrame,
    memberships: Mapping[str, Sequence[str]],
    grouped: pd.DataFrame,
    champion_rule: Rule,
    artifacts_dir: Path,
    protocol: Mapping[str, object],
    *,
    data_hashes: Mapping[str, str],
    unknown_values: Mapping[str, int],
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> dict[str, object]:
    """Run every preregistered attribution from already-causal factor frames."""
    _validate_protocol(protocol)
    if any("2026" in str(name) for name in data_hashes):
        raise ValueError("attribution data hashes must not contain 2026")
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    periods = _periods(protocol)
    evaluator = _TargetEvaluator(daily, periods, fee_rate, init_cash)
    champion_target, _ = positions_for_rule(grouped, champion_rule)
    champion_results = evaluator.evaluate(champion_target)

    trade_diagnostics = _champion_trade_diagnostics(daily, champion_results)
    _write_csv(artifacts_dir / "trade_diagnostics.csv", trade_diagnostics)
    window_rows: list[dict[str, object]] = []
    for window, result in champion_results.items():
        diagnostics = trade_diagnostics.loc[trade_diagnostics["window"] == window]
        turnover = float(
            (result.orders["size"].astype(float) * result.orders["price"].astype(float)).sum()
            / init_cash
        ) if not result.orders.empty else 0.0
        window_rows.append(
            {
                "window": window,
                **result.metrics,
                "turnover": turnover,
                "mean_mfe": float(diagnostics["mfe"].mean()) if not diagnostics.empty else None,
                "mean_mae": float(diagnostics["mae"].mean()) if not diagnostics.empty else None,
            }
        )
    _write_csv(artifacts_dir / "window_metrics.csv", pd.DataFrame(window_rows))

    catalog_rows: list[dict[str, object]] = [
        {"object_type": "group", "object_id": name, "parent": "", "mapped_score": "", "total_days": len(grouped)}
        for name in FACTOR_COLUMNS
    ]
    reverse_group = {
        column: group for group, columns in memberships.items() for column in columns
    }
    signal_rows: list[dict[str, object]] = []
    state_rows: list[dict[str, object]] = []
    classifications: list[dict[str, object]] = []

    group_rows: list[dict[str, object]] = []
    coalition_results: dict[frozenset[str], dict[str, PeriodBacktestResult]] = {}
    group_names = tuple(FACTOR_COLUMNS)
    for coalition in all_coalitions(group_names):
        counterfactual_groups = grouped.copy()
        for name in set(group_names) - set(coalition):
            counterfactual_groups[name] = 0.0
        target, _ = positions_for_rule(counterfactual_groups, champion_rule)
        results = evaluator.evaluate(target)
        coalition_results[coalition] = results
        coalition_id = "+".join(name for name in group_names if name in coalition) or "cash"
        group_rows.extend(
            _comparison_rows(
                "group_coalition",
                coalition_id,
                "coalition",
                champion_target,
                target,
                champion_results,
                results,
            )
        )

    interaction_by_factor: dict[str, list[dict[str, object]]] = {
        name: [] for name in group_names
    }
    for window in periods:
        return_utilities = {
            coalition: 0.0 if not coalition else _safe_float(results[window].metrics["strategy_return"])
            for coalition, results in coalition_results.items()
        }
        sharpe_utilities = {
            coalition: 0.0 if not coalition else _safe_float(results[window].metrics["sharpe"])
            for coalition, results in coalition_results.items()
        }
        return_shapley = shapley_values(return_utilities, group_names)
        sharpe_shapley = shapley_values(sharpe_utilities, group_names)
        return_interactions = shapley_interactions(return_utilities, group_names)
        sharpe_interactions = shapley_interactions(sharpe_utilities, group_names)
        for metric, values in (("return", return_shapley), ("sharpe", sharpe_shapley)):
            for name, value in values.items():
                group_rows.append(
                    {
                        "object_type": "group_shapley",
                        "object_id": name,
                        "counterfactual": "exact_shapley",
                        "window": window,
                        "metric": metric,
                        "value": value,
                    }
                )
        for metric, values in (("return", return_interactions), ("sharpe", sharpe_interactions)):
            for pair, value in values.items():
                group_rows.append(
                    {
                        "object_type": "group_interaction",
                        "object_id": "+".join(pair),
                        "counterfactual": "pairwise_shapley_interaction",
                        "window": window,
                        "metric": metric,
                        "value": value,
                    }
                )
        if window in PRIMARY_YEARS:
            for pair, return_value in return_interactions.items():
                record = {
                    "window": window,
                    "return_interaction": return_value,
                    "sharpe_interaction": sharpe_interactions[pair],
                }
                for name in pair:
                    interaction_by_factor[name].append(record)

    grand = frozenset(group_names)
    for name in group_names:
        coalition = grand - {name}
        target_groups = grouped.copy()
        target_groups[name] = 0.0
        target, _ = positions_for_rule(target_groups, champion_rule)
        removal_rows = _comparison_rows(
            "group", name, "zero_contribution", champion_target, target,
            champion_results, coalition_results[coalition],
        )
        classification = _annual_classification(removal_rows, protocol)
        ret_eps = float(protocol["materiality"]["return_delta"])
        sharpe_eps = float(protocol["materiality"]["sharpe_delta"])
        interaction_years = count_material_interaction_years(
            pd.DataFrame(interaction_by_factor[name]),
            ret_eps,
            sharpe_eps,
        )
        if classification == "inconclusive" and interaction_years >= int(protocol["materiality"]["stable_year_count"]):
            classification = "interaction"
        classifications.append(
            _classification_record(
                "group", name, "zero_contribution", removal_rows, protocol, classification
            )
        )
    _write_csv(artifacts_dir / "group_attribution.csv", pd.DataFrame(group_rows))

    for column in mapped.columns:
        catalog_rows.append(
            {
                "object_type": "signal",
                "object_id": column,
                "parent": reverse_group[column],
                "mapped_score": "",
                "total_days": len(mapped),
            }
        )
        zeroed = zero_signal_contribution(mapped, column)
        zero_groups = aggregate_signal_groups(zeroed, memberships)
        zero_target, _ = positions_for_rule(zero_groups, champion_rule)
        zero_rows = _comparison_rows(
            "signal", column, "zero_contribution", champion_target, zero_target,
            champion_results, evaluator.evaluate(zero_target),
        )
        signal_rows.extend(zero_rows)
        classifications.append(
            _classification_record(
                "signal", column, "zero_contribution", zero_rows, protocol
            )
        )

        dropped_groups = drop_signal_and_reaggregate(mapped, memberships, column)
        dropped_target, _ = positions_for_rule(dropped_groups, champion_rule)
        dropped_rows = _comparison_rows(
            "signal", column, "drop_and_reaggregate", champion_target, dropped_target,
            champion_results, evaluator.evaluate(dropped_target),
        )
        signal_rows.extend(dropped_rows)
        classifications.append(
            _classification_record(
                "signal", column, "drop_and_reaggregate", dropped_rows, protocol
            )
        )

        primary_values = raw[column].map(signal_primary)
        visible = primary_values.loc[primary_values.index.year >= 2021]
        for state in sorted(value for value in visible.dropna().unique()):
            mask = primary_values.eq(state)
            counts = mask.groupby(mask.index.year).sum()
            total = int(mask.loc[mask.index.year >= 2021].sum())
            score_values = mapped.loc[mask, column].dropna().unique()
            score = float(score_values[0]) if len(score_values) else 0.0
            requirement = protocol["state_sample_requirement"]
            eligible_years = int((counts.reindex(range(2021, 2026), fill_value=0) >= int(requirement["minimum_days_per_year"])).sum())
            eligible = total >= int(requirement["minimum_total_days"]) and eligible_years >= int(requirement["minimum_years"])
            state_id = f"{column}::{state}"
            catalog_rows.append(
                {
                    "object_type": "state",
                    "object_id": state_id,
                    "parent": column,
                    "mapped_score": score,
                    "total_days": total,
                    "eligible_years": eligible_years,
                }
            )
            if not eligible:
                classifications.append(
                    {
                        "object_type": "state", "object_id": state_id,
                        "counterfactual": "zero_mapped_state", "classification": "insufficient_sample",
                        "median_return_delta": 0.0, "median_sharpe_delta": 0.0,
                        "max_dominant_share": 0.0, "material_positive_years": 0,
                        "material_negative_years": 0,
                    }
                )
                continue
            if score == 0.0:
                classifications.append(
                    {
                        "object_type": "state", "object_id": state_id,
                        "counterfactual": "zero_mapped_state", "classification": "unmodeled_state",
                        "median_return_delta": 0.0, "median_sharpe_delta": 0.0,
                        "max_dominant_share": 0.0, "material_positive_years": 0,
                        "material_negative_years": 0,
                    }
                )
                continue
            state_mapped = zero_signal_contribution(mapped, column, mask)
            state_groups = aggregate_signal_groups(state_mapped, memberships)
            state_target, _ = positions_for_rule(state_groups, champion_rule)
            evidence = _comparison_rows(
                "state", state_id, "zero_mapped_state", champion_target, state_target,
                champion_results, evaluator.evaluate(state_target),
            )
            state_rows.extend(evidence)
            classifications.append(
                _classification_record(
                    "state", state_id, "zero_mapped_state", evidence, protocol
                )
            )

    _write_csv(artifacts_dir / "factor_catalog.csv", pd.DataFrame(catalog_rows))
    _write_csv(artifacts_dir / "signal_attribution.csv", pd.DataFrame(signal_rows))
    _write_csv(artifacts_dir / "state_attribution.csv", pd.DataFrame(state_rows))

    weight_rows: list[dict[str, object]] = []
    for index, name in enumerate(FACTOR_COLUMNS):
        for delta in (-float(protocol["weight_sensitivity"]["step"]), float(protocol["weight_sensitivity"]["step"])):
            changed_rule = rule_with_weight_delta(champion_rule, index, delta)
            target, _ = positions_for_rule(grouped, changed_rule)
            evidence = _comparison_rows(
                "weight", name, f"delta={delta:+.2f}", champion_target, target,
                champion_results, evaluator.evaluate(target),
            )
            label = _local_label(evidence, protocol)
            for row in evidence:
                row["local_classification"] = label
                row["weights"] = json.dumps(changed_rule.weights)
            weight_rows.extend(evidence)
    _write_csv(artifacts_dir / "weight_sensitivity.csv", pd.DataFrame(weight_rows))

    threshold_rows: list[dict[str, object]] = []
    threshold_fields = {"entry": "enter", "exit": "exit"}
    for parameter, field in threshold_fields.items():
        values = protocol["threshold_sensitivity"][parameter]
        baseline_value = getattr(champion_rule, field)
        for value in values:
            if float(value) == float(baseline_value):
                continue
            changed_rule = replace(champion_rule, **{field: float(value)})
            target, _ = positions_for_rule(grouped, changed_rule)
            evidence = _comparison_rows(
                "threshold", parameter, f"{field}={float(value):.2f}", champion_target, target,
                champion_results, evaluator.evaluate(target),
            )
            label = _local_label(evidence, protocol)
            for row in evidence:
                row["local_classification"] = label
                row["value"] = float(value)
            threshold_rows.extend(evidence)
    _write_csv(artifacts_dir / "threshold_sensitivity.csv", pd.DataFrame(threshold_rows))

    machine_rows: list[dict[str, object]] = []
    parameter_fields = {
        "entry_confirm_days": "confirm_days",
        "min_hold_days": "min_hold_days",
        "exit_confirm_days": "exit_confirm_days",
    }
    for parameter, field in parameter_fields.items():
        for value in protocol["state_machine_sensitivity"][parameter]:
            if int(value) == int(getattr(champion_rule, field)):
                continue
            changed_rule = replace(champion_rule, **{field: int(value)})
            target, _ = positions_for_rule(grouped, changed_rule)
            evidence = _comparison_rows(
                "state_machine", parameter, f"{field}={int(value)}", champion_target, target,
                champion_results, evaluator.evaluate(target),
            )
            label = _local_label(evidence, protocol)
            for row in evidence:
                row["local_classification"] = label
                row["value"] = int(value)
            machine_rows.extend(evidence)
    _write_csv(artifacts_dir / "state_machine_sensitivity.csv", pd.DataFrame(machine_rows))

    classifications_frame = pd.DataFrame(classifications).sort_values(
        ["classification", "object_type", "object_id", "counterfactual"],
        kind="stable",
    ).reset_index(drop=True)
    _write_csv(artifacts_dir / "classification.csv", classifications_frame)
    _heatmap(classifications_frame, artifacts_dir / "attribution_heatmap.html")

    stable_negative = classifications_frame.loc[
        classifications_frame["classification"] == "stable_negative",
        ["object_type", "object_id", "counterfactual"],
    ].to_dict("records")
    summary = {
        "status": "COMPLETE",
        "experiment_type": "champion_attribution",
        "sample_cutoff": "2025-12-31",
        "holdout_accessed": False,
        "frozen_challenger": None,
        "visible_data_hashes": dict(sorted(data_hashes.items())),
        "unknown_values": dict(sorted(unknown_values.items())),
        "stable_negative": stable_negative,
        "stable_negative_count": len(stable_negative),
        "classification_counts": {
            str(name): int(count)
            for name, count in classifications_frame["classification"].value_counts().sort_index().items()
        },
        "evaluated_target_count": len(evaluator.cache),
    }
    _write_json(artifacts_dir / "metrics.json", summary)
    return summary


def run_champion_attribution(
    raw_dir: Path,
    baseline_root: Path,
    artifacts_dir: Path,
    protocol: Mapping[str, object],
    *,
    fee_rate: float = 0.0005,
    init_cash: float = 1_000_000.0,
) -> dict[str, object]:
    """Load only pre-2026 inputs and execute the frozen champion attribution."""
    _validate_protocol(protocol)
    champion = protocol.get("champion")
    if not isinstance(champion, Mapping):
        raise ValueError("attribution protocol is missing champion identity")
    baseline = resolve_baseline(Path(baseline_root), str(champion["version"]))
    if baseline.sha256 != str(champion["sha256"]):
        raise ValueError("attribution champion hash differs from frozen baseline")
    data = load_market_data(
        Path(raw_dir), "588080.SH", "etf", cutoff=RESEARCH_CUTOFF
    )
    if any("2026" in name for name in data.hashes):
        raise AssertionError("2026 file entered visible attribution hashes")
    factor_result = generate_factor_frame(data)
    raw = factor_result.frame.filter(like="raw__")
    if len(raw.columns) != 12:
        raise AssertionError(f"expected 12 registered CZSC signals, got {len(raw.columns)}")
    mapped, unknown = map_signal_frame(raw)
    memberships = signal_groups(mapped.columns)
    grouped = aggregate_signal_groups(mapped, memberships)
    pd.testing.assert_frame_equal(
        grouped.loc[:, list(FACTOR_COLUMNS)],
        factor_result.frame.loc[:, list(FACTOR_COLUMNS)],
    )
    summary = run_attribution_from_frames(
        data.daily,
        raw,
        mapped,
        memberships,
        grouped,
        baseline.rule,
        artifacts_dir,
        protocol,
        data_hashes=data.hashes,
        unknown_values=unknown,
        fee_rate=fee_rate,
        init_cash=init_cash,
    )
    summary["champion"] = {"version": baseline.version, "sha256": baseline.sha256}
    _write_json(Path(artifacts_dir) / "metrics.json", summary)
    return summary
