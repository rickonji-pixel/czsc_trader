from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import shutil

from czsc_trader.data import load_market_data
from czsc_trader.temp_workspace import create_temporary_directory

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


def _assert_append_only(
    current: Path,
    proposed: Path,
    *,
    mutable_terminal_period=None,
) -> None:
    import pandas as pd

    old = pd.read_csv(current, dtype=str).fillna("")
    new = pd.read_csv(proposed, dtype=str).fillna("")
    key = str(old.columns[0])
    if key not in new or old[key].duplicated().any() or new[key].duplicated().any():
        raise ValueError(f"{current.name}: invalid time key")
    immutable = old
    if mutable_terminal_period is not None:
        old_periods = pd.to_datetime(old[key]).dt.to_period("W-SUN")
        new_periods = pd.to_datetime(new[key]).dt.to_period("W-SUN")
        mutable_old = old_periods == mutable_terminal_period
        mutable_new = new_periods == mutable_terminal_period
        mutable_old_count = int(mutable_old.sum())
        mutable_new_count = int(mutable_new.sum())
        if mutable_old_count == 0 and mutable_new_count == 0:
            pass
        elif (
            mutable_old_count != 1
            or mutable_new_count != 1
            or not bool(mutable_old.iloc[-1])
        ):
            raise ValueError(f"{current.name}: invalid terminal weekly roll-forward")
        else:
            immutable = old.loc[~mutable_old]
    aligned = new.set_index(key).reindex(immutable[key])
    if aligned.isna().any().any() or not immutable.set_index(key).equals(aligned):
        raise ValueError(f"{current.name}: update would mutate published rows")


def _mutable_weekly_terminal_period(
    current_root: Path,
    proposed_root: Path,
    code: str,
):
    """Return the current partial-week period when newly fetched daily rows extend it."""
    import pandas as pd

    def daily_dates(root: Path):
        values = []
        for path in root.glob(f"{code}_daily_*.csv"):
            frame = pd.read_csv(path, usecols=[0], dtype=str)
            values.extend(frame.iloc[:, 0].tolist())
        return pd.DatetimeIndex(pd.to_datetime(values)).sort_values()

    old_dates = daily_dates(current_root)
    new_dates = daily_dates(proposed_root)
    if old_dates.empty or new_dates.empty:
        return None
    added = new_dates[~new_dates.isin(old_dates)]
    if added.empty or added.min() <= old_dates.max():
        return None
    terminal_period = old_dates.max().to_period("W-SUN")
    if added.min().to_period("W-SUN") == terminal_period:
        return terminal_period
    return None


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
    staging = create_temporary_directory(
        data_parent, "backtest-update", repository_root=context.root
    )
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
        mutable_week = _mutable_weekly_terminal_period(
            context.backtest_data_root,
            staging,
            code,
        )
        for current in context.backtest_data_root.glob(f"{code}_*.csv"):
            replacement = staging / current.name
            if not replacement.is_file():
                raise ValueError(f"{current.name}: update dropped a published file")
            _assert_append_only(
                current,
                replacement,
                mutable_terminal_period=(
                    mutable_week if "_weekly_" in current.name else None
                ),
            )
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
