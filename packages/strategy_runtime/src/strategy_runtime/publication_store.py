"""Portable on-disk storage for one validated SRT data publication."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

import pandas as pd
from dataflows import (
    DataIdentity,
    DataRequest,
    DataResult,
    DataStatus,
    canonical_frame_sha256,
)

from .errors import RuntimeContractError
from .models import (
    ExecutionPricingData,
    PublicationStatus,
    PublishedStrategyData,
    StrategyRuntimeContext,
)
from .protocols import ExecutableStrategy


_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return _json_value(value.item())
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise RuntimeContractError(f"publication metadata is not JSON compatible: {type(value)}")


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def publication_manifest_name(release_id: str) -> str:
    safe = release_id.lower().replace("-", "_")
    if _SAFE_NAME.fullmatch(safe) is None:
        raise RuntimeContractError("release ID cannot form a safe publication filename")
    return f"srt_{safe}_publication.json"


def _strategy_symbol(strategy: ExecutableStrategy) -> str:
    subjects = {
        str(requirement.subject).upper()
        for requirement in strategy.definition.inputs.requirements
        if requirement.subject
        and requirement.dataset.startswith(("etf.", "stock."))
        and re.fullmatch(r"\d{6}\.(?:SH|SZ)", str(requirement.subject).upper())
    }
    if len(subjects) != 1:
        raise RuntimeContractError(
            "strategy publication must declare exactly one A-share instrument"
        )
    return next(iter(subjects))


def _definition_symbol(definition) -> str:
    subjects = {
        str(requirement.subject).upper()
        for requirement in definition.inputs.requirements
        if requirement.subject
        and requirement.dataset.startswith(("etf.", "stock."))
        and re.fullmatch(r"\d{6}\.(?:SH|SZ)", str(requirement.subject).upper())
    }
    if len(subjects) != 1:
        raise RuntimeContractError(
            "strategy publication must declare exactly one A-share instrument"
        )
    return next(iter(subjects))


def _read_bound_generation(
    directory: Path,
    *,
    release_id: str,
    symbol: str,
) -> dict[str, object]:
    root = Path(directory).resolve()
    marker = root / f"{symbol.split('.', 1)[0]}_strategy_generation.json"
    try:
        generation = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeContractError(f"cannot read SRT generation: {exc}") from exc
    if not isinstance(generation, dict) or generation.get("schema_version") not in {1, 2}:
        raise RuntimeContractError("SRT generation schema is unsupported")
    if str(generation.get("symbol", "")).upper() != symbol:
        raise RuntimeContractError("SRT generation symbol differs from strategy")
    if generation.get("asset_type") not in {"etf", "stock"}:
        raise RuntimeContractError("SRT generation asset type is invalid")
    dataset = generation.get("dataset")
    if not isinstance(dataset, str) or not dataset:
        raise RuntimeContractError("SRT generation dataset is invalid")
    try:
        date.fromisoformat(str(generation["data_cutoff"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeContractError("SRT generation data cutoff is invalid") from exc
    generation_id = generation.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise RuntimeContractError("SRT generation identity is invalid")
    releases = generation.get("strategy_releases")
    if (
        not isinstance(releases, list)
        or not releases
        or any(not isinstance(item, str) or not item for item in releases)
    ):
        raise RuntimeContractError("SRT generation strategy releases are invalid")
    if release_id not in releases:
        raise RuntimeContractError(
            f"SRT generation does not contain strategy publication: {release_id}"
        )
    files = generation.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeContractError("SRT generation has no authenticated files")
    manifest_name = publication_manifest_name(release_id)
    if manifest_name not in files:
        raise RuntimeContractError(
            f"SRT generation does not bind strategy publication: {release_id}"
        )
    for name, expected in files.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(expected, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        ):
            raise RuntimeContractError("SRT generation file identity is invalid")
        path = (root / name).resolve()
        if path.parent != root:
            raise RuntimeContractError("SRT generation file path escapes publication root")
        try:
            actual = _file_sha256(path)
        except OSError as exc:
            raise RuntimeContractError(
                f"SRT generation file is unavailable: {name}: {exc}"
            ) from exc
        if actual != expected:
            raise RuntimeContractError(f"SRT generation file was modified: {name}")
    return generation


def write_publication(publication: PublishedStrategyData, directory: Path) -> Path:
    """Write a READY publication and its frames without weakening its identity."""

    if not publication.ready:
        raise RuntimeContractError("only READY publications can be stored")
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    safe_release = publication.release_id.lower().replace("-", "_")
    inputs: dict[str, object] = {}
    for name in sorted(publication.input_results):
        if _SAFE_NAME.fullmatch(name) is None:
            raise RuntimeContractError(f"unsafe publication input name: {name}")
        request = publication.input_requests[name]
        result = publication.input_results[name]
        if not result.ready or result.identity is None:
            raise RuntimeContractError(f"publication input is not READY: {name}")
        filename = f"srt_{safe_release}_{name}.csv.gz"
        path = target / filename
        result.dataframe.to_csv(
            path,
            index=False,
            compression={"method": "gzip", "compresslevel": 6, "mtime": 0},
        )
        stored_frame = pd.read_csv(path)
        identity = result.identity
        inputs[name] = {
            "file": filename,
            "file_sha256": _file_sha256(path),
            "stored_content_sha256": canonical_frame_sha256(stored_frame),
            "request": {
                "dataset": str(request.dataset),
                "symbol": request.symbol,
                "start": request.start,
                "end": request.end,
                "required_cutoff": request.required_cutoff,
                "frequency": request.frequency,
                "options": _json_value(request.options),
            },
            "identity": {
                "dataset": identity.dataset,
                "source": identity.source,
                "symbol": identity.symbol,
                "data_start": identity.data_start,
                "data_cutoff": identity.data_cutoff,
                "content_sha256": identity.content_sha256,
                "metadata": _json_value(identity.metadata),
            },
        }
    manifest = {
        "schema_version": 2,
        "release_id": publication.release_id,
        "release_hash": publication.release_hash,
        "status": publication.status.value,
        "requested_cutoff": publication.requested_cutoff,
        "inputs": inputs,
    }
    path = target / publication_manifest_name(publication.release_id)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_publication(directory: Path, release_id: str) -> PublishedStrategyData:
    """Read a stored publication, rejecting missing, modified, or incomplete data."""

    root = Path(directory)
    manifest_path = root / publication_manifest_name(release_id)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeContractError(f"cannot read SRT publication: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") not in {1, 2}
        or manifest.get("release_id") != release_id
        or manifest.get("status") != PublicationStatus.READY.value
    ):
        raise RuntimeContractError("stored SRT publication identity or status is invalid")
    raw_inputs = manifest.get("inputs")
    if not isinstance(raw_inputs, dict) or not raw_inputs:
        raise RuntimeContractError("stored SRT publication has no inputs")
    requests: dict[str, DataRequest] = {}
    results: dict[str, DataResult] = {}
    for name, raw in raw_inputs.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise RuntimeContractError("stored SRT publication input is invalid")
        filename = raw.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise RuntimeContractError("stored SRT publication filename is unsafe")
        path = root / filename
        try:
            actual_sha256 = _file_sha256(path)
        except OSError as exc:
            raise RuntimeContractError(
                f"cannot read stored SRT publication file: {filename}: {exc}"
            ) from exc
        if actual_sha256 != raw.get("file_sha256"):
            raise RuntimeContractError(f"stored SRT publication file was modified: {filename}")
        request_value = raw.get("request")
        identity_value = raw.get("identity")
        if not isinstance(request_value, dict) or not isinstance(identity_value, dict):
            raise RuntimeContractError("stored SRT request or identity is invalid")
        request = DataRequest(**request_value)
        identity = DataIdentity(**identity_value)
        frame = pd.read_csv(path)
        stored_content_sha256 = raw.get("stored_content_sha256")
        if manifest.get("schema_version") == 2:
            if not isinstance(stored_content_sha256, str):
                raise RuntimeContractError(
                    f"stored SRT publication has no content identity: {filename}"
                )
            if canonical_frame_sha256(frame) != stored_content_sha256:
                raise RuntimeContractError(
                    f"stored SRT publication content identity differs: {filename}"
                )
        results[name] = DataResult(DataStatus.READY, frame, identity)
        requests[name] = request
    return PublishedStrategyData(
        release_id=release_id,
        release_hash=str(manifest.get("release_hash")),
        status=PublicationStatus.READY,
        requested_cutoff=str(manifest.get("requested_cutoff")),
        input_requests=requests,
        input_results=results,
    )


def load_strategy_runtime_context(
    directory: Path,
    strategy: ExecutableStrategy,
) -> StrategyRuntimeContext:
    """Load one authenticated strategy publication and its execution prices."""

    generation = _read_bound_generation(
        directory,
        release_id=strategy.definition.release_id,
        symbol=_strategy_symbol(strategy),
    )
    publication = read_publication(directory, strategy.definition.release_id)
    from .runner import StrategyRunner

    StrategyRunner.validate_publication(strategy, publication)
    if publication.requested_cutoff != generation["data_cutoff"]:
        raise RuntimeContractError(
            "strategy publication cutoff differs from its SRT generation"
        )
    try:
        adjusted = publication.input_results["adjusted_daily"].dataframe
        execution = publication.input_results["execution_daily"].dataframe
    except KeyError as exc:
        raise RuntimeContractError(
            "SRT generation has no complete execution-pricing publication"
        ) from exc
    return StrategyRuntimeContext(
        publication,
        ExecutionPricingData(_strategy_symbol(strategy), adjusted, execution),
    )
