"""Fetch, validate, and safely publish flat A-share market data."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import TypeAlias

import numpy as np
import pandas as pd

from .data import (
    AMOUNT_RELATIVE_TOLERANCE,
    FLOAT_COMPARISON_EPSILON,
    PRICE_TOLERANCE,
    VOLUME_RELATIVE_TOLERANCE,
)


FREQUENCIES = ("30m", "daily", "weekly")
SESSION_TIMES = ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")
VENDOR_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Volume", "Amount")

MarketFetcher: TypeAlias = Callable[
    [str, str, date, date, str], tuple[pd.DataFrame, dict[str, str]]
]


def _normalize_symbol(symbol: str) -> tuple[str, str]:
    value = str(symbol).strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ)", value)
    if not match:
        raise ValueError("symbol must be a six-digit A-share code ending in SH or SZ")
    return value, match.group(1)


def _normalize_frame(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    missing = sorted(set(VENDOR_COLUMNS).difference(frame.columns))
    if missing:
        raise ValueError(f"{name}: missing columns {missing}")
    normalized = frame.loc[:, VENDOR_COLUMNS].copy()
    normalized["Date"] = pd.to_datetime(normalized["Date"], errors="coerce")
    if normalized["Date"].isna().any():
        raise ValueError(f"{name}: invalid timestamps")
    for column in VENDOR_COLUMNS[1:]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    if normalized.isna().any().any():
        raise ValueError(f"{name}: null or non-numeric values")
    if normalized["Date"].duplicated().any():
        raise ValueError(f"{name}: duplicate timestamps")
    if not normalized["Date"].is_monotonic_increasing:
        raise ValueError(f"{name}: timestamps are not increasing")
    prices = normalized[["Open", "High", "Low", "Close"]]
    invalid_ohlc = (
        (prices <= 0).any(axis=1)
        | (normalized["High"] < normalized[["Open", "Close"]].max(axis=1))
        | (normalized["Low"] > normalized[["Open", "Close"]].min(axis=1))
        | (normalized["High"] < normalized["Low"])
        | (normalized[["Volume", "Amount"]] < 0).any(axis=1)
    )
    if invalid_ohlc.any():
        raise ValueError(f"{name}: invalid OHLCV relationships")
    return normalized.reset_index(drop=True)


def _relative_match(left: pd.Series, right: pd.Series, tolerance: float) -> bool:
    denominator = np.maximum(np.abs(right.to_numpy(dtype=float)), 1.0)
    relative = np.abs(left.to_numpy(dtype=float) - right.to_numpy(dtype=float)) / denominator
    return bool(np.all(relative <= tolerance))


def validate_market_frames(
    intraday: pd.DataFrame,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
) -> dict[str, object]:
    """Validate complete A-share sessions and reconcile all three frequencies."""
    intraday = _normalize_frame(intraday, "30m")
    daily = _normalize_frame(daily, "daily")
    weekly = _normalize_frame(weekly, "weekly")

    times = tuple(sorted(intraday["Date"].dt.strftime("%H:%M").unique()))
    if times != tuple(sorted(SESSION_TIMES)):
        raise ValueError(f"30m: unexpected session times {times}")
    trade_dates = intraday["Date"].dt.normalize()
    counts = intraday.groupby(trade_dates).size()
    if not counts.eq(8).all():
        details = ", ".join(f"{day.date()}={count}" for day, count in counts.items() if count != 8)
        raise ValueError(f"30m: each historical trade date must contain eight bars; {details}")

    intraday_daily = (
        intraday.assign(_date=trade_dates)
        .groupby("_date", sort=True)
        .agg(
            Open=("Open", "first"),
            High=("High", "max"),
            Low=("Low", "min"),
            Close=("Close", "last"),
            Volume=("Volume", "sum"),
            Amount=("Amount", "sum"),
        )
    )
    daily_indexed = daily.assign(_date=daily["Date"].dt.normalize()).set_index("_date")
    if not intraday_daily.index.equals(daily_indexed.index):
        raise ValueError("30m/daily reconciliation: trade dates differ")
    for column in ("Open", "High", "Low", "Close"):
        if not np.allclose(
            intraday_daily[column],
            daily_indexed[column],
            rtol=0.0,
            atol=PRICE_TOLERANCE + FLOAT_COMPARISON_EPSILON,
        ):
            raise ValueError(f"30m/daily reconciliation: {column} differs")
    if not _relative_match(
        intraday_daily["Volume"], daily_indexed["Volume"], VOLUME_RELATIVE_TOLERANCE
    ):
        raise ValueError("30m/daily reconciliation: Volume differs")
    if not _relative_match(
        intraday_daily["Amount"], daily_indexed["Amount"], AMOUNT_RELATIVE_TOLERANCE
    ):
        raise ValueError("30m/daily reconciliation: Amount differs")

    daily_weekly = (
        daily.assign(_week=daily["Date"].dt.to_period("W-SUN"))
        .groupby("_week", sort=True)
        .agg(
            Date=("Date", "max"),
            Open=("Open", "first"),
            High=("High", "max"),
            Low=("Low", "min"),
            Close=("Close", "last"),
            Volume=("Volume", "sum"),
            Amount=("Amount", "sum"),
        )
        .set_index("Date")
    )
    weekly_indexed = weekly.set_index("Date")
    if not daily_weekly.index.equals(weekly_indexed.index):
        raise ValueError("daily/weekly reconciliation: week-ending trade dates differ")
    for column in ("Open", "High", "Low", "Close"):
        if not np.allclose(
            daily_weekly[column],
            weekly_indexed[column],
            rtol=0.0,
            atol=PRICE_TOLERANCE + FLOAT_COMPARISON_EPSILON,
        ):
            raise ValueError(f"daily/weekly reconciliation: {column} differs")
    if not _relative_match(
        daily_weekly["Volume"], weekly_indexed["Volume"], VOLUME_RELATIVE_TOLERANCE
    ):
        raise ValueError("daily/weekly reconciliation: Volume differs")
    if not _relative_match(
        daily_weekly["Amount"], weekly_indexed["Amount"], AMOUNT_RELATIVE_TOLERANCE
    ):
        raise ValueError("daily/weekly reconciliation: Amount differs")

    return {
        "status": "PASS",
        "intraday": {
            "bar_count": int(len(intraday)),
            "complete_day_count": int(len(counts)),
            "session_times": list(SESSION_TIMES),
        },
        "reconciliation": {
            "daily_matched_days": int(len(intraday_daily)),
            "weekly_matched_periods": int(len(daily_weekly)),
            "price_tolerance": PRICE_TOLERANCE,
            "volume_relative_tolerance": VOLUME_RELATIVE_TOLERANCE,
            "amount_relative_tolerance": AMOUNT_RELATIVE_TOLERANCE,
        },
    }


def _default_fetcher(
    symbol: str,
    asset_type: str,
    start: date,
    end: date,
    period: str,
) -> tuple[pd.DataFrame, dict[str, str]]:
    if asset_type == "stock":
        from dataflows.tushare_stock import fetch_stock_ohlcv

        return fetch_stock_ohlcv(symbol, start.isoformat(), end.isoformat(), period)
    from dataflows.tushare_etf import fetch_etf_ohlcv

    return fetch_etf_ohlcv(symbol, start.isoformat(), end.isoformat(), period)


def _csv_frame(frame: pd.DataFrame, period: str) -> pd.DataFrame:
    source = _normalize_frame(frame, period).rename(
        columns={
            "Date": "datetime" if period == "30m" else "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
            "Amount": "amount",
        }
    )
    time_column = "datetime" if period == "30m" else "date"
    source[time_column] = source[time_column].dt.strftime(
        "%Y-%m-%d %H:%M:%S" if period == "30m" else "%Y-%m-%d"
    )
    return source


def prepare_market_data(
    symbol: str,
    asset_type: str,
    start: date,
    end: date,
    data_dir: Path,
    *,
    fetcher: MarketFetcher | None = None,
) -> dict[str, object]:
    """Fetch and publish one fully validated flat market-data generation."""
    normalized_symbol, code = _normalize_symbol(symbol)
    normalized_asset = str(asset_type).strip().lower()
    if normalized_asset not in {"stock", "etf"}:
        raise ValueError("asset_type must be stock or etf")
    if start > end:
        raise ValueError("start must not be after end")
    effective_fetcher = fetcher or _default_fetcher
    frames: dict[str, pd.DataFrame] = {}
    metadata: dict[str, dict[str, str]] = {}
    for period in FREQUENCIES:
        frame, item_metadata = effective_fetcher(
            normalized_symbol, normalized_asset, start, end, period
        )
        if item_metadata.get("vendor_symbol") != normalized_symbol:
            raise ValueError(f"{period}: vendor symbol does not match request")
        if item_metadata.get("asset_type") != normalized_asset:
            raise ValueError(f"{period}: asset type does not match request")
        frames[period] = frame
        metadata[period] = item_metadata
    adjusted_metadata = [
        item for item in metadata.values() if item.get("adjustment") is not None
    ]
    if adjusted_metadata and len(adjusted_metadata) != len(metadata):
        raise ValueError("all frequencies must use the same adjustment contract")
    adjustment_records = {
        (
            item.get("adjustment"),
            item.get("adjustment_factor_source"),
            item.get("adjustment_factor_sha256"),
        )
        for item in adjusted_metadata
    }
    adjustment: dict[str, str] | None = None
    if adjustment_records:
        if len(adjustment_records) != 1:
            raise ValueError("frequencies use inconsistent adjustment factors")
        mode, factor_source, factor_sha256 = adjustment_records.pop()
        if mode != "hfq" or not factor_source or not factor_sha256:
            raise ValueError("market data must declare a complete hfq adjustment contract")
        adjustment = {
            "mode": mode,
            "factor_source": factor_source,
            "factor_sha256": factor_sha256,
        }
    validation = validate_market_frames(frames["30m"], frames["daily"], frames["weekly"])

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{code}_staging_", dir=data_dir))
    published: list[Path] = []
    backups: dict[Path, Path] = {}
    try:
        file_records: dict[str, dict[str, object]] = {}
        for period in FREQUENCIES:
            output = _csv_frame(frames[period], period)
            time_column = "datetime" if period == "30m" else "date"
            years = pd.to_datetime(output[time_column]).dt.year
            for year in sorted(years.unique()):
                yearly = output.loc[years == year].reset_index(drop=True)
                filename = f"{code}_{period}_{int(year)}.csv"
                path = staging / filename
                yearly.to_csv(
                    path,
                    index=False,
                    encoding="utf-8-sig",
                    lineterminator="\n",
                )
                timestamps = pd.to_datetime(yearly[time_column])
                file_records[filename] = {
                    "frequency": period,
                    "year": int(year),
                    "rows": int(len(yearly)),
                    "first": timestamps.min().isoformat(),
                    "last": timestamps.max().isoformat(),
                    "sha256": sha256(path.read_bytes()).hexdigest(),
                }
        generated_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "schema_version": 2 if adjustment is not None else 1,
            "symbol": normalized_symbol,
            "code": code,
            "asset_type": normalized_asset,
            "vendor": "tushare",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "generated_at_utc": generated_at,
            "files": file_records,
            "fetch_metadata": metadata,
        }
        if adjustment is not None:
            manifest["adjustment"] = adjustment
        (staging / f"{code}_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (staging / f"{code}_validation.json").write_text(
            json.dumps({**validation, "generated_at_utc": generated_at}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        candidates = [
            *[staging / filename for filename in file_records],
            staging / f"{code}_manifest.json",
            staging / f"{code}_validation.json",
        ]
        backup_root = staging / "backups"
        backup_root.mkdir()
        for source in candidates:
            destination = data_dir / source.name
            if destination.exists():
                backup = backup_root / source.name
                destination.replace(backup)
                backups[destination] = backup
            source.replace(destination)
            published.append(destination)
    except Exception:
        for destination in reversed(published):
            if destination.exists():
                destination.unlink()
        for destination, backup in backups.items():
            if backup.exists():
                backup.replace(destination)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "symbol": normalized_symbol,
        "asset_type": normalized_asset,
        "validation_status": "PASS",
        "manifest": str((data_dir / f"{code}_manifest.json").resolve()),
        "files": sorted(file_records),
    }
