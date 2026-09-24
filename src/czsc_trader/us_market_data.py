"""Manifest-verified loading for immutable Longbridge US generations."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re

import pandas as pd

from czsc_trader.data import MarketData, NORMALIZED_COLUMNS, _validate_frame


_US_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]*\.US")


def _object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Missing US generation metadata: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _verified_bytes(path: Path, expected: str, label: str) -> bytes:
    raw = path.read_bytes()
    if len(expected) != 64 or sha256(raw).hexdigest() != expected.lower():
        raise ValueError(f"{label}: SHA-256 differs from manifest")
    return raw


def _generation_dir(
    root: Path,
    symbol: str,
    cutoff: pd.Timestamp,
    generation_id: str | None,
) -> tuple[Path, dict]:
    generations = root / "us" / "generations"
    if generation_id is not None:
        if not re.fullmatch(r"US-[A-F0-9]{16}", generation_id):
            raise ValueError("invalid US generation identity")
        candidates = [generations / generation_id]
    else:
        candidates = sorted(path.parent for path in generations.glob("*/manifest.json"))
    matches: list[tuple[Path, dict]] = []
    for directory in candidates:
        if not (directory / "manifest.json").is_file():
            continue
        manifest = _object(directory / "manifest.json")
        if (
            manifest.get("generation_id") == directory.name
            and symbol in manifest.get("symbols", [])
            and pd.Timestamp(str(manifest.get("requested_end"))).normalize() >= cutoff
        ):
            matches.append((directory, manifest))
    if not matches:
        raise ValueError(f"no US generation covers {symbol} through {cutoff.date()}")
    if len(matches) != 1:
        raise ValueError(
            f"multiple US generations cover {symbol}; bind an explicit generation_id"
        )
    return matches[0]


def _read_frequency(
    directory: Path,
    generation: dict,
    symbol_manifest: dict,
    symbol: str,
    frequency: str,
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, str], dict[str, dict]]:
    frames: list[pd.DataFrame] = []
    hashes: dict[str, str] = {}
    visible: dict[str, dict] = {}
    prefix = symbol.removesuffix(".US").replace(".", "_")
    label = "execution_daily" if frequency == "execution_daily" else frequency
    pattern = re.compile(rf"{re.escape(prefix)}_{label}_(\d{{4}})\.csv")
    for filename, record in sorted(symbol_manifest.get("files", {}).items()):
        if not isinstance(record, dict) or record.get("frequency") != (
            "daily" if frequency == "execution_daily" else frequency
        ):
            continue
        match = pattern.fullmatch(str(filename))
        if match is None or int(match.group(1)) > cutoff.year:
            continue
        relative = f"{symbol}/{filename}"
        path = directory / relative
        expected = str(record.get("sha256", "")).lower()
        if generation.get("file_hashes", {}).get(relative) != expected:
            raise ValueError(f"{relative}: generation and symbol manifests differ")
        _verified_bytes(path, expected, relative)
        source_time = "datetime" if frequency in {"1m", "5m", "15m", "30m"} else "date"
        frame = pd.read_csv(path)
        required = {source_time, "open", "high", "low", "close", "volume", "amount"}
        if set(frame.columns) != required:
            raise ValueError(f"{relative}: columns differ from the canonical schema")
        frame = frame.rename(columns={source_time: "dt", "volume": "vol"})
        frame["dt"] = pd.to_datetime(frame["dt"], errors="raise")
        frame.insert(1, "symbol", symbol)
        frame = frame[list(NORMALIZED_COLUMNS)]
        declared_first = pd.Timestamp(str(record.get("first")))
        declared_last = pd.Timestamp(str(record.get("last")))
        if (
            len(frame) != int(record.get("rows", -1))
            or frame["dt"].min() != declared_first
            or frame["dt"].max() != declared_last
        ):
            raise ValueError(f"{relative}: rows/first/last differ from manifest")
        frame = frame.loc[frame["dt"].dt.normalize() <= cutoff].copy()
        if not frame.empty:
            frames.append(frame)
            hashes[relative] = expected
            visible[filename] = record
    if not frames:
        raise ValueError(f"US generation has no visible {frequency} data for {symbol}")
    result = pd.concat(frames, ignore_index=True).sort_values("dt").reset_index(drop=True)
    _validate_frame(result, frequency)
    return result, hashes, visible


def load_us_replay_data(
    root: Path,
    symbol: str,
    asset_type: str,
    cutoff: pd.Timestamp,
    *,
    generation_id: str | None = None,
) -> tuple[MarketData, pd.DataFrame, Path, str]:
    """Load daily research and execution prices from one explicit US generation."""

    normalized = str(symbol).strip().upper()
    if _US_SYMBOL.fullmatch(normalized) is None:
        raise ValueError("US symbol must end in .US")
    if str(asset_type).lower() != "stock":
        raise ValueError("Longbridge US replay currently supports stocks only")
    cutoff = pd.Timestamp(cutoff).normalize()
    directory, generation = _generation_dir(
        Path(root), normalized, cutoff, generation_id
    )
    relative_manifest = f"{normalized}/manifest.json"
    manifest_path = directory / relative_manifest
    expected_manifest_hash = str(generation.get("file_hashes", {}).get(relative_manifest, ""))
    symbol_manifest = json.loads(
        _verified_bytes(manifest_path, expected_manifest_hash, relative_manifest).decode("utf-8")
    )
    if (
        symbol_manifest.get("symbol") != normalized
        or symbol_manifest.get("asset_type") != "stock"
        or symbol_manifest.get("vendor") != "longbridge"
        or symbol_manifest.get("adjustment") != "forward"
    ):
        raise ValueError("US symbol manifest differs from the requested replay contract")
    daily, daily_hashes, visible_daily = _read_frequency(
        directory, generation, symbol_manifest, normalized, "daily", cutoff
    )
    weekly, weekly_hashes, visible_weekly = _read_frequency(
        directory, generation, symbol_manifest, normalized, "weekly", cutoff
    )
    execution, execution_hashes, visible_execution = _read_frequency(
        directory, generation, symbol_manifest, normalized, "execution_daily", cutoff
    )
    if not pd.DatetimeIndex(daily["dt"]).equals(pd.DatetimeIndex(execution["dt"])):
        raise ValueError("US adjusted and execution daily sessions differ")
    if daily["dt"].max().normalize() != cutoff:
        raise ValueError(
            f"US daily data does not reach requested cutoff: {cutoff.date()}"
        )
    # A daily strategy must not pretend that pre-2023 intraday bars exist.  TXE
    # accepts an empty touch table for MARKET-at-open execution.
    intraday = pd.DataFrame(columns=NORMALIZED_COLUMNS)
    visible_manifest = {
        **symbol_manifest,
        "generation_id": directory.name,
        "visible_cutoff": cutoff.date().isoformat(),
        "files": {**visible_daily, **visible_weekly, **visible_execution},
    }
    adjusted = MarketData(
        intraday,
        daily,
        weekly,
        {**daily_hashes, **weekly_hashes},
        normalized,
        "stock",
        visible_manifest,
    )
    return adjusted, execution, directory, directory.name


def load_us_one_minute_prices(
    root: Path,
    symbol: str,
    cutoff: pd.Timestamp,
    generation_id: str,
    adjusted_daily: pd.DataFrame,
    execution_daily: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return hash-verified signal bars and unadjusted execution bars."""
    normalized = str(symbol).strip().upper()
    cutoff = pd.Timestamp(cutoff).normalize()
    directory, generation = _generation_dir(root, normalized, cutoff, generation_id)
    relative = f"{normalized}/manifest.json"
    expected = str(generation.get("file_hashes", {}).get(relative, ""))
    symbol_manifest = json.loads(
        _verified_bytes(directory / relative, expected, relative).decode("utf-8")
    )
    if symbol_manifest.get("symbol") != normalized:
        raise ValueError("US minute manifest differs from requested symbol")
    signal, _, _ = _read_frequency(
        directory, generation, symbol_manifest, normalized, "1m", cutoff
    )
    adjusted = adjusted_daily.set_index(pd.to_datetime(adjusted_daily["dt"]).dt.normalize())
    raw = execution_daily.set_index(pd.to_datetime(execution_daily["dt"]).dt.normalize())
    factors = adjusted["close"].astype(float).div(raw["close"].astype(float))
    bar_factors = signal["dt"].dt.normalize().map(factors)
    if bar_factors.isna().any() or bar_factors.le(0).any():
        raise ValueError("US minute execution prices have no positive adjustment factor")
    execution = signal.copy()
    for column in ("open", "high", "low", "close"):
        execution[column] = execution[column].astype(float).div(bar_factors.to_numpy(dtype=float))
    return signal, execution
