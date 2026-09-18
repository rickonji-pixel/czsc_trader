"""TDR-owned, offline review snapshots built through SRT and DFLS contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

import pandas as pd
from dataflows import Dataflows
from strategy_runtime import (
    DeploymentSpec, StrategyRunner, canonical_sha256, publish_history,
    read_publication, write_publication,
)

from czsc_trader.backtesting.datasets import ReplayData, _fingerprint
from czsc_trader.backtesting.srt_bridge import execution_intraday_frequencies
from czsc_trader.data import MarketData
from czsc_trader.temp_workspace import create_temporary_directory


MANIFEST = "review_dataset.json"
TABLES = (
    "adjusted_daily", "adjusted_intraday", "adjusted_weekly",
    "execution_daily", "execution_intraday", "execution_five_minute", "signal_one_minute",
)


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return sha256(stream.read()).hexdigest()


def _child(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if Path(name).is_absolute() or path.parent != root.resolve():
        raise ValueError(f"review snapshot requires a direct child filename: {name}")
    return path


def _local_dataflows(root: Path, sources: list[dict]) -> Dataflows:
    """Read only explicitly hash-pinned canonical DFLS CSVs from the research pool."""
    if not isinstance(sources, list) or not sources:
        raise ValueError("review_data_sources must declare the controlled local inputs")
    catalog = {}
    for item in sources:
        if not isinstance(item, dict) or not {"dataset", "path", "sha256"} <= item.keys():
            raise ValueError("review data source requires dataset, path and sha256")
        if not isinstance(item.get("metadata", {}), dict) or not isinstance(item.get("dtypes", {}), dict):
            raise ValueError("review data source metadata and dtypes must be objects")
        key = (item["dataset"], item.get("symbol"), item.get("frequency", "daily"))
        if key in catalog:
            raise ValueError(f"duplicate review data source: {key}")
        relative = Path(item["path"])
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root.resolve()):
            raise ValueError("review data source must be inside the controlled research pool")
        if len(str(item["sha256"])) != 64:
            raise ValueError("review data source requires a SHA256")
        catalog[key] = (path, item)

    def fetch(request):
        key = (str(request.dataset), request.symbol, request.frequency)
        if key not in catalog:
            raise ValueError(f"no controlled source for SRT input: {key}")
        path, item = catalog[key]
        required_hash = request.options.get("source_sha256")
        if required_hash is not None and required_hash != item["sha256"]:
            raise ValueError("controlled source differs from SRT pinned feature evidence")
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError(f"controlled source hash mismatch: {item['path']}")
        frame = pd.read_csv(
            BytesIO(raw), compression="gzip" if path.suffix == ".gz" else None,
            float_precision="round_trip", dtype=item.get("dtypes"),
        )
        dates = pd.to_datetime(frame["Date"], errors="raise")
        end = pd.Timestamp(request.end)
        if len(request.end) == 10:
            end += pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
        frame = frame.loc[dates.between(pd.Timestamp(request.start), end)].reset_index(drop=True)
        return frame, {
            **item.get("metadata", {}), "vendor": "controlled-local",
            "source_path": item["path"], "source_sha256": item["sha256"],
        }

    # An explicit registry has no vendor/network fallback.
    return Dataflows({key[0]: fetch for key in catalog})


def verify_review_dataset(directory: Path, expected_hash: str | None = None) -> dict:
    raw = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    digest = raw.pop("snapshot_hash")
    if raw.get("schema_version") != 1 or canonical_sha256(raw) != digest:
        raise ValueError("review dataset manifest hash mismatch")
    if expected_hash is not None and expected_hash != digest:
        raise ValueError("review dataset differs from the sealed snapshot")
    if not raw.get("files") or set(raw.get("tables", {})) != set(TABLES):
        raise ValueError("review dataset manifest is incomplete")
    for name, expected in raw["files"].items():
        if _hash_file(_child(directory, name)) != expected:
            raise ValueError(f"review dataset file hash mismatch: {name}")
    for item in raw["tables"].values():
        if item is not None and item["file"] not in raw["files"]:
            raise ValueError("review dataset table is not hash-bound")
    return {**raw, "snapshot_hash": digest}


def load_review_dataset(directory: Path, expected_hash: str | None = None) -> ReplayData:
    manifest = verify_review_dataset(directory, expected_hash)
    frames = {}
    for name, item in manifest["tables"].items():
        if item is None:
            frames[name] = None
            continue
        frame = pd.read_csv(
            _child(directory, item["file"]), float_precision="round_trip",
            dtype={key: value for key, value in item["dtypes"].items() if key not in item["dates"]},
        )
        for column in item["dates"]:
            frame[column] = pd.to_datetime(frame[column], errors="raise")
        frames[name] = frame
    adjusted = MarketData(
        intraday=frames["adjusted_intraday"], daily=frames["adjusted_daily"],
        weekly=frames["adjusted_weekly"], hashes=manifest["market_hashes"],
        symbol=manifest["symbol"], asset_type=manifest["asset_type"],
        manifest=manifest["market_manifest"],
    )
    replay = ReplayData(
        "research", directory, adjusted, frames["execution_daily"], frames["execution_intraday"],
        "", date.fromisoformat(manifest["cutoff"]),
        frames["execution_five_minute"], frames["signal_one_minute"],
    )
    fingerprint = _fingerprint(
        "research", adjusted, replay.execution_daily, replay.execution_intraday,
        replay.execution_five_minute, replay.signal_one_minute,
    )
    if fingerprint != manifest["replay_fingerprint"]:
        raise ValueError("review replay fingerprint mismatch")
    return replace(replay, fingerprint=fingerprint)


def publish_review_dataset(context, manifest: dict, protocol, directory: Path) -> dict:
    """Publish once; incomplete staging never becomes a usable review dataset."""
    from czsc_trader.candidate_evaluation import (
        CandidateEvaluationContext, _snapshot, prepare_evaluation_workspace,
    )

    recipe_hash = canonical_sha256({"manifest": manifest, "protocol": protocol.to_dict()})
    if directory.exists():
        existing = verify_review_dataset(directory)
        if existing["recipe_hash"] != recipe_hash:
            raise ValueError("review dataset already exists for different evaluation inputs")
        return existing
    periods = tuple((name, (pd.Timestamp(window["start"]), pd.Timestamp(window["end"])))
                    for name, window in manifest["windows"].items())
    run = CandidateEvaluationContext(
        context, manifest["symbol"], manifest.get("asset_type", "etf"), periods,
        family_id=manifest["strategy_id"],
    )
    strategies = [_snapshot(run, item)[1] for item in manifest["candidates"]]
    if not strategies or len({s.definition.release_id for s in strategies}) != len(strategies):
        raise ValueError("review requires non-empty, unique runtime identities")
    replay = prepare_evaluation_workspace(
        run, protocol, include_five_minute=any(execution_intraday_frequencies(s) for s in strategies),
    ).replay_data
    dataflows = _local_dataflows(context.research_data_root, manifest.get("review_data_sources", []))
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Retain failed staging for diagnosis; only the final atomic rename publishes READY.
    staging = create_temporary_directory(context.root, "review-data", repository_root=context.root)
    runtimes = {}
    for strategy in strategies:
        definition = strategy.definition
        deployment = DeploymentSpec(
            "review", definition.release_id, definition.release_hash, run.symbol,
            "review", "review", {"repository_root": str(context.root)},
        )
        publication = publish_history(
            strategy, dataflows, deployment,
            start=pd.to_datetime(replay.adjusted.daily["dt"]).min().date(), through=replay.cutoff,
        )
        if not publication.ready:
            raise ValueError(f"review publication failed for {definition.release_id}: {publication.error}")
        StrategyRunner.validate_publication(strategy, publication)
        write_publication(publication, staging)
        StrategyRunner.validate_publication(strategy, read_publication(staging, definition.release_id))
        runtimes[definition.release_id] = definition.runtime_sha256
    frames = {
        "adjusted_daily": replay.adjusted.daily, "adjusted_intraday": replay.adjusted.intraday,
        "adjusted_weekly": replay.adjusted.weekly,
        **{key: getattr(replay, key) for key in TABLES[3:]},
    }
    tables = {}
    for name, frame in frames.items():
        if frame is None:
            tables[name] = None
            continue
        filename = f"{name}.csv.gz"
        frame.to_csv(staging / filename, index=False, compression={"method": "gzip", "mtime": 0})
        tables[name] = {"file": filename,
                        "dtypes": {col: str(dtype) for col, dtype in frame.dtypes.items()},
                        "dates": [col for col in frame if pd.api.types.is_datetime64_any_dtype(frame[col])]}
    content = {
        "schema_version": 1, "recipe_hash": recipe_hash,
        "symbol": run.symbol, "asset_type": run.asset_type, "cutoff": replay.cutoff.isoformat(),
        "replay_fingerprint": replay.fingerprint, "market_hashes": replay.adjusted.hashes,
        "market_manifest": replay.adjusted.manifest, "runtimes": runtimes,
        "tables": tables, "sources": manifest["review_data_sources"],
        "files": {path.name: _hash_file(path) for path in sorted(staging.iterdir())},
    }
    content["snapshot_hash"] = canonical_sha256(content)
    (staging / MANIFEST).write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    load_review_dataset(staging, content["snapshot_hash"])
    staging.rename(directory)
    return content
