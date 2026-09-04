from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import shutil
import tempfile

from czsc_trader.data import load_market_data

from .context import RepositoryContext
from .errors import ValidationError
from .results import CommandResult


@dataclass(frozen=True)
class PrepareDataCommand:
    symbol: str
    asset_type: str
    start: date
    end: date


@dataclass(frozen=True)
class UpdateBacktestDataCommand:
    symbol: str
    asset_type: str
    through: date


def _assert_append_only(current: Path, proposed: Path) -> None:
    import pandas as pd

    old = pd.read_csv(current, dtype=str).fillna("")
    new = pd.read_csv(proposed, dtype=str).fillna("")
    key = str(old.columns[0])
    if key not in new or old[key].duplicated().any() or new[key].duplicated().any():
        raise ValueError(f"{current.name}: invalid time key")
    aligned = new.set_index(key).reindex(old[key])
    if aligned.isna().any().any() or not old.set_index(key).equals(aligned):
        raise ValueError(f"{current.name}: update would mutate published rows")


def update_backtest_data(
    context: RepositoryContext,
    request: UpdateBacktestDataCommand,
) -> CommandResult:
    """Fetch to staging and publish only an append-compatible backtest dataset."""
    from czsc_trader.market_data_prep import prepare_market_data

    code = request.symbol.split(".", 1)[0]
    current_manifest = context.backtest_data_root / f"{code}_manifest.json"
    if current_manifest.is_file():
        payload = json.loads(current_manifest.read_text(encoding="utf-8"))
        start = date.fromisoformat(str(payload["requested_start"]))
    else:
        start = date(2010, 1, 1)
    data_parent = context.root / "data"
    data_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".backtest_update_", dir=data_parent))
    backup = staging / "backup"
    backup.mkdir()
    published: list[Path] = []
    moved: list[tuple[Path, Path]] = []
    try:
        summary = prepare_market_data(
            request.symbol,
            request.asset_type,
            start,
            request.through,
            staging,
            env_file=context.root / ".env",
        )
        proposed = [path for path in staging.glob(f"{code}_*") if path.is_file()]
        for current in context.backtest_data_root.glob(f"{code}_*.csv"):
            replacement = staging / current.name
            if not replacement.is_file():
                raise ValueError(f"{current.name}: update dropped a published file")
            _assert_append_only(current, replacement)
        context.backtest_data_root.mkdir(parents=True, exist_ok=True)
        for source in proposed:
            destination = context.backtest_data_root / source.name
            if destination.exists():
                saved = backup / source.name
                destination.replace(saved)
                moved.append((destination, saved))
            source.replace(destination)
            published.append(destination)
    except Exception as exc:
        for destination in reversed(published):
            destination.unlink(missing_ok=True)
        for destination, saved in moved:
            if saved.exists():
                saved.replace(destination)
        raise ValidationError(
            "backtest_data_update_failed",
            str(exc),
            context={"symbol": request.symbol, "through": request.through.isoformat()},
        ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return CommandResult(
        "PASS",
        "data.update-backtest",
        {
            **summary,
            "dataset": "backtest",
            "through": request.through.isoformat(),
        },
        {"manifest": str(context.backtest_data_root / f"{code}_manifest.json")},
    )


def prepare_data(
    context: RepositoryContext,
    request: PrepareDataCommand,
) -> CommandResult:
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
