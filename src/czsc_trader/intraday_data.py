"""Validated high-frequency research data kept beside the core research pool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import partial
import json
from pathlib import Path
import re
import shutil
from typing import TypeAlias

import pandas as pd

from dataflows.bar_utils import (
    validate_a_share_intraday_bars,
    validate_intraday_against_daily,
)

from .identity import raw_file_sha256
from .temp_workspace import create_temporary_directory


INTRADAY_RESEARCH_FREQUENCIES = ("15m", "5m", "1m")
VENDOR_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Volume", "Amount")
IntradayFetcher: TypeAlias = Callable[
    [str, date, date, str], tuple[pd.DataFrame, dict[str, str]]
]


@dataclass(frozen=True)
class IntradayResearchData:
    symbol: str
    frames: dict[str, pd.DataFrame]
    manifest: dict[str, object]
    hashes: dict[str, str]


def _symbol_parts(symbol: str) -> tuple[str, str]:
    normalized = str(symbol).strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ)", normalized)
    if not match:
        raise ValueError("symbol must be a six-digit A-share code ending in SH or SZ")
    return normalized, match.group(1)


def _normalize_frame(frame: pd.DataFrame, period: str) -> pd.DataFrame:
    missing = sorted(set(VENDOR_COLUMNS).difference(frame.columns))
    if missing:
        raise ValueError(f"{period}: missing columns {missing}")
    normalized = frame.loc[:, VENDOR_COLUMNS].copy()
    normalized["Date"] = pd.to_datetime(normalized["Date"], errors="coerce")
    if normalized["Date"].isna().any() or normalized["Date"].duplicated().any():
        raise ValueError(f"{period}: invalid or duplicate timestamps")
    if not normalized["Date"].is_monotonic_increasing:
        raise ValueError(f"{period}: timestamps are not increasing")
    for column in VENDOR_COLUMNS[1:]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    if normalized.isna().any().any():
        raise ValueError(f"{period}: null or non-numeric values")
    return normalized.reset_index(drop=True)


def _default_fetcher(
    symbol: str,
    start: date,
    end: date,
    period: str,
    *,
    env_file: str | Path | None = None,
) -> tuple[pd.DataFrame, dict[str, str]]:
    from dataflows.tushare_etf import fetch_etf_ohlcv

    return fetch_etf_ohlcv(
        symbol,
        start.isoformat(),
        end.isoformat(),
        period,
        env_file=env_file,
    )


def _csv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.rename(
        columns={
            "Date": "datetime",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
            "Amount": "amount",
        }
    ).copy()
    result["datetime"] = pd.to_datetime(result["datetime"]).dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return result


def _aggregate_1m_days(
    one_minute: pd.DataFrame,
    period: str,
    dates: set[pd.Timestamp],
) -> pd.DataFrame:
    """Derive missing 5m/15m sessions from complete one-minute source bars."""

    minutes = {"5m": 5, "15m": 15}[period]
    pieces: list[pd.DataFrame] = []
    timestamps = one_minute["Date"].dt.normalize()
    for trade_date in sorted(dates):
        day = one_minute.loc[timestamps == trade_date].reset_index(drop=True)
        expected = 240
        if len(day) != expected:
            raise ValueError(
                f"{trade_date.date()}: cannot derive {period} from {len(day)} 1m bars"
            )
        sessions = (day.iloc[:120], day.iloc[120:])
        for session in sessions:
            if len(session) != 120 or len(session) % minutes:
                raise ValueError(f"{trade_date.date()}: invalid 1m session structure")
            group_ids = pd.Series(range(len(session)), index=session.index) // minutes
            aggregated = session.groupby(group_ids, sort=True).agg(
                Date=("Date", "last"),
                Open=("Open", "first"),
                High=("High", "max"),
                Low=("Low", "min"),
                Close=("Close", "last"),
                Volume=("Volume", "sum"),
                Amount=("Amount", "sum"),
            )
            pieces.append(aggregated.reset_index(drop=True))
    if not pieces:
        return pd.DataFrame(columns=VENDOR_COLUMNS)
    return pd.concat(pieces, ignore_index=True).sort_values("Date").reset_index(drop=True)


def prepare_intraday_research_data(
    symbol: str,
    start: date,
    end: date,
    data_dir: Path,
    *,
    fetcher: IntradayFetcher | None = None,
    env_file: str | Path | None = None,
) -> dict[str, object]:
    """Fetch, reconcile and atomically publish 15m/5m/1m ETF research bars."""

    normalized_symbol, code = _symbol_parts(symbol)
    if start > end:
        raise ValueError("start must not be after end")
    effective_fetcher = fetcher or partial(_default_fetcher, env_file=env_file)
    daily_raw, daily_metadata = effective_fetcher(normalized_symbol, start, end, "daily")
    daily = _normalize_frame(daily_raw, "daily")
    frames: dict[str, pd.DataFrame] = {}
    metadata: dict[str, dict[str, str]] = {}
    factor_hashes = {str(daily_metadata.get("adjustment_factor_sha256", ""))}
    for period in INTRADAY_RESEARCH_FREQUENCIES:
        raw, item_metadata = effective_fetcher(normalized_symbol, start, end, period)
        if item_metadata.get("vendor_symbol") != normalized_symbol:
            raise ValueError(f"{period}: vendor symbol does not match request")
        if item_metadata.get("asset_type") != "etf":
            raise ValueError(f"{period}: asset type must be etf")
        frame = _normalize_frame(raw, period)
        validate_a_share_intraday_bars(frame, period, require_complete_days=True)
        frames[period] = frame
        metadata[period] = item_metadata
        factor_hashes.add(str(item_metadata.get("adjustment_factor_sha256", "")))

    daily_dates = set(daily["Date"].dt.normalize())
    base_dates = set(frames["1m"]["Date"].dt.normalize())
    if base_dates != daily_dates:
        missing = sorted(item.date().isoformat() for item in daily_dates - base_dates)
        extra = sorted(item.date().isoformat() for item in base_dates - daily_dates)
        raise ValueError(
            "1m and daily frequencies cover different trading dates; "
            f"missing={missing[:20]}, extra={extra[:20]}"
        )
    derived_repairs: dict[str, list[str]] = {}
    for period in ("15m", "5m"):
        dates = set(frames[period]["Date"].dt.normalize())
        extra = dates - base_dates
        if extra:
            details = sorted(item.date().isoformat() for item in extra)
            raise ValueError(f"{period}: unexpected trading dates {details[:20]}")
        missing = base_dates - dates
        if missing:
            derived = _aggregate_1m_days(frames["1m"], period, missing)
            frames[period] = (
                pd.concat([frames[period], derived], ignore_index=True)
                .sort_values("Date")
                .reset_index(drop=True)
            )
            derived_repairs[period] = sorted(
                item.date().isoformat() for item in missing
            )

    validation: dict[str, object] = {}
    for period, frame in frames.items():
        session = validate_a_share_intraday_bars(
            frame, period, require_complete_days=True
        )
        reconciliation = validate_intraday_against_daily(frame, daily, period)
        validation[period] = {
            "bar_count": int(session["bar_count"]),
            "complete_day_count": int(session["complete_day_count"]),
            "expected_bars_per_day": int(session["expected_bars_per_day"]),
            "daily_matched_days": int(reconciliation["matched_day_count"]),
            "derived_repair_dates": derived_repairs.get(period, []),
        }
    if "" in factor_hashes or len(factor_hashes) != 1:
        raise ValueError("daily and minute frequencies use different adjustment factors")

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    staging = create_temporary_directory(
        data_dir, "intraday-data", prefix=f"{code.lower()}-"
    )
    try:
        file_records: dict[str, dict[str, object]] = {}
        for period, frame in frames.items():
            output = _csv_frame(frame)
            years = pd.to_datetime(output["datetime"]).dt.year
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
                timestamps = pd.to_datetime(yearly["datetime"])
                file_records[filename] = {
                    "frequency": period,
                    "year": int(year),
                    "rows": int(len(yearly)),
                    "first": timestamps.min().isoformat(),
                    "last": timestamps.max().isoformat(),
                    "sha256": raw_file_sha256(path),
                }
        manifest = {
            "schema_version": 1,
            "symbol": normalized_symbol,
            "asset_type": "etf",
            "vendor": "tushare",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "frequencies": list(INTRADAY_RESEARCH_FREQUENCIES),
            "adjustment": {
                "mode": "hfq",
                "factor_source": "fund_adj",
                "factor_sha256": factor_hashes.pop(),
            },
            "validation": validation,
            "fetch_metadata": metadata,
            "files": file_records,
        }
        manifest_path = staging / f"{code}_intraday_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        published_names = sorted(file_records) + [manifest_path.name]
        for filename in published_names:
            source = staging / filename
            destination = data_dir / filename
            shutil.copyfile(source, destination)
        return {
            "status": "PASS",
            "manifest": str(data_dir / manifest_path.name),
            "files": len(file_records),
            "validation": validation,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def load_intraday_research_data(data_dir: Path, symbol: str) -> IntradayResearchData:
    """Load and hash-check a published high-frequency research generation."""

    normalized_symbol, code = _symbol_parts(symbol)
    data_dir = Path(data_dir)
    manifest_path = data_dir / f"{code}_intraday_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("symbol") != normalized_symbol:
        raise ValueError("intraday manifest symbol does not match request")
    records = manifest.get("files")
    if not isinstance(records, dict) or not records:
        raise ValueError("intraday manifest files must be a non-empty object")
    grouped: dict[str, list[pd.DataFrame]] = {
        item: [] for item in INTRADAY_RESEARCH_FREQUENCIES
    }
    hashes: dict[str, str] = {}
    for filename, record in records.items():
        if not isinstance(record, dict):
            raise ValueError(f"{filename}: invalid record")
        match = re.fullmatch(rf"{code}_(15m|5m|1m)_(\d{{4}})\.csv", str(filename))
        if not match or record.get("frequency") != match.group(1):
            raise ValueError(f"{filename}: invalid intraday filename or frequency")
        path = data_dir / str(filename)
        digest = raw_file_sha256(path)
        if digest != str(record.get("sha256", "")).lower():
            raise ValueError(f"{filename}: SHA-256 differs from manifest")
        frame = pd.read_csv(path, encoding="utf-8-sig")
        frame = frame.rename(
            columns={
                "datetime": "Date",
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
                "amount": "Amount",
            }
        )
        grouped[match.group(1)].append(_normalize_frame(frame, match.group(1)))
        hashes[str(filename)] = digest
    frames: dict[str, pd.DataFrame] = {}
    for period, pieces in grouped.items():
        if not pieces:
            raise ValueError(f"intraday manifest missing {period}")
        frame = pd.concat(pieces, ignore_index=True).sort_values("Date").reset_index(drop=True)
        validate_a_share_intraday_bars(frame, period, require_complete_days=True)
        frames[period] = frame
    return IntradayResearchData(normalized_symbol, frames, manifest, hashes)
