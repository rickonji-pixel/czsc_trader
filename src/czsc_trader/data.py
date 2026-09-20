"""Manifest-driven immutable loading and validation for local K-line files."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re

import pandas as pd
from dataflows.errors import DataContractError
from dataflows.history_validation import (
    inspect_daily_against_weekly,
    inspect_intraday_against_daily,
    inspect_ohlcv_frame,
)

from .identity import raw_file_sha256


SYMBOL = "588080.SH"
FREQUENCIES = ("30m", "daily", "weekly")
NORMALIZED_COLUMNS = ("dt", "symbol", "open", "high", "low", "close", "vol", "amount")
@dataclass(frozen=True)
class MarketData:
    """Validated K-lines at the three supplied frequencies."""

    intraday: pd.DataFrame
    daily: pd.DataFrame
    weekly: pd.DataFrame
    hashes: dict[str, str]
    symbol: str = SYMBOL
    asset_type: str = "etf"
    manifest: dict[str, object] = field(default_factory=dict)

    def truncate(self, cutoff: pd.Timestamp | str) -> "MarketData":
        """Return a causal view containing no bar after ``cutoff`` day."""
        cutoff_ts = pd.Timestamp(cutoff).normalize()
        intraday = self.intraday.loc[self.intraday["dt"].dt.normalize() <= cutoff_ts].copy()
        daily = self.daily.loc[self.daily["dt"] <= cutoff_ts].copy()
        weekly = self.weekly.loc[self.weekly["dt"] <= cutoff_ts].copy()
        return MarketData(
            intraday,
            daily,
            weekly,
            self.hashes.copy(),
            self.symbol,
            self.asset_type,
            self.manifest.copy(),
        )


def _symbol_parts(symbol: str) -> tuple[str, str]:
    value = str(symbol).strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ)", value)
    if not match:
        raise ValueError("symbol must be a six-digit A-share code ending in SH or SZ")
    return value, match.group(1)


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing market-data metadata: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: expected a JSON object")
    return payload


def _read_one(path: Path, freq: str, symbol: str) -> pd.DataFrame:
    source_time = "datetime" if freq == "30m" else "date"
    frame = pd.read_csv(path)
    expected = {source_time, "open", "high", "low", "close", "volume", "amount"}
    if set(frame.columns) != expected:
        raise ValueError(f"{path.name}: columns {list(frame.columns)} do not match {sorted(expected)}")
    frame = frame.rename(columns={source_time: "dt", "volume": "vol"})
    frame["dt"] = pd.to_datetime(frame["dt"])
    frame.insert(1, "symbol", symbol)
    return frame[list(NORMALIZED_COLUMNS)]


def _dfls_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(
        columns={
            "dt": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "vol": "Volume",
            "amount": "Amount",
        }
    ).loc[:, ["Date", "Open", "High", "Low", "Close", "Volume", "Amount"]]


def _require_dfls(report) -> None:
    try:
        report.require_pass()
    except DataContractError as exc:
        raise ValueError(str(exc)) from exc


def _validate_frame(frame: pd.DataFrame, name: str) -> None:
    _require_dfls(
        inspect_ohlcv_frame(
            _dfls_frame(frame),
            name,
            require_complete_days=name == "30m",
        )
    )


def _validate_reconciliation(
    intraday: pd.DataFrame,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    *,
    allow_trailing_partial_week: bool = False,
) -> None:
    dfls_intraday = _dfls_frame(intraday)
    dfls_daily = _dfls_frame(daily)
    dfls_weekly = _dfls_frame(weekly)
    _require_dfls(inspect_intraday_against_daily(dfls_intraday, dfls_daily, "30m"))

    weekly_daily = dfls_daily
    if allow_trailing_partial_week:
        last_week = pd.to_datetime(dfls_daily["Date"]).dt.to_period("W-SUN").iloc[-1]
        weekly_periods = set(pd.to_datetime(dfls_weekly["Date"]).dt.to_period("W-SUN"))
        if last_week not in weekly_periods:
            weekly_daily = dfls_daily.loc[
                pd.to_datetime(dfls_daily["Date"]).dt.to_period("W-SUN") != last_week
            ]
    _require_dfls(inspect_daily_against_weekly(weekly_daily, dfls_weekly))


def _validate_manifest_record(
    filename: str,
    record: dict[str, object],
    frame: pd.DataFrame,
) -> None:
    try:
        declared_rows = int(record["rows"])
        declared_first = pd.Timestamp(str(record["first"]))
        declared_last = pd.Timestamp(str(record["last"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{filename}: invalid rows/first/last manifest metadata") from exc
    actual = pd.to_datetime(frame["dt"], errors="coerce")
    if (
        len(frame) != declared_rows
        or actual.isna().any()
        or actual.min() != declared_first
        or actual.max() != declared_last
    ):
        raise ValueError(f"{filename}: rows/first/last differ from manifest")


def load_market_data(
    raw_dir: Path,
    symbol: str = SYMBOL,
    asset_type: str | None = None,
    *,
    cutoff: pd.Timestamp | str | None = None,
) -> MarketData:
    """Load, normalize, hash, and fully reconcile the supplied raw K-lines."""
    raw_dir = Path(raw_dir)
    normalized_symbol, code = _symbol_parts(symbol)
    manifest = _read_json_object(raw_dir / f"{code}_manifest.json")
    validation = _read_json_object(raw_dir / f"{code}_validation.json")
    if validation.get("status") != "PASS":
        raise ValueError(f"{code}: market-data validation status is not PASS")
    if manifest.get("symbol") != normalized_symbol:
        raise ValueError(
            f"{code}: manifest symbol {manifest.get('symbol')} does not match {normalized_symbol}"
        )
    manifest_name = manifest.get("name")
    if not isinstance(manifest_name, str) or not manifest_name.strip():
        raise ValueError(f"{code}: manifest name must be a non-empty string")
    manifest_asset = str(manifest.get("asset_type", ""))
    if asset_type is not None and manifest_asset != str(asset_type).lower():
        raise ValueError(
            f"{code}: manifest asset type {manifest_asset} does not match {asset_type}"
        )
    if manifest_asset not in {"stock", "etf"}:
        raise ValueError(f"{code}: manifest asset type must be stock or etf")
    file_records = manifest.get("files")
    if not isinstance(file_records, dict) or not file_records:
        raise ValueError(f"{code}: manifest files must be a non-empty object")
    cutoff_ts = pd.Timestamp(cutoff).normalize() if cutoff is not None else None
    hashes: dict[str, str] = {}
    grouped: dict[str, list[pd.DataFrame]] = {freq: [] for freq in FREQUENCIES}
    visible_records: dict[str, object] = {}

    for filename, record in file_records.items():
        if not isinstance(record, dict):
            raise ValueError(f"{filename}: invalid manifest file record")
        match = re.fullmatch(rf"{code}_(30m|daily|weekly)_(\d{{4}})\.csv", str(filename))
        if not match:
            raise ValueError(f"{filename}: invalid flat market-data filename")
        freq = match.group(1)
        year = int(match.group(2))
        if record.get("frequency") != freq:
            raise ValueError(f"{filename}: manifest frequency differs from filename")
        if cutoff_ts is not None and year > cutoff_ts.year:
            continue
        path = raw_dir / str(filename)
        if not path.is_file():
            raise FileNotFoundError(f"Missing raw K-line file: {filename}")
        digest = raw_file_sha256(path)
        if str(record.get("sha256", "")).lower() != digest:
            raise ValueError(f"{filename}: SHA-256 differs from manifest")
        hashes[path.name] = digest
        frame = _read_one(path, freq, normalized_symbol)
        _validate_manifest_record(str(filename), record, frame)
        if cutoff_ts is not None:
            frame = frame.loc[frame["dt"].dt.normalize() <= cutoff_ts].copy()
        if not frame.empty:
            grouped[freq].append(frame)
            visible_records[str(filename)] = record

    missing_frequencies = [freq for freq, frames in grouped.items() if not frames]
    if missing_frequencies:
        raise ValueError(f"{code}: manifest missing frequencies {missing_frequencies}")

    intraday = pd.concat(grouped["30m"], ignore_index=True).sort_values("dt").reset_index(drop=True)
    daily = pd.concat(grouped["daily"], ignore_index=True).sort_values("dt").reset_index(drop=True)
    weekly = pd.concat(grouped["weekly"], ignore_index=True).sort_values("dt").reset_index(drop=True)
    for name, frame in (("30m", intraday), ("daily", daily), ("weekly", weekly)):
        _validate_frame(frame, name)
    _validate_reconciliation(
        intraday,
        daily,
        weekly,
        allow_trailing_partial_week=cutoff_ts is not None,
    )
    visible_manifest = manifest.copy()
    visible_manifest["files"] = visible_records
    if cutoff_ts is not None:
        visible_manifest["visible_cutoff"] = str(cutoff_ts.date())
    return MarketData(
        intraday,
        daily,
        weekly,
        hashes,
        normalized_symbol,
        manifest_asset,
        visible_manifest,
    )


def load_execution_prices(
    raw_dir: Path,
    symbol: str = SYMBOL,
    asset_type: str | None = None,
    *,
    cutoff: pd.Timestamp | str | None = None,
) -> pd.DataFrame:
    """Load manifest-verified unadjusted daily prices used for order pricing."""
    raw_dir = Path(raw_dir)
    normalized_symbol, code = _symbol_parts(symbol)
    manifest = _read_json_object(raw_dir / f"{code}_execution_manifest.json")
    if manifest.get("symbol") != normalized_symbol:
        raise ValueError("execution-price manifest symbol does not match request")
    if asset_type is not None and manifest.get("asset_type") != str(asset_type).lower():
        raise ValueError("execution-price manifest asset type does not match request")
    if manifest.get("adjustment") != "none":
        raise ValueError("execution prices must be unadjusted")
    records = manifest.get("files")
    if not isinstance(records, dict) or not records:
        raise ValueError("execution-price manifest files must be a non-empty object")
    cutoff_ts = pd.Timestamp(cutoff).normalize() if cutoff is not None else None
    frames: list[pd.DataFrame] = []
    for filename, record in records.items():
        if not isinstance(record, dict):
            raise ValueError(f"{filename}: invalid execution-price record")
        match = re.fullmatch(rf"{code}_execution_daily_(\d{{4}})\.csv", str(filename))
        if not match:
            raise ValueError(f"{filename}: invalid execution-price filename")
        if record.get("frequency") != "daily":
            raise ValueError(f"{filename}: execution-price frequency must be daily")
        if cutoff_ts is not None and int(match.group(1)) > cutoff_ts.year:
            continue
        path = raw_dir / str(filename)
        digest = raw_file_sha256(path)
        if str(record.get("sha256", "")).lower() != digest:
            raise ValueError(f"{filename}: SHA-256 differs from manifest")
        frame = _read_one(path, "daily", normalized_symbol)
        _validate_manifest_record(str(filename), record, frame)
        if cutoff_ts is not None:
            frame = frame.loc[frame["dt"] <= cutoff_ts].copy()
        if not frame.empty:
            frames.append(frame)
    if not frames:
        raise ValueError(f"{code}: no visible execution prices")
    result = pd.concat(frames, ignore_index=True).sort_values("dt").reset_index(drop=True)
    _validate_frame(result, "execution daily")
    return result


def load_execution_manifest(
    raw_dir: Path,
    symbol: str = SYMBOL,
    asset_type: str | None = None,
) -> dict[str, object]:
    """Load execution metadata including the published next exchange session."""
    normalized_symbol, code = _symbol_parts(symbol)
    manifest = _read_json_object(Path(raw_dir) / f"{code}_execution_manifest.json")
    if manifest.get("symbol") != normalized_symbol:
        raise ValueError("execution-price manifest symbol does not match request")
    if asset_type is not None and manifest.get("asset_type") != str(asset_type).lower():
        raise ValueError("execution-price manifest asset type does not match request")
    try:
        next_session = pd.Timestamp(str(manifest["next_trading_session"])).normalize()
    except (KeyError, ValueError) as exc:
        raise ValueError("execution-price manifest missing next trading session") from exc
    manifest = manifest.copy()
    manifest["next_trading_session"] = str(next_session.date())
    return manifest
