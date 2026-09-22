"""Validated, immutable candidate packages prepared by strategy researchers."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
from typing import Any

from strategy_manager import CandidateSnapshot
from strategy_runtime import (
    ChartRuntime,
    StrategyCandidate,
    StrategyRuntime,
    canonical_sha256,
    validate_observation_descriptor,
)
from strategy_runtime.implementation_identity import implementation_sha256


MANIFEST_NAME = "candidate_submission.json"


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _safe_relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or "\\" in value or ":" in value:
        raise ValueError(f"{field} contains an unsafe path")
    return value


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_paths(values: object, field: str) -> tuple[str, ...]:
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(item, str) for item in values)
    ):
        raise ValueError(f"{field} must be a non-empty path list")
    normalized = tuple(_safe_relative(item, field) for item in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} contains duplicate paths")
    return normalized


@dataclass(frozen=True)
class CandidatePackage:
    root: Path
    snapshot: CandidateSnapshot
    runtime_root: Path
    runtime_binding: dict[str, Any]
    manifest: dict[str, Any]
    package_hash: str

    @property
    def candidate_reference(self) -> str:
        return f"{self.snapshot.strategy_id}-{self.snapshot.candidate_id}"

    @property
    def install_files(self) -> tuple[str, ...]:
        return tuple(self.runtime_binding["install_files"])


def validate_chart_contract(
    runtime_root: Path, descriptor: object, install_files: tuple[str, ...],
) -> dict[str, Any]:
    ChartRuntime().validate_descriptor(
        descriptor,
        source_root=runtime_root,
        install_files=install_files,
    )
    return dict(descriptor)


def load_candidate_package(path: Path) -> CandidatePackage:
    root = Path(path).resolve()
    if not root.is_dir():
        raise ValueError(f"candidate package directory is unavailable: {root}")
    manifest_path = root / MANIFEST_NAME
    manifest = _read_object(manifest_path)
    expected_manifest = {
        "schema_version", "candidate_snapshot", "runtime_binding", "runtime_root",
        "files", "package_hash",
    }
    if set(manifest) != expected_manifest or manifest["schema_version"] != 1:
        raise ValueError("candidate package manifest fields are invalid")
    snapshot_name = _safe_relative(manifest["candidate_snapshot"], "candidate_snapshot")
    binding_name = _safe_relative(manifest["runtime_binding"], "runtime_binding")
    runtime_name = _safe_relative(manifest["runtime_root"], "runtime_root")
    runtime_root = (root / PurePosixPath(runtime_name)).resolve()
    if runtime_root.parent != (root / PurePosixPath(runtime_name)).parent.resolve():
        raise ValueError("candidate runtime root escapes the package")
    if runtime_root.name != "strategy_runtime" or not (runtime_root / "strategies").is_dir():
        raise ValueError("candidate runtime root must contain strategy_runtime/strategies")

    raw_files = manifest["files"]
    if not isinstance(raw_files, dict) or not raw_files:
        raise ValueError("candidate package file identities are missing")
    files: dict[str, str] = {}
    for name, expected_hash in raw_files.items():
        relative = _safe_relative(name, "files")
        target = (root / PurePosixPath(relative)).resolve()
        if not target.is_file() or not isinstance(expected_hash, str):
            raise ValueError(f"candidate package file is missing: {relative}")
        actual_hash = _file_sha256(target)
        if actual_hash != expected_hash:
            raise ValueError(f"candidate package file hash differs: {relative}")
        files[relative] = actual_hash
    actual_files = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() and item != manifest_path
    }
    if actual_files != set(files):
        raise ValueError("candidate package contains untracked or missing files")
    identity = {
        "schema_version": 1,
        "candidate_snapshot": snapshot_name,
        "runtime_binding": binding_name,
        "runtime_root": runtime_name,
        "files": files,
    }
    package_hash = canonical_sha256(identity)
    if manifest["package_hash"] != package_hash:
        raise ValueError("candidate package hash does not match its manifest")

    snapshot = CandidateSnapshot.from_dict(_read_object(root / snapshot_name))
    binding = _read_object(root / binding_name)
    required_binding = {
        "schema_version", "candidate_id", "source_files", "implementation_sha256",
        "install_files", "charts", "observation",
    }
    if set(binding) != required_binding or binding["schema_version"] != 1:
        raise ValueError("candidate runtime binding fields are invalid")
    reference = f"{snapshot.strategy_id}-{snapshot.candidate_id}"
    if binding["candidate_id"] != reference:
        raise ValueError("candidate runtime binding belongs to another candidate")
    source_files = _validate_paths(binding["source_files"], "source_files")
    install_files = _validate_paths(binding["install_files"], "install_files")
    if not set(source_files).issubset(install_files):
        raise ValueError("candidate install files omit runtime source closure")
    descriptor = snapshot.strategy_payload.get("runtime")
    if not isinstance(descriptor, dict) or tuple(descriptor.get("source_files", ())) != source_files:
        raise ValueError("candidate payload and runtime binding source files differ")
    actual_implementation = implementation_sha256(source_files, source_root=runtime_root)
    if (
        binding["implementation_sha256"] != actual_implementation
        or descriptor.get("source_sha256") != actual_implementation
    ):
        raise ValueError("candidate implementation hash differs from runtime binding")
    expected_runtime_files = {
        f"{runtime_name}/{name}" for name in install_files
    }
    if not expected_runtime_files.issubset(files):
        raise ValueError("candidate manifest omits installable runtime files")

    StrategyRuntime().describe(
        StrategyCandidate(
            snapshot.strategy_id,
            snapshot.candidate_id,
            snapshot.strategy_payload,
            runtime_root,
        )
    )
    validate_chart_contract(runtime_root, binding["charts"], install_files)
    validate_observation_descriptor(binding["observation"])
    return CandidatePackage(root, snapshot, runtime_root, binding, manifest, package_hash)
