from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import shutil
from collections.abc import Callable

from czsc_trader.data import load_execution_prices, load_market_data
from czsc_trader.generation_integrity import file_sha256, validate_strategy_generation
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
    strategy_id: str
    strategy_version: str


def _initial_backtest_start(context: RepositoryContext, code: str) -> date:
    research_manifest = context.research_data_root / f"{code}_manifest.json"
    if not research_manifest.is_file():
        return date(2010, 1, 1)
    payload = json.loads(research_manifest.read_text(encoding="utf-8"))
    try:
        return date.fromisoformat(str(payload["requested_start"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"{research_manifest.name}: missing or invalid requested_start"
        ) from exc


def _append_only_keys(
    old,
    new,
    current: Path,
    declared: tuple[str, ...] | None = None,
) -> list[str]:
    """Resolve a stable key for time-series and cross-sectional inputs."""

    keys = list(declared or (str(old.columns[0]),))
    if not keys or any(key not in old or key not in new for key in keys):
        raise ValueError(f"{current.name}: invalid primary key")
    time_key = keys[0]
    if declared is None and old[time_key].duplicated().any():
        for candidate in ("ConstituentSymbol", "Symbol", "symbol"):
            if candidate in old and candidate in new:
                keys.append(candidate)
                break
    if old.duplicated(keys).any() or new.duplicated(keys).any():
        raise ValueError(f"{current.name}: invalid primary key")
    return keys


def _assert_append_only(
    current: Path,
    proposed: Path,
    *,
    mutable_terminal_period=None,
    primary_key: tuple[str, ...] | None = None,
) -> None:
    import pandas as pd

    old = pd.read_csv(current, dtype=str).fillna("")
    new = pd.read_csv(proposed, dtype=str).fillna("")
    keys = _append_only_keys(old, new, current, primary_key)
    key = keys[0]
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
    aligned = new.set_index(keys).reindex(immutable.set_index(keys).index)
    if aligned.isna().any().any() or not immutable.set_index(keys).equals(aligned):
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


def _srt_primary_keys(staging: Path) -> dict[str, tuple[str, ...]]:
    """Read DFLS primary keys from SRT publication manifests in one generation."""

    result: dict[str, tuple[str, ...]] = {}
    for path in staging.glob("srt_*_publication.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        inputs = payload.get("inputs") if isinstance(payload, dict) else None
        if not isinstance(inputs, dict):
            raise ValueError(f"{path.name}: invalid SRT publication manifest")
        for value in inputs.values():
            identity = value.get("identity") if isinstance(value, dict) else None
            metadata = identity.get("metadata") if isinstance(identity, dict) else None
            filename = value.get("file") if isinstance(value, dict) else None
            primary_key = metadata.get("primary_key") if isinstance(metadata, dict) else None
            if isinstance(filename, str) and isinstance(primary_key, list) and primary_key:
                result[filename] = tuple(str(item) for item in primary_key)
    return result


def _runtime_data_contract(definition) -> dict[str, object]:
    """Describe the sole SRT-owned input contract and TDR channel needs."""

    from czsc_trader.backtesting.srt_bridge import execution_intraday_frequencies

    return {
        "source": "SRT",
        "release_id": definition.release_id,
        "runtime_sha256": definition.runtime_sha256,
        "history": asdict(definition.history),
        "inputs": [asdict(item) for item in definition.inputs.requirements],
        "execution_intraday_frequencies": list(
            execution_intraday_frequencies(definition)
        ),
    }


def _publish_runtime_history(
    context: RepositoryContext,
    *,
    definition,
    symbol: str,
    start: date,
    through: date,
    staging: Path,
) -> dict[str, object]:
    """Ask SRT to publish and validate all historical strategy inputs."""

    from dataflows import Dataflows
    from strategy_runtime import (
        publish_history,
        validate_publication,
        write_publication,
    )

    publication = publish_history(
        definition,
        Dataflows(),
        symbol=symbol,
        start=start,
        through=through,
        settings={
            "env_file": str(context.root / ".env"),
            "repository_root": str(context.root),
        },
    )
    validate_publication(definition, publication)
    if not publication.ready:
        raise ValueError(
            "SRT historical publication is not ready: "
            f"status={publication.status.value}, error={publication.error or 'UNKNOWN'}"
        )
    if publication.requested_cutoff != through.isoformat():
        raise ValueError("SRT historical publication cutoff differs from requested cutoff")
    manifest = write_publication(publication, staging)
    return {
        "status": publication.status.value,
        "release_id": definition.release_id,
        "release_hash": definition.release_hash,
        "runtime_sha256": definition.runtime_sha256,
        "requested_cutoff": publication.requested_cutoff,
        "manifest": manifest.name,
        "inputs": {
            name: {
                "dataset": result.identity.dataset,
                "subject": result.identity.symbol,
                "data_start": result.identity.data_start,
                "data_cutoff": result.identity.data_cutoff,
                "content_sha256": result.identity.content_sha256,
            }
            for name, result in sorted(publication.input_results.items())
        },
    }


def _generation_id(
    *,
    symbol: str,
    asset_type: str,
    data_cutoff: str,
    releases: tuple[str, ...],
    file_hashes: dict[str, str],
) -> str:
    digest = sha256()
    digest.update(symbol.encode("utf-8"))
    digest.update(asset_type.encode("utf-8"))
    digest.update(data_cutoff.encode("utf-8"))
    for value in releases:
        digest.update(value.encode("utf-8"))
    for filename, value in sorted(file_hashes.items()):
        digest.update(filename.encode("utf-8"))
        digest.update(value.encode("ascii"))
    return f"GEN-{digest.hexdigest()[:16].upper()}"


def _commit_generation(
    staging: Path,
    target: Path,
    files: list[Path],
    *,
    append_only: bool,
    code: str,
    verify: Callable[[], None] | None = None,
) -> None:
    """Replace one fully-built generation, restoring the previous one on failure."""
    if append_only:
        mutable_week = _mutable_weekly_terminal_period(target, staging, code)
        srt_primary_keys = _srt_primary_keys(staging)
        for current in target.glob(f"{code}_*.csv"):
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
        for source in files:
            current = target / source.name
            if current.is_file() and source.suffixes[-2:] == [".csv", ".gz"]:
                _assert_append_only(
                    current,
                    source,
                    primary_key=srt_primary_keys.get(source.name),
                )

    target.mkdir(parents=True, exist_ok=True)
    backup = staging / "backup"
    backup.mkdir()
    published: list[Path] = []
    moved: list[tuple[Path, Path]] = []
    try:
        for source in files:
            destination = target / source.name
            if destination.exists():
                saved = backup / source.name
                destination.replace(saved)
                moved.append((destination, saved))
            source.replace(destination)
            published.append(destination)
        if verify is not None:
            verify()
    except Exception:
        for destination in reversed(published):
            destination.unlink(missing_ok=True)
        for destination, saved in moved:
            if saved.exists():
                saved.replace(destination)
        raise


def _verify_committed_generation(
    target: Path,
    *,
    symbol: str,
    asset_type: str,
    dataset: str,
    release_id: str,
    definition,
    data_cutoff: str,
) -> None:
    import pandas as pd
    from strategy_runtime import PublishedDataSource

    market = load_market_data(target, symbol, asset_type)
    execution = load_execution_prices(target, symbol, asset_type)
    market_sessions = pd.DatetimeIndex(pd.to_datetime(market.daily["dt"]).dt.normalize())
    execution_sessions = pd.DatetimeIndex(
        pd.to_datetime(execution["dt"]).dt.normalize()
    )
    if not market_sessions.equals(execution_sessions):
        raise ValueError("committed adjusted and execution daily sessions differ")
    committed_cutoff = PublishedDataSource(target).verify(definition)
    if committed_cutoff.isoformat() != data_cutoff:
        raise ValueError(
            "committed SRT publication cutoff differs from market data cutoff"
        )


def _publish_strategy_generation(
    context: RepositoryContext,
    *,
    symbol: str,
    asset_type: str,
    start: date,
    through: date,
    definition,
    target: Path,
    dataset: str,
    append_only: bool,
) -> CommandResult:
    """Build, validate, and commit all market and release support inputs together."""
    from czsc_trader.market_data_prep import prepare_market_data

    code = symbol.split(".", 1)[0]
    data_parent = context.root / "data"
    data_parent.mkdir(parents=True, exist_ok=True)
    staging = create_temporary_directory(
        data_parent, f"{dataset}-generation", repository_root=context.root
    )
    try:
        summary = prepare_market_data(
            symbol, asset_type, start, through, staging, env_file=context.root / ".env"
        )
        contract = _runtime_data_contract(definition)
        intraday_summary: dict[str, object] | None = None
        if contract["execution_intraday_frequencies"]:
            from czsc_trader.intraday_data import prepare_intraday_research_data

            intraday_summary = prepare_intraday_research_data(
                symbol, start, through, staging, env_file=context.root / ".env"
            )

        runtime_publication = _publish_runtime_history(
            context,
            definition=definition,
            symbol=symbol,
            start=start,
            through=through,
            staging=staging,
        )

        raw_data_cutoff = summary.get("data_cutoff")
        if not isinstance(raw_data_cutoff, str) or not raw_data_cutoff.strip():
            raise ValueError("market data publication did not declare a data cutoff")
        data_cutoff = raw_data_cutoff
        if runtime_publication["requested_cutoff"] != data_cutoff:
            raise ValueError("SRT publication cutoff differs from market data cutoff")
        files = [path for path in staging.iterdir() if path.is_file()]
        current_release = definition.release_id
        previous_marker = target / f"{code}_strategy_generation.json"
        if previous_marker.is_file():
            validate_strategy_generation(
                target,
                symbol=symbol,
                asset_type=asset_type,
                dataset=dataset,
            )
        previous = (
            json.loads(previous_marker.read_text(encoding="utf-8"))
            if previous_marker.is_file()
            else {}
        )
        prior_releases = {
            str(item)
            for item in previous.get("strategy_releases", [])
            if str(item) != current_release
        }
        releases = tuple(sorted({current_release, *prior_releases}))
        staged_names = {path.name for path in files}
        file_hashes: dict[str, str] = {
            path.name: file_sha256(path) for path in files
        }
        previous_files = previous.get("files", {})
        if isinstance(previous_files, dict):
            for filename, expected in previous_files.items():
                if filename in staged_names or filename == previous_marker.name:
                    continue
                source = target / str(filename)
                if not source.is_file():
                    raise ValueError(
                        f"previous generation file is missing: {filename}"
                    )
                actual = file_sha256(source)
                if actual != str(expected).lower():
                    raise ValueError(
                        f"previous generation file hash differs: {filename}"
                    )
                file_hashes[str(filename)] = actual
        generation_id = _generation_id(
            symbol=symbol.upper(), asset_type=asset_type, data_cutoff=data_cutoff,
            releases=releases, file_hashes=file_hashes,
        )
        prior_contracts = [
            item
            for item in previous.get("data_contracts", [])
            if isinstance(item, dict) and item.get("release_id") != current_release
        ]
        prior_publications = [
            item
            for item in previous.get("runtime_publications", [])
            if isinstance(item, dict) and item.get("release_id") != current_release
        ]
        generation = {
            "schema_version": 1,
            "generation_id": generation_id,
            "dataset": dataset,
            "symbol": symbol.upper(),
            "asset_type": asset_type,
            "data_cutoff": data_cutoff,
            "strategy_releases": list(releases),
            "data_contracts": [*prior_contracts, contract],
            "runtime_publications": [*prior_publications, runtime_publication],
            "files": dict(sorted(file_hashes.items())),
        }
        generation_path = staging / f"{code}_strategy_generation.json"
        generation_path.write_text(
            json.dumps(generation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        files.append(generation_path)

        _commit_generation(
            staging,
            target,
            files,
            append_only=append_only,
            code=code,
            verify=lambda: _verify_committed_generation(
                target,
                symbol=symbol,
                asset_type=asset_type,
                dataset=dataset,
                release_id=current_release,
                definition=definition,
                data_cutoff=data_cutoff,
            ),
        )
    except Exception as exc:
        raise ValidationError(
            f"{dataset}_data_publication_failed",
            str(exc),
            context={"symbol": symbol, "through": through.isoformat()},
        ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return CommandResult(
        "PASS",
        f"data.publish-{dataset}",
        {
            **summary,
            "generation_id": generation_id,
            "strategy_releases": list(releases),
            "data_contracts": [contract],
            "intraday": intraday_summary,
            "runtime_publication": runtime_publication,
            "dataset": dataset,
            "through": through.isoformat(),
        },
        {"manifest": str(target / f"{code}_manifest.json")},
    )


def update_backtest_data(
    context: RepositoryContext,
    request: UpdateBacktestDataCommand,
) -> CommandResult:
    """Publish a strategy-aware, append-compatible backtest generation."""
    from strategy_manager import StrategyRegistry
    from strategy_runtime import StrategyRelease, StrategyRuntime

    code = request.symbol.split(".", 1)[0]
    try:
        version = StrategyRegistry(context.strategy_root).get_version(
            request.strategy_id, request.strategy_version
        )
        release = StrategyRelease.from_mapping(version.to_dict())
        definition = StrategyRuntime().describe(release, symbol=request.symbol)
        contract = _runtime_data_contract(definition)
        if contract["execution_intraday_frequencies"] and request.asset_type != "etf":
            raise ValueError("strategy requires ETF intraday data")
    except Exception as exc:
        raise ValidationError(
            "backtest_data_contract_invalid",
            str(exc),
            context={
                "strategy": f"{request.strategy_id}-{request.strategy_version}",
                "symbol": request.symbol,
            },
        ) from exc
    current_manifest = context.backtest_data_root / f"{code}_manifest.json"
    start = (
        date.fromisoformat(str(json.loads(current_manifest.read_text(encoding="utf-8"))["requested_start"]))
        if current_manifest.is_file()
        else _initial_backtest_start(context, code)
    )
    result = _publish_strategy_generation(
        context,
        symbol=request.symbol,
        asset_type=request.asset_type,
        start=start,
        through=request.through,
        definition=definition,
        target=context.backtest_data_root,
        dataset="backtest",
        append_only=True,
    )
    return CommandResult(
        result.status,
        "data.update-backtest",
        {**result.result, "strategy": definition.release_id,
         "data_contract": result.result["data_contracts"][0],
         "runtime_publication": result.result["runtime_publication"]},
        result.artifacts,
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
