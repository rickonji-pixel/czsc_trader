"""CZSC registry census and causal event-study primitives.

The module deliberately stops at descriptive evidence.  It does not assign
weights, compose a strategy, or decide whether a signal is tradable.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

import czsc
import czsc._native as czsc_native
import numpy as np
import pandas as pd

from .data import MarketData
from .factors import _to_raw_bars


BASE_COLUMNS = {"symbol", "dt", "id", "freq", "open", "close", "high", "low", "vol", "amount"}
S001_FUNCTIONS = {
    "30m": {"cxt_bi_status_V230101", "cxt_third_buy_V230228"},
    "daily": {
        "cxt_bi_status_V230101",
        "cxt_five_bi_V230619",
        "cxt_seven_bi_V230620",
        "tas_ma_base_V221101",
        "tas_macd_base_V221028",
        "vol_window_V230731",
        "pressure_support_V240406",
    },
    "weekly": {"cxt_bi_status_V230101"},
}


@dataclass(frozen=True)
class SignalCensusResult:
    """Daily-aligned observations and their auditable registry catalog."""

    primary: pd.DataFrame
    full: pd.DataFrame
    catalog: pd.DataFrame
    failures: tuple[dict[str, str], ...]
    registry_summary: dict[str, object]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _short_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:16].upper()


def primary_value(value: object) -> str | None:
    """Return CZSC v1 while retaining no directional interpretation."""
    if value is None or pd.isna(value):
        return None
    return str(value).split("_", 1)[0]


def semantic_value(value: object) -> str | None:
    """Return the full v1/v2/v3 state and drop only the numeric score."""
    if value is None or pd.isna(value):
        return None
    text = str(value)
    head, separator, tail = text.rpartition("_")
    if separator and tail.lstrip("-").isdigit():
        return head
    return text


def _signal_columns(frame: pd.DataFrame) -> list[str]:
    return [str(column) for column in frame.columns if str(column) not in BASE_COLUMNS and len(str(column).split("_")) == 3]


def _output_index(output: pd.DataFrame) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(pd.to_datetime(output["dt"]), name="dt")
    if index.tz is not None:
        index = index.tz_localize(None)
    return index


def _derived_config(column: str, values: pd.Series, frequency: str) -> dict[str, object]:
    observed = values.dropna()
    if observed.empty:
        raise ValueError("signal returned no observable state")
    configs = czsc_native.derive_signals_config([f"{column}_{observed.iloc[0]}"])
    if len(configs) != 1:
        raise ValueError("CZSC could not derive one configuration from signal output")
    config = dict(configs[0])
    config["freq"] = frequency
    return config


def _template_pattern(name: str) -> re.Pattern[str]:
    """Compile a tolerant matcher for legacy keys that omit their version suffix."""
    template = czsc_native.get_signal_template(name)
    if not template:
        return re.compile(r"(?!)")
    template = re.sub(r"V\d{6,8}$", "", str(template))
    parts = re.split(r"(\{[^}]+\})", template)
    body = "".join(r".+?" if part.startswith("{") else re.escape(part) for part in parts)
    return re.compile(rf"^{body}(?:V\d{{6,8}})?$")


def _evaluate_batch(
    bars: list,
    configs: Sequence[dict[str, object]],
    *,
    sdt: str,
    init_n: int,
    frequency: str,
) -> tuple[dict[str, tuple[pd.Series, dict[str, object], str]], list[dict[str, str]]]:
    """Evaluate a batch, recursively isolating any incompatible registration."""
    if not configs:
        return {}, []
    try:
        output = czsc.generate_czsc_signals(
            bars, list(configs), sdt=sdt, init_n=init_n, df=True
        )
    except Exception as exc:  # native signal compatibility is part of the census evidence
        if len(configs) == 1:
            return {}, [{
                "name": str(configs[0]["name"]),
                "frequency": frequency,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }]
        midpoint = len(configs) // 2
        left, left_failures = _evaluate_batch(
            bars, configs[:midpoint], sdt=sdt, init_n=init_n, frequency=frequency
        )
        right, right_failures = _evaluate_batch(
            bars, configs[midpoint:], sdt=sdt, init_n=init_n, frequency=frequency
        )
        return {**left, **right}, [*left_failures, *right_failures]

    index = _output_index(output)
    mapped: dict[str, tuple[pd.Series, dict[str, object], str]] = {}
    mapping_failures: list[dict[str, str]] = []
    unmapped: list[tuple[str, pd.Series]] = []
    for column in _signal_columns(output):
        values = pd.Series(output[column].astype("string").to_numpy(), index=index, name=column)
        try:
            config = _derived_config(column, values, frequency)
            name = str(config["name"])
            if name in mapped:
                raise ValueError(f"duplicate output for registered signal {name}")
            mapped[name] = (values, config, column)
        except Exception:
            if len(configs) == 1:
                fallback = dict(configs[0])
                fallback["parameter_policy"] = "native_defaults"
                mapped[str(fallback["name"])] = (values, fallback, column)
            else:
                unmapped.append((column, values))

    requested = {str(config["name"]): config for config in configs}
    unresolved_names = set(requested) - set(mapped)
    pending = list(unmapped)
    while pending:
        progress = False
        next_pending: list[tuple[str, pd.Series]] = []
        for column, values in pending:
            candidates = [
                name
                for name in unresolved_names
                if _template_pattern(name).fullmatch(re.sub(r"V\d{6,8}$", "", column))
            ]
            if len(candidates) != 1:
                next_pending.append((column, values))
                continue
            name = candidates[0]
            fallback = dict(requested[name])
            fallback["parameter_policy"] = "native_defaults"
            mapped[name] = (values, fallback, column)
            unresolved_names.remove(name)
            progress = True
        pending = next_pending
        if not progress:
            break

    missing = [config for name, config in requested.items() if name not in mapped]
    if missing and len(configs) > 1:
        recovered: dict[str, tuple[pd.Series, dict[str, object], str]] = {}
        recovered_failures: list[dict[str, str]] = []
        for config in missing:
            part, failures = _evaluate_batch(
                bars, [config], sdt=sdt, init_n=init_n, frequency=frequency
            )
            recovered.update(part)
            recovered_failures.extend(failures)
        mapped.update(recovered)
        mapping_failures.extend(recovered_failures)
    elif missing:
        mapping_failures.append({
            "name": str(missing[0]["name"]),
            "frequency": frequency,
            "error_type": "MissingSignalOutput",
            "error": "CZSC returned no identifiable signal column",
        })
    for column, _ in pending:
        mapping_failures.append({
            "name": "UNMAPPED_OUTPUT",
            "frequency": frequency,
            "output_key": column,
            "error_type": "UnmappedSignalOutput",
            "error": "output key cannot be mapped uniquely to a registered signal",
        })
    return mapped, mapping_failures


def _align_daily(
    values: pd.Series,
    *,
    frequency_tag: str,
    target: pd.DatetimeIndex,
    daily_snapshot: str | None,
) -> pd.Series:
    result = values.copy()
    if frequency_tag == "30m":
        snapshot = daily_snapshot or "15:00"
        result = result.loc[result.index.strftime("%H:%M") == snapshot]
        result.index = result.index.normalize()
        return result.reindex(target)
    result.index = result.index.normalize()
    if frequency_tag == "weekly":
        expanded = result.reindex(result.index.union(target)).sort_index().ffill()
        return expanded.reindex(target)
    return result.reindex(target)


def _counts_json(values: pd.Series, transform: Any) -> str:
    counts = Counter(
        transformed
        for value in values.dropna()
        if (transformed := transform(value)) is not None
    )
    return _canonical_json(dict(sorted(counts.items())))


def generate_signal_census(
    data: MarketData,
    frequency_specs: Sequence[Mapping[str, object]],
    *,
    evaluation_start: pd.Timestamp | str,
    registry: Sequence[Mapping[str, object]] | None = None,
) -> SignalCensusResult:
    """Run one native-default configuration for each registered K-line signal."""
    inventory = [dict(item) for item in (registry or czsc_native.list_all_signals())]
    included = [item for item in inventory if item.get("category") == "kline"]
    excluded = [item for item in inventory if item.get("category") != "kline"]
    target = pd.DatetimeIndex(pd.to_datetime(data.daily["dt"]).dt.normalize(), name="dt")
    evaluation_start = pd.Timestamp(evaluation_start).normalize()
    evaluation_mask = target >= evaluation_start
    primary_columns: dict[str, pd.Series] = {}
    full_columns: dict[str, pd.Series] = {}
    catalog_rows: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    frames = {"30m": data.intraday, "daily": data.daily, "weekly": data.weekly}

    for spec in frequency_specs:
        tag = str(spec["tag"])
        frequency = str(spec["czsc_freq"])
        source = frames[tag]
        bars = _to_raw_bars(source, frequency)
        configs = [{"name": str(item["name"]), "freq": frequency} for item in included]
        mapped, frequency_failures = _evaluate_batch(
            bars,
            configs,
            sdt=str(pd.Timestamp(source["dt"].min()).date()),
            init_n=int(spec["warmup_bars"]),
            frequency=frequency,
        )
        failures.extend(frequency_failures)
        item_by_name = {str(item["name"]): item for item in included}
        failed_names = {item["name"] for item in frequency_failures if item["name"] != "UNMAPPED_OUTPUT"}

        for name in sorted(item_by_name):
            item = item_by_name[name]
            if name not in mapped:
                error = next(
                    (entry for entry in frequency_failures if entry["name"] == name),
                    {"error_type": "MissingSignalOutput", "error": "no output"},
                )
                catalog_rows.append({
                    "signal_id": "",
                    "name": name,
                    "namespace": str(item.get("namespace", "")),
                    "registry_category": "kline",
                    "frequency": tag,
                    "status": "FAILED" if name in failed_names else "UNMAPPED",
                    "config_json": "",
                    "output_key": "",
                    "s001_reference_function": name in S001_FUNCTIONS.get(tag, set()),
                    "observed_days": 0,
                    "primary_state_count": 0,
                    "primary_value_counts_json": "{}",
                    "full_value_counts_json": "{}",
                    "error_type": error["error_type"],
                    "error": error["error"],
                })
                continue
            raw, config, output_key = mapped[name]
            aligned = _align_daily(
                raw,
                frequency_tag=tag,
                target=target,
                daily_snapshot=str(spec.get("daily_snapshot", "")) or None,
            )
            signal_id = f"SIG-{tag.upper()}-{_short_hash(config)}"
            full = aligned.map(semantic_value).astype("string").rename(signal_id)
            primary = aligned.map(primary_value).astype("string").rename(signal_id)
            if signal_id in primary_columns:
                raise ValueError(f"duplicate signal identity: {signal_id}")
            primary_columns[signal_id] = primary
            full_columns[signal_id] = full
            visible = aligned.loc[evaluation_mask]
            catalog_rows.append({
                "signal_id": signal_id,
                "name": name,
                "namespace": str(item.get("namespace", "")),
                "registry_category": "kline",
                "frequency": tag,
                "status": "GENERATED",
                "config_json": _canonical_json(config),
                "output_key": output_key,
                "s001_reference_function": name in S001_FUNCTIONS.get(tag, set()),
                "observed_days": int(visible.notna().sum()),
                "primary_state_count": int(visible.map(primary_value).nunique(dropna=True)),
                "primary_value_counts_json": _counts_json(visible, primary_value),
                "full_value_counts_json": _counts_json(visible, semantic_value),
                "error_type": "",
                "error": "",
            })

    for item in sorted(excluded, key=lambda value: str(value["name"])):
        catalog_rows.append({
            "signal_id": "",
            "name": str(item["name"]),
            "namespace": str(item.get("namespace", "")),
            "registry_category": str(item.get("category", "")),
            "frequency": "",
            "status": "EXCLUDED_SCOPE",
            "config_json": "",
            "output_key": "",
            "s001_reference_function": False,
            "observed_days": 0,
            "primary_state_count": 0,
            "primary_value_counts_json": "{}",
            "full_value_counts_json": "{}",
            "error_type": "ScopeExclusion",
            "error": "requires position, event, or multi-frequency trader state",
        })

    primary_frame = pd.DataFrame(primary_columns, index=target)
    full_frame = pd.DataFrame(full_columns, index=target)
    summary: dict[str, object] = {
        "registered_total": len(inventory),
        "registered_by_category": dict(sorted(Counter(str(item.get("category", "")) for item in inventory).items())),
        "included_functions": len(included),
        "excluded_functions": len(excluded),
        "requested_configurations": len(included) * len(frequency_specs),
        "generated_configurations": len(primary_columns),
        "failed_or_unmapped_configurations": len(included) * len(frequency_specs) - len(primary_columns),
    }
    return SignalCensusResult(
        primary=primary_frame,
        full=full_frame,
        catalog=pd.DataFrame(catalog_rows),
        failures=tuple(failures),
        registry_summary=summary,
    )


def _outcome_arrays(
    execution: pd.DataFrame,
    horizons: Iterable[int],
    fee_rate: float,
) -> dict[int, dict[str, np.ndarray]]:
    source = execution.sort_values("dt").reset_index(drop=True)
    opens = source["open"].to_numpy(dtype=float)
    closes = source["close"].to_numpy(dtype=float)
    highs = source["high"].to_numpy(dtype=float)
    lows = source["low"].to_numpy(dtype=float)
    size = len(source)
    result: dict[int, dict[str, np.ndarray]] = {}
    for horizon in horizons:
        if horizon < 1:
            raise ValueError("event horizon must be positive")
        entry = np.full(size, np.nan)
        exit_close = np.full(size, np.nan)
        raw_return = np.full(size, np.nan)
        net_return = np.full(size, np.nan)
        mfe = np.full(size, np.nan)
        mae = np.full(size, np.nan)
        for index in range(0, size - horizon):
            entry[index] = opens[index + 1]
            exit_close[index] = closes[index + horizon]
            raw_return[index] = exit_close[index] / entry[index] - 1.0
            net_return[index] = (
                exit_close[index] * (1.0 - fee_rate)
                / (entry[index] * (1.0 + fee_rate))
                - 1.0
            )
            mfe[index] = highs[index + 1 : index + horizon + 1].max() / entry[index] - 1.0
            mae[index] = lows[index + 1 : index + horizon + 1].min() / entry[index] - 1.0
        result[horizon] = {
            "entry_price": entry,
            "exit_price": exit_close,
            "raw_return": raw_return,
            "net_return": net_return,
            "mfe": mfe,
            "mae": mae,
        }
    return result


def _metric_values(frame: pd.DataFrame) -> dict[str, object]:
    returns = frame["net_return"].astype(float)
    deviation = float(returns.std(ddof=1)) if len(returns) > 1 else np.nan
    return {
        "event_count": int(len(frame)),
        "raw_return_mean": float(frame["raw_return"].mean()),
        "raw_return_median": float(frame["raw_return"].median()),
        "net_return_mean": float(returns.mean()),
        "net_return_median": float(returns.median()),
        "net_win_rate": float(returns.gt(0).mean()),
        "net_return_std": deviation,
        "standardized_effect": float(returns.mean() / deviation) if deviation and np.isfinite(deviation) else np.nan,
        "mfe_mean": float(frame["mfe"].mean()),
        "mfe_median": float(frame["mfe"].median()),
        "mae_mean": float(frame["mae"].mean()),
        "mae_median": float(frame["mae"].median()),
    }


def evaluate_signal_events(
    primary: pd.DataFrame,
    full: pd.DataFrame,
    catalog: pd.DataFrame,
    execution_daily: pd.DataFrame,
    *,
    evaluation_start: pd.Timestamp | str,
    horizons: Sequence[int],
    fee_rate: float,
    sparse_events_below: int,
    narrow_coverage_years_below: int,
    concentrated_year_share_above: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, object]]:
    """Evaluate state-transition events with T+1 executable prices."""
    execution = execution_daily.copy()
    execution["dt"] = pd.to_datetime(execution["dt"]).dt.normalize()
    execution = execution.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
    dates = pd.DatetimeIndex(execution["dt"], name="dt")
    primary = primary.reindex(dates)
    full = full.reindex(dates)
    outcomes = _outcome_arrays(execution, horizons, fee_rate)
    evaluation_start = pd.Timestamp(evaluation_start).normalize()
    config_lookup = catalog.loc[catalog["status"] == "GENERATED"].set_index("signal_id")
    event_rows: list[dict[str, object]] = []
    annual_rows: list[dict[str, object]] = []
    aggregate_rows: list[dict[str, object]] = []
    behavior_members: defaultdict[str, list[dict[str, str]]] = defaultdict(list)

    for signal_id in primary.columns:
        series = primary[signal_id]
        full_series = full[signal_id]
        config = config_lookup.loc[signal_id]
        visible_states = sorted(str(value) for value in series.loc[dates >= evaluation_start].dropna().unique())
        for state in visible_states:
            active = series.eq(state)
            starts = active & ~active.shift(1, fill_value=False)
            starts &= dates >= evaluation_start
            indices = np.flatnonzero(starts.to_numpy())
            state_id = f"STATE-{_short_hash({'signal_id': signal_id, 'state': state})}"
            vector = active.loc[dates >= evaluation_start].fillna(False).astype(np.uint8).to_numpy().tobytes()
            behavior_hash = hashlib.sha256(vector).hexdigest()
            behavior_members[behavior_hash].append({
                "state_id": state_id,
                "signal_id": signal_id,
                "name": str(config["name"]),
                "frequency": str(config["frequency"]),
                "state": state,
            })
            years = Counter(int(dates[index].year) for index in indices)
            coverage_years = len(years)
            max_year_share = max(years.values(), default=0) / len(indices) if len(indices) else np.nan

            for index in indices:
                event_rows.append({
                    "state_id": state_id,
                    "signal_id": signal_id,
                    "signal_date": dates[index].date().isoformat(),
                    "frequency": str(config["frequency"]),
                    "name": str(config["name"]),
                    "state_primary": state,
                    "state_full": None if pd.isna(full_series.iloc[index]) else str(full_series.iloc[index]),
                })

            for horizon in horizons:
                outcome = outcomes[int(horizon)]
                rows = []
                for index in indices:
                    if not np.isfinite(outcome["net_return"][index]):
                        continue
                    rows.append({
                        "signal_date": dates[index],
                        **{key: float(values[index]) for key, values in outcome.items()},
                    })
                sample = pd.DataFrame(rows)
                if sample.empty:
                    continue
                sample["year"] = sample["signal_date"].dt.year
                yearly_signs: list[int] = []
                for year, year_frame in sample.groupby("year", sort=True):
                    metrics = _metric_values(year_frame)
                    annual_rows.append({
                        "state_id": state_id,
                        "signal_id": signal_id,
                        "frequency": str(config["frequency"]),
                        "name": str(config["name"]),
                        "state_primary": state,
                        "horizon": int(horizon),
                        "year": int(year),
                        **metrics,
                    })
                    yearly_signs.append(int(np.sign(float(metrics["net_return_mean"]))))
                metrics = _metric_values(sample)
                positive_years = sum(value > 0 for value in yearly_signs)
                negative_years = sum(value < 0 for value in yearly_signs)
                valid_years = positive_years + negative_years
                sign_consistency = max(positive_years, negative_years) / valid_years if valid_years else np.nan
                quality_flags = []
                if len(indices) < sparse_events_below:
                    quality_flags.append("SPARSE")
                if coverage_years < narrow_coverage_years_below:
                    quality_flags.append("NARROW_YEARS")
                if np.isfinite(max_year_share) and max_year_share > concentrated_year_share_above:
                    quality_flags.append("YEAR_CONCENTRATED")
                aggregate_rows.append({
                    "state_id": state_id,
                    "signal_id": signal_id,
                    "frequency": str(config["frequency"]),
                    "name": str(config["name"]),
                    "namespace": str(config["namespace"]),
                    "s001_reference_function": bool(config["s001_reference_function"]),
                    "state_primary": state,
                    "horizon": int(horizon),
                    "episode_count": int(len(indices)),
                    "coverage_years": int(coverage_years),
                    "max_year_share": float(max_year_share),
                    "positive_years": positive_years,
                    "negative_years": negative_years,
                    "year_sign_consistency": float(sign_consistency),
                    "forward_bias": "POSITIVE" if metrics["net_return_mean"] > 0 else "NEGATIVE" if metrics["net_return_mean"] < 0 else "FLAT",
                    "evidence_quality": "OK" if not quality_flags else "|".join(quality_flags),
                    "behavior_sha256": behavior_hash,
                    **metrics,
                })

    duplicate_groups = [
        {"behavior_sha256": digest, "members": members}
        for digest, members in sorted(behavior_members.items())
        if len(members) > 1
    ]
    redundancy = {
        "schema_version": 1,
        "definition": "exact equality of the daily primary-state active vector",
        "state_vectors": len(behavior_members),
        "duplicate_groups": duplicate_groups,
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_member_count": sum(len(group["members"]) for group in duplicate_groups),
    }
    return (
        pd.DataFrame(aggregate_rows),
        pd.DataFrame(annual_rows),
        pd.DataFrame(event_rows),
        redundancy,
    )
