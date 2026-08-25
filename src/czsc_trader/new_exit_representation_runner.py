"""Causal new-representation diagnosis for the preregistered 0825_EX06 study."""

from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd


DAILY_DESCRIPTOR_IDS = (
    "close_to_ma20",
    "ma20_slope5",
    "ma10_ma20_spread",
    "drawdown_from_high20",
    "close_location20",
    "return5",
    "return10",
    "prior_low10_buffer",
    "atr5_to_atr20",
    "tr_to_atr20",
    "realized_vol5_to20",
    "downside_semivol5_to20",
    "negative_day_share5",
    "max_drawdown5_to_atr20",
    "daily_close_location",
    "gap_abs_to_atr20",
    "volume5_to20",
    "volume_t_to20",
    "down_up_volume_ratio10",
    "signed_volume_imbalance5",
    "signed_volume_imbalance10",
    "return_volume_corr10",
    "log_volume_slope5",
)

INTRADAY_DESCRIPTOR_IDS = (
    "intraday_down_volume_share",
    "intraday_realized_vol_to20",
    "intraday_close_location",
    "last4_30m_return",
)

WEEKLY_DESCRIPTOR_IDS = ("weekly_close_to_ma10",)
DESCRIPTOR_IDS = DAILY_DESCRIPTOR_IDS + INTRADAY_DESCRIPTOR_IDS + WEEKLY_DESCRIPTOR_IDS


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    result = numerator.astype(float).div(denominator.astype(float))
    return result.where(np.isfinite(result) & denominator.ne(0))


def validate_protocol(protocol: Mapping[str, object]) -> None:
    """Reject drift from the frozen diagnostic-only protocol."""
    if protocol.get("experiment_type") != "new_exit_representation_diagnosis":
        raise ValueError("unexpected experiment type")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("protocol must retain PRE_REGISTERED status")
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise ValueError("visible sample must stop at 2025-12-31")
    if protocol.get("holdout_access_allowed") is not False:
        raise ValueError("protocol must forbid holdout access")
    if int(protocol.get("expected_descriptor_count", -1)) != len(DESCRIPTOR_IDS):
        raise ValueError("descriptor identity count drifted")
    if int(protocol.get("expected_event_count", -1)) != 20:
        raise ValueError("event count drifted")
    if int(protocol.get("primary_material_event_count", -1)) != 14:
        raise ValueError("primary material event count drifted")
    if protocol.get("quantile_boundaries") != [0.2, 0.4, 0.6, 0.8]:
        raise ValueError("quantile boundaries drifted")
    if int(protocol.get("exact_permutation_combinations", -1)) != 364:
        raise ValueError("permutation count drifted")
    if len(protocol.get("discovery_event_ids", [])) != 2:
        raise ValueError("discovery event identities drifted")
    if not protocol.get("confirmation_event_id"):
        raise ValueError("confirmation event identity is missing")
    promotion = protocol.get("promotion")
    if not isinstance(promotion, Mapping) or any(bool(value) for value in promotion.values()):
        raise ValueError("diagnostic protocol must disable promotion")


def _require_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    required = ["dt", "open", "high", "low", "close", "volume"]
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError(f"OHLCV columns missing: {missing}")
    result = frame.loc[:, required].copy()
    result["dt"] = pd.to_datetime(result["dt"])
    result = result.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
    return result


def _rolling_downside_rms(returns: pd.Series, window: int) -> pd.Series:
    downside_squared = returns.clip(upper=0).pow(2)
    return downside_squared.rolling(window, min_periods=window).mean().pow(0.5)


def _max_drawdown(values: np.ndarray) -> float:
    if len(values) == 0 or not np.isfinite(values).all():
        return np.nan
    peaks = np.maximum.accumulate(values)
    return float(np.max(1.0 - values / peaks))


def _rolling_slope(values: np.ndarray) -> float:
    if not np.isfinite(values).all():
        return np.nan
    x = np.arange(len(values), dtype=float)
    centered = x - x.mean()
    return float(np.dot(centered, values - values.mean()) / np.dot(centered, centered))


def compute_daily_descriptors(daily: pd.DataFrame) -> pd.DataFrame:
    """Compute the frozen 23 daily descriptors without event outcomes."""
    prices = _require_ohlcv(daily)
    close = prices["close"].astype(float)
    opened = prices["open"].astype(float)
    high = prices["high"].astype(float)
    low = prices["low"].astype(float)
    volume = prices["volume"].astype(float)
    returns = close.pct_change()
    ma10 = close.rolling(10, min_periods=10).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    previous_close = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr5 = true_range.rolling(5, min_periods=5).mean()
    atr20 = true_range.rolling(20, min_periods=20).mean()
    realized5 = returns.rolling(5, min_periods=5).std(ddof=1)
    realized20 = returns.rolling(20, min_periods=20).std(ddof=1)
    downside5 = _rolling_downside_rms(returns, 5)
    downside20 = _rolling_downside_rms(returns, 20)
    negative_volume = volume.where(returns.lt(0))
    positive_volume = volume.where(returns.gt(0))
    negative_volume_mean = _safe_divide(
        negative_volume.fillna(0).rolling(10, min_periods=10).sum(),
        returns.lt(0).astype(float).rolling(10, min_periods=10).sum(),
    )
    positive_volume_mean = _safe_divide(
        positive_volume.fillna(0).rolling(10, min_periods=10).sum(),
        returns.gt(0).astype(float).rolling(10, min_periods=10).sum(),
    )
    signed_volume = np.sign(returns.fillna(0)) * volume
    volume_sum5 = volume.rolling(5, min_periods=5).sum()
    volume_sum10 = volume.rolling(10, min_periods=10).sum()
    atr20_fraction = _safe_divide(atr20, close)
    max_drawdown5 = close.rolling(5, min_periods=5).apply(_max_drawdown, raw=True)
    day_range = high - low
    range20 = high.rolling(20, min_periods=20).max() - low.rolling(20, min_periods=20).min()
    log_volume = np.log1p(volume)

    output = pd.DataFrame(
        {
            "dt": prices["dt"],
            "close_to_ma20": _safe_divide(close, ma20) - 1,
            "ma20_slope5": _safe_divide(ma20, ma20.shift(5)) - 1,
            "ma10_ma20_spread": _safe_divide(ma10, ma20) - 1,
            "drawdown_from_high20": _safe_divide(
                close, high.rolling(20, min_periods=20).max()
            )
            - 1,
            "close_location20": _safe_divide(
                close - low.rolling(20, min_periods=20).min(), range20
            ),
            "return5": close.pct_change(5),
            "return10": close.pct_change(10),
            "prior_low10_buffer": _safe_divide(
                close, low.shift(1).rolling(10, min_periods=10).min()
            )
            - 1,
            "atr5_to_atr20": _safe_divide(atr5, atr20),
            "tr_to_atr20": _safe_divide(true_range, atr20),
            "realized_vol5_to20": _safe_divide(realized5, realized20),
            "downside_semivol5_to20": _safe_divide(downside5, downside20),
            "negative_day_share5": returns.lt(0)
            .astype(float)
            .where(returns.notna())
            .rolling(5, min_periods=5)
            .mean(),
            "max_drawdown5_to_atr20": _safe_divide(max_drawdown5, atr20_fraction),
            "daily_close_location": _safe_divide(close - low, day_range),
            "gap_abs_to_atr20": _safe_divide(
                (opened / previous_close - 1).abs(), _safe_divide(atr20, previous_close)
            ),
            "volume5_to20": _safe_divide(
                volume.rolling(5, min_periods=5).mean(),
                volume.rolling(20, min_periods=20).mean(),
            ),
            "volume_t_to20": _safe_divide(volume, volume.rolling(20, min_periods=20).mean()),
            "down_up_volume_ratio10": _safe_divide(
                negative_volume_mean, positive_volume_mean
            ),
            "signed_volume_imbalance5": _safe_divide(
                signed_volume.rolling(5, min_periods=5).sum(), volume_sum5
            ),
            "signed_volume_imbalance10": _safe_divide(
                signed_volume.rolling(10, min_periods=10).sum(), volume_sum10
            ),
            "return_volume_corr10": returns.rolling(10, min_periods=10).corr(log_volume),
            "log_volume_slope5": log_volume.rolling(5, min_periods=5).apply(
                _rolling_slope, raw=True
            ),
        }
    )
    return output.loc[:, ["dt", *DAILY_DESCRIPTOR_IDS]]


def compute_intraday_descriptors(intraday: pd.DataFrame) -> pd.DataFrame:
    """Aggregate four frozen 30-minute descriptors to one causal row per day."""
    bars = _require_ohlcv(intraday)
    bars["trade_date"] = bars["dt"].dt.normalize()
    rows: list[dict[str, object]] = []
    for trade_date, group in bars.groupby("trade_date", sort=True):
        group = group.sort_values("dt").reset_index(drop=True)
        if len(group) < 4:
            raise ValueError(f"intraday day has fewer than four bars: {trade_date}")
        returns = np.empty(len(group), dtype=float)
        returns[0] = float(group.loc[0, "close"] / group.loc[0, "open"] - 1)
        returns[1:] = (
            group["close"].iloc[1:].to_numpy(dtype=float)
            / group["close"].iloc[:-1].to_numpy(dtype=float)
            - 1
        )
        volume = group["volume"].to_numpy(dtype=float)
        total_volume = float(volume.sum())
        down_share = float(volume[returns < 0].sum() / total_volume) if total_volume else np.nan
        day_low = float(group["low"].min())
        day_high = float(group["high"].max())
        day_range = day_high - day_low
        rows.append(
            {
                "dt": pd.Timestamp(trade_date),
                "intraday_down_volume_share": down_share,
                "_intraday_realized_vol": float(np.sqrt(np.square(returns).sum())),
                "intraday_close_location": (
                    float((group.iloc[-1]["close"] - day_low) / day_range)
                    if day_range
                    else np.nan
                ),
                "last4_30m_return": float(
                    group.iloc[-1]["close"] / group.iloc[-4]["open"] - 1
                ),
            }
        )
    result = pd.DataFrame(rows)
    history_median = (
        result["_intraday_realized_vol"]
        .shift(1)
        .rolling(20, min_periods=20)
        .median()
    )
    result["intraday_realized_vol_to20"] = _safe_divide(
        result["_intraday_realized_vol"], history_median
    )
    return result.loc[:, ["dt", *INTRADAY_DESCRIPTOR_IDS]]


def compute_weekly_descriptor(
    daily_dates: pd.Series, weekly: pd.DataFrame
) -> pd.Series:
    """Align the most recent completed weekly close/MA10 value to daily dates."""
    if not {"dt", "close"}.issubset(weekly.columns):
        raise ValueError("weekly frame must contain dt and close")
    weeks = weekly.loc[:, ["dt", "close"]].copy()
    weeks["dt"] = pd.to_datetime(weeks["dt"])
    weeks = weeks.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
    weeks["weekly_close_to_ma10"] = _safe_divide(
        weeks["close"], weeks["close"].rolling(10, min_periods=10).mean()
    ) - 1
    requested = pd.DataFrame(
        {"_order": np.arange(len(daily_dates)), "dt": pd.to_datetime(daily_dates).to_numpy()}
    ).sort_values("dt")
    aligned = pd.merge_asof(
        requested,
        weeks.loc[:, ["dt", "weekly_close_to_ma10"]],
        on="dt",
        direction="backward",
        allow_exact_matches=True,
    ).sort_values("_order")
    return aligned["weekly_close_to_ma10"].reset_index(drop=True)


def _quantile_bin(value: float, quantiles: Sequence[float]) -> str | None:
    if not np.isfinite(value) or len(quantiles) != 4 or not np.isfinite(quantiles).all():
        return None
    for index, boundary in enumerate(quantiles, start=1):
        if value <= boundary:
            return f"Q{index}"
    return "Q5"


def assign_causal_quantile_bins(
    frame: pd.DataFrame,
    protocol: Mapping[str, object],
    *,
    descriptor_ids: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Assign current rows to bins computed strictly from earlier rows."""
    ids = tuple(descriptor_ids or [column for column in frame if column != "dt"])
    window = int(protocol["daily_history_window"])
    minimum = int(protocol["daily_minimum_history"])
    values = frame.loc[:, ["dt", *ids]].copy().sort_values("dt").reset_index(drop=True)
    output = pd.DataFrame({"dt": pd.to_datetime(values["dt"])})
    for descriptor in ids:
        series = pd.to_numeric(values[descriptor], errors="coerce")
        assigned: list[str | None] = []
        for index, current in enumerate(series):
            history = series.iloc[max(0, index - window) : index].dropna()
            if len(history) < minimum or not np.isfinite(current):
                assigned.append(None)
                continue
            quantiles = history.quantile(
                protocol["quantile_boundaries"], interpolation="linear"
            ).to_numpy(dtype=float)
            assigned.append(_quantile_bin(float(current), quantiles))
        output[descriptor] = pd.Series(assigned, dtype=object)
    return output


def discover_and_confirm_signatures(
    events: pd.DataFrame,
    matrix: pd.DataFrame,
    protocol: Mapping[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply the fixed two-event discovery and one-event time confirmation."""
    discovery_ids = [str(value) for value in protocol["discovery_event_ids"]]
    confirmation_id = str(protocol["confirmation_event_id"])
    if not events["event_id"].is_unique:
        raise ValueError("event identities must be unique")
    value_lookup = matrix.pivot(index="event_id", columns="descriptor", values="quantile_bin")
    discovered_rows: list[dict[str, object]] = []
    confirmed_rows: list[dict[str, object]] = []
    for descriptor in sorted(value_lookup.columns.astype(str)):
        values = [value_lookup.at[event_id, descriptor] for event_id in discovery_ids]
        if any(pd.isna(value) for value in values) or len(set(values)) != 1:
            continue
        quantile_bin = str(values[0])
        discovered_rows.append({"descriptor": descriptor, "quantile_bin": quantile_bin})
        if pd.isna(value_lookup.at[confirmation_id, descriptor]):
            continue
        if str(value_lookup.at[confirmation_id, descriptor]) != quantile_bin:
            continue
        support_ids = set(
            value_lookup.index[value_lookup[descriptor].astype(str).eq(quantile_bin)].astype(str)
        )
        protective = events.loc[events["outcome_label"].eq("protective_exit")]
        protective_ids = set(protective["event_id"].astype(str))
        protected_support_ids = support_ids & protective_ids
        downtrend_ids = set(
            protective.loc[protective["regime"].eq("downtrend"), "event_id"].astype(str)
        )
        joint_ids = set(
            protective.loc[
                protective["block_label"].eq("joint_margin_block"), "event_id"
            ].astype(str)
        )
        protective_support = len(protected_support_ids)
        downtrend_support = len(support_ids & downtrend_ids)
        joint_support = len(support_ids & joint_ids)
        low_contamination = (
            protective_support <= int(protocol["maximum_protective_support"])
            and (
                not bool(protocol["require_zero_downtrend_protective_support"])
                or downtrend_support == 0
            )
            and (
                not bool(protocol["require_zero_joint_margin_protective_support"])
                or joint_support == 0
            )
        )
        confirmed_rows.append(
            {
                "descriptor": descriptor,
                "quantile_bin": quantile_bin,
                "protective_support": protective_support,
                "downtrend_protective_support": downtrend_support,
                "joint_margin_protective_support": joint_support,
                "other_false_support": len(
                    support_ids
                    & set(
                        events.loc[
                            events["outcome_label"].eq("false_exit")
                            & ~events["event_id"].isin([*discovery_ids, confirmation_id]),
                            "event_id",
                        ].astype(str)
                    )
                ),
                "neutral_support": len(
                    support_ids
                    & set(
                        events.loc[events["outcome_label"].eq("neutral_exit"), "event_id"].astype(str)
                    )
                ),
                "low_contamination": low_contamination,
            }
        )
    discovered = pd.DataFrame(discovered_rows, columns=["descriptor", "quantile_bin"])
    confirmed = pd.DataFrame(
        confirmed_rows,
        columns=[
            "descriptor",
            "quantile_bin",
            "protective_support",
            "downtrend_protective_support",
            "joint_margin_protective_support",
            "other_false_support",
            "neutral_support",
            "low_contamination",
        ],
    )
    return discovered, confirmed


def exact_permutation_audit(
    events: pd.DataFrame,
    matrix: pd.DataFrame,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Enumerate all primary-label allocations and control family-wise selection."""
    dominant_ids = [
        *[str(value) for value in protocol["discovery_event_ids"]],
        str(protocol["confirmation_event_id"]),
    ]
    protective_ids = events.loc[
        events["outcome_label"].eq("protective_exit"), "event_id"
    ].astype(str).tolist()
    material_ids = dominant_ids + protective_ids
    if len(material_ids) != int(protocol["primary_material_event_count"]):
        raise ValueError("primary material event cohort differs from protocol")
    if not events["event_id"].is_unique:
        raise ValueError("event identities must be unique")
    context = events.set_index("event_id")
    signatures: dict[tuple[str, str], set[str]] = {}
    material = matrix.loc[matrix["event_id"].astype(str).isin(material_ids)].copy()
    material = material.loc[material["quantile_bin"].notna()]
    for (descriptor, quantile_bin), group in material.groupby(
        ["descriptor", "quantile_bin"], sort=True
    ):
        signatures[(str(descriptor), str(quantile_bin))] = set(
            group["event_id"].astype(str)
        )

    def qualifies(pseudo_positive: set[str]) -> bool:
        pseudo_protective = set(material_ids) - pseudo_positive
        for support in signatures.values():
            if not pseudo_positive.issubset(support):
                continue
            contamination = support & pseudo_protective
            if len(contamination) > int(protocol["maximum_protective_support"]):
                continue
            contaminated_rows = context.loc[list(contamination)] if contamination else None
            if (
                contaminated_rows is not None
                and bool(protocol["require_zero_downtrend_protective_support"])
                and contaminated_rows["regime"].eq("downtrend").any()
            ):
                continue
            if (
                contaminated_rows is not None
                and bool(protocol["require_zero_joint_margin_protective_support"])
                and contaminated_rows["block_label"].eq("joint_margin_block").any()
            ):
                continue
            return True
        return False

    qualifying_count = sum(
        qualifies(set(candidate)) for candidate in combinations(material_ids, 3)
    )
    combination_count = sum(1 for _ in combinations(material_ids, 3))
    if combination_count != int(protocol["exact_permutation_combinations"]):
        raise ValueError("exact permutation combination count drifted")
    return {
        "combination_count": combination_count,
        "qualifying_combination_count": qualifying_count,
        "exact_p_value": qualifying_count / combination_count,
        "observed_label_set_qualifies": qualifies(set(dominant_ids)),
    }


def classify_representation(
    *,
    event_count: int,
    descriptor_count: int,
    discovered_count: int,
    confirmed: pd.DataFrame,
    permutation_audit: Mapping[str, object],
    evidence_ok: bool,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Apply the preregistered five-way machine classification order."""
    if (
        not evidence_ok
        or event_count != int(protocol["expected_event_count"])
        or descriptor_count != int(protocol["expected_descriptor_count"])
    ):
        classification = "insufficient_new_representation_evidence"
    elif confirmed.empty:
        classification = "no_shared_new_representation"
    elif bool(confirmed["low_contamination"].astype(bool).any()):
        if float(permutation_audit["exact_p_value"]) <= float(
            protocol["maximum_exact_p_value"]
        ):
            classification = "new_representation_confirmed"
        else:
            classification = "suggestive_but_multiplicity_unconfirmed"
    else:
        classification = "shared_but_contaminated_new_representation"
    return {
        "classification": classification,
        "event_count": int(event_count),
        "descriptor_count": int(descriptor_count),
        "discovered_signature_count": int(discovered_count),
        "confirmed_signature_count": int(len(confirmed)),
        "low_contamination_signature_count": int(
            confirmed.get("low_contamination", pd.Series(dtype=bool)).astype(bool).sum()
        ),
        "exact_p_value": float(permutation_audit.get("exact_p_value", 1.0)),
    }
