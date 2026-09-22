"""Validated, immutable candidate packages prepared by strategy researchers."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module, invalidate_caches
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any

from strategy_manager import CandidateSnapshot
from strategy_runtime import StrategyCandidate, StrategyRuntime, canonical_sha256
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
    required = {
        "module", "qualname", "contract_version", "source_files", "source_sha256",
    }
    if not isinstance(descriptor, dict) or set(descriptor) != required:
        raise ValueError("candidate chart descriptor is incomplete")
    module_name = descriptor["module"]
    qualname = descriptor["qualname"]
    if (
        not isinstance(module_name, str)
        or not module_name.startswith("strategy_runtime.charts.")
        or not all(part.isidentifier() for part in module_name.split("."))
        or not isinstance(qualname, str)
        or not qualname.isidentifier()
        or descriptor["contract_version"] != 1
    ):
        raise ValueError("candidate chart implementation identity is invalid")
    source_files = _validate_paths(descriptor["source_files"], "charts.source_files")
    implementation_file = module_name.removeprefix("strategy_runtime.").replace(".", "/") + ".py"
    if implementation_file not in source_files or not set(source_files).issubset(install_files):
        raise ValueError("candidate chart source closure is incomplete")
    actual = implementation_sha256(source_files, source_root=runtime_root)
    if descriptor["source_sha256"] != actual:
        raise ValueError("candidate chart source hash differs from package files")
    import strategy_runtime

    package_root = str(runtime_root)
    if package_root not in strategy_runtime.__path__:
        strategy_runtime.__path__.insert(0, package_root)
    charts_package = import_module("strategy_runtime.charts")
    charts_root = str(runtime_root / "charts")
    if charts_root not in charts_package.__path__:
        charts_package.__path__.insert(0, charts_root)
    invalidate_caches()
    module = sys.modules.get(module_name)
    already_loaded = module is not None
    if module is not None and getattr(module, "__srt_chart_source_sha256__", None) != actual:
        raise ValueError(
            "candidate chart module was already imported from a different source closure"
        )
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        module = import_module(module_name)
    finally:
        sys.dont_write_bytecode = previous_bytecode
    if not already_loaded:
        expected_module = (runtime_root / implementation_file).resolve()
        module_file = getattr(module, "__file__", None)
        if module_file is None or Path(module_file).resolve() != expected_module:
            raise ValueError("candidate chart implementation loaded from another package")
    implementation = getattr(module, qualname, None)
    if implementation is None:
        raise ValueError("candidate chart implementation is unavailable")
    for method in ("render_backtest", "render_forward_observation"):
        if not callable(getattr(implementation, method, None)):
            raise ValueError(f"candidate chart implementation has no {method}")
    module.__srt_chart_source_sha256__ = actual
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
        "install_files", "charts",
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
    return CandidatePackage(root, snapshot, runtime_root, binding, manifest, package_hash)
