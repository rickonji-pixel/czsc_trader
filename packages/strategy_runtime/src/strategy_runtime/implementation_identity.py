"""Stable source-closure identities for frozen strategy implementations."""

from __future__ import annotations

from hashlib import sha256
from importlib.resources import files
import json
from pathlib import PurePosixPath

from .errors import RuntimeCompatibilityError


def implementation_sha256(source_files: tuple[str, ...]) -> str:
    root = files("strategy_runtime")
    digest = sha256()
    for name in sorted(source_files):
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeCompatibilityError("implementation source path is unsafe")
        resource = root.joinpath(*relative.parts)
        if not resource.is_file():
            raise RuntimeCompatibilityError(f"implementation source is missing: {name}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(resource.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_runtime_binding(release_id: str) -> dict[str, object]:
    resource = files("strategy_runtime").joinpath("bindings", f"{release_id}.json")
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeCompatibilityError(
            f"runtime binding is unavailable for frozen release {release_id}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("release_id") != release_id:
        raise RuntimeCompatibilityError("runtime binding identity is invalid")
    source_files = payload.get("source_files")
    expected = payload.get("implementation_sha256")
    if (
        not isinstance(source_files, list)
        or not source_files
        or not all(isinstance(item, str) for item in source_files)
        or not isinstance(expected, str)
    ):
        raise RuntimeCompatibilityError("runtime binding fields are invalid")
    return payload
