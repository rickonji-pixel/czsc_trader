"""Deterministic CZSC information inventory for the terminal research route."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Mapping

import numpy as np
import pandas as pd

from .czsc_factor_stability import event_onsets, factor_redundancy_audit


FREQUENCIES = ("30分钟", "日线", "周线")
ROUTE_FAMILIES = (
    "atomic_structure",
    "sparse_event",
    "state_dynamic",
    "cross_frequency",
    "champion_error_condition",
    "eligible_pairwise_interaction",
)
FROZEN_INVENTORY_SHA256 = "0baf02a789774a33ba376eabe33030327bf3f6584382db2d71b9c75c4753fd32"
EVENT_NAME_TOKENS = (
    "_first_buy_",
    "_first_sell_",
    "_second_bs_",
    "_third_bs_",
    "_third_buy_",
    "_bs_V",
)
EVENT_VALUE_TOKENS = ("买", "卖", "底背驰", "顶背驰", "停顿", "突破")
NEUTRAL_VALUES = {"其他", "任意", "中性", "无", ""}


@dataclass(frozen=True)
class ParsedSignalValue:
    primary: str | None
    secondary: str | None
    tertiary: str | None
    original: str | None


@dataclass(frozen=True)
class SignalClassification:
    status: str
    family: str | None
    reason: str | None


@dataclass(frozen=True)
class AdmissionResult:
    admitted: pd.DataFrame
    rejected: pd.DataFrame
    yearly_metrics: pd.DataFrame
    causal_audit: dict[str, object]
    influence_audit: pd.DataFrame
    redundancy_audit: pd.DataFrame
    conditional_audit: pd.DataFrame


def validate_route_family_protocol(protocol: dict[str, object]) -> None:
    """Reject a family protocol that can drift into future or mutable scope."""
    expected = {
        "handler": "czsc_route_family_diagnostic",
        "symbol": "588080.SH",
        "visible_end": "2025-12-31",
        "access_2026": False,
        "inventory_sha256": FROZEN_INVENTORY_SHA256,
        "frequencies": list(FREQUENCIES),
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"route family protocol {key} differs from terminal program")
    if protocol.get("family") not in ROUTE_FAMILIES:
        raise ValueError("route family protocol has an unknown family")
    champion = protocol.get("champion")
    if not isinstance(champion, dict):
        raise ValueError("route family champion identity is missing")
    if champion.get("version") != "baseline_20260826" or champion.get("sha256") != "fc22ca5a973f77faf528cdb3efba4900163e18fe0c08cf79234d79ef22f5c822":
        raise ValueError("route family champion identity differs from terminal program")
    names = protocol.get("signal_names")
    if not isinstance(names, list) or not names or len(set(map(str, names))) != len(names):
        raise ValueError("route family signal_names must be non-empty and unique")


def parse_signal_value(value: object) -> ParsedSignalValue:
    """Preserve the first three semantic fields of a CZSC signal value."""
    if value is None:
        return ParsedSignalValue(None, None, None, None)
    original = str(value)
    parts = original.split("_")
    fields: list[str | None] = [part if part else None for part in parts[:3]]
    fields.extend([None] * (3 - len(fields)))
    return ParsedSignalValue(fields[0], fields[1], fields[2], original)


def classify_signal_family(name: str) -> SignalClassification:
    """Classify one registered native signal without hiding exclusions."""
    if name.startswith(("cxt_", "byi_")):
        family = (
            "sparse_event"
            if any(token in name for token in EVENT_NAME_TOKENS)
            else "atomic_structure"
        )
        return SignalClassification("candidate", family, None)
    if name.startswith(("zdy_bi_end_", "zdy_zs_")):
        return SignalClassification("candidate", "atomic_structure", None)
    if name.startswith("zdy_"):
        return SignalClassification(
            "excluded", None, "hybrid_ta_not_pure_czsc_structure"
        )
    return SignalClassification("excluded", None, "non_czsc_structure_signal")


def inventory_records(names: Iterable[str]) -> list[dict[str, object]]:
    """Return a stable, complete accounting of registered CZSC signals."""
    records: list[dict[str, object]] = []
    for name in sorted(set(map(str, names))):
        classification = classify_signal_family(name)
        row = {
            "name": name,
            **asdict(classification),
            "frequencies": list(FREQUENCIES),
            "default_parameters": {"di": 1},
        }
        records.append(row)
    return records


def probe_signal_frequencies(
    records: Iterable[dict[str, object]],
    execute: Callable[[str, str], object],
) -> list[dict[str, object]]:
    """Probe every planned frequency while preserving every failure reason."""
    probes: list[dict[str, object]] = []
    for record in sorted(records, key=lambda row: str(row["name"])):
        if record.get("status") != "candidate":
            continue
        for frequency in FREQUENCIES:
            row: dict[str, object] = {
                "name": str(record["name"]),
                "family": str(record["family"]),
                "frequency": frequency,
            }
            try:
                execute(str(record["name"]), frequency)
            except Exception as exc:  # the audit must retain native failure identity
                row.update(
                    {
                        "status": "unavailable",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                row.update({"status": "generated", "error": None})
            probes.append(row)
    return probes


def _factor_name(raw_name: str, representation: str, value: str) -> str:
    return f"state__{raw_name}__{representation}::{value}"


def build_family_factors(
    raw: pd.DataFrame,
    family: str,
    protocol: dict[str, object],
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """Expand exact CZSC value fields into deterministic binary factors."""
    if family not in {"atomic_structure", "sparse_event"}:
        raise ValueError(f"unsupported direct factor family: {family}")
    pieces: list[pd.Series] = []
    records: list[dict[str, object]] = []
    fingerprints: dict[bytes, str] = {}
    for raw_name in sorted(map(str, raw.columns)):
        parsed = raw[raw_name].map(parse_signal_value)
        values = {
            "v1": parsed.map(lambda item: item.primary),
            "v2": parsed.map(lambda item: item.secondary),
            "v3": parsed.map(lambda item: item.tertiary),
            "joint": parsed.map(
                lambda item: (
                    "|".join([item.primary, item.secondary, item.tertiary])
                    if item.primary is not None
                    and item.secondary is not None
                    and item.tertiary is not None
                    else None
                )
            ),
        }
        for representation, categories in values.items():
            observed = sorted({str(value) for value in categories.dropna().unique()})
            for category in observed:
                semantic_values = category.split("|") if representation == "joint" else [category]
                is_event = any(
                    token in value
                    for value in semantic_values
                    for token in EVENT_VALUE_TOKENS
                )
                if family == "sparse_event" and not is_event:
                    continue
                if family == "atomic_structure" and is_event:
                    continue
                if all(value in NEUTRAL_VALUES for value in semantic_values):
                    continue
                name = _factor_name(raw_name, representation, category)
                indicator = categories.eq(category).astype(float).rename(name)
                fingerprint = indicator.to_numpy(dtype=np.uint8).tobytes()
                canonical = fingerprints.get(fingerprint)
                if canonical is not None:
                    records.append(
                        {
                            "factor": name,
                            "raw_signal": raw_name,
                            "representation": representation,
                            "value": category,
                            "factor_kind": "event" if family == "sparse_event" else "state",
                            "status": "aliased_duplicate",
                            "canonical_factor": canonical,
                        }
                    )
                    continue
                fingerprints[fingerprint] = name
                pieces.append(indicator)
                records.append(
                    {
                        "factor": name,
                        "raw_signal": raw_name,
                        "representation": representation,
                        "value": category,
                        "factor_kind": "event" if family == "sparse_event" else "state",
                        "status": "candidate",
                        "canonical_factor": name,
                    }
                )
    frame = pd.concat(pieces, axis=1) if pieces else pd.DataFrame(index=raw.index)
    return frame, records


def replay_family_factors(
    raw: pd.DataFrame,
    records: Iterable[dict[str, object]],
) -> pd.DataFrame:
    """Recreate frozen factor identities without rediscovering aliases."""
    parsed_by_raw: dict[str, pd.Series] = {}
    pieces: list[pd.Series] = []
    for record in records:
        name = str(record["factor"])
        raw_name = str(record["raw_signal"])
        representation = str(record["representation"])
        expected = str(record["value"])
        if raw_name not in raw:
            pieces.append(pd.Series(0.0, index=raw.index, name=name))
            continue
        parsed = parsed_by_raw.setdefault(raw_name, raw[raw_name].map(parse_signal_value))
        if representation == "v1":
            values = parsed.map(lambda item: item.primary)
        elif representation == "v2":
            values = parsed.map(lambda item: item.secondary)
        elif representation == "v3":
            values = parsed.map(lambda item: item.tertiary)
        elif representation == "joint":
            values = parsed.map(
                lambda item: (
                    "|".join([item.primary, item.secondary, item.tertiary])
                    if item.primary is not None
                    and item.secondary is not None
                    and item.tertiary is not None
                    else None
                )
            )
        else:
            raise ValueError(f"unknown factor representation: {representation}")
        pieces.append(values.eq(expected).astype(float).rename(name))
    return pd.concat(pieces, axis=1) if pieces else pd.DataFrame(index=raw.index)


def _append_canonical_factor(
    pieces: list[pd.Series],
    records: list[dict[str, object]],
    fingerprints: dict[bytes, str],
    indicator: pd.Series,
    metadata: dict[str, object],
) -> None:
    values = indicator.fillna(0.0).astype(float)
    fingerprint = values.to_numpy(dtype=np.uint8).tobytes()
    canonical = fingerprints.get(fingerprint)
    record = {"factor": str(values.name), **metadata}
    if canonical is not None:
        records.append(
            {**record, "status": "aliased_duplicate", "canonical_factor": canonical}
        )
        return
    fingerprints[fingerprint] = str(values.name)
    pieces.append(values)
    records.append(
        {**record, "status": "candidate", "canonical_factor": str(values.name)}
    )


def build_dynamic_factors(
    base: pd.DataFrame,
    factor_kinds: Mapping[str, str],
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """Create frozen transition, age, and post-event representations."""
    pieces: list[pd.Series] = []
    records: list[dict[str, object]] = []
    fingerprints: dict[bytes, str] = {}
    for parent in sorted(map(str, base.columns)):
        active = base[parent].fillna(0.0).astype(bool)
        kind = str(factor_kinds[parent])
        definitions: list[tuple[str, pd.Series, str]] = []
        if kind == "state":
            onset = active & ~active.shift(fill_value=False)
            exit_ = ~active & active.shift(fill_value=False)
            groups = (~active).cumsum()
            age = active.astype(int).groupby(groups).cumsum().where(active, 0)
            definitions.extend(
                [
                    ("onset", onset, "event"),
                    ("exit", exit_, "event"),
                    ("age_1_3", age.between(1, 3), "state"),
                    ("age_4_8", age.between(4, 8), "state"),
                    ("age_9_plus", age.ge(9), "state"),
                ]
            )
        else:
            onset = event_onsets(active)
            positions = np.flatnonzero(onset.to_numpy(dtype=bool))
            last = -10_000
            since: list[int] = []
            position_set = set(map(int, positions))
            for offset in range(len(onset)):
                if offset in position_set:
                    last = offset
                    since.append(0)
                else:
                    since.append(offset - last)
            since_series = pd.Series(since, index=base.index)
            prior = pd.Series(False, index=base.index)
            for position in positions:
                if any(0 < int(position) - int(previous) <= 20 for previous in positions if previous < position):
                    prior.iloc[int(position)] = True
            definitions.extend(
                [
                    ("post_1_5", since_series.between(1, 5), "state"),
                    ("post_6_20", since_series.between(6, 20), "state"),
                    ("repeat_within_20", prior, "event"),
                ]
            )
        for definition, indicator, derived_kind in definitions:
            name = f"dynamic__{parent}__{definition}"
            _append_canonical_factor(
                pieces,
                records,
                fingerprints,
                indicator.astype(float).rename(name),
                {
                    "parent_factor": parent,
                    "definition": definition,
                    "factor_kind": derived_kind,
                },
            )
    frame = pd.concat(pieces, axis=1) if pieces else pd.DataFrame(index=base.index)
    return frame, records


def build_cross_frequency_factors(
    raw: pd.DataFrame,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """Construct exact same-state alignment and divergence across completed frequencies."""
    grouped: dict[str, dict[str, str]] = {}
    for column in map(str, raw.columns):
        parts = column.split("__")
        if len(parts) < 3 or parts[0] != "raw":
            continue
        grouped.setdefault("__".join(parts[2:]), {})[parts[1]] = column
    pieces: list[pd.Series] = []
    records: list[dict[str, object]] = []
    fingerprints: dict[bytes, str] = {}
    for signal, columns in sorted(grouped.items()):
        if set(columns) != {"30m", "daily", "weekly"}:
            continue
        values = {
            frequency: raw[column].map(parse_signal_value).map(lambda item: item.primary)
            for frequency, column in columns.items()
        }
        common = sorted(
            set(values["30m"].dropna())
            & set(values["daily"].dropna())
            & set(values["weekly"].dropna())
            - NEUTRAL_VALUES
        )
        for category in common:
            definitions = {
                f"30m_daily::{category}": values["30m"].eq(category) & values["daily"].eq(category),
                f"daily_weekly::{category}": values["daily"].eq(category) & values["weekly"].eq(category),
                f"all::{category}": values["30m"].eq(category) & values["daily"].eq(category) & values["weekly"].eq(category),
            }
            for definition, indicator in definitions.items():
                name = f"cross__{signal}__{definition}"
                _append_canonical_factor(
                    pieces,
                    records,
                    fingerprints,
                    indicator.astype(float).rename(name),
                    {
                        "source_signal": signal,
                        "definition": definition,
                        "factor_kind": "state",
                    },
                )
        for left, right, label in (
            ("30m", "daily", "30m_daily_divergence"),
            ("daily", "weekly", "daily_weekly_divergence"),
        ):
            valid = values[left].notna() & values[right].notna()
            indicator = valid & values[left].ne(values[right])
            name = f"cross__{signal}__{label}"
            _append_canonical_factor(
                pieces,
                records,
                fingerprints,
                indicator.astype(float).rename(name),
                {
                    "source_signal": signal,
                    "definition": label,
                    "factor_kind": "state",
                },
            )
    frame = pd.concat(pieces, axis=1) if pieces else pd.DataFrame(index=raw.index)
    return frame, records


def _support_reason(
    indicator: pd.Series,
    kind: str,
    protocol: dict[str, object],
) -> str | None:
    active = event_onsets(indicator) if kind == "event" else indicator.fillna(0.0).astype(bool)
    years = pd.DatetimeIndex(active.index).year
    if kind == "state":
        if float(active.mean()) > float(protocol["state_max_active_ratio"]):
            return "state_activation_ratio"
        for year in range(2021, 2026):
            values = active[years == year]
            if int(values.sum()) < int(protocol["state_min_active_days_per_year"]):
                return "state_active_support"
            if int((~values).sum()) < int(protocol["state_min_control_days_per_year"]):
                return "state_control_support"
        return None
    counts = pd.Series(active.to_numpy(dtype=int), index=years).groupby(level=0).sum()
    supported = counts[counts >= int(protocol["event_min_occurrences_per_supported_year"])]
    if int(active.sum()) < int(protocol["event_min_independent_occurrences"]):
        return "event_total_support"
    if len(supported) < int(protocol["event_min_years"]):
        return "event_year_support"
    return None


def _conditional_yearly_effects(
    indicator: pd.Series,
    outcomes: pd.DataFrame,
    references: pd.DataFrame,
    endpoint: str,
    kind: str,
    min_abs_correlation: float,
) -> tuple[list[dict[str, object]], list[str]]:
    active = event_onsets(indicator) if kind == "event" else indicator.fillna(0.0).astype(bool)
    variable_references = references.loc[:, references.nunique(dropna=False).gt(1)]
    correlations = variable_references.astype(float).corrwith(indicator.astype(float)).abs().dropna()
    selected = [
        str(name)
        for name, value in correlations.items()
        if str(name) != "target_position" and float(value) >= min_abs_correlation
    ]
    if "target_position" in references:
        selected.insert(0, "target_position")
    strata = (
        references[selected].astype("string").agg("|".join, axis=1)
        if selected
        else pd.Series("all", index=references.index)
    )
    rows: list[dict[str, object]] = []
    for year in range(2021, 2026):
        valid = (
            (outcomes.index.year == year)
            & outcomes[endpoint].notna().to_numpy()
        )
        weighted: list[tuple[int, float]] = []
        for stratum in sorted(strata.loc[valid].dropna().unique()):
            group = valid & strata.eq(stratum).to_numpy()
            active_mask = group & active.to_numpy(dtype=bool)
            control_mask = group & ~active.to_numpy(dtype=bool)
            if not active_mask.any() or not control_mask.any():
                continue
            delta = float(
                outcomes.loc[active_mask, endpoint].mean()
                - outcomes.loc[control_mask, endpoint].mean()
            )
            weighted.append((int(active_mask.sum()), delta))
        effect = (
            float(np.average([item[1] for item in weighted], weights=[item[0] for item in weighted]))
            if weighted
            else float("nan")
        )
        rows.append(
            {
                "factor": str(indicator.name),
                "year": year,
                "endpoint": endpoint,
                "effect": effect,
                "matched_strata": len(weighted),
            }
        )
    return rows, selected


def _direction_passes(
    effects: pd.Series,
    kind: str,
    protocol: dict[str, object],
    *,
    supported_years: set[int],
) -> bool:
    raw = pd.to_numeric(effects, errors="coerce")
    if kind == "event":
        required = raw.reindex(sorted(supported_years))
        if required.isna().any():
            return False
        numeric = required
    else:
        numeric = raw.dropna()
    if numeric.empty or numeric.eq(0.0).any():
        return False
    signs = np.sign(numeric.to_numpy(dtype=float))
    positive = int((signs > 0).sum())
    negative = int((signs < 0).sum())
    if kind == "event":
        return len(numeric) >= int(protocol["event_min_years"]) and min(positive, negative) == 0
    return max(positive, negative) >= int(protocol["state_min_same_direction_years"])


def _state_influence_passes(effects: pd.Series, protocol: dict[str, object]) -> bool:
    numeric = pd.to_numeric(effects, errors="coerce").dropna()
    absolute = numeric.abs()
    if absolute.sum() == 0.0:
        return False
    if float(absolute.max() / absolute.sum()) > float(protocol["maximum_annual_absolute_effect_share"]):
        return False
    direction = np.sign(float(numeric.mean()))
    return all(np.sign(float(numeric.drop(index=index).mean())) == direction for index in numeric.index)


def _event_influence_passes(
    indicator: pd.Series,
    outcomes: pd.DataFrame,
    endpoint: str,
    protocol: dict[str, object],
) -> bool:
    active = event_onsets(indicator).reindex(outcomes.index).fillna(False).astype(bool)
    valid = outcomes[endpoint].notna()
    active_values = outcomes.loc[active & valid, endpoint]
    control_mean = float(outcomes.loc[~active & valid, endpoint].mean())
    original = float(active_values.mean() - control_mean)
    if original == 0.0 or len(active_values) <= 1:
        return False
    minimum_ratio = float(protocol["minimum_event_leave_one_out_effect_ratio"])
    for offset in range(len(active_values)):
        effect = float(np.delete(active_values.to_numpy(dtype=float), offset).mean() - control_mean)
        if np.sign(effect) != np.sign(original) or abs(effect) + 1e-15 < abs(original) * minimum_ratio:
            return False
    return True


def admit_factors(
    candidates: pd.DataFrame,
    references: pd.DataFrame,
    outcomes: pd.DataFrame,
    protocol: dict[str, object],
    *,
    factor_kinds: dict[str, str],
    causal_passed: set[str],
) -> AdmissionResult:
    """Apply the common terminal-route gates without using strategy performance."""
    if not candidates.index.equals(outcomes.index) or not references.index.equals(outcomes.index):
        raise ValueError("candidate, reference, and outcome indices must match")
    redundancy_references = references.loc[:, references.nunique(dropna=False).gt(1)]
    redundancy_rows = factor_redundancy_audit(candidates, redundancy_references)
    redundancy = pd.DataFrame(redundancy_rows)
    redundancy_by_factor = redundancy.set_index("factor")
    admitted_rows: list[dict[str, object]] = []
    rejected_rows: list[dict[str, object]] = []
    yearly_rows: list[dict[str, object]] = []
    conditional_rows: list[dict[str, object]] = []
    influence_rows: list[dict[str, object]] = []
    endpoints = [
        f"return_{int(protocol['primary_horizon'])}",
        f"max_drawdown_{int(protocol['primary_horizon'])}",
    ]
    for name in map(str, candidates.columns):
        indicator = candidates[name].astype(float).rename(name)
        kind = str(factor_kinds[name])
        if name not in causal_passed:
            rejected_rows.append({"factor": name, "reason": "causal_replay"})
            continue
        if bool(redundancy_by_factor.loc[name, "exact_match"]):
            rejected_rows.append({"factor": name, "reason": "exact_reference_duplicate"})
            continue
        support_reason = _support_reason(indicator, kind, protocol)
        if support_reason is not None:
            rejected_rows.append({"factor": name, "reason": support_reason})
            continue
        if kind == "event":
            event_active = event_onsets(indicator)
            event_year_counts = pd.Series(
                event_active.to_numpy(dtype=int),
                index=pd.DatetimeIndex(event_active.index).year,
            ).groupby(level=0).sum()
            supported_years = {
                int(year)
                for year, count in event_year_counts.items()
                if int(count) >= int(protocol["event_min_occurrences_per_supported_year"])
            }
        else:
            supported_years = set(range(2021, 2026))
        endpoint_choices: list[dict[str, object]] = []
        endpoint_failure = "cross_year_direction"
        for endpoint in endpoints:
            rows, selected = _conditional_yearly_effects(
                indicator,
                outcomes,
                references,
                endpoint,
                kind,
                float(protocol["conditional_reference_min_abs_correlation"]),
            )
            for row in rows:
                row["references"] = selected
            conditional_rows.extend(rows)
            yearly_rows.extend(rows)
            effects = pd.Series({int(row["year"]): float(row["effect"]) for row in rows})
            if not _direction_passes(
                effects,
                kind,
                protocol,
                supported_years=supported_years,
            ):
                continue
            aggregate = float(effects.dropna().mean())
            if abs(aggregate) + 1e-15 < float(protocol["minimum_absolute_effect"]):
                endpoint_failure = "minimum_absolute_effect"
                continue
            influence_pass = (
                _event_influence_passes(indicator, outcomes, endpoint, protocol)
                if kind == "event"
                else _state_influence_passes(effects, protocol)
            )
            influence_rows.append(
                {
                    "factor": name,
                    "endpoint": endpoint,
                    "pass": bool(influence_pass),
                }
            )
            if not influence_pass:
                endpoint_failure = "event_influence" if kind == "event" else "state_influence"
                continue
            endpoint_choices.append(
                {
                    "factor": name,
                    "factor_kind": kind,
                    "endpoint": endpoint,
                    "direction": int(np.sign(aggregate)),
                    "conditional_effect": aggregate,
                    "closest_reference": str(redundancy_by_factor.loc[name, "closest_reference"]),
                    "max_abs_correlation": float(redundancy_by_factor.loc[name, "max_abs_correlation"]),
                }
            )
        if endpoint_choices:
            admitted_rows.append(max(endpoint_choices, key=lambda row: abs(float(row["conditional_effect"]))))
        else:
            rejected_rows.append({"factor": name, "reason": endpoint_failure})
    admitted_columns = [
        "factor", "factor_kind", "endpoint", "direction", "conditional_effect",
        "closest_reference", "max_abs_correlation",
    ]
    return AdmissionResult(
        admitted=pd.DataFrame(admitted_rows, columns=admitted_columns),
        rejected=pd.DataFrame(rejected_rows, columns=["factor", "reason"]),
        yearly_metrics=pd.DataFrame(yearly_rows),
        causal_audit={"status": "PASS", "factors_checked": len(causal_passed)},
        influence_audit=pd.DataFrame(influence_rows),
        redundancy_audit=redundancy,
        conditional_audit=pd.DataFrame(conditional_rows),
    )
