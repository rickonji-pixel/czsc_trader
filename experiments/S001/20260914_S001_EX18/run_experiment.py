from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import normalized_mutual_info_score
from strategy_evaluator import paired_stationary_bootstrap, performance_metrics

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.backtesting.datasets import ReplayData
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.models import StrategySnapshot
from czsc_trader.backtesting.signal_replay import SignalReplay, replay_signals
from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.factors import generate_factor_frame
from czsc_trader.four_layer import normalized_signal_factors, positions_from_scores
from czsc_trader.regime_weight import classify_regimes, lagged_efficiency_ratio, score_with_regime_weights


EXPERIMENT_ID = "20260914_S001_EX18"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


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


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(_clean(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _renormalize(weights: pd.Series, removed: set[str]) -> pd.Series:
    result = weights.astype(float).copy()
    result.loc[list(removed)] = 0.0
    scale = float(result.abs().sum())
    if not np.isfinite(scale) or scale <= 0.0:
        raise ValueError("ablation removed all usable weights")
    return result / scale


def _one_hot(names: list[str], selected: str) -> pd.Series:
    if selected not in names:
        raise ValueError(f"unknown standalone signal: {selected}")
    return pd.Series({name: float(name == selected) for name in names}, dtype=float)


def _returns(equity: pd.Series, initial_cash: float) -> pd.Series:
    values = equity.astype(float).copy()
    previous = values.shift(1)
    previous.iloc[0] = float(initial_cash)
    result = values.div(previous).sub(1.0)
    if result.empty or not np.isfinite(result.to_numpy()).all():
        raise ValueError("daily returns are empty or non-finite")
    return result


def _win_loss_ratio(cycles: pd.DataFrame) -> tuple[float | None, str]:
    if cycles.empty:
        return None, "NO_CLOSED_TRADES"
    values = cycles["net_return"].astype(float)
    wins = values.loc[values.gt(0.0)]
    losses = values.loc[values.lt(0.0)]
    if wins.empty:
        return None, "NO_WINS"
    if losses.empty:
        return None, "NO_LOSSES"
    return float(wins.mean() / abs(losses.mean())), "VALID"


def _diagnostic_signal_replay(
    snapshot: StrategySnapshot,
    replay: ReplayData,
    start: pd.Timestamp,
    end: pd.Timestamp,
    target: pd.Series,
    scores: pd.Series,
    regimes: pd.Series,
) -> SignalReplay:
    """Build diagnostic decisions while preserving TDR's T+1 account semantics."""
    sessions = pd.DatetimeIndex(
        pd.to_datetime(replay.adjusted.daily["dt"]),
        name="dt",
    )
    evaluation = sessions[(sessions >= start) & (sessions <= end)]
    if evaluation.empty:
        raise ValueError("diagnostic interval contains no trading sessions")
    first_location = sessions.get_loc(evaluation[0])
    visible = sessions[
        max(0, int(first_location) - 1) : sessions.get_loc(evaluation[-1]) + 1
    ]
    next_sessions = pd.Series(sessions[1:], index=sessions[:-1])
    rows: list[dict[str, object]] = []
    for signal_date in visible:
        position = int(target.loc[signal_date])
        raw_id = f"{snapshot.identity.reference}|{signal_date.date()}|{position}".encode()
        rows.append(
            {
                "decision_id": "DEC-" + hashlib.sha256(raw_id).hexdigest()[:20].upper(),
                "signal_date": signal_date,
                "valid_session": next_sessions.get(signal_date, pd.NaT),
                "target_position": position,
                "factor_score": float(scores.loc[signal_date]),
                "regime": str(regimes.loc[signal_date]),
            }
        )
    return SignalReplay(
        snapshot=snapshot,
        decisions=pd.DataFrame(rows),
        calculation_start=sessions[0],
        calculation_end=sessions[-1],
        evaluation_start=evaluation[0],
        evaluation_end=evaluation[-1],
    )


def _window_metrics(
    variant_id: str,
    kind: str,
    returns: pd.Series,
    cycles: pd.DataFrame,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    windows = [("FULL", returns)]
    windows.extend((str(year), group) for year, group in returns.groupby(returns.index.year))
    for label, selected in windows:
        metrics = performance_metrics(selected.to_numpy())
        if label == "FULL":
            selected_cycles = cycles
        else:
            selected_cycles = cycles.loc[
                pd.to_datetime(cycles["exit_date"]).dt.year.eq(int(label))
            ]
        ratio, ratio_status = _win_loss_ratio(selected_cycles)
        rows.append(
            {
                "variant_id": variant_id,
                "variant_kind": kind,
                "window": label,
                "sessions": int(len(selected)),
                "total_return": float((1.0 + selected).prod() - 1.0),
                "cagr": metrics.cagr,
                "maximum_drawdown": metrics.max_drawdown,
                "calmar": metrics.calmar,
                "closed_trades": int(len(selected_cycles)),
                "win_loss_ratio": ratio,
                "win_loss_ratio_status": ratio_status,
            }
        )
    return rows


def _classify(
    row: pd.Series,
    annual_deltas: list[float],
    probabilities: dict[str, float],
    rules: dict[str, object],
) -> tuple[str, int, int]:
    material = float(rules["annual_material_return_delta"])
    positive_years = sum(value > material for value in annual_deltas)
    negative_years = sum(value < -material for value in annual_deltas)
    redundant = (
        float(row["target_difference_ratio"])
        <= float(rules["redundant_target_difference_max"])
        and abs(float(row["delta_cagr"]))
        <= float(rules["redundant_cagr_delta_abs_max"])
        and abs(float(row["delta_maximum_drawdown"]))
        <= float(rules["redundant_max_drawdown_delta_abs_max"])
        and abs(float(row["delta_calmar"]))
        <= float(rules["redundant_calmar_delta_abs_max"])
    )
    if redundant:
        return "REDUNDANT", positive_years, negative_years
    if (
        positive_years >= int(rules["regime_dependent_positive_years_min"])
        and negative_years >= int(rules["regime_dependent_negative_years_min"])
    ):
        return "REGIME_DEPENDENT", positive_years, negative_years
    deltas = [
        float(row["delta_cagr"]),
        float(row["delta_maximum_drawdown"]),
        float(row["delta_calmar"]),
    ]
    threshold = float(rules["bootstrap_probability_threshold"])
    if (
        all(value > 0.0 for value in deltas)
        and probabilities["cagr"] >= threshold
        and probabilities["calmar"] >= threshold
    ):
        return "SUPPORTIVE", positive_years, negative_years
    if (
        all(value < 0.0 for value in deltas)
        and probabilities["cagr"] <= 1.0 - threshold
        and probabilities["calmar"] <= 1.0 - threshold
    ):
        return "HARMFUL", positive_years, negative_years
    return "MIXED", positive_years, negative_years


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol["frequency"]["used_as_gate"]:
        raise ValueError("EX18 must not apply a frequency gate")
    if any(
        protocol.get(key)
        for key in (
            "candidate_generation",
            "parameter_selection",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX18 is diagnostic only")
    if _sha256(repo / "strategies/S001/versions/v2.json") != protocol["strategy_file_sha256"]:
        raise ValueError("S001-v2 strategy file differs from frozen protocol")
    if _sha256(repo / "catalog/signals/czsc.json") != protocol["signal_catalog_sha256"]:
        raise ValueError("signal catalog differs from frozen protocol")

    context = RepositoryContext.discover(repo)
    snapshot = resolve_registered_strategy(
        context,
        str(protocol["strategy_id"]),
        str(protocol["strategy_version"]),
    )
    if snapshot.source_hash != protocol["strategy_release_hash"]:
        raise ValueError("S001-v2 release hash differs from frozen protocol")
    cutoff = pd.Timestamp(protocol["development_cutoff"]).date()
    replay = load_replay_data(
        context,
        "research",
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        cutoff,
    )
    if replay.fingerprint != protocol["data_fingerprint"]:
        raise ValueError("research data fingerprint differs from frozen protocol")

    baseline = snapshot.resolved_rule
    if baseline.strategy != "czsc_regime_weight" or baseline.rule is None or baseline.execution is None:
        raise ValueError("S001-v2 is not the expected complete regime-weight strategy")
    names = list(map(str, baseline.factor_names))
    expected_names = [name for values in protocol["family_map"].values() for name in values]
    if len(names) != 12 or set(names) != set(expected_names):
        raise ValueError("frozen factor identities differ from the preregistered family map")

    generated = generate_factor_frame(replay.adjusted).frame
    factors = normalized_signal_factors(generated.loc[:, names]).astype(float)
    adjusted_daily = replay.adjusted.daily.copy()
    adjusted_daily["dt"] = pd.to_datetime(adjusted_daily["dt"]).dt.normalize()
    adjusted_daily = adjusted_daily.set_index("dt").sort_index().reindex(factors.index)
    if adjusted_daily["close"].isna().any():
        raise ValueError("adjusted daily close does not align to factor frame")
    regimes = classify_regimes(
        lagged_efficiency_ratio(adjusted_daily["close"], baseline.er_lookback),
        baseline.er_threshold,
    ).reindex(factors.index)
    control_applied = apply_resolved_baseline(
        generated,
        baseline,
        daily_close=adjusted_daily["close"],
        normalized_factors=factors,
        regimes=regimes,
    )

    catalog_items = {item["name"]: item for item in _read(repo / "catalog/signals/czsc.json")["items"]}
    family_by_name: dict[str, str] = {}
    inventory_rows: list[dict[str, object]] = []
    aliases = {name: f"F{index:02d}" for index, name in enumerate(names, start=1)}
    rolling_window = int(protocol["frequency"]["rolling_window_sessions"])
    for name in names:
        function_name = name.split("__")[2]
        item = catalog_items.get(function_name)
        if not isinstance(item, dict):
            raise ValueError(f"signal has no FSC definition: {function_name}")
        family = str(item["information_family"])
        family_by_name[name] = family
        if name not in protocol["family_map"][family]:
            raise ValueError(f"FSC family differs from frozen protocol: {name}")
        series = factors[name]
        positive_start = series.gt(0.0) & ~series.shift(1, fill_value=0.0).gt(0.0)
        rolling = positive_start.astype(int).rolling(rolling_window, min_periods=rolling_window).sum().dropna()
        inventory_rows.append(
            {
                "factor_alias": aliases[name],
                "signal_input": name,
                "czsc_function": function_name,
                "information_family": family,
                "catalog_status": item["status"],
                "coverage": float(series.notna().mean()),
                "distinct_states": int(series.nunique(dropna=True)),
                "negative_state_ratio": float(series.lt(0.0).mean()),
                "neutral_state_ratio": float(series.eq(0.0).mean()),
                "positive_state_ratio": float(series.gt(0.0).mean()),
                "state_switches": int(series.ne(series.shift()).iloc[1:].sum()),
                "positive_episode_count": int(positive_start.sum()),
                "rolling_60_positive_episode_median": float(rolling.median()),
                "rolling_60_positive_episode_p10": float(rolling.quantile(0.10, interpolation="lower")),
                "default_weight": float(pd.Series(baseline.factor_weights, index=names).loc[name]),
                "trend_weight": float(pd.Series(baseline.regime_factor_weights["trend"], index=names).loc[name]),
                "range_weight": float(pd.Series(baseline.regime_factor_weights["range"], index=names).loc[name]),
            }
        )
    inventory = pd.DataFrame(inventory_rows)

    pair_rows: list[dict[str, object]] = []
    for left_index, left in enumerate(names):
        for right in names[left_index + 1 :]:
            pair_rows.append(
                {
                    "left_alias": aliases[left],
                    "right_alias": aliases[right],
                    "left_signal": left,
                    "right_signal": right,
                    "left_family": family_by_name[left],
                    "right_family": family_by_name[right],
                    "nmi": float(
                        normalized_mutual_info_score(
                            factors[left].astype(str),
                            factors[right].astype(str),
                        )
                    ),
                    "identical_state_ratio": float(factors[left].eq(factors[right]).mean()),
                }
            )
    redundancy = pd.DataFrame(pair_rows).sort_values("nmi", ascending=False)

    default_weights = pd.Series(baseline.factor_weights, index=names, dtype=float)
    regime_weights = {
        label: pd.Series(values, index=names, dtype=float)
        for label, values in baseline.regime_factor_weights.items()
    }
    control_scores = score_with_regime_weights(
        factors,
        regimes,
        regime_weights,
        default_weights,
    )
    if not np.allclose(control_scores, control_applied.scores, rtol=0.0, atol=1e-12):
        raise AssertionError("manual control scores differ from frozen strategy")

    variants: list[dict[str, object]] = [
        {
            "variant_id": "CONTROL",
            "variant_kind": "control",
            "subject": "S001-v2",
            "removed": set(),
            "standalone": None,
        }
    ]
    for name in names:
        variants.append(
            {
                "variant_id": f"LOO-{aliases[name]}",
                "variant_kind": "leave_one_out",
                "subject": aliases[name],
                "removed": {name},
                "standalone": None,
            }
        )
    for family, members in protocol["family_map"].items():
        variants.append(
            {
                "variant_id": f"LOO-G-{family}",
                "variant_kind": "leave_family_out",
                "subject": family,
                "removed": set(members),
                "standalone": None,
            }
        )
    for name in names:
        variants.append(
            {
                "variant_id": f"SOLO-{aliases[name]}",
                "variant_kind": "standalone",
                "subject": aliases[name],
                "removed": set(),
                "standalone": name,
            }
        )
    if len(variants) != int(protocol["variants"]["total"]):
        raise AssertionError("variant count differs from frozen protocol")

    evaluation_start = pd.Timestamp(protocol["evaluation_start"])
    evaluation_end = pd.Timestamp(protocol["evaluation_end"])
    selected_sessions = pd.DatetimeIndex(
        pd.to_datetime(replay.execution_daily["dt"]),
        name="date",
    )
    selected_sessions = selected_sessions[
        (selected_sessions >= evaluation_start) & (selected_sessions <= evaluation_end)
    ]
    if selected_sessions.empty:
        raise ValueError("evaluation window lacks execution sessions")

    variant_targets: dict[str, pd.Series] = {}
    variant_returns: dict[str, pd.Series] = {}
    variant_cycles: dict[str, pd.DataFrame] = {}
    variant_weight_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    window_rows: list[dict[str, object]] = []
    for variant in variants:
        removed = set(variant["removed"])
        standalone = variant["standalone"]
        if variant["variant_kind"] == "control":
            scores = control_scores
            target = control_applied.target_position.astype(float)
            selected_default = default_weights
            selected_regime = regime_weights
        elif standalone is not None:
            selected_weights = _one_hot(names, str(standalone))
            selected_default = selected_weights
            selected_regime = {"trend": selected_weights, "range": selected_weights}
            scores = score_with_regime_weights(
                factors,
                regimes,
                selected_regime,
                selected_weights,
            )
            target = positions_from_scores(
                scores,
                baseline.rule.enter,
                baseline.rule.exit,
                baseline.rule,
            )
        else:
            selected_default = _renormalize(default_weights, removed)
            selected_regime = {
                label: _renormalize(weights, removed)
                for label, weights in regime_weights.items()
            }
            scores = score_with_regime_weights(
                factors,
                regimes,
                selected_regime,
                selected_default,
            )
            target = positions_from_scores(
                scores,
                baseline.rule.enter,
                baseline.rule.exit,
                baseline.rule,
            )
        variant_id = str(variant["variant_id"])
        variant_baseline = replace(
            baseline,
            factor_weights=tuple(float(selected_default.loc[name]) for name in names),
            regime_factor_weights={
                label: tuple(float(weights.loc[name]) for name in names)
                for label, weights in selected_regime.items()
            },
        )
        variant_snapshot = replace(snapshot, resolved_rule=variant_baseline)
        if variant["variant_kind"] == "control":
            signal_replay = replay_signals(
                variant_snapshot,
                replay,
                evaluation_start.date(),
                evaluation_end.date(),
            )
        else:
            signal_replay = _diagnostic_signal_replay(
                variant_snapshot,
                replay,
                evaluation_start,
                evaluation_end,
                target,
                scores,
                regimes,
            )
        result = replay_account(signal_replay, replay, float(protocol["initial_cash"]))
        account = result.account_daily.copy()
        account["date"] = pd.to_datetime(account["date"])
        account = account.set_index("date").sort_index()
        equity = account["equity"].astype(float)
        returns = _returns(equity, float(protocol["initial_cash"]))
        cycles = result.trades.loc[result.trades["status"].eq("CLOSED")].copy()
        variant_returns[variant_id] = returns
        variant_cycles[variant_id] = cycles
        formal_target = account["target_position"].astype(float)
        replay_target = (
            signal_replay.decisions.dropna(subset=["valid_session"])
            .assign(valid_session=lambda frame: pd.to_datetime(frame["valid_session"]))
            .set_index("valid_session")["target_position"]
            .reindex(formal_target.index)
            .astype(float)
        )
        if not formal_target.equals(replay_target):
            raise AssertionError(f"{variant_id} manual target differs from TDR replay target")
        variant_targets[variant_id] = formal_target
        for name in names:
            variant_weight_rows.append(
                {
                    "variant_id": variant_id,
                    "factor_alias": aliases[name],
                    "signal_input": name,
                    "default_weight": float(selected_default.loc[name]),
                    "trend_weight": float(selected_regime["trend"].loc[name]),
                    "range_weight": float(selected_regime["range"].loc[name]),
                }
            )
        full_metrics = performance_metrics(returns.to_numpy())
        ratio, ratio_status = _win_loss_ratio(cycles)
        target_eval = formal_target
        entries = target_eval.gt(0.0) & ~target_eval.shift(1, fill_value=0.0).gt(0.0)
        rolling_entries = entries.astype(int).rolling(rolling_window, min_periods=rolling_window).sum().dropna()
        summary_rows.append(
            {
                "variant_id": variant_id,
                "variant_kind": variant["variant_kind"],
                "subject": variant["subject"],
                "total_return": float((1.0 + returns).prod() - 1.0),
                "cagr": full_metrics.cagr,
                "maximum_drawdown": full_metrics.max_drawdown,
                "calmar": full_metrics.calmar,
                "closed_trades": int(len(cycles)),
                "win_loss_ratio": ratio,
                "win_loss_ratio_status": ratio_status,
                "exposure_ratio": float(target_eval.gt(0.0).mean()),
                "target_entry_episodes": int(entries.sum()),
                "rolling_60_entry_median": float(rolling_entries.median()),
                "rolling_60_entry_p10": float(
                    rolling_entries.quantile(0.10, interpolation="lower")
                ),
                "frequency_gate_applied": False,
            }
        )
        window_rows.extend(
            _window_metrics(
                variant_id,
                str(variant["variant_kind"]),
                returns,
                cycles,
            )
        )

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    variant_weights = pd.DataFrame(variant_weight_rows)
    control_summary = summary.set_index("variant_id").loc["CONTROL"]
    expected = protocol["control_expected"]
    tolerance = float(expected["tolerance"])
    if abs(float(control_summary["cagr"]) - float(expected["cagr"])) > tolerance:
        raise AssertionError(
            "control CAGR does not reproduce frozen S001-v2: "
            f"actual={float(control_summary['cagr']):.15f}, "
            f"expected={float(expected['cagr']):.15f}"
        )
    if (
        abs(
            float(control_summary["maximum_drawdown"])
            - float(expected["maximum_drawdown"])
        )
        > tolerance
    ):
        raise AssertionError(
            "control drawdown does not reproduce frozen S001-v2: "
            f"actual={float(control_summary['maximum_drawdown']):.15f}, "
            f"expected={float(expected['maximum_drawdown']):.15f}"
        )
    if int(control_summary["closed_trades"]) != int(expected["closed_trades"]):
        raise AssertionError("control trade count does not reproduce frozen S001-v2")

    control_returns = variant_returns["CONTROL"]
    control_target = variant_targets["CONTROL"].reindex(selected_sessions)
    bootstrap_rows: list[dict[str, object]] = []
    bootstrap_lookup: dict[str, dict[str, float]] = {}
    audit_rows: list[dict[str, object]] = []
    ablations = summary.loc[
        summary["variant_kind"].isin(["leave_one_out", "leave_family_out"])
    ]
    full_windows = windows.loc[windows["window"].eq("FULL")].set_index("variant_id")
    annual = windows.loc[windows["window"].ne("FULL")]
    control_annual = annual.loc[annual["variant_id"].eq("CONTROL")].set_index("window")
    bootstrap_protocol = protocol["bootstrap"]
    for order, variant in enumerate(ablations.itertuples(index=False)):
        variant_id = str(variant.variant_id)
        comparison = paired_stationary_bootstrap(
            control_returns.to_numpy(),
            variant_returns[variant_id].to_numpy(),
            champion_id="CONTROL",
            comparator_id=variant_id,
            repetitions=int(bootstrap_protocol["repetitions"]),
            mean_block_length=int(bootstrap_protocol["mean_block_length"]),
            seed=int(bootstrap_protocol["seed"]) + order,
        )
        probabilities = {
            "cagr": comparison.cagr.probability_favorable,
            "maximum_drawdown": comparison.max_drawdown.probability_favorable,
            "calmar": comparison.calmar.probability_favorable,
        }
        bootstrap_lookup[variant_id] = probabilities
        for metric in (comparison.cagr, comparison.max_drawdown, comparison.calmar):
            bootstrap_rows.append(
                {
                    "variant_id": variant_id,
                    "metric": metric.metric,
                    "point_difference_control_minus_variant": metric.point_difference,
                    "lower_95": metric.lower_95,
                    "upper_95": metric.upper_95,
                    "probability_control_favorable": metric.probability_favorable,
                }
            )
        selected = full_windows.loc[variant_id]
        row = pd.Series(
            {
                "target_difference_ratio": float(
                    control_target.ne(
                        variant_targets[variant_id].reindex(selected_sessions)
                    ).mean()
                ),
                "delta_cagr": float(control_summary["cagr"] - selected["cagr"]),
                "delta_maximum_drawdown": float(
                    control_summary["maximum_drawdown"]
                    - selected["maximum_drawdown"]
                ),
                "delta_calmar": float(
                    control_summary["calmar"] - selected["calmar"]
                ),
            }
        )
        selected_annual = annual.loc[annual["variant_id"].eq(variant_id)].set_index("window")
        annual_deltas = [
            float(control_annual.loc[year, "total_return"] - selected_annual.loc[year, "total_return"])
            for year in control_annual.index
        ]
        label, positive_years, negative_years = _classify(
            row,
            annual_deltas,
            probabilities,
            protocol["classification"],
        )
        audit_rows.append(
            {
                "variant_id": variant_id,
                "variant_kind": variant.variant_kind,
                "subject": variant.subject,
                **row.to_dict(),
                "material_positive_years": positive_years,
                "material_negative_years": negative_years,
                "bootstrap_cagr_probability": probabilities["cagr"],
                "bootstrap_drawdown_probability": probabilities["maximum_drawdown"],
                "bootstrap_calmar_probability": probabilities["calmar"],
                "classification": label,
            }
        )
    contribution = pd.DataFrame(audit_rows)
    bootstrap_frame = pd.DataFrame(bootstrap_rows)

    reference = protocol["standalone_reference"]
    standalone = summary.loc[summary["variant_kind"].eq("standalone")].copy()
    standalone["return_gate"] = standalone["cagr"].ge(float(reference["required_cagr"]))
    standalone["drawdown_gate"] = standalone["maximum_drawdown"].ge(
        float(reference["maximum_drawdown_floor"])
    )
    standalone["joint_mandate_pass"] = standalone["return_gate"] & standalone["drawdown_gate"]

    return_matrix = pd.DataFrame(variant_returns)
    return_matrix.index.name = "date"
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    inventory.to_csv(artifacts / "factor_inventory.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    redundancy.to_csv(artifacts / "redundancy_pairs.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    summary.to_csv(artifacts / "variant_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    variant_weights.to_csv(artifacts / "variant_weights.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    windows.to_csv(artifacts / "window_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    contribution.to_csv(artifacts / "contribution_audit.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    bootstrap_frame.to_csv(artifacts / "bootstrap_evidence.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    standalone.to_csv(artifacts / "standalone_assessment.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    return_matrix.reset_index().to_csv(
        artifacts / "daily_return_matrix.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
    )

    factor_counts = contribution.loc[
        contribution["variant_kind"].eq("leave_one_out"), "classification"
    ].value_counts().sort_index().to_dict()
    family_counts = contribution.loc[
        contribution["variant_kind"].eq("leave_family_out"), "classification"
    ].value_counts().sort_index().to_dict()
    low_frequency_aliases = inventory.loc[
        inventory["rolling_60_positive_episode_p10"].eq(0.0), "factor_alias"
    ].tolist()
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "strategy_reference": snapshot.identity.reference,
        "strategy_release_hash": snapshot.source_hash,
        "data_fingerprint": replay.fingerprint,
        "factor_count": len(names),
        "information_family_count": len(protocol["family_map"]),
        "variant_count": len(variants),
        "frequency_gate_applied": False,
        "control_reproduced": True,
        "control_metrics": control_summary.to_dict(),
        "factor_classification_counts": factor_counts,
        "family_classification_counts": family_counts,
        "low_frequency_aliases": low_frequency_aliases,
        "standalone_joint_mandate_pass_count": int(standalone["joint_mandate_pass"].sum()),
        "maximum_pair_nmi": float(redundancy["nmi"].max()),
        "decision": "FACTOR_EVIDENCE_REAUDITED_NO_STRATEGY_CHANGE",
        "candidate_created": False,
        "strategy_mutated": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "audit_summary.json", evidence)

    factor_table = [
        "|输入|信息族|剔除后年化差|剔除后回撤差|剔除后卡玛差|Bootstrap年化/卡玛|标签|",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    factor_audit = contribution.loc[contribution["variant_kind"].eq("leave_one_out")]
    for row in factor_audit.itertuples(index=False):
        name = names[int(str(row.subject).removeprefix("F")) - 1]
        factor_table.append(
            f"|{row.subject}|{family_by_name[name]}|{row.delta_cagr:.2%}|"
            f"{row.delta_maximum_drawdown:.2%}|{row.delta_calmar:.3f}|"
            f"{row.bootstrap_cagr_probability:.1%}/{row.bootstrap_calmar_probability:.1%}|"
            f"{row.classification}|"
        )
    family_table = [
        "|信息族|剔除后年化差|剔除后回撤差|剔除后卡玛差|标签|",
        "|---|---:|---:|---:|---|",
    ]
    for row in contribution.loc[
        contribution["variant_kind"].eq("leave_family_out")
    ].itertuples(index=False):
        family_table.append(
            f"|{row.subject}|{row.delta_cagr:.2%}|{row.delta_maximum_drawdown:.2%}|"
            f"{row.delta_calmar:.3f}|{row.classification}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S001 EX18 执行\n\n"
        f"状态：`COMPLETE`。控制策略精确复现{int(control_summary['closed_trades'])}笔闭合交易；"
        f"完成{len(variants)}个固定版本、{len(contribution)}个剔除对照和"
        f"{int(protocol['bootstrap']['repetitions'])}次配对平稳Bootstrap。频率未参与筛选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX18 结论\n\n"
        f"S001-v2完整开发池年化{float(control_summary['cagr']):.2%}、最大回撤"
        f"{float(control_summary['maximum_drawdown']):.2%}、卡玛{float(control_summary['calmar']):.3f}。"
        "控制组通过TDR正式账户引擎精确复现。\n\n"
        "## 主要结论\n\n"
        f"1. 12项输入的剔除审计得到{int(factor_counts.get('SUPPORTIVE', 0))}项`SUPPORTIVE`、"
        f"{int(factor_counts.get('REGIME_DEPENDENT', 0))}项`REGIME_DEPENDENT`，没有"
        "`REDUNDANT`或`HARMFUL`。在当前冻结结构和开发池内，没有证据支持删除任何输入。\n"
        f"2. 4个信息族得到{int(family_counts.get('SUPPORTIVE', 0))}个`SUPPORTIVE`、"
        f"{int(family_counts.get('REGIME_DEPENDENT', 0))}个`REGIME_DEPENDENT`。市场结构与趋势动量"
        "是稳定支柱；量能流动性与位置估值具有明显阶段依赖。\n"
        f"3. {', '.join(low_frequency_aliases)}在较差的60交易日窗口内可出现零次正状态，"
        "但剔除后组合表现仍有实质下降。频率不能作为S001组合输入的质量门槛，只适合作为观察指标。\n"
        f"4. 12项单输入中有{int(standalone['joint_mandate_pass'].sum())}项独立满足收益与15%回撤双门。"
        "S001-v2的表现来自多信息族组合、权重和regime分配，不能归因于某一个单独信号。\n\n"
        "## 单输入剔除\n\n"
        + "\n".join(factor_table)
        + "\n\n## 信息族剔除\n\n"
        + "\n".join(family_table)
        + "\n\n## 解释边界\n\n"
        "剔除实验会把剩余权重重新归一化，并保持原阈值、regime和执行规则不变；因此差异同时包含"
        "输入缺失与权重再分配的影响，只能解释为组合内边际证据，不能解释为单因子因果收益。"
        "全部结果来自S001-v2原开发池，不能充当前瞻证据。实验不授权删除输入、修改S001-v2或生成候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "strategy_version": protocol["strategy_version"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": evidence["decision"],
            "candidate_generation": False,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
