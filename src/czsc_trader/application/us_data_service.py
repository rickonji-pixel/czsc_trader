"""Atomic multi-symbol US history publication for research preparation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Any

import pandas as pd

from dataflows import DataRequest, DataStatus, Dataflows, Dataset
from dataflows.market_resolver import MARKET_US, detect_market, normalize_symbol_for_vendor

from czsc_trader.temp_workspace import create_temporary_directory

from .context import RepositoryContext
from .errors import ValidationError
from .results import CommandResult


_FREQUENCIES = ("1m", "5m", "15m", "30m", "daily", "weekly")
_INTRADAY = {"1m", "5m", "15m", "30m"}


@dataclass(frozen=True)
class PrepareUSHistoryCommand:
    symbols: tuple[str, ...]
    start: date
    end: date
    daily_start: date | None = None
    frequencies: tuple[str, ...] = ("30m", "daily", "weekly")
    adjustment: str = "forward"
    trade_sessions: str = "intraday"


def _normalize_command(command: PrepareUSHistoryCommand) -> PrepareUSHistoryCommand:
    if command.start > command.end:
        raise ValueError("intraday start must not follow end")
    daily_start = command.daily_start or command.start
    if daily_start > command.end:
        raise ValueError("daily start must not follow end")
    if not command.symbols:
        raise ValueError("at least one US symbol is required")
    if any(detect_market(value) != MARKET_US for value in command.symbols):
        raise ValueError("US history publication accepts US equity symbols only")
    symbols = tuple(
        normalize_symbol_for_vendor(value, "longbridge", MARKET_US)
        for value in command.symbols
    )
    if len(symbols) > 100:
        raise ValueError("one batch may not consume more than 100 monthly symbol slots")
    if len(set(symbols)) != len(symbols):
        raise ValueError("US history symbols must be unique")
    frequencies = tuple(str(value).strip().lower() for value in command.frequencies)
    if not frequencies or len(set(frequencies)) != len(frequencies):
        raise ValueError("frequencies must be non-empty and unique")
    unsupported = sorted(set(frequencies).difference(_FREQUENCIES))
    if unsupported:
        raise ValueError(f"unsupported US history frequencies: {unsupported}")
    adjustment = str(command.adjustment).strip().lower()
    trade_sessions = str(command.trade_sessions).strip().lower()
    if adjustment not in {"forward", "none"}:
        raise ValueError("adjustment must be forward or none")
    if trade_sessions not in {"intraday", "all"}:
        raise ValueError("trade_sessions must be intraday or all")
    return PrepareUSHistoryCommand(
        symbols,
        command.start,
        command.end,
        daily_start,
        frequencies,
        adjustment,
        trade_sessions,
    )


def create_longbridge_dataflows(env_file: Path) -> Dataflows:
    """Create a batch-scoped DFLS facade sharing one SDK connection and limiter."""

    from dataflows.longbridge_stock import (
        RequestThrottle,
        create_quote_context,
        fetch_stock_ohlcv,
        fetch_stock_unadjusted_daily,
        fetch_us_trading_calendar,
    )

    context = create_quote_context(env_file)
    throttle = RequestThrottle()

    def adjusted(request: DataRequest):
        return fetch_stock_ohlcv(
            str(request.symbol),
            request.start,
            request.end,
            request.frequency,
            adjustment=str(request.options.get("adjustment", "forward")),
            trade_sessions=str(request.options.get("trade_sessions", "intraday")),
            quote_context=context,
            request_throttle=throttle,
        )

    def execution(request: DataRequest):
        return fetch_stock_unadjusted_daily(
            str(request.symbol),
            request.start,
            request.end,
            quote_context=context,
            request_throttle=throttle,
        )

    def calendar(request: DataRequest):
        return fetch_us_trading_calendar(
            request.start,
            request.end,
            quote_context=context,
            request_throttle=throttle,
        )

    return Dataflows(
        {
            Dataset.STOCK_OHLCV.value: adjusted,
            Dataset.STOCK_UNADJUSTED_DAILY.value: execution,
            Dataset.TRADING_CALENDAR.value: calendar,
        }
    )


def _identity_payload(result) -> dict[str, Any]:
    identity = result.identity
    if identity is None:
        raise ValueError("ready data result is missing identity")
    return {
        "dataset": identity.dataset,
        "source": identity.source,
        "symbol": identity.symbol,
        "data_start": identity.data_start,
        "data_cutoff": identity.data_cutoff,
        "content_sha256": identity.content_sha256,
        "metadata": dict(identity.metadata),
    }


def _write_yearly(
    frame: pd.DataFrame,
    target: Path,
    *,
    symbol: str,
    label: str,
    frequency: str,
) -> dict[str, dict[str, Any]]:
    timestamps = pd.to_datetime(frame["Date"], errors="raise")
    date_column = "datetime" if frequency in _INTRADAY else "date"
    output = frame.rename(
        columns={
            "Date": date_column,
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
            "Amount": "amount",
        }
    ).copy()
    output[date_column] = timestamps.dt.strftime(
        "%Y-%m-%d %H:%M:%S" if date_column == "datetime" else "%Y-%m-%d"
    )
    records: dict[str, dict[str, Any]] = {}
    slug = symbol.removesuffix(".US").replace(".", "_")
    for year in sorted(timestamps.dt.year.unique()):
        selected = output.loc[timestamps.dt.year.eq(year)].copy()
        filename = f"{slug}_{label}_{int(year)}.csv"
        path = target / filename
        selected.to_csv(path, index=False, lineterminator="\n")
        selected_dates = pd.to_datetime(selected[date_column], errors="raise")
        records[filename] = {
            "frequency": frequency,
            "year": int(year),
            "rows": len(selected),
            "first": selected_dates.min().isoformat(),
            "last": selected_dates.max().isoformat(),
            "sha256": sha256(path.read_bytes()).hexdigest(),
        }
    return records


def _ready(dataflows: Dataflows, request: DataRequest, label: str):
    result = dataflows.fetch(request)
    if result.status is not DataStatus.READY:
        detail = result.error.message if result.error else result.status.value
        raise ValueError(f"{label}: {result.status.value}: {detail}")
    return result


def prepare_us_history(
    context: RepositoryContext,
    command: PrepareUSHistoryCommand,
    *,
    dataflows: Dataflows | None = None,
) -> CommandResult:
    """Fetch a multi-symbol bundle without partially publishing failed batches."""

    try:
        request = _normalize_command(command)
        flow = dataflows or create_longbridge_dataflows(context.root / ".env")
        staging = create_temporary_directory(
            context.root,
            "us-history",
            repository_root=context.root,
        )
        symbol_manifests: dict[str, dict[str, Any]] = {}
        try:
            calendar_start = min(request.start, request.daily_start or request.start)
            calendar_result = _ready(
                flow,
                DataRequest(
                    Dataset.TRADING_CALENDAR,
                    "US",
                    calendar_start.isoformat(),
                    request.end.isoformat(),
                    request.end.isoformat(),
                    "daily",
                    {"vendor": "longbridge"},
                ),
                "US trading calendar",
            )
            calendar_path = staging / "calendar.csv"
            calendar_result.dataframe.rename(
                columns={
                    "Date": "date",
                    "IsOpen": "is_open",
                    "PreviousTradingDate": "previous_trading_date",
                }
            ).to_csv(calendar_path, index=False, lineterminator="\n")

            for symbol in request.symbols:
                symbol_dir = staging / symbol
                symbol_dir.mkdir()
                files: dict[str, dict[str, Any]] = {}
                inputs: dict[str, dict[str, Any]] = {}
                for frequency in request.frequencies:
                    start = (
                        request.start
                        if frequency in _INTRADAY
                        else request.daily_start or request.start
                    )
                    result = _ready(
                        flow,
                        DataRequest(
                            Dataset.STOCK_OHLCV,
                            symbol,
                            start.isoformat(),
                            request.end.isoformat(),
                            None,
                            frequency,
                            {
                                "vendor": "longbridge",
                                "adjustment": request.adjustment,
                                "trade_sessions": request.trade_sessions,
                            },
                        ),
                        f"{symbol} {frequency}",
                    )
                    files.update(
                        _write_yearly(
                            result.dataframe,
                            symbol_dir,
                            symbol=symbol,
                            label=frequency,
                            frequency=frequency,
                        )
                    )
                    inputs[frequency] = _identity_payload(result)

                execution = _ready(
                    flow,
                    DataRequest(
                        Dataset.STOCK_UNADJUSTED_DAILY,
                        symbol,
                        (request.daily_start or request.start).isoformat(),
                        request.end.isoformat(),
                        None,
                        "daily",
                        {"vendor": "longbridge"},
                    ),
                    f"{symbol} execution daily",
                )
                files.update(
                    _write_yearly(
                        execution.dataframe,
                        symbol_dir,
                        symbol=symbol,
                        label="execution_daily",
                        frequency="daily",
                    )
                )
                inputs["execution_daily"] = _identity_payload(execution)
                symbol_manifest = {
                    "schema_version": 1,
                    "symbol": symbol,
                    "asset_type": "stock",
                    "vendor": "longbridge",
                    "requested_intraday_start": request.start.isoformat(),
                    "requested_daily_start": (request.daily_start or request.start).isoformat(),
                    "requested_end": request.end.isoformat(),
                    "adjustment": request.adjustment,
                    "trade_sessions": request.trade_sessions,
                    "inputs": inputs,
                    "files": dict(sorted(files.items())),
                }
                (symbol_dir / "manifest.json").write_text(
                    json.dumps(symbol_manifest, ensure_ascii=False, indent=2, default=str) + "\n",
                    encoding="utf-8",
                )
                symbol_manifests[symbol] = symbol_manifest

            file_hashes = {
                str(path.relative_to(staging)).replace("\\", "/"): sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in sorted(staging.rglob("*"))
                if path.is_file()
            }
            identity_material = {
                "schema_version": 1,
                "symbols": list(request.symbols),
                "frequencies": list(request.frequencies),
                "adjustment": request.adjustment,
                "trade_sessions": request.trade_sessions,
                "file_hashes": file_hashes,
                "calendar_identity": _identity_payload(calendar_result),
            }
            generation_id = "US-" + sha256(
                json.dumps(identity_material, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:16].upper()
            manifest = {
                **identity_material,
                "generation_id": generation_id,
                "requested_intraday_start": request.start.isoformat(),
                "requested_daily_start": (request.daily_start or request.start).isoformat(),
                "requested_end": request.end.isoformat(),
                "symbol_manifests": symbol_manifests,
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )

            generations = context.raw_dir / "us" / "generations"
            generations.mkdir(parents=True, exist_ok=True)
            destination = generations / generation_id
            if destination.exists():
                existing = json.loads(
                    (destination / "manifest.json").read_text(encoding="utf-8")
                )
                if existing != manifest:
                    raise ValueError(f"US generation collision: {generation_id}")
            else:
                staging.replace(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    except Exception as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(
            "us_history_preparation_failed",
            str(exc),
            context={"symbols": list(command.symbols)},
        ) from exc

    return CommandResult(
        "PASS",
        "data.prepare-us-history",
        {
            "generation_id": generation_id,
            "symbols": list(request.symbols),
            "frequencies": list(request.frequencies),
            "requested_intraday_start": request.start.isoformat(),
            "requested_daily_start": (request.daily_start or request.start).isoformat(),
            "requested_end": request.end.isoformat(),
        },
        {"manifest": str(destination / "manifest.json")},
        (
            "Longbridge minute and daily bars are preserved as distinct vendor scopes; "
            "no cross-frequency volume reconciliation was asserted.",
        ),
    )
