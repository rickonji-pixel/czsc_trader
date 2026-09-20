"""Fetch, validate, and safely publish flat A-share market data."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from functools import partial
import json
from pathlib import Path
import re
import shutil
from typing import Any, TypeAlias

import pandas as pd
from dataflows.errors import DataContractError
from dataflows.history_validation import (
    inspect_ohlcv_frame,
    validate_market_frames as dfls_validate_market_frames,
)

from .identity import raw_file_sha256
from .temp_workspace import create_temporary_directory

FREQUENCIES = ("30m", "daily", "weekly")
VENDOR_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Volume", "Amount")

MarketFetcher: TypeAlias = Callable[
    [str, str, date, date, str], tuple[pd.DataFrame, dict[str, Any]]
]
ExecutionPriceFetcher: TypeAlias = Callable[
    [str, str, date, date], tuple[pd.DataFrame, dict[str, Any]]
]
InstrumentNameFetcher: TypeAlias = Callable[[str, str], str]
CalendarFetcher: TypeAlias = Callable[[date], tuple[date, dict[str, str]]]
SessionCalendarFetcher: TypeAlias = Callable[
    [date, date], tuple[pd.DataFrame, dict[str, object]]
]


def _normalize_symbol(symbol: str) -> tuple[str, str]:
    value = str(symbol).strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ)", value)
    if not match:
        raise ValueError("symbol must be a six-digit A-share code ending in SH or SZ")
    return value, match.group(1)


def _normalize_frame(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    frequency = "daily" if name == "execution daily" else name
    try:
        inspect_ohlcv_frame(
            frame,
            frequency,
            require_complete_days=frequency == "30m",
        ).require_pass()
    except DataContractError as exc:
        raise ValueError(str(exc)) from exc
    normalized = frame.loc[:, VENDOR_COLUMNS].copy()
    normalized["Date"] = pd.to_datetime(normalized["Date"])
    for column in VENDOR_COLUMNS[1:]:
        normalized[column] = pd.to_numeric(normalized[column])
    return normalized.reset_index(drop=True)


def validate_market_frames(
    intraday: pd.DataFrame,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    execution_daily: pd.DataFrame | None = None,
    trading_calendar: pd.DataFrame | None = None,
    *,
    expected_start: date | str | pd.Timestamp | None = None,
) -> dict[str, object]:
    """Delegate market-series validation to DFLS."""

    try:
        return dfls_validate_market_frames(
            intraday,
            daily,
            weekly,
            execution_daily,
            trading_calendar,
            expected_start=expected_start,
        )
    except DataContractError as exc:
        for finding in exc.context.get("findings", []):
            if finding.get("code") == "CALENDAR_COVERAGE_MISMATCH":
                context = finding.get("context", {})
                raise ValueError(
                    "daily/trading calendar reconciliation: trade dates differ; "
                    f"missing={context.get('missing', [])}, "
                    f"unexpected={context.get('unexpected', [])}"
                ) from exc
        raise ValueError(str(exc)) from exc


def _default_fetcher(
    symbol: str,
    asset_type: str,
    start: date,
    end: date,
    period: str,
    *,
    env_file: str | Path | None = None,
) -> tuple[pd.DataFrame, dict[str, str]]:
    if asset_type == "stock":
        from dataflows.tushare_stock import fetch_stock_ohlcv

        return fetch_stock_ohlcv(
            symbol, start.isoformat(), end.isoformat(), period, env_file=env_file
        )
    from dataflows.tushare_etf import fetch_etf_ohlcv

    return fetch_etf_ohlcv(
        symbol, start.isoformat(), end.isoformat(), period, env_file=env_file
    )


def _default_execution_price_fetcher(
    symbol: str,
    asset_type: str,
    start: date,
    end: date,
    *,
    env_file: str | Path | None = None,
) -> tuple[pd.DataFrame, dict[str, str]]:
    if asset_type == "stock":
        from dataflows.tushare_stock import fetch_stock_unadjusted_daily

        return fetch_stock_unadjusted_daily(
            symbol, start.isoformat(), end.isoformat(), env_file=env_file
        )
    from dataflows.tushare_etf import fetch_etf_unadjusted_daily

    return fetch_etf_unadjusted_daily(
        symbol, start.isoformat(), end.isoformat(), env_file=env_file
    )


def _default_calendar_fetcher(
    after: date, *, env_file: str | Path | None = None
) -> tuple[date, dict[str, str]]:
    from dataflows.tushare_common import fetch_next_trading_session

    return fetch_next_trading_session(after, env_file=env_file)


def _default_session_calendar_fetcher(
    start: date,
    end: date,
    *,
    env_file: str | Path | None = None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    from dataflows.tushare_strategy_data import fetch_trading_calendar

    return fetch_trading_calendar(
        "SSE",
        start.isoformat(),
        end.isoformat(),
        env_file=env_file,
    )


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
    execution_fetcher: ExecutionPriceFetcher | None = None,
    name_fetcher: InstrumentNameFetcher | None = None,
    calendar_fetcher: CalendarFetcher | None = None,
    session_calendar_fetcher: SessionCalendarFetcher | None = None,
    env_file: str | Path | None = None,
) -> dict[str, object]:
    """Fetch and publish one fully validated flat market-data generation."""
    normalized_symbol, code = _normalize_symbol(symbol)
    normalized_asset = str(asset_type).strip().lower()
    if normalized_asset not in {"stock", "etf"}:
        raise ValueError("asset_type must be stock or etf")
    if start > end:
        raise ValueError("start must not be after end")
    if name_fetcher is None:
        from dataflows.tushare_common import fetch_instrument_name

        effective_name_fetcher = partial(fetch_instrument_name, env_file=env_file)
    else:
        effective_name_fetcher = name_fetcher
    instrument_name = str(
        effective_name_fetcher(normalized_symbol, normalized_asset)
    ).strip()
    if not instrument_name:
        raise ValueError("instrument name must not be empty")
    effective_fetcher = fetcher or partial(_default_fetcher, env_file=env_file)
    frames: dict[str, pd.DataFrame] = {}
    metadata: dict[str, dict[str, Any]] = {}
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
    effective_execution_fetcher = execution_fetcher or partial(
        _default_execution_price_fetcher, env_file=env_file
    )
    execution_frame, execution_metadata = effective_execution_fetcher(
        normalized_symbol, normalized_asset, start, end
    )
    execution_frame = _normalize_frame(execution_frame, "execution daily")
    last_session = pd.Timestamp(execution_frame["Date"].max()).date()
    effective_calendar_fetcher = calendar_fetcher or partial(
        _default_calendar_fetcher, env_file=env_file
    )
    next_session, calendar_metadata = effective_calendar_fetcher(last_session)
    if next_session <= last_session:
        raise ValueError("next trading session must be after the latest complete close")
    if next_session <= end:
        raise ValueError(
            f"latest complete close {last_session} is behind requested end {end}"
        )
    if execution_metadata.get("vendor_symbol") != normalized_symbol:
        raise ValueError("execution daily: vendor symbol does not match request")
    if execution_metadata.get("asset_type") != normalized_asset:
        raise ValueError("execution daily: asset type does not match request")
    if execution_metadata.get("period") != "daily":
        raise ValueError("execution daily: period must be daily")
    if execution_metadata.get("adjustment") != "none":
        raise ValueError("execution daily: prices must be unadjusted")
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
    normalized_daily = _normalize_frame(frames["daily"], "daily")
    observed_end = pd.Timestamp(normalized_daily["Date"].max()).date()
    effective_session_calendar_fetcher = session_calendar_fetcher or partial(
        _default_session_calendar_fetcher, env_file=env_file
    )
    session_calendar, session_calendar_metadata = effective_session_calendar_fetcher(
        start, observed_end
    )
    validation = validate_market_frames(
        frames["30m"],
        frames["daily"],
        frames["weekly"],
        execution_frame,
        session_calendar,
        expected_start=start,
    )

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    staging = create_temporary_directory(
        data_dir, "market-data", prefix=f"{code.lower()}-"
    )
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
                    "sha256": raw_file_sha256(path),
                }
        execution_records: dict[str, dict[str, object]] = {}
        execution_output = _csv_frame(execution_frame, "daily")
        execution_years = pd.to_datetime(execution_output["date"]).dt.year
        for year in sorted(execution_years.unique()):
            yearly = execution_output.loc[execution_years == year].reset_index(drop=True)
            filename = f"{code}_execution_daily_{int(year)}.csv"
            path = staging / filename
            yearly.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")
            timestamps = pd.to_datetime(yearly["date"])
            execution_records[filename] = {
                "frequency": "daily",
                "year": int(year),
                "rows": int(len(yearly)),
                "first": timestamps.min().isoformat(),
                "last": timestamps.max().isoformat(),
                "sha256": raw_file_sha256(path),
            }
        generated_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "schema_version": 2 if adjustment is not None else 1,
            "symbol": normalized_symbol,
            "name": instrument_name,
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
        execution_manifest = {
            "schema_version": 2,
            "symbol": normalized_symbol,
            "name": instrument_name,
            "code": code,
            "asset_type": normalized_asset,
            "vendor": execution_metadata["vendor"],
            "adjustment": "none",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "next_trading_session": next_session.isoformat(),
            "calendar": calendar_metadata,
            "session_calendar": session_calendar_metadata,
            "generated_at_utc": generated_at,
            "files": execution_records,
            "fetch_metadata": execution_metadata,
        }
        (staging / f"{code}_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (staging / f"{code}_validation.json").write_text(
            json.dumps({**validation, "generated_at_utc": generated_at}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        (staging / f"{code}_execution_manifest.json").write_text(
            json.dumps(execution_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        candidates = [
            *[staging / filename for filename in file_records],
            *[staging / filename for filename in execution_records],
            staging / f"{code}_manifest.json",
            staging / f"{code}_validation.json",
            staging / f"{code}_execution_manifest.json",
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
        "data_cutoff": last_session.isoformat(),
        "manifest": str((data_dir / f"{code}_manifest.json").resolve()),
        "execution_price_manifest": str(
            (data_dir / f"{code}_execution_manifest.json").resolve()
        ),
        "files": sorted(file_records),
        "execution_price_files": sorted(execution_records),
    }
