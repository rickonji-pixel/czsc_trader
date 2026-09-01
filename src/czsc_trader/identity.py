"""Portable SHA-256 identities for semantic JSON, text, and raw files."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path


def _json_object(value: Mapping[str, object] | Path) -> dict[str, object]:
    if isinstance(value, Path):
        try:
            payload = json.loads(value.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read JSON identity source {value}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"JSON identity source must be an object: {value}")
        return payload
    return dict(value)


def canonical_json_sha256(value: Mapping[str, object] | Path) -> str:
    """Hash a JSON object by meaning, independent of formatting and EOL."""
    canonical = json.dumps(
        _json_object(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def normalized_text_sha256(path: Path) -> str:
    """Hash text bytes after normalizing CRLF and standalone CR to LF."""
    content = Path(path).read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return sha256(content).hexdigest()


def raw_file_sha256(path: Path) -> str:
    """Hash exact file bytes for raw market data and binary artifacts."""
    return sha256(Path(path).read_bytes()).hexdigest()
