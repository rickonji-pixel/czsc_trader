"""Causal, interpretable position-risk features for 588080 research."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RiskOverlaySpec:
    """One fixed, interpretable pressure-state candidate."""

    family: str
    pressure_position: float
    parameters: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if self.family not in {
            "trend_damage",
            "negative_persistence",
            "intraday_pressure",
        }:
            raise ValueError("unsupported position-risk family")
        if self.pressure_position not in {0.5, 0.75}:
            raise ValueError("pressure position must be 0.50 or 0.75")
        names = tuple(name for name, _ in self.parameters)
        expected = {
            "trend_damage": ("lookback", "drawdown"),
            "negative_persistence": ("window", "negative_count"),
            "intraday_pressure": ("close_location", "down_volume_share"),
        }[self.family]
        if names != expected or not all(np.isfinite(value) for _, value in self.parameters):
            raise ValueError("position-risk parameters differ from family schema")

    def parameter(self, name: str) -> float:
        return dict(self.parameters)[name]

    @property
    def candidate_id(self) -> str:
        if self.family == "trend_damage":
            return (
                f"trend_L{int(self.parameter('lookback'))}_"
                f"D{self.parameter('drawdown'):.2f}_P{self.pressure_position:.2f}"
            )
        if self.family == "negative_persistence":
            return (
                f"persist_K{int(self.parameter('window'))}_"
                f"N{int(self.parameter('negative_count'))}_P{self.pressure_position:.2f}"
            )
        return (
            f"intraday_C{self.parameter('close_location'):.2f}_"
            f"V{self.parameter('down_volume_share'):.2f}_P{self.pressure_position:.2f}"
        )


def build_family_specs(family: str) -> tuple[RiskOverlaySpec, ...]:
    """Build one exact preregistered candidate family."""
    if family == "trend_damage":
        return tuple(
            RiskOverlaySpec(
                family,
                position,
                (("lookback", float(lookback)), ("drawdown", float(drawdown))),
            )
            for lookback, drawdown, position in product(
                (20, 60, 120), (0.06, 0.10), (0.5, 0.75)
            )
        )
    if family == "negative_persistence":
        return tuple(
            RiskOverlaySpec(
                family,
                position,
                (("window", float(window)), ("negative_count", float(count))),
            )
            for (window, count), position in product(
                ((5, 4), (10, 7), (20, 13)), (0.5, 0.75)
            )
        )
    if family == "intraday_pressure":
        return tuple(
            RiskOverlaySpec(
                family,
                position,
                (
                    ("close_location", float(close_location)),
                    ("down_volume_share", float(volume_share)),
                ),
            )
            for close_location, volume_share, position in product(
                (0.25, 0.35), (0.60, 0.70), (0.5, 0.75)
            )
        )
    raise ValueError(f"unsupported position-risk family: {family}")


def _indexed(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    if "dt" not in frame.columns:
        raise ValueError(f"{name} must contain dt")
    normalized = frame.copy()
    normalized["dt"] = pd.to_datetime(normalized["dt"])
    normalized = normalized.set_index("dt").sort_index()
    if not normalized.index.is_unique:
        raise ValueError(f"{name} dates must be unique")
    return normalized


def _validate_prices(frame: pd.DataFrame, name: str) -> None:
    required = {"open", "high", "low", "close", "vol"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{name} missing columns {sorted(missing)}")
    prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
    volume = frame["vol"].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0.0).any():
        raise ValueError(f"{name} prices must be positive and finite")
    if not np.isfinite(volume).all() or (volume < 0.0).any():
        raise ValueError(f"{name} volume must be non-negative and finite")
    if (
        (frame["high"] < frame[["open", "close"]].max(axis=1)).any()
        or (frame["low"] > frame[["open", "close"]].min(axis=1)).any()
        or (frame["high"] < frame["low"]).any()
    ):
        raise ValueError(f"{name} OHLC bounds are invalid")


def build_risk_features(daily: pd.DataFrame, intraday: pd.DataFrame) -> pd.DataFrame:
    """Build causal daily features from completed daily and intraday bars."""
    daily_indexed = _indexed(daily, "daily")
    intraday_indexed = _indexed(intraday, "intraday")
    _validate_prices(daily_indexed, "daily")
    _validate_prices(intraday_indexed, "intraday")

    intraday_work = intraday_indexed.copy()
    intraday_work["session"] = intraday_work.index.normalize()
    intraday_work["down_vol"] = intraday_work["vol"].where(
        intraday_work["close"] < intraday_work["open"], 0.0
    )
    grouped = intraday_work.groupby("session", sort=True).agg(
        total_volume=("vol", "sum"), down_volume=("down_vol", "sum")
    )
    required_sessions = daily_indexed.index.normalize()
    missing_sessions = required_sessions.difference(grouped.index)
    if not missing_sessions.empty:
        raise ValueError("intraday data missing daily sessions")

    features = pd.DataFrame(index=daily_indexed.index)
    features["close"] = daily_indexed["close"].astype(float)
    features["ret_1d"] = features["close"].pct_change()
    for window in (5, 20):
        features[f"sma_{window}"] = features["close"].rolling(window).mean()
    for lookback in (20, 60, 120):
        features[f"prior_high_{lookback}"] = (
            daily_indexed["high"].shift(1).rolling(lookback).max()
        )
    negative = features["ret_1d"].lt(0.0).astype(float)
    for window in (5, 10, 20):
        features[f"negative_count_{window}"] = negative.rolling(window).sum()

    spread = daily_indexed["high"] - daily_indexed["low"]
    features["close_location"] = np.where(
        spread.gt(0.0),
        (daily_indexed["close"] - daily_indexed["low"]) / spread,
        0.5,
    )
    aligned = grouped.reindex(required_sessions)
    aligned.index = daily_indexed.index
    features["down_volume_share"] = np.where(
        aligned["total_volume"].gt(0.0),
        aligned["down_volume"] / aligned["total_volume"],
        0.0,
    )
    return features


def _validate_state_inputs(features: pd.DataFrame, champion_target: pd.Series) -> None:
    if not features.index.equals(champion_target.index):
        raise ValueError("risk features and champion target must share an index")
    if not features.index.is_monotonic_increasing or features.index.has_duplicates:
        raise ValueError("risk feature index must be unique and increasing")
    if champion_target.isna().any() or not champion_target.astype(float).isin([0.0, 1.0]).all():
        raise ValueError("champion target must be binary and complete")


def build_pressure_state(
    features: pd.DataFrame,
    champion_target: pd.Series,
    spec: RiskOverlaySpec,
) -> pd.Series:
    """Apply one causal, stateful pressure rule to a champion path."""
    _validate_state_inputs(features, champion_target)
    pressure = pd.Series(False, index=features.index, name="pressure")
    active = False
    non_pressure_days = 0
    for location, signal_date in enumerate(features.index):
        if float(champion_target.loc[signal_date]) == 0.0:
            active = False
            non_pressure_days = 0
            continue
        row = features.loc[signal_date]
        below_sma20 = bool(
            pd.notna(row.get("sma_20")) and float(row["close"]) < float(row["sma_20"])
        )
        if spec.family == "trend_damage":
            lookback = int(spec.parameter("lookback"))
            prior_high = row.get(f"prior_high_{lookback}")
            trigger = bool(
                below_sma20
                and pd.notna(prior_high)
                and float(row["close"]) / float(prior_high) - 1.0
                <= -spec.parameter("drawdown")
            )
            if active and pd.notna(row.get("sma_20")) and float(row["close"]) >= float(row["sma_20"]):
                active = False
            elif not active and trigger:
                active = True
        elif spec.family == "negative_persistence":
            window = int(spec.parameter("window"))
            count = row.get(f"negative_count_{window}")
            trigger = bool(
                below_sma20
                and pd.notna(count)
                and float(count) >= spec.parameter("negative_count")
            )
            two_positive = bool(
                location >= 1
                and float(features.iloc[location]["ret_1d"]) > 0.0
                and float(features.iloc[location - 1]["ret_1d"]) > 0.0
            )
            recovered = bool(
                pd.notna(row.get("sma_5"))
                and float(row["close"]) >= float(row["sma_5"])
                and two_positive
            )
            if active and recovered:
                active = False
            elif not active and trigger:
                active = True
        else:
            trigger = bool(
                below_sma20
                and float(row["close_location"])
                <= spec.parameter("close_location")
                and float(row["down_volume_share"])
                >= spec.parameter("down_volume_share")
            )
            if active:
                non_pressure_days = 0 if trigger else non_pressure_days + 1
                recovered = bool(
                    non_pressure_days >= 2
                    and pd.notna(row.get("sma_5"))
                    and float(row["close"]) >= float(row["sma_5"])
                )
                if recovered:
                    active = False
                    non_pressure_days = 0
            elif trigger:
                active = True
                non_pressure_days = 0
        pressure.loc[signal_date] = active
    return pressure


def compose_overlay_target(
    champion_target: pd.Series,
    pressure: pd.Series,
    pressure_position: float,
) -> pd.Series:
    """Reduce only active champion exposure while a pressure state is active."""
    if not champion_target.index.equals(pressure.index):
        raise ValueError("champion target and pressure must share an index")
    if pressure.isna().any() or pressure_position not in {0.5, 0.75}:
        raise ValueError("invalid pressure path or position")
    champion = champion_target.astype(float)
    if not champion.isin([0.0, 1.0]).all():
        raise ValueError("champion target must be binary")
    multiplier = np.where(pressure.astype(bool), pressure_position, 1.0)
    return pd.Series(champion.to_numpy() * multiplier, index=champion.index, name="target_position")


def build_position_risk_events(
    target_position: pd.Series,
    champion_target: pd.Series,
    scores: pd.Series,
    features: pd.DataFrame,
    pressure: pd.Series,
    spec: RiskOverlaySpec,
) -> pd.DataFrame:
    """Describe every champion or position-risk transition with provenance."""
    inputs = (champion_target, scores, pressure)
    if any(not target_position.index.equals(values.index) for values in inputs):
        raise ValueError("position-risk event inputs must share an index")
    if not target_position.index.equals(features.index):
        raise ValueError("position-risk features must share the target index")
    target = target_position.astype(float)
    if not target.isin([0.0, spec.pressure_position, 1.0]).all():
        raise ValueError("unsupported position-risk target")
    previous = target.shift(1, fill_value=0.0)
    rows: list[dict[str, object]] = []
    for signal_date in target.index[target.ne(previous)]:
        before = float(previous.loc[signal_date])
        after = float(target.loc[signal_date])
        if before == 0.0 and after > 0.0:
            event_type, reason = "Entry", "champion entry"
        elif 0.0 < before < after:
            event_type, reason = "Increase", "risk pressure cleared"
        elif before > after > 0.0:
            event_type, reason = "Reduce", "risk pressure activated"
        elif before > 0.0 and after == 0.0:
            event_type, reason = "Exit", "champion exit"
        else:
            raise ValueError(f"unsupported position transition {before} -> {after}")
        feature_row = features.loc[signal_date]
        rows.append(
            {
                "event_id": f"PositionRisk:{spec.candidate_id}:{pd.Timestamp(signal_date):%Y%m%d}:{event_type}",
                "signal_date": pd.Timestamp(signal_date),
                "event_type": event_type,
                "factor_score": float(scores.loc[signal_date]),
                "family": spec.family,
                "candidate_id": spec.candidate_id,
                "pressure_position": spec.pressure_position,
                "pressure": bool(pressure.loc[signal_date]),
                "baseline_position": float(champion_target.loc[signal_date]),
                "before_position": before,
                "after_position": after,
                "close": float(feature_row["close"]),
                "sma_5": float(feature_row["sma_5"]),
                "sma_20": float(feature_row["sma_20"]),
                "close_location": float(feature_row["close_location"]),
                "down_volume_share": float(feature_row["down_volume_share"]),
                "reason": reason,
            }
        )
    return pd.DataFrame(rows)
