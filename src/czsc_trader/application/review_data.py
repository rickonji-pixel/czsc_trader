"""TDR-owned, offline review snapshots built through SRT and DFLS contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
from strategy_runtime import (
    canonical_sha256,
)

from czsc_trader.backtesting.datasets import ReplayData, _fingerprint
from czsc_trader.backtesting.srt_bridge import (
    build_srt_signal_replay,
    execution_intraday_frequencies,
)
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


def _snapshot_file(root: Path, name: str) -> Path:
    relative = Path(name)
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError(f"review snapshot contains an unsafe path: {name}")
    return path


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
        if _hash_file(_snapshot_file(directory, name)) != expected:
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
    snapshots = [_snapshot(run, item) for item in manifest["candidates"]]
    strategies = [item[1] for item in snapshots]
    if not strategies or len({s.release_id for s in strategies}) != len(strategies):
        raise ValueError("review requires non-empty, unique runtime identities")
    replay = prepare_evaluation_workspace(
        run, protocol, include_five_minute=any(execution_intraday_frequencies(s) for s in strategies),
    ).replay_data
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Retain failed staging for diagnosis; only the final atomic rename publishes READY.
    staging = create_temporary_directory(context.root, "review-data", repository_root=context.root)
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
    sealed_replay = replace(replay, root=staging)
    runtimes = {}
    for snapshot, definition in snapshots:
        for _, (start, end) in periods:
            build_srt_signal_replay(
                snapshot=snapshot,
                replay_data=sealed_replay,
                start=start,
                end=end,
                repository_root=context.root,
            )
        runtimes[definition.release_id] = definition.runtime_sha256
    content = {
        "schema_version": 1, "recipe_hash": recipe_hash,
        "symbol": run.symbol, "asset_type": run.asset_type, "cutoff": replay.cutoff.isoformat(),
        "replay_fingerprint": replay.fingerprint, "market_hashes": replay.adjusted.hashes,
        "market_manifest": replay.adjusted.manifest, "runtimes": runtimes,
        "tables": tables,
        "files": {
            path.relative_to(staging).as_posix(): _hash_file(path)
            for path in sorted(item for item in staging.rglob("*") if item.is_file())
        },
    }
    content["snapshot_hash"] = canonical_sha256(content)
    (staging / MANIFEST).write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    load_review_dataset(staging, content["snapshot_hash"])
    staging.rename(directory)
    return content
