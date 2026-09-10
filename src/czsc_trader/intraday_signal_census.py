"""True intraday CZSC signal census for evidence-speed research.

The census records primary-state transition days.  It deliberately does not
assign a buy/sell direction, calculate returns, or construct a strategy.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
from typing import Mapping, Sequence

import czsc._native as czsc_native
import numpy as np
import pandas as pd

from .factors import _to_raw_bars
from .signal_census import (
    _canonical_json,
    _evaluate_batch,
    _short_hash,
    primary_value,
    semantic_value,
)


@dataclass(frozen=True)
class IntradaySignalCensusResult:
    """Audit artifacts from one native-default intraday registry census."""

    catalog: pd.DataFrame
    states: pd.DataFrame
    events: pd.DataFrame
    redundancy: dict[str, list[dict[str, str]]]
    failures: tuple[dict[str, str], ...]
    summary: dict[str, object]


def _standardize_bars(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    required = {"Date", "Open", "High", "Low", "Close", "Volume", "Amount"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"intraday data missing columns: {missing}")
    result = frame.rename(
        columns={
            "Date": "dt",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "vol",
            "Amount": "amount",
        }
    ).copy()
    result["dt"] = pd.to_datetime(result["dt"])
    result.insert(0, "symbol", symbol)
    result.insert(2, "id", np.arange(len(result), dtype=np.int64))
    result.insert(3, "freq", "15分钟")
    return result


def _event_density(
    event_dates: pd.DatetimeIndex,
    calendar: pd.DatetimeIndex,
    *,
    window_sessions: int,
) -> tuple[float, float, int, int]:
    vector = pd.Series(0, index=calendar, dtype="int64")
    if len(event_dates):
        vector.loc[calendar.intersection(event_dates)] = 1
    rolling = vector.rolling(window_sessions, min_periods=window_sessions).sum().dropna()
    if rolling.empty:
        return float("nan"), float("nan"), 0, 0
    return (
        float(rolling.median()),
        float(rolling.quantile(0.10, interpolation="lower")),
        int(rolling.min()),
        int(rolling.max()),
    )


def generate_intraday_signal_census(
    frame: pd.DataFrame,
    symbol: str,
    daily_regime: pd.Series,
    *,
    evaluation_start: pd.Timestamp | str,
    warmup_bars: int,
    window_sessions: int,
    median_event_days_required: int,
    p10_event_days_required: int,
    distinct_event_days_required: int,
    registry: Sequence[Mapping[str, object]] | None = None,
) -> IntradaySignalCensusResult:
    """Evaluate every registered K-line signal on each completed 15m bar."""

    inventory = [dict(item) for item in (registry or czsc_native.list_all_signals())]
    included = [item for item in inventory if item.get("category") == "kline"]
    excluded = [item for item in inventory if item.get("category") != "kline"]
    standard = _standardize_bars(frame, symbol).sort_values("dt").reset_index(drop=True)
    if standard["dt"].duplicated().any():
        raise ValueError("15m timestamps must be unique")
    evaluation_start = pd.Timestamp(evaluation_start).normalize()
    calendar = pd.DatetimeIndex(
        sorted(standard.loc[standard["dt"] >= evaluation_start, "dt"].dt.normalize().unique()),
        name="date",
    )
    regimes = daily_regime.copy()
    regimes.index = pd.DatetimeIndex(pd.to_datetime(regimes.index)).normalize()
    regimes = regimes.astype("string").reindex(calendar)
    if regimes.isna().any():
        missing = regimes.index[regimes.isna()].strftime("%Y-%m-%d").tolist()
        raise ValueError(f"daily regime missing evaluation dates: {missing[:10]}")

    bars = _to_raw_bars(standard, "15分钟")
    configs = [{"name": str(item["name"]), "freq": "15分钟"} for item in included]
    mapped, failures = _evaluate_batch(
        bars,
        configs,
        sdt=str(standard["dt"].min().date()),
        init_n=warmup_bars,
        frequency="15分钟",
    )
    item_by_name = {str(item["name"]): item for item in included}
    failure_by_name = {
        str(item["name"]): item for item in failures if item["name"] != "UNMAPPED_OUTPUT"
    }
    catalog_rows: list[dict[str, object]] = []
    state_rows: list[dict[str, object]] = []
    event_rows: list[dict[str, object]] = []
    redundancy_members: defaultdict[str, list[dict[str, str]]] = defaultdict(list)

    for name in sorted(item_by_name):
        item = item_by_name[name]
        if name not in mapped:
            error = failure_by_name.get(
                name,
                {"error_type": "MissingSignalOutput", "error": "no identifiable output"},
            )
            catalog_rows.append(
                {
                    "signal_id": "",
                    "name": name,
                    "namespace": str(item.get("namespace", "")),
                    "status": "FAILED",
                    "config_json": "",
                    "output_key": "",
                    "observed_bars": 0,
                    "primary_state_count": 0,
                    "error_type": str(error["error_type"]),
                    "error": str(error["error"]),
                }
            )
            continue

        raw, config, output_key = mapped[name]
        signal_id = f"SIG-15M-{_short_hash(config)}"
        visible = raw.loc[raw.index.normalize() >= evaluation_start]
        primary = visible.map(primary_value).astype("string")
        full = visible.map(semantic_value).astype("string")
        states = sorted(str(value) for value in primary.dropna().unique())
        catalog_rows.append(
            {
                "signal_id": signal_id,
                "name": name,
                "namespace": str(item.get("namespace", "")),
                "status": "GENERATED",
                "config_json": _canonical_json(config),
                "output_key": output_key,
                "observed_bars": int(primary.notna().sum()),
                "primary_state_count": len(states),
                "error_type": "",
                "error": "",
            }
        )

        for state in states:
            active = primary.eq(state)
            transitions = active & ~active.shift(1, fill_value=False)
            timestamps = pd.DatetimeIndex(primary.index[transitions.fillna(False)])
            event_table = pd.DataFrame({"event_time": timestamps})
            event_table["date"] = event_table["event_time"].dt.normalize()
            event_table = event_table.drop_duplicates("date", keep="first")
            event_dates = pd.DatetimeIndex(event_table["date"], name="date")
            median_days, p10_days, minimum_days, maximum_days = _event_density(
                event_dates,
                calendar,
                window_sessions=window_sessions,
            )
            vector = pd.Series(0, index=calendar, dtype=np.uint8)
            vector.loc[calendar.intersection(event_dates)] = 1
            behavior_hash = hashlib.sha256(vector.to_numpy().tobytes()).hexdigest()
            state_id = f"STATE-{_short_hash({'signal_id': signal_id, 'state': state})}"
            years = Counter(int(value.year) for value in event_dates)
            regime_counts = Counter(str(regimes.loc[value]) for value in event_dates)
            event_count = len(event_dates)
            max_year_share = max(years.values(), default=0) / event_count if event_count else np.nan
            density_capable = bool(
                median_days >= median_event_days_required
                and p10_days >= p10_event_days_required
                and event_count >= distinct_event_days_required
            )
            state_rows.append(
                {
                    "state_id": state_id,
                    "signal_id": signal_id,
                    "name": name,
                    "namespace": str(item.get("namespace", "")),
                    "state_primary": state,
                    "event_days": event_count,
                    "coverage_years": len(years),
                    "max_year_share": max_year_share,
                    "rolling_30d_median_event_days": median_days,
                    "rolling_30d_p10_event_days": p10_days,
                    "rolling_30d_min_event_days": minimum_days,
                    "rolling_30d_max_event_days": maximum_days,
                    "density_capable": density_capable,
                    "regime_counts_json": _canonical_json(dict(sorted(regime_counts.items()))),
                    "behavior_hash": behavior_hash,
                }
            )
            redundancy_members[behavior_hash].append(
                {
                    "state_id": state_id,
                    "signal_id": signal_id,
                    "name": name,
                    "state_primary": state,
                }
            )
            if event_table.empty:
                continue
            full_at_time = full.reindex(pd.DatetimeIndex(event_table["event_time"]))
            for (_, event), full_state in zip(event_table.iterrows(), full_at_time, strict=True):
                event_date = pd.Timestamp(event["date"])
                event_rows.append(
                    {
                        "state_id": state_id,
                        "signal_id": signal_id,
                        "name": name,
                        "state_primary": state,
                        "state_full": "" if pd.isna(full_state) else str(full_state),
                        "event_time": pd.Timestamp(event["event_time"]),
                        "event_date": event_date.date().isoformat(),
                        "regime": str(regimes.loc[event_date]),
                    }
                )

    for item in sorted(excluded, key=lambda value: str(value["name"])):
        catalog_rows.append(
            {
                "signal_id": "",
                "name": str(item["name"]),
                "namespace": str(item.get("namespace", "")),
                "status": "EXCLUDED_SCOPE",
                "config_json": "",
                "output_key": "",
                "observed_bars": 0,
                "primary_state_count": 0,
                "error_type": "ScopeExclusion",
                "error": "requires trader, position, event, or multi-frequency state",
            }
        )

    states_frame = pd.DataFrame(state_rows).sort_values(
        ["density_capable", "rolling_30d_p10_event_days", "rolling_30d_median_event_days"],
        ascending=[False, False, False],
    )
    events_frame = pd.DataFrame(event_rows).sort_values(
        ["event_time", "state_id"]
    )
    redundancy = {
        key: members
        for key, members in sorted(redundancy_members.items())
        if len(members) > 1
    }
    summary: dict[str, object] = {
        "registered_total": len(inventory),
        "registered_by_category": dict(
            sorted(Counter(str(item.get("category", "")) for item in inventory).items())
        ),
        "included_functions": len(included),
        "excluded_functions": len(excluded),
        "generated_configurations": int(
            sum(row["status"] == "GENERATED" for row in catalog_rows)
        ),
        "failed_configurations": int(sum(row["status"] == "FAILED" for row in catalog_rows)),
        "observed_primary_states": len(states_frame),
        "daily_deduplicated_events": len(events_frame),
        "density_capable_states": int(states_frame["density_capable"].sum()),
        "unique_event_behaviors": int(states_frame["behavior_hash"].nunique()),
        "redundant_behavior_groups": len(redundancy),
        "evaluation_sessions": len(calendar),
        "evaluation_first": calendar.min().date().isoformat(),
        "evaluation_last": calendar.max().date().isoformat(),
    }
    return IntradaySignalCensusResult(
        catalog=pd.DataFrame(catalog_rows),
        states=states_frame,
        events=events_frame,
        redundancy=redundancy,
        failures=tuple(failures),
        summary=summary,
    )
