"""Deterministic state-expanded CZSC factors for EX05 research."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import czsc
import numpy as np
import pandas as pd

from .factors import (
    _run_signals,
    generate_factor_frame,
    signal_primary,
)
from .four_layer import normalized_signal_factors


EVENT_PRIMARY_TOKENS = ("买", "卖", "底背驰", "顶背驰")


def is_event_primary(primary: str) -> bool:
    """Return whether a CZSC primary value represents a discrete structure event."""
    return primary not in {"其他", "任意", "中性"} and any(
        token in primary for token in EVENT_PRIMARY_TOKENS
    )


def independent_event_count(indicator: pd.Series) -> int:
    """Count inactive-to-active transitions, collapsing consecutive event days."""
    active = indicator.fillna(0.0).astype(bool)
    return int((active & ~active.shift(fill_value=False)).sum())


def _raw_name_contains_signal(raw_name: str, signal_name: str) -> bool:
    marker = f"__{signal_name}"
    start = raw_name.find(marker)
    if start < 0:
        return False
    end = start + len(marker)
    return end == len(raw_name) or raw_name.startswith("__", end)


def build_signal_support(
    available_names: set[str],
    requirements: Sequence[str],
    raw_names: Sequence[str],
    state_records: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """Classify required CZSC event signals without hiding support gaps."""
    support: dict[str, dict[str, object]] = {}
    for signal_name in requirements:
        matching_raw = [
            str(raw_name)
            for raw_name in raw_names
            if _raw_name_contains_signal(str(raw_name), str(signal_name))
        ]
        matching_states = [
            row
            for row in state_records
            if any(str(row.get("raw_signal")) == raw_name for raw_name in matching_raw)
            and row.get("factor_kind") == "event"
        ]
        available = signal_name in available_names
        generated = bool(matching_raw)
        canonical_observed = any(row.get("status") == "candidate" for row in matching_states)
        aliased = bool(matching_states) and not canonical_observed
        if not available:
            status = "unavailable_in_czsc_1_0_1"
        elif aliased:
            status = "aliased_duplicate"
        elif canonical_observed:
            status = "observed"
        elif generated:
            status = "generated"
        else:
            status = "available"
        support[str(signal_name)] = {
            "status": status,
            "available": available,
            "generated": generated,
            "observed": bool(matching_states),
            "raw_signals": matching_raw,
        }
    return support


@dataclass(frozen=True)
class CandidateFactors:
    factors: pd.DataFrame
    origin_weights: pd.Series
    metadata: dict[str, Any]


def predeclared_interactions(mapped: pd.DataFrame) -> pd.DataFrame:
    """Build the four exact multi-signal consensus factors declared by EX05."""
    bi_columns = [
        "raw__30m__cxt_bi_status_V230101",
        "raw__daily__cxt_bi_status_V230101",
        "raw__weekly__cxt_bi_status_V230101",
    ]
    trend_columns = [
        "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_5",
        "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_10",
        "raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_20",
        "raw__daily__tas_macd_base_V221028__di_1__fastperiod_12__signalperiod_9__slowperiod_26",
    ]
    required = bi_columns + trend_columns
    if not set(required) <= set(mapped.columns):
        return pd.DataFrame(index=mapped.index)
    return pd.DataFrame(
        {
            "interaction__bi_all_bull": mapped[bi_columns].eq(1.0).all(axis=1).astype(float),
            "interaction__bi_all_bear": mapped[bi_columns].eq(-1.0).all(axis=1).astype(float),
            "interaction__trend_all_bull": mapped[trend_columns].eq(1.0).all(axis=1).astype(float),
            "interaction__trend_all_bear": mapped[trend_columns].eq(-1.0).all(axis=1).astype(float),
        },
        index=mapped.index,
    )


def _state_name(raw_name: str, primary: str) -> str:
    return f"state__{raw_name}::{primary}"


def _replay_frozen_factor(
    name: str,
    raw: pd.DataFrame,
    base: pd.DataFrame,
    interactions: pd.DataFrame,
) -> pd.Series:
    if name in base:
        return base[name].astype(float)
    if name in interactions:
        return interactions[name].astype(float)
    if name.startswith("state__") and "::" in name:
        raw_name, primary = name[len("state__") :].rsplit("::", 1)
        if raw_name not in raw:
            return pd.Series(0.0, index=base.index, name=name)
        values = raw[raw_name].map(signal_primary)
        return values.eq(primary).astype(float).rename(name)
    return pd.Series(0.0, index=base.index, name=name)


def build_candidate_factors(
    raw: pd.DataFrame,
    base: pd.DataFrame,
    base_weights: pd.Series,
    protocol: Mapping[str, object],
    *,
    frozen_names: Sequence[str] | None = None,
) -> CandidateFactors:
    """Expand categorical states, filter deterministic noise, and retain exact replay."""
    raw = raw.reindex(base.index)
    interactions = predeclared_interactions(base)
    if frozen_names is not None:
        factors = pd.concat(
            [_replay_frozen_factor(name, raw, base, interactions) for name in frozen_names],
            axis=1,
        )
        factors.columns = list(frozen_names)
        origin = base_weights.reindex(factors.columns).fillna(0.0).astype(float)
        return CandidateFactors(
            factors.astype(float),
            origin,
            {
                "mode": "frozen_replay",
                "factor_count": len(factors.columns),
                "state_factor_count": sum(name.startswith("state__") for name in factors.columns),
                "interaction_count": sum(name.startswith("interaction__") for name in factors.columns),
            },
        )

    minimum_coverage = float(protocol["state_min_coverage"])
    minimum_days = int(protocol["state_min_active_days"])
    maximum_ratio = float(protocol["state_max_active_ratio"])
    state_columns: list[pd.Series] = []
    canonical_by_fingerprint: dict[bytes, str] = {}
    dropped_duplicates = 0
    state_records: list[dict[str, object]] = []
    for raw_name in sorted(map(str, raw.columns)):
        coverage = float(raw[raw_name].notna().mean())
        primary_values = raw[raw_name].map(signal_primary)
        counts = primary_values.value_counts()
        for primary in sorted(map(str, counts.index)):
            active_days = int(counts.loc[primary])
            active_ratio = active_days / len(primary_values)
            name = _state_name(raw_name, primary)
            indicator = primary_values.eq(primary).astype(float).rename(name)
            factor_kind = "event" if is_event_primary(primary) else "state"
            event_count = independent_event_count(indicator) if factor_kind == "event" else 0
            if factor_kind == "event":
                if event_count < int(protocol.get("event_min_independent_occurrences", 1)):
                    continue
            elif (
                coverage < minimum_coverage
                or active_days < minimum_days
                or active_ratio > maximum_ratio
            ):
                continue
            fingerprint = indicator.to_numpy(dtype=np.uint8).tobytes()
            canonical = canonical_by_fingerprint.get(fingerprint)
            if canonical is not None:
                dropped_duplicates += 1
                if factor_kind == "event":
                    active_index = pd.DatetimeIndex(indicator.index[indicator.astype(bool)])
                    state_records.append(
                        {
                            "factor": name,
                            "raw_signal": raw_name,
                            "primary": primary,
                            "factor_kind": factor_kind,
                            "coverage": coverage,
                            "active_days": active_days,
                            "active_ratio": active_ratio,
                            "independent_events": event_count,
                            "years": sorted({int(value) for value in active_index.year}),
                            "half_year_windows": sorted(
                                {
                                    f"{dt.year}H{1 if dt.month <= 6 else 2}"
                                    for dt in active_index
                                }
                            ),
                            "status": "aliased_duplicate",
                            "canonical_factor": canonical,
                        }
                    )
                continue
            canonical_by_fingerprint[fingerprint] = name
            state_columns.append(indicator)
            record: dict[str, object] = {
                "factor": name,
                "raw_signal": raw_name,
                "primary": primary,
                "factor_kind": factor_kind,
                "coverage": coverage,
                "active_days": active_days,
                "active_ratio": active_ratio,
                "status": "candidate",
                "canonical_factor": name,
            }
            if factor_kind == "event":
                active_index = pd.DatetimeIndex(indicator.index[indicator.astype(bool)])
                record.update(
                    {
                        "independent_events": event_count,
                        "years": sorted({int(value) for value in active_index.year}),
                        "half_year_windows": sorted(
                            {
                                f"{dt.year}H{1 if dt.month <= 6 else 2}"
                                for dt in active_index
                            }
                        ),
                    }
                )
            state_records.append(record)
    additions = [*state_columns, *[interactions[column] for column in interactions.columns]]
    factors = pd.concat([base.astype(float), *additions], axis=1)
    origin = base_weights.reindex(factors.columns).fillna(0.0).astype(float)
    return CandidateFactors(
        factors,
        origin,
        {
            "mode": "discovery",
            "base_factor_count": len(base.columns),
            "raw_signal_count": len(raw.columns),
            "state_factor_count": len(state_columns),
            "interaction_count": len(interactions.columns),
            "factor_count": len(factors.columns),
            "deduplicated_state_count": dropped_duplicates,
            "states": state_records,
        },
    )


def generate_candidate_factors(
    data: Any,
    protocol: Mapping[str, object],
    base_weights: pd.Series,
    *,
    frozen_names: Sequence[str] | None = None,
) -> CandidateFactors:
    """Generate the preregistered EX05 universe from validated market data."""
    champion_frame = generate_factor_frame(data).frame
    existing_raw = champion_frame.filter(like="raw__")
    base = normalized_signal_factors(existing_raw)
    configs = [
        {"name": str(name), "freq": "日线", "di": 1}
        for name in protocol["new_daily_signals"]
    ]
    new_raw = _run_signals(data.daily, "日线", configs, 30, "daily")
    new_raw.index = new_raw.index.normalize()
    new_raw = new_raw.reindex(base.index)
    raw = pd.concat([existing_raw.reindex(base.index), new_raw], axis=1)
    candidate = build_candidate_factors(
        raw,
        base,
        base_weights,
        protocol,
        frozen_names=frozen_names,
    )
    requirements = [str(name) for name in protocol.get("event_signal_requirements", [])]
    if not requirements:
        return candidate
    metadata = dict(candidate.metadata)
    metadata["signal_support"] = build_signal_support(
        set(map(str, czsc._native.list_signal_names())),
        requirements,
        list(map(str, raw.columns)),
        list(metadata.get("states", [])),
    )
    return CandidateFactors(candidate.factors, candidate.origin_weights, metadata)


def validate_sparse_weights(
    weights: pd.Series,
    factor_names: Sequence[str],
    protocol: Mapping[str, object],
) -> None:
    """Validate sparse normalization, capacity, and protected information."""
    if list(weights.index) != list(factor_names):
        raise ValueError("weight identities or order differ from candidate factors")
    values = weights.astype(float)
    if not np.isfinite(values).all():
        raise ValueError("weights must be finite")
    if abs(float(values.abs().sum()) - 1.0) > 1e-12:
        raise ValueError("weights must have L1 norm one")
    active = values[values.ne(0.0)]
    if len(active) > int(protocol["maximum_active_factors"]):
        raise ValueError("active factor cap exceeded")
    trend_names = [
        name
        for name in values.index
        if name.startswith("raw__") and ("tas_ma_" in name or "tas_macd_" in name)
    ]
    trend_weight = float(values.reindex(trend_names).fillna(0.0).abs().sum())
    if trend_weight + 1e-15 < float(protocol["minimum_trend_weight"]):
        raise ValueError("protected trend weight is too low")
    if bool(protocol["protect_volume_window"]):
        volume_names = [
            name
            for name in values.index
            if name.startswith("raw__daily__vol_window_V230731")
        ]
        if not volume_names or values.reindex(volume_names).fillna(0.0).abs().sum() == 0.0:
            raise ValueError("protected volume window factor is missing")
    minimum = float(protocol["minimum_absolute_weight"])
    if active.abs().lt(minimum - 1e-15).any():
        raise ValueError("active factor weight is below minimum")


def sparse_coordinate_optimize(
    origin: pd.Series,
    step: float,
    rounds: int,
    protocol: Mapping[str, object],
    evaluator: Callable[[pd.Series], tuple[float, ...]],
) -> pd.Series:
    """Greedily activate, remove, or change signed weights deterministically."""
    current = origin.astype(float).copy()
    current = current / float(current.abs().sum())
    validate_sparse_weights(current, current.index, protocol)
    current_objective = evaluator(current)
    minimum = float(protocol["minimum_absolute_weight"])
    for _ in range(int(rounds)):
        for name in current.index:
            best_weights = current
            best_objective = current_objective
            for delta in (-float(step), float(step)):
                candidate = current.copy()
                candidate.loc[name] += delta
                if abs(float(candidate.loc[name])) < minimum:
                    candidate.loc[name] = 0.0
                norm = float(candidate.abs().sum())
                if norm == 0.0:
                    continue
                candidate /= norm
                try:
                    validate_sparse_weights(candidate, candidate.index, protocol)
                except ValueError:
                    continue
                objective = evaluator(candidate)
                if objective > best_objective:
                    best_weights = candidate
                    best_objective = objective
            current, current_objective = best_weights, best_objective
    return current.rename("weight")


def rank_factor_results(rows: pd.DataFrame) -> pd.DataFrame:
    """Rank EX05 algorithm configurations using return-only evidence."""
    return rows.sort_values(
        [
            "win_count",
            "min_return_delta",
            "median_return_delta",
            "mean_return_delta",
            "mean_active_factors",
            "weight_shift",
            "spec_id",
        ],
        ascending=[False, False, False, False, True, True, True],
        kind="stable",
    ).reset_index(drop=True)
