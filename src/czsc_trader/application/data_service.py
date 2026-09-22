from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from czsc_trader.data import load_market_data

from .context import RepositoryContext
from .errors import ValidationError
from .results import CommandResult


@dataclass(frozen=True)
class PrepareDataCommand:
    """Research-facing request for preparing exploratory market data."""

    symbol: str
    asset_type: str
    start: date
    end: date


def prepare_data(
    context: RepositoryContext,
    request: PrepareDataCommand,
) -> CommandResult:
    """Prepare market data for strategy-mechanism exploration under ``data/raw``."""

    from czsc_trader.market_data_prep import prepare_market_data

    try:
        summary = prepare_market_data(
            request.symbol,
            request.asset_type,
            request.start,
            request.end,
            context.raw_dir,
            env_file=context.root / ".env",
        )
    except (OSError, ValueError) as exc:
        raise ValidationError(
            "market_data_preparation_failed",
            str(exc),
            context={"symbol": request.symbol},
        ) from exc
    return CommandResult(
        status="PASS",
        command="data.prepare",
        result=summary,
        artifacts={"manifest": summary["manifest"]},
    )


def validate_data(context: RepositoryContext, symbol: str) -> CommandResult:
    try:
        data = load_market_data(context.raw_dir, symbol)
    except (OSError, ValueError) as exc:
        raise ValidationError(
            "market_data_validation_failed",
            str(exc),
            context={"symbol": symbol},
        ) from exc
    manifest = data.manifest
    frequencies = sorted(
        {
            str(record["frequency"])
            for record in manifest["files"].values()
            if isinstance(record, dict) and "frequency" in record
        },
        key=("30m", "daily", "weekly").index,
    )
    return CommandResult(
        status="PASS",
        command="data.validate",
        result={
            "symbol": data.symbol,
            "name": manifest["name"],
            "asset_type": data.asset_type,
            "requested_start": manifest.get("requested_start"),
            "requested_end": manifest.get("requested_end"),
            "validation_status": "PASS",
            "frequencies": frequencies,
            "file_count": len(data.hashes),
        },
    )
