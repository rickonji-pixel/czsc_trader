from datetime import date
import json
from pathlib import Path

import pytest

from czsc_trader.experiment_archive import (
    REQUIRED_DOCUMENTS,
    build_experiment_manifest,
    create_experiment_dir,
    validate_experiment_archive,
)


REQUIRED_ARTIFACTS = (
    "artifacts/protocol.json",
    "artifacts/candidate_results.csv",
    "artifacts/champion_metrics.json",
    "artifacts/metrics.json",
)


def test_create_experiment_dir_starts_at_ex01(tmp_path: Path) -> None:
    created = create_experiment_dir(tmp_path, date(2026, 8, 24))

    assert created == tmp_path / "0824_EX01"
    assert created.is_dir()


def test_experiment_number_resets_by_date_and_does_not_fill_gaps(
    tmp_path: Path,
) -> None:
    (tmp_path / "0824_EX01").mkdir()
    (tmp_path / "0824_EX03").mkdir()

    assert create_experiment_dir(tmp_path, date(2026, 8, 24)).name == "0824_EX04"
    assert create_experiment_dir(tmp_path, date(2026, 8, 25)).name == "0825_EX01"


def test_create_experiment_dir_rejects_more_than_99_rounds(tmp_path: Path) -> None:
    (tmp_path / "0824_EX99").mkdir()

    with pytest.raises(RuntimeError, match="EX99"):
        create_experiment_dir(tmp_path, date(2026, 8, 24))


def _complete_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "0824_EX01"
    (archive / "artifacts").mkdir(parents=True)
    for name in REQUIRED_DOCUMENTS:
        (archive / name).write_text(f"# {name}\n", encoding="utf-8")
    for name in REQUIRED_ARTIFACTS:
        (archive / name).write_text(f"fixture for {name}\n", encoding="utf-8")
    return archive


def _metadata() -> dict[str, object]:
    return {
        "experiment_id": "0824_EX01",
        "date": "2026-08-24",
        "status": "FAIL",
        "symbol": "588080.SH",
        "asset_type": "etf",
        "champion": {
            "version": "baseline_20260823",
            "sha256": "abc123",
        },
        "visible_sample_end": "2025-12-31",
        "holdout_accessed": False,
    }


def test_build_and_validate_experiment_manifest(tmp_path: Path) -> None:
    archive = _complete_archive(tmp_path)

    manifest = build_experiment_manifest(archive, _metadata())
    validated = validate_experiment_archive(archive)

    assert validated == manifest
    assert "experiment_manifest.json" not in manifest["files"]
    assert set(REQUIRED_DOCUMENTS).issubset(manifest["files"])
    assert set(REQUIRED_ARTIFACTS).issubset(manifest["files"])
    assert all(not Path(name).is_absolute() for name in manifest["files"])
    assert all(record["bytes"] > 0 for record in manifest["files"].values())
    assert all(len(record["sha256"]) == 64 for record in manifest["files"].values())


@pytest.mark.parametrize("missing", REQUIRED_DOCUMENTS)
def test_validator_rejects_missing_required_document(
    tmp_path: Path, missing: str
) -> None:
    archive = _complete_archive(tmp_path)
    build_experiment_manifest(archive, _metadata())
    (archive / missing).unlink()

    with pytest.raises(ValueError, match="required document"):
        validate_experiment_archive(archive)


def test_validator_rejects_tampered_file(tmp_path: Path) -> None:
    archive = _complete_archive(tmp_path)
    build_experiment_manifest(archive, _metadata())
    path = archive / "04_conclusion.md"
    original = path.read_text(encoding="utf-8")
    path.write_text("!" + original[1:], encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256"):
        validate_experiment_archive(archive)


def test_validator_treats_lf_and_crlf_as_same_text_archive(tmp_path: Path) -> None:
    """Catch Windows checkout conversion invalidating a portable text archive."""
    archive = _complete_archive(tmp_path)
    path = archive / "01_goal.md"
    path.write_bytes(b"# Goal\n\nPortable text\n")
    build_experiment_manifest(archive, _metadata())

    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    validate_experiment_archive(archive)


def test_validator_rejects_unmanifested_file(tmp_path: Path) -> None:
    archive = _complete_archive(tmp_path)
    build_experiment_manifest(archive, _metadata())
    (archive / "untracked.txt").write_text("not declared\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not declared"):
        validate_experiment_archive(archive)


def test_validator_rejects_local_output_provenance(tmp_path: Path) -> None:
    archive = _complete_archive(tmp_path)
    build_experiment_manifest(archive, _metadata())
    path = archive / "experiment_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["source"] = "outputs/588080_0824_R02"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="outputs"):
        validate_experiment_archive(archive)
