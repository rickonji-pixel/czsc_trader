from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any


def file_sha256(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_strategy_generation(
    root: Path,
    *,
    symbol: str,
    asset_type: str,
    dataset: str,
    release_id: str | None = None,
) -> dict[str, Any]:
    """Require one complete committed generation and verify every bound file."""

    directory = Path(root)
    code = symbol.split(".", 1)[0]
    marker = directory / f"{code}_strategy_generation.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read strategy generation marker: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("strategy generation marker schema is invalid")
    if not isinstance(payload.get("generation_id"), str) or not payload["generation_id"]:
        raise ValueError("strategy generation ID is invalid")
    if payload.get("symbol") != symbol.upper():
        raise ValueError("strategy generation symbol differs from request")
    if payload.get("asset_type") != asset_type.lower():
        raise ValueError("strategy generation asset type differs from request")
    if payload.get("dataset") != dataset:
        raise ValueError("strategy generation dataset differs from request")
    if not isinstance(payload.get("data_cutoff"), str):
        raise ValueError("strategy generation data cutoff is invalid")
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("strategy generation has no bound files")
    for filename, expected in files.items():
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("strategy generation contains an unsafe filename")
        path = directory / filename
        if not path.is_file():
            raise ValueError(f"strategy generation file is missing: {filename}")
        if file_sha256(path) != str(expected).lower():
            raise ValueError(f"strategy generation file hash differs: {filename}")
    if release_id is not None:
        releases = payload.get("strategy_releases")
        if not isinstance(releases, list) or release_id not in releases:
            raise ValueError(
                f"strategy generation does not bind requested release: {release_id}"
            )
        publication = f"srt_{release_id.lower().replace('-', '_')}_publication.json"
        if publication not in files:
            raise ValueError(
                f"strategy generation does not bind publication manifest: {publication}"
            )
    return payload
