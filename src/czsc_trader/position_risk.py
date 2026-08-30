"""Causal, interpretable position-risk features for 588080 research."""

from __future__ import annotations

import numpy as np
import pandas as pd


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
