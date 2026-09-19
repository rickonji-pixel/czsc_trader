"""Read-only validation of SRT production publications."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re


class PublicationInboxError(RuntimeError):
    """An SRT publication is absent, incomplete, or incompatible with PTE accounts."""


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(data_dir: Path, symbol: str) -> Path:
    normalized = symbol.upper()
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ)", normalized):
        raise PublicationInboxError("account publication symbol is unsafe")
    code = normalized.split(".", 1)[0]
    return Path(data_dir).resolve() / f"{code}_strategy_generation.json"


def _read_generation(data_dir: Path, symbol: str) -> dict[str, object]:
    path = _manifest_path(data_dir, symbol)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationInboxError(
            f"cannot read SRT generation {path.name}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise PublicationInboxError("SRT generation manifest must be an object")
    return manifest


def _safe_published_file(data_dir: Path, name: str) -> Path:
    root = Path(data_dir).resolve()
    if Path(name).name != name:
        raise PublicationInboxError("SRT generation file path is unsafe")
    path = (root / name).resolve()
    if path.parent != root:
        raise PublicationInboxError("SRT generation file path escapes the data directory")
    return path


def _generation_signature(
    data_dir: Path, symbol: str, manifest: dict[str, object]
) -> tuple[tuple[str, int, int], ...]:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise PublicationInboxError("SRT generation has no authenticated files")
    paths = [("__manifest__", _manifest_path(data_dir, symbol))]
    for name in files:
        if not isinstance(name, str):
            raise PublicationInboxError("SRT generation file entry is invalid")
        paths.append((name, _safe_published_file(data_dir, name)))
    try:
        return tuple(
            (name, path.stat().st_size, path.stat().st_mtime_ns)
            for name, path in paths
        )
    except OSError as exc:
        raise PublicationInboxError(f"SRT generation file is unavailable: {exc}") from exc


def verify_generation(data_dir: Path, symbol: str) -> dict[str, object]:
    """Authenticate one committed SRT generation without modifying it."""
    normalized = symbol.upper()
    manifest = _read_generation(data_dir, symbol)
    if manifest.get("schema_version") != 2:
        raise PublicationInboxError("SRT generation schema is unsupported")
    if str(manifest.get("symbol", "")).upper() != normalized:
        raise PublicationInboxError("SRT generation symbol differs from account")
    try:
        date.fromisoformat(str(manifest["data_cutoff"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicationInboxError("SRT generation has no valid data cutoff") from exc
    generation_id = manifest.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise PublicationInboxError("SRT generation has no identity")
    releases = manifest.get("strategy_releases")
    if (
        not isinstance(releases, list)
        or not releases
        or any(not isinstance(item, str) or not item for item in releases)
    ):
        raise PublicationInboxError("SRT generation has no strategy releases")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise PublicationInboxError("SRT generation has no authenticated files")
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise PublicationInboxError("SRT generation file entry is invalid")
        published = _safe_published_file(data_dir, name)
        if not published.is_file() or _sha256(published) != expected:
            raise PublicationInboxError(f"SRT generation is incomplete or mixed: {name}")
    return manifest


class PublicationInbox:
    """Validate publications required by active PTE strategy accounts."""

    def __init__(self, *, data_dir: Path, store) -> None:
        self.data_dir = Path(data_dir)
        self.store = store
        self._verified: dict[
            str, tuple[tuple[tuple[str, int, int], ...], dict[str, object]]
        ] = {}

    def _generation(self, symbol: str) -> dict[str, object]:
        candidate = _read_generation(self.data_dir, symbol)
        signature = _generation_signature(self.data_dir, symbol, candidate)
        cached = self._verified.get(symbol)
        if cached is not None and cached[0] == signature:
            return cached[1]
        verified = verify_generation(self.data_dir, symbol)
        self._verified[symbol] = (signature, verified)
        return verified

    def observe(self) -> dict[str, object]:
        grouped: dict[tuple[str, str], set[str]] = {}
        for account in self.store.strategy_virtual_accounts():
            if account.get("status") == "RETIRED":
                continue
            symbol = str(account["symbol"]).upper()
            asset = str(account["asset_type"])
            release_id = f"{account['strategy_id']}-{account['strategy_version']}"
            grouped.setdefault((symbol, asset), set()).add(release_id)
        if not grouped:
            raise PublicationInboxError("no active strategy account publication to observe")

        instruments: list[dict[str, object]] = []
        cutoffs: set[str] = set()
        for (symbol, asset), required_releases in sorted(grouped.items()):
            generation = self._generation(symbol)
            if generation.get("asset_type") != asset:
                raise PublicationInboxError(
                    f"{symbol}: SRT generation asset type differs from account"
                )
            available = set(generation["strategy_releases"])
            missing = sorted(required_releases - available)
            if missing:
                raise PublicationInboxError(
                    f"{symbol}: SRT generation is missing releases: {missing}"
                )
            cutoff = str(generation["data_cutoff"])
            cutoffs.add(cutoff)
            instruments.append(
                {"symbol": symbol, "asset_type": asset, "result": generation}
            )
        if len(cutoffs) != 1:
            raise PublicationInboxError("SRT instrument generations have different cutoffs")
        cutoff = cutoffs.pop()
        return {
            "data_cutoff": cutoff,
            "instruments": instruments,
            "generation_ids": [
                item["result"]["generation_id"] for item in instruments
            ],
        }
