"""Immutable loading and validation for the local 588080 K-line files."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

import pandas as pd


SYMBOL = "588080.SH"
YEARS = (2024, 2025, 2026)
FREQUENCIES = ("30m", "daily", "weekly")
NORMALIZED_COLUMNS = ("dt", "symbol", "open", "high", "low", "close", "vol", "amount")
SESSION_TIMES = ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")


@dataclass(frozen=True)
class MarketData:
    """Validated K-lines at the three supplied frequencies."""

    intraday: pd.DataFrame
    daily: pd.DataFrame
    weekly: pd.DataFrame
    hashes: dict[str, str]

    def truncate(self, cutoff: pd.Timestamp | str) -> "MarketData":
        """Return a causal view containing no bar after ``cutoff`` day."""
        cutoff_ts = pd.Timestamp(cutoff).normalize()
        intraday = self.intraday.loc[self.intraday["dt"].dt.normalize() <= cutoff_ts].copy()
        daily = self.daily.loc[self.daily["dt"] <= cutoff_ts].copy()
        weekly = self.weekly.loc[self.weekly["dt"] <= cutoff_ts].copy()
        return MarketData(intraday, daily, weekly, self.hashes.copy())


def _required_paths(raw_dir: Path) -> list[Path]:
    paths = [raw_dir / f"588080_{freq}_{year}.csv" for freq in FREQUENCIES for year in YEARS]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing raw K-line files: {missing}")
    return paths


def _read_expected_hashes(raw_dir: Path) -> dict[str, str]:
    readme = raw_dir / "README.md"
    if not readme.is_file():
        return {}
    text = readme.read_text(encoding="utf-8")
    return dict(
        re.findall(
            r"`(588080_(?:30m|daily|weekly)_\d{4}\.csv)`.*?`([0-9A-F]{64})`",
            text,
        )
    )


def _read_one(path: Path, freq: str) -> pd.DataFrame:
    source_time = "datetime" if freq == "30m" else "date"
    frame = pd.read_csv(path)
    expected = {source_time, "open", "high", "low", "close", "volume", "amount"}
    if set(frame.columns) != expected:
        raise ValueError(f"{path.name}: columns {list(frame.columns)} do not match {sorted(expected)}")
    frame = frame.rename(columns={source_time: "dt", "volume": "vol"})
    frame["dt"] = pd.to_datetime(frame["dt"])
    frame.insert(1, "symbol", SYMBOL)
    return frame[list(NORMALIZED_COLUMNS)]


def _validate_frame(frame: pd.DataFrame, name: str) -> None:
    if frame.empty:
        raise ValueError(f"{name}: no rows")
    if frame.isna().any().any():
        raise ValueError(f"{name}: null values found")
    if not frame["dt"].is_monotonic_increasing:
        raise ValueError(f"{name}: timestamps are not increasing")
    if frame["dt"].duplicated().any():
        raise ValueError(f"{name}: duplicate timestamps found")
    prices = frame[["open", "high", "low", "close"]]
    if (prices <= 0).any().any():
        raise ValueError(f"{name}: non-positive price found")
    if (frame["high"] < frame[["open", "close"]].max(axis=1)).any():
        raise ValueError(f"{name}: high below open or close")
    if (frame["low"] > frame[["open", "close"]].min(axis=1)).any():
        raise ValueError(f"{name}: low above open or close")
    if (frame[["vol", "amount"]] < 0).any().any():
        raise ValueError(f"{name}: negative volume or amount")


def _validate_reconciliation(intraday: pd.DataFrame, daily: pd.DataFrame, weekly: pd.DataFrame) -> None:
    sessions = intraday["dt"].dt.strftime("%H:%M")
    if tuple(sorted(sessions.unique())) != SESSION_TIMES:
        raise ValueError("30m: unexpected session timestamps")
    trade_dates = intraday["dt"].dt.normalize()
    if not intraday.groupby(trade_dates).size().eq(8).all():
        raise ValueError("30m: each trade date must contain eight bars")

    intraday_daily = intraday.assign(date=trade_dates).groupby("date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        vol=("vol", "sum"),
        amount=("amount", "sum"),
    )
    daily_indexed = daily.set_index("dt")
    if not intraday_daily.index.equals(daily_indexed.index):
        raise ValueError("30m/daily: trade dates differ")
    price_columns = ["open", "high", "low", "close"]
    if not intraday_daily[price_columns].equals(daily_indexed[price_columns]):
        raise ValueError("30m/daily: OHLC values differ")
    for column in ("vol", "amount"):
        relative = (intraday_daily[column] - daily_indexed[column]).abs() / daily_indexed[column]
        if relative.max() >= 1e-6:
            raise ValueError(f"30m/daily: {column} relative difference exceeds tolerance")

    daily_with_week = daily.assign(_week=daily["dt"].dt.to_period("W-SUN"))
    aggregated_weekly = daily_with_week.groupby("_week").agg(
        dt=("dt", "max"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        vol=("vol", "sum"),
        amount=("amount", "sum"),
    ).set_index("dt")
    weekly_indexed = weekly.set_index("dt")
    columns = ["open", "high", "low", "close", "vol", "amount"]
    if not aggregated_weekly[columns].equals(weekly_indexed[columns]):
        raise ValueError("daily/weekly: aggregated values differ")


def load_market_data(raw_dir: Path) -> MarketData:
    """Load, normalize, hash, and fully reconcile the supplied raw K-lines."""
    raw_dir = Path(raw_dir)
    paths = _required_paths(raw_dir)
    expected_hashes = _read_expected_hashes(raw_dir)
    hashes: dict[str, str] = {}
    grouped: dict[str, list[pd.DataFrame]] = {freq: [] for freq in FREQUENCIES}

    for path in paths:
        freq = path.stem.split("_")[1]
        digest = sha256(path.read_bytes()).hexdigest().upper()
        if expected_hashes and expected_hashes.get(path.name) != digest:
            raise ValueError(f"{path.name}: SHA-256 differs from README")
        hashes[path.name] = digest
        grouped[freq].append(_read_one(path, freq))

    intraday = pd.concat(grouped["30m"], ignore_index=True).sort_values("dt").reset_index(drop=True)
    daily = pd.concat(grouped["daily"], ignore_index=True).sort_values("dt").reset_index(drop=True)
    weekly = pd.concat(grouped["weekly"], ignore_index=True).sort_values("dt").reset_index(drop=True)
    for name, frame in (("30m", intraday), ("daily", daily), ("weekly", weekly)):
        _validate_frame(frame, name)
    _validate_reconciliation(intraday, daily, weekly)
    return MarketData(intraday, daily, weekly, hashes)
