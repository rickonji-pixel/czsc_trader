from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
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
    strategy_id: str
    strategy_version: str


@dataclass(frozen=True)
class PrepareStrategySupportCommand:
    strategy_id: str
    strategy_version: str
    through: date


@dataclass(frozen=True)
class PublishRuntimeDataCommand:
    """Publish one complete, strategy-aware runtime data generation."""

    symbol: str
    asset_type: str
    start: date
    through: date
    strategy_releases: tuple[tuple[str, str], ...]


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


def _runtime_supports(snapshot) -> tuple[tuple[str, object], ...]:
    """Return every release-owned, point-in-time input required at runtime."""
    rule = getattr(snapshot, "resolved_rule", None)
    constituent = getattr(rule, "constituent_moneyflow_intraday", None)
    causal = getattr(rule, "causal_feature_gate", None)
    if constituent is not None:
        return (("constituent_moneyflow", constituent),)
    if causal is not None:
        return (("causal_feature", causal),)
    return ()


def _generation_supports(snapshot, contract, dataset: str) -> tuple[tuple[str, object], ...]:
    """Apply one contract in the target dataset's execution context."""
    if dataset == "runtime":
        return _runtime_supports(snapshot)
    if "causal_feature_panel" in contract.strategy_support:
        causal = getattr(getattr(snapshot, "resolved_rule", None), "causal_feature_gate", None)
        if causal is None:
            raise ValueError("causal-feature strategy support specification is missing")
        return (("causal_feature", causal),)
    return ()


def _generation_id(
    *, symbol: str, asset_type: str, data_cutoff: str, releases: tuple[str, ...], files: list[Path],
) -> str:
    digest = sha256()
    digest.update(symbol.encode("utf-8"))
    digest.update(asset_type.encode("utf-8"))
    digest.update(data_cutoff.encode("utf-8"))
    for value in releases:
        digest.update(value.encode("utf-8"))
    for path in sorted(files, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return f"GEN-{digest.hexdigest()[:16].upper()}"


def _commit_generation(
    staging: Path,
    target: Path,
    files: list[Path],
    *,
    append_only: bool,
    code: str,
) -> None:
    """Replace one fully-built generation, restoring the previous one on failure."""
    if append_only:
        mutable_week = _mutable_weekly_terminal_period(target, staging, code)
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
                _assert_append_only(current, source)

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
    except Exception:
        for destination in reversed(published):
            destination.unlink(missing_ok=True)
        for destination, saved in moved:
            if saved.exists():
                saved.replace(destination)
        raise


def _publish_strategy_generation(
    context: RepositoryContext,
    *,
    symbol: str,
    asset_type: str,
    start: date,
    through: date,
    snapshots: tuple,
    target: Path,
    dataset: str,
    append_only: bool,
) -> CommandResult:
    """Build, validate, and commit all market and release support inputs together."""
    from czsc_trader.backtesting import resolve_backtest_data_contract
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
        contracts = tuple(resolve_backtest_data_contract(snapshot) for snapshot in snapshots)
        intraday_summary: dict[str, object] | None = None
        if any(contract.intraday_frequencies for contract in contracts):
            from czsc_trader.intraday_data import prepare_intraday_research_data

            intraday_summary = prepare_intraday_research_data(
                symbol, start, through, staging, env_file=context.root / ".env"
            )

        support: list[dict[str, object]] = []
        for snapshot, contract in zip(snapshots, contracts, strict=True):
            for kind, spec in _generation_supports(snapshot, contract, dataset):
                if kind == "constituent_moneyflow":
                    from czsc_trader.constituent_moneyflow_runtime import publish_support_data
                else:
                    from czsc_trader.causal_feature_gate_runtime import publish_support_data
                support.append({
                    **publish_support_data(
                        context.root, staging, snapshot.identity.reference, spec,
                        through.isoformat(),
                    ),
                    "support_type": kind,
                })

        data_cutoff = str(summary.get("data_cutoff") or through.isoformat())
        for item in support:
            item.setdefault("data_cutoff", data_cutoff)
        if any(str(item["data_cutoff"]) != data_cutoff for item in support):
            raise ValueError("strategy support cutoff differs from market data cutoff")
        files = [path for path in staging.iterdir() if path.is_file()]
        releases = tuple(snapshot.identity.reference for snapshot in snapshots)
        generation_id = _generation_id(
            symbol=symbol.upper(), asset_type=asset_type, data_cutoff=data_cutoff,
            releases=releases, files=files,
        )
        generation = {
            "schema_version": 1,
            "generation_id": generation_id,
            "dataset": dataset,
            "symbol": symbol.upper(),
            "asset_type": asset_type,
            "data_cutoff": data_cutoff,
            "strategy_releases": list(releases),
            "data_contracts": [contract.as_dict() for contract in contracts],
            "files": {
                path.name: sha256(path.read_bytes()).hexdigest()
                for path in sorted(files, key=lambda item: item.name)
            },
        }
        generation_path = staging / f"{code}_strategy_generation.json"
        generation_path.write_text(
            json.dumps(generation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        files.append(generation_path)
        _commit_generation(
            staging, target, files, append_only=append_only, code=code,
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
            "data_contracts": [contract.as_dict() for contract in contracts],
            "intraday": intraday_summary,
            "strategy_support": support,
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
    from czsc_trader.backtesting import (
        resolve_backtest_data_contract,
        resolve_registered_strategy,
    )

    code = request.symbol.split(".", 1)[0]
    try:
        snapshot = resolve_registered_strategy(
            context, request.strategy_id, request.strategy_version
        )
        if (
            resolve_backtest_data_contract(snapshot).intraday_frequencies
            and request.asset_type != "etf"
        ):
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
        snapshots=(snapshot,),
        target=context.backtest_data_root,
        dataset="backtest",
        append_only=True,
    )
    return CommandResult(
        result.status,
        "data.update-backtest",
        {**result.result, "strategy": snapshot.identity.reference,
         "data_contract": result.result["data_contracts"][0],
         "strategy_support": (
             result.result["strategy_support"][0]
             if len(result.result["strategy_support"]) == 1
             else result.result["strategy_support"] or None
         )},
        result.artifacts,
    )


def publish_runtime_data(
    context: RepositoryContext,
    request: PublishRuntimeDataCommand,
) -> CommandResult:
    """Publish a complete PTE generation for every release sharing one instrument."""
    from czsc_trader.backtesting import resolve_registered_strategy

    if not request.strategy_releases:
        raise ValidationError(
            "runtime_data_contract_invalid",
            "runtime data publication requires at least one strategy release",
            context={"symbol": request.symbol},
        )
    try:
        snapshots = tuple(
            resolve_registered_strategy(context, strategy_id, version)
            for strategy_id, version in request.strategy_releases
        )
        for snapshot in snapshots:
            execution = snapshot.resolved_rule.execution
            rule_symbol = str(
                execution.instrument.symbol if execution is not None else snapshot.resolved_rule.symbol
            ).upper()
            if rule_symbol != request.symbol.upper():
                raise ValueError(
                    f"{snapshot.identity.reference}: strategy symbol {rule_symbol} "
                    f"differs from publication symbol {request.symbol.upper()}"
                )
    except Exception as exc:
        raise ValidationError(
            "runtime_data_contract_invalid",
            str(exc),
            context={"symbol": request.symbol},
        ) from exc
    return _publish_strategy_generation(
        context,
        symbol=request.symbol,
        asset_type=request.asset_type,
        start=request.start,
        through=request.through,
        snapshots=snapshots,
        target=context.raw_dir,
        dataset="runtime",
        append_only=False,
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


def prepare_strategy_support_data(
    context: RepositoryContext,
    request: PrepareStrategySupportCommand,
) -> CommandResult:
    """Publish extra point-in-time inputs required by one deployable strategy release."""
    from strategy_manager import StrategyRegistry

    from czsc_trader.baselines import resolve_strategy_payload
    from czsc_trader.causal_feature_gate_runtime import (
        publish_support_data as publish_causal_feature_support,
    )
    from czsc_trader.constituent_moneyflow_runtime import (
        publish_support_data as publish_constituent_support,
    )

    try:
        registry = StrategyRegistry(context.strategy_root)
        release = registry.resolve_strategy(request.strategy_id, request.strategy_version)
        registry.assert_deployable(release.strategy_id, release.version, "PAPER")
        if release.release_hash is None:
            raise ValueError("deployable strategy version must have a release hash")
        rule = release.strategy_payload.get("rule")
        rule_symbol = rule.get("symbol") if isinstance(rule, dict) else None
        strategy_symbol = release.strategy_payload.get("symbol") or rule_symbol
        if not isinstance(strategy_symbol, str) or not strategy_symbol.strip():
            strategy = registry.get_strategy(release.strategy_id)
            if isinstance(strategy.scope, list) and len(strategy.scope) == 1:
                strategy_symbol = strategy.scope[0]
        if not isinstance(strategy_symbol, str) or not strategy_symbol.strip():
            raise ValueError("strategy support data requires one explicit symbol")
        resolved = resolve_strategy_payload(
            context.strategy_dependency_root,
            release.strategy_payload,
            release_id=release.release_id,
            release_hash=release.release_hash,
            symbol=strategy_symbol,
            repository_root=context.root,
        )
        if resolved.strategy == "constituent_moneyflow_intraday_overlay":
            spec = resolved.constituent_moneyflow_intraday
            if spec is None:
                raise ValueError("intraday overlay strategy support specification is missing")
            result = {
                **publish_constituent_support(
                    context.root,
                    context.raw_dir,
                    release.release_id,
                    spec,
                    request.through.isoformat(),
                ),
                "support_required": True,
            }
        elif resolved.strategy == "causal_feature_gate":
            spec = resolved.causal_feature_gate
            if spec is None:
                raise ValueError("causal-feature strategy support specification is missing")
            result = {
                **publish_causal_feature_support(
                    context.root,
                    context.raw_dir,
                    release.release_id,
                    spec,
                    request.through.isoformat(),
                ),
                "support_required": True,
            }
        else:
            result = {
                "release_id": release.release_id,
                "data_cutoff": request.through.isoformat(),
                "support_required": False,
            }
    except Exception as exc:
        raise ValidationError(
            "strategy_support_data_preparation_failed",
            str(exc),
            context={
                "strategy": request.strategy_id,
                "strategy_version": request.strategy_version,
                "through": request.through.isoformat(),
            },
        ) from exc
    return CommandResult(
        status="PASS",
        command="data.prepare-strategy-support",
        result=result,
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
