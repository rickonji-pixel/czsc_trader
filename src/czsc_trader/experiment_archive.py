"""Tracked, portable archives for formal research rounds."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re


REQUIRED_DOCUMENTS = (
    "01_goal.md",
    "02_design.md",
    "03_execution.md",
    "04_conclusion.md",
)
MANIFEST_NAME = "experiment_manifest.json"
TEXT_SUFFIXES = {".csv", ".html", ".json", ".md", ".py", ".txt"}


def create_experiment_dir(root: Path, run_date: date) -> Path:
    """Create the next non-overwriting date-scoped ``MMDD_EXX`` directory."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    prefix = f"{run_date:%m%d}_EX"
    pattern = re.compile(rf"{re.escape(prefix)}(\d{{2}})")
    numbers = [
        int(match.group(1))
        for path in root.iterdir()
        if path.is_dir() and (match := pattern.fullmatch(path.name))
    ]
    revision = max(numbers, default=0) + 1
    if revision > 99:
        raise RuntimeError(f"{prefix} has already reached EX99")
    experiment_dir = root / f"{prefix}{revision:02d}"
    experiment_dir.mkdir(exist_ok=False)
    return experiment_dir


def _file_record(path: Path) -> dict[str, object]:
    content = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES:
        content = content.replace(b"\r\n", b"\n")
    return {"bytes": len(content), "sha256": sha256(content).hexdigest()}


def _is_managed_file(experiment_dir: Path, path: Path) -> bool:
    relative = path.relative_to(experiment_dir)
    return (
        path.is_file()
        and path.name != MANIFEST_NAME
        and (not relative.parts or relative.parts[0] != "runtime")
    )


def build_experiment_manifest(
    experiment_dir: Path,
    metadata: dict[str, object],
) -> dict[str, object]:
    """Write a deterministic manifest for every managed file in an archive."""
    experiment_dir = Path(experiment_dir).resolve()
    files = {
        path.relative_to(experiment_dir).as_posix(): _file_record(path)
        for path in sorted(experiment_dir.rglob("*"))
        if _is_managed_file(experiment_dir, path)
    }
    manifest = {"schema_version": 1, **metadata, "files": files}
    serialized = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if "outputs/" in serialized or re.search(r"_R\d{2}", serialized):
        raise ValueError("experiment manifest must not reference outputs revisions")
    (experiment_dir / MANIFEST_NAME).write_text(serialized, encoding="utf-8")
    return manifest


def validate_experiment_archive(experiment_dir: Path) -> dict[str, object]:
    """Validate required documents and every file declared by the manifest."""
    experiment_dir = Path(experiment_dir).resolve()
    manifest_path = experiment_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f"missing experiment manifest: {MANIFEST_NAME}")
    serialized = manifest_path.read_text(encoding="utf-8")
    if "outputs/" in serialized or re.search(r"_R\d{2}", serialized):
        raise ValueError("experiment manifest must not reference outputs revisions")
    manifest = json.loads(serialized)
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("experiment manifest files must be an object")
    actual_names = {
        path.relative_to(experiment_dir).as_posix()
        for path in experiment_dir.rglob("*")
        if _is_managed_file(experiment_dir, path)
    }
    undeclared = sorted(actual_names - set(files))
    if undeclared:
        raise ValueError(f"experiment files not declared by manifest: {undeclared}")

    for name in REQUIRED_DOCUMENTS:
        if name not in files or not (experiment_dir / name).is_file():
            raise ValueError(f"missing required document: {name}")

    for relative_name, expected in files.items():
        relative = Path(str(relative_name))
        if relative.is_absolute():
            raise ValueError(f"manifest path must be relative: {relative_name}")
        path = (experiment_dir / relative).resolve()
        try:
            path.relative_to(experiment_dir)
        except ValueError as exc:
            raise ValueError(f"manifest path escapes archive: {relative_name}") from exc
        if not path.is_file():
            raise ValueError(f"missing declared experiment file: {relative_name}")
        actual = _file_record(path)
        if actual["bytes"] != expected.get("bytes"):
            raise ValueError(f"file size differs from manifest: {relative_name}")
        if actual["sha256"] != expected.get("sha256"):
            raise ValueError(f"SHA-256 differs from manifest: {relative_name}")
    return manifest
