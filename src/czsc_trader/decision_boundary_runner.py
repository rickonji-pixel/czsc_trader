"""Cross-year decision-boundary diagnosis for the frozen 0824_EX04 strategy."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from itertools import product
from math import comb
from pathlib import Path
import json

import numpy as np
import pandas as pd


def validate_protocol(protocol: Mapping[str, object]) -> None:
    """Reject drift from the preregistered decision-boundary diagnosis."""
    if protocol.get("experiment_type") != "ex04_decision_boundary_diagnosis":
        raise ValueError("not an EX04 decision boundary diagnosis protocol")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("decision boundary protocol must remain PRE_REGISTERED")
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise ValueError("visible sample must stop at 2025-12-31")
    if protocol.get("holdout_access_allowed") is not False:
        raise ValueError("holdout access must remain disabled")
    promotion = protocol.get("promotion")
    if not isinstance(promotion, Mapping) or any(bool(value) for value in promotion.values()):
        raise ValueError("promotion and optimization must remain disabled")
    years = tuple(int(value) for value in protocol.get("years", ()))
    if years != (2021, 2022, 2023, 2024, 2025):
        raise ValueError("decision boundary years differ from preregistration")
    source = protocol.get("source_evidence")
    if not isinstance(source, Mapping) or not source:
        raise ValueError("source evidence identities are missing")
    if any("2026" in str(value) for value in source.values()):
        raise ValueError("2026 evidence is forbidden")


def _require_columns(frame: pd.DataFrame, columns: set[str], *, label: str) -> None:
    missing = columns - set(frame.columns)
    if missing:
        raise ValueError(f"{label} columns are incomplete: {sorted(missing)}")


def _frontier_segments(ledger: pd.DataFrame) -> list[dict[str, object]]:
    ordered = ledger.sort_values(["window", "date"], kind="stable").reset_index(drop=True)
    mask = ordered["baseline_execution_position"].astype(float).eq(1.0) & ordered[
        "ex04_execution_position"
    ].astype(float).eq(0.0)
    boundary = ordered["window"].ne(ordered["window"].shift()) | mask.ne(mask.shift())
    group_ids = boundary.cumsum()
    rows: list[dict[str, object]] = []
    for _, interval in ordered.loc[mask].groupby(group_ids.loc[mask], sort=False):
        values = interval["log_wealth_delta"].astype(float)
        if not np.isfinite(values.to_numpy()).all():
            raise ValueError("frontier interval contains non-finite action values")
        rows.append(
            {
                "window": str(interval["window"].iloc[0]),
                "start_date": pd.Timestamp(interval["date"].iloc[0]),
                "end_date": pd.Timestamp(interval["date"].iloc[-1]),
                "interval_days": int(len(interval)),
                "path_mechanisms": "+".join(
                    dict.fromkeys(interval["path_mechanism"].astype(str).tolist())
                ),
                "alternative_log_value": -float(values.sum()),
            }
        )
    return rows


def build_frontier_events(
    ledger: pd.DataFrame,
    decision_events: pd.DataFrame,
    protocol: Mapping[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build one causal, fee-inclusive observation per position-divergence interval."""
    validate_protocol(protocol)
    _require_columns(
        ledger,
        {
            "window",
            "date",
            "baseline_execution_position",
            "ex04_execution_position",
            "path_mechanism",
            "log_wealth_delta",
        },
        label="daily path ledger",
    )
    _require_columns(
        decision_events,
        {
            "event_id",
            "window",
            "signal_date",
            "execution_date",
            "event_type",
            "regime",
            "block_label",
            "ex04_margin",
        },
        label="decision events",
    )
    ledger = ledger.copy()
    events = decision_events.copy()
    ledger["date"] = pd.to_datetime(ledger["date"])
    events["signal_date"] = pd.to_datetime(events["signal_date"])
    events["execution_date"] = pd.to_datetime(events["execution_date"])
    cutoff = pd.Timestamp(str(protocol["visible_sample_end"]))
    if ledger["date"].max() > cutoff or events[["signal_date", "execution_date"]].max().max() > cutoff:
        raise ValueError("frontier evidence exceeds 2025-12-31")
    if ledger["window"].astype(str).str.contains("2026").any():
        raise ValueError("2026 ledger window is forbidden")
    if events["window"].astype(str).str.contains("2026").any():
        raise ValueError("2026 decision event is forbidden")
    event_keys = ["window", "execution_date"]
    if events.duplicated(event_keys, keep=False).any() or events["event_id"].duplicated().any():
        raise ValueError("decision events violate duplicate one-to-one identity")

    segments = _frontier_segments(ledger)
    minimum_dates = ledger.groupby(ledger["window"].astype(str))["date"].min().to_dict()
    event_lookup = {
        (str(row.window), pd.Timestamp(row.execution_date)): row
        for row in events.itertuples(index=False)
    }
    used_event_ids: set[str] = set()
    frontier_rows: list[dict[str, object]] = []
    excluded_rows: list[dict[str, object]] = []
    event_type_map = protocol["event_type_map"]
    if not isinstance(event_type_map, Mapping):
        raise ValueError("event type map is missing")
    near_max = float(protocol["margin_bands"]["near_absolute_max"])
    eligible_regimes = {str(value) for value in protocol["eligible_regimes"]}

    for segment in segments:
        key = (str(segment["window"]), pd.Timestamp(segment["start_date"]))
        event = event_lookup.get(key)
        if event is None:
            is_period_initial = pd.Timestamp(segment["start_date"]) == pd.Timestamp(
                minimum_dates[str(segment["window"])]
            )
            if not is_period_initial:
                raise ValueError(f"{key}: missing causal event for noninitial interval")
            excluded_rows.append({**segment, "reason": "missing_causal_event"})
            continue
        event_id = str(event.event_id)
        used_event_ids.add(event_id)
        event_type = str(event.event_type)
        if event_type not in event_type_map:
            raise ValueError(f"unsupported decision event type: {event_type}")
        ex04_margin = float(event.ex04_margin)
        if not np.isfinite(ex04_margin):
            raise ValueError("decision event contains non-finite EX04 margin")
        regime = str(event.regime)
        row = {
            "event_id": event_id,
            "window": str(segment["window"]),
            "year": int(str(segment["window"])[:4]),
            "signal_date": pd.Timestamp(event.signal_date),
            **{key: value for key, value in segment.items() if key != "window"},
            "event_type": event_type,
            "action_family": str(event_type_map[event_type]),
            "regime": regime,
            "block_label": str(event.block_label),
            "ex04_margin": ex04_margin,
            "margin_band": "near" if abs(ex04_margin) <= near_max else "far",
            "model_eligible": regime in eligible_regimes,
        }
        frontier_rows.append(row)

    unused = set(events["event_id"].astype(str)) - used_event_ids
    if unused:
        raise ValueError(f"unused decision events remain: {sorted(unused)}")
    expected_unmatched = int(protocol["sample"]["expected_unmatched_interval_count"])
    if len(excluded_rows) != expected_unmatched:
        raise ValueError(
            f"unmatched interval count differs from preregistration: {len(excluded_rows)}"
        )
    frontier = pd.DataFrame(frontier_rows).sort_values(
        ["start_date", "event_id"], kind="stable"
    ).reset_index(drop=True)
    excluded = pd.DataFrame(excluded_rows)
    return frontier, excluded


def enumerate_boundary_rules(protocol: Mapping[str, object]) -> pd.DataFrame:
    """Enumerate the outcome-independent, preregistered simple rule library."""
    validate_protocol(protocol)
    domains = {
        "action_family": tuple(str(value) for value in protocol["action_families"]),
        "regime": tuple(str(value) for value in protocol["eligible_regimes"]),
        "block_label": tuple(str(value) for value in protocol["block_labels"]),
        "margin_band": tuple(str(value) for value in protocol["margin_bands"]["labels"]),
    }
    rows: list[dict[str, object]] = [
        {
            "rule_id": "always_ex04",
            "complexity": 0,
            "action_family": None,
            "regime": None,
            "block_label": None,
            "margin_band": None,
        }
    ]
    for family in protocol["rule_families"]:
        fields = tuple(str(value) for value in family)
        for values in product(*(domains[field] for field in fields)):
            conditions = dict(zip(fields, values, strict=True))
            row: dict[str, object] = {
                "rule_id": "|".join(f"{field}={conditions[field]}" for field in fields),
                "complexity": len(fields),
                "action_family": None,
                "regime": None,
                "block_label": None,
                "margin_band": None,
            }
            row.update(conditions)
            rows.append(row)
    frame = pd.DataFrame(rows)
    if len(frame) != len(frame.drop_duplicates("rule_id")):
        raise AssertionError("boundary rule IDs are not unique")
    return frame


def _rule_mask(events: pd.DataFrame, rule: Mapping[str, object]) -> pd.Series:
    if str(rule["rule_id"]) == "always_ex04":
        return pd.Series(False, index=events.index, dtype=bool)
    mask = pd.Series(True, index=events.index, dtype=bool)
    for field in ("action_family", "regime", "block_label", "margin_band"):
        value = rule.get(field)
        if value is not None and not pd.isna(value):
            mask &= events[field].astype(str).eq(str(value))
    return mask


def _eligible_events(events: pd.DataFrame, protocol: Mapping[str, object]) -> pd.DataFrame:
    eligible = events.loc[
        events["regime"].astype(str).isin(
            [str(value) for value in protocol["eligible_regimes"]]
        )
    ].copy()
    if "model_eligible" in eligible:
        eligible = eligible.loc[eligible["model_eligible"].astype(bool)].copy()
    values = eligible["alternative_log_value"].astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("eligible frontier events contain non-finite action values")
    return eligible


def select_training_rule(
    events: pd.DataFrame,
    rules: pd.DataFrame,
    held_out_year: int,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Select one rule without reading any held-out-year outcome."""
    validate_protocol(protocol)
    years = tuple(int(value) for value in protocol["years"])
    if int(held_out_year) not in years:
        raise ValueError("held-out year differs from preregistration")
    training_years = tuple(year for year in years if year != int(held_out_year))
    eligible = _eligible_events(events, protocol)
    training = eligible.loc[eligible["year"].astype(int).isin(training_years)].copy()
    minimum = int(protocol["selection"]["minimum_training_events"])
    candidates: list[dict[str, object]] = []
    for rule in rules.to_dict("records"):
        if str(rule["rule_id"]) == "always_ex04":
            continue
        matched = training.loc[_rule_mask(training, rule)].copy()
        support = len(matched)
        if support < minimum:
            continue
        annual = matched.groupby(matched["year"].astype(int))["alternative_log_value"].mean()
        if set(annual.index.astype(int)) != set(training_years):
            continue
        pooled = float(matched["alternative_log_value"].astype(float).mean())
        if pooled <= 0.0:
            continue
        candidates.append(
            {
                "held_out_year": int(held_out_year),
                "selected_rule_id": str(rule["rule_id"]),
                "selected_is_fallback": False,
                "training_support": support,
                "positive_training_years": int(annual.gt(0.0).sum()),
                "worst_annual_mean": float(annual.min()),
                "median_annual_mean": float(annual.median()),
                "training_pooled_mean": pooled,
                "complexity": int(rule["complexity"]),
            }
        )
    if not candidates:
        return {
            "held_out_year": int(held_out_year),
            "selected_rule_id": "always_ex04",
            "selected_is_fallback": True,
            "training_support": 0,
            "positive_training_years": 0,
            "worst_annual_mean": 0.0,
            "median_annual_mean": 0.0,
            "training_pooled_mean": 0.0,
            "complexity": 0,
        }
    ranked = sorted(
        candidates,
        key=lambda row: (
            -int(row["positive_training_years"]),
            -float(row["worst_annual_mean"]),
            -float(row["median_annual_mean"]),
            -float(row["training_pooled_mean"]),
            -int(row["training_support"]),
            int(row["complexity"]),
            str(row["selected_rule_id"]),
        ),
    )
    return ranked[0]


def run_leave_one_year_out(
    events: pd.DataFrame,
    rules: pd.DataFrame,
    protocol: Mapping[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Select on four years and emit event-level predictions for the fifth."""
    validate_protocol(protocol)
    eligible = _eligible_events(events, protocol)
    rule_index = rules.set_index(rules["rule_id"].astype(str), drop=False)
    fold_rows: list[dict[str, object]] = []
    prediction_rows: list[dict[str, object]] = []
    for year in (int(value) for value in protocol["years"]):
        selected = select_training_rule(eligible, rules, year, protocol)
        fold_rows.append(selected)
        rule = rule_index.loc[str(selected["selected_rule_id"])].to_dict()
        held_out = eligible.loc[eligible["year"].astype(int).eq(year)].copy()
        matched = _rule_mask(held_out, rule)
        for (_, event), is_match in zip(held_out.iterrows(), matched, strict=True):
            prediction_rows.append(
                {
                    "held_out_year": year,
                    "event_id": str(event["event_id"]),
                    "selected_rule_id": str(selected["selected_rule_id"]),
                    "matched": bool(is_match),
                    "alternative_log_value": float(event["alternative_log_value"]),
                }
            )
    return pd.DataFrame(fold_rows), pd.DataFrame(prediction_rows)


def _one_sided_sign_pvalue(positive: int, observations: int) -> float:
    if observations <= 0:
        return 1.0
    return float(sum(comb(observations, value) for value in range(positive, observations + 1))) / (
        2**observations
    )


def classify_boundary(
    folds: pd.DataFrame,
    predictions: pd.DataFrame,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Apply every preregistered out-of-fold stability gate."""
    validate_protocol(protocol)
    rules = folds.loc[~folds["selected_is_fallback"].astype(bool), "selected_rule_id"].astype(str)
    modal_rule = str(rules.value_counts().index[0]) if not rules.empty else None
    same_rule_folds = int(rules.eq(modal_rule).sum()) if modal_rule is not None else 0
    matched = predictions.loc[predictions["matched"].astype(bool)].copy()
    annual = matched.groupby(matched["held_out_year"].astype(int))["alternative_log_value"].mean()
    supported_years = int(len(annual))
    positive_years = int(annual.gt(0.0).sum())
    sign_pvalue = _one_sided_sign_pvalue(positive_years, supported_years)
    pooled_mean = (
        float(matched["alternative_log_value"].astype(float).mean()) if not matched.empty else 0.0
    )
    positive_values = matched.loc[
        matched["alternative_log_value"].astype(float).gt(0.0), "alternative_log_value"
    ].astype(float)
    positive_sum = float(positive_values.sum())
    concentration = (
        float(positive_values.max() / positive_sum)
        if positive_sum > 0.0 and not positive_values.empty
        else 1.0
    )
    rules_cfg = protocol["classification"]
    gates = {
        "same_rule_stable": same_rule_folds >= int(rules_cfg["minimum_same_rule_folds"]),
        "all_years_supported": supported_years
        >= int(rules_cfg["minimum_oof_supported_years"]),
        "minimum_oof_support": len(matched) >= int(rules_cfg["minimum_oof_events"]),
        "all_oof_years_positive": positive_years
        >= int(rules_cfg["minimum_positive_oof_years"]),
        "annual_sign_test": sign_pvalue <= float(rules_cfg["annual_sign_test_alpha"]),
        "positive_pooled_oof_mean": pooled_mean > 0.0,
        "not_single_event_dominated": concentration
        <= float(rules_cfg["maximum_single_positive_gain_share"]),
    }
    return {
        "classification": (
            "stable_boundary_found" if all(gates.values()) else "no_stable_boundary"
        ),
        "modal_rule_id": modal_rule,
        "same_rule_folds": same_rule_folds,
        "oof_matched_events": int(len(matched)),
        "oof_supported_years": supported_years,
        "positive_oof_years": positive_years,
        "annual_mean_action_values": {
            str(int(year)): float(value) for year, value in annual.items()
        },
        "annual_sign_test_pvalue": sign_pvalue,
        "pooled_oof_mean_action_value": pooled_mean,
        "maximum_single_positive_gain_share": concentration,
        "gates": gates,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _validate_file_identity(root: Path, identity: Mapping[str, object], *, label: str) -> str:
    path = root / str(identity["path"])
    if not path.is_file():
        raise FileNotFoundError(f"missing {label} source: {path}")
    digest = sha256(path.read_bytes()).hexdigest()
    if digest != str(identity["sha256"]):
        raise ValueError(f"{label} source hash differs from preregistration")
    return digest


def _source_identities(source: Mapping[str, object]) -> dict[str, dict[str, str]]:
    paths = {
        key.removesuffix("_path"): str(value)
        for key, value in source.items()
        if str(key).endswith("_path")
    }
    hashes = {
        key.removesuffix("_sha256"): str(value)
        for key, value in source.items()
        if str(key).endswith("_sha256")
    }
    if not paths or set(paths) != set(hashes):
        raise ValueError("source evidence path/hash declarations are incomplete")
    return {
        name: {"path": paths[name], "sha256": hashes[name]}
        for name in sorted(paths)
    }


def run_decision_boundary_diagnosis(
    repository_root: Path,
    experiment_dir: Path,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Run the preregistered diagnosis using only tracked pre-2026 evidence."""
    validate_protocol(protocol)
    root = Path(repository_root).resolve()
    experiment_dir = Path(experiment_dir).resolve()
    artifacts_dir = experiment_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    research = protocol.get("research_object")
    source = protocol.get("source_evidence")
    if not isinstance(research, Mapping) or not isinstance(source, Mapping):
        raise ValueError("research or source identity is missing")
    identities = _source_identities(source)
    validated = {
        "research_object": {
            "path": str(research["path"]),
            "sha256": _validate_file_identity(root, research, label="research object"),
        }
    }
    for name, identity in identities.items():
        validated[name] = {
            "path": identity["path"],
            "sha256": _validate_file_identity(root, identity, label=name),
        }
    if any("2026" in value["path"] for value in validated.values()):
        raise ValueError("2026 source path is forbidden")

    ledger_identity = identities["daily_path_ledger"]
    events_identity = identities["decision_events"]
    ledger = pd.read_csv(root / ledger_identity["path"])
    decision_events = pd.read_csv(root / events_identity["path"])
    frontier, excluded = build_frontier_events(ledger, decision_events, protocol)
    rules = enumerate_boundary_rules(protocol)
    folds, predictions = run_leave_one_year_out(frontier, rules, protocol)
    classification = classify_boundary(folds, predictions, protocol)

    sample = protocol["sample"]
    eligible_count = int(frontier["model_eligible"].astype(bool).sum())
    expected = {
        "frontier": int(sample["expected_frontier_event_count"]),
        "eligible": int(sample["expected_eligible_event_count"]),
        "excluded": int(sample["expected_unmatched_interval_count"]),
    }
    actual = {
        "frontier": int(len(frontier)),
        "eligible": eligible_count,
        "excluded": int(len(excluded)),
    }
    if actual != expected:
        raise ValueError(f"decision frontier counts differ from preregistration: {actual}")
    if len(rules) != 49:
        raise ValueError("boundary rule count differs from preregistration")

    identity_audit = {
        "status": "PASS",
        "holdout_accessed": False,
        "visible_sample_end": str(protocol["visible_sample_end"]),
        "validated_sources": validated,
    }
    _write_json(artifacts_dir / "identity_audit.json", identity_audit)
    frontier.to_csv(artifacts_dir / "frontier_events.csv", index=False, encoding="utf-8-sig")
    excluded.to_csv(
        artifacts_dir / "excluded_intervals.csv", index=False, encoding="utf-8-sig"
    )
    rules.to_csv(artifacts_dir / "boundary_rules.csv", index=False, encoding="utf-8-sig")
    folds.to_csv(artifacts_dir / "fold_selection.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(
        artifacts_dir / "out_of_fold_predictions.csv", index=False, encoding="utf-8-sig"
    )
    _write_json(artifacts_dir / "boundary_classification.json", classification)
    summary = {
        "status": "COMPLETE",
        "holdout_accessed": False,
        "frozen_challenger": None,
        "frontier_event_count": int(len(frontier)),
        "eligible_event_count": eligible_count,
        "excluded_interval_count": int(len(excluded)),
        "rule_count": int(len(rules)),
        "fold_count": int(len(folds)),
        "prediction_count": int(len(predictions)),
        "classification": classification,
    }
    _write_json(artifacts_dir / "metrics.json", summary)
    for forbidden in (
        "frozen_challenger.json",
        "orders.csv",
        "candidate_results.csv",
        "holdout_metrics.json",
    ):
        if (artifacts_dir / forbidden).exists():
            raise ValueError(f"decision boundary diagnosis wrote forbidden {forbidden}")
    return summary
