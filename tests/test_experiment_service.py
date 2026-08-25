from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.errors import SafetyError
from czsc_trader.application.experiment_service import replay_experiment, run_experiment
from czsc_trader.experiment_archive import (
    REQUIRED_DOCUMENTS,
    build_experiment_manifest,
)
from czsc_trader.research.registry import ExperimentRegistry


class RecordingHandler:
    handler_id = "fixture"

    def __init__(self) -> None:
        self.runs: list[Path] = []
        self.replays: list[tuple[Path, Path]] = []

    def validate_protocol(self, protocol):
        assert protocol["handler"] == self.handler_id

    def run(self, context, experiment_dir):
        self.runs.append(experiment_dir)
        return {"status": "COMPLETE"}

    def replay(self, context, source_dir, output_dir):
        self.replays.append((source_dir, output_dir))
        (output_dir / "replay.json").write_text(
            json.dumps({"status": "COMPLETE"}), encoding="utf-8"
        )
        (output_dir / "experiment_manifest.json").write_text(
            json.dumps({"experiment_id": output_dir.name}), encoding="utf-8"
        )
        return {"status": "COMPLETE", "experiment_dir": str(output_dir)}


def _context(root: Path) -> RepositoryContext:
    return RepositoryContext(
        root=root,
        raw_dir=root / "data" / "raw",
        baseline_root=root / "configs" / "rule_baselines",
        experiments_root=root / "experiments",
        outputs_root=root / "outputs",
    )


def _archive(root: Path) -> Path:
    archive = root / "experiments" / "0901_EX01"
    artifacts = archive / "artifacts"
    artifacts.mkdir(parents=True)
    for name in REQUIRED_DOCUMENTS:
        (archive / name).write_text(f"# {name}\n", encoding="utf-8")
    (artifacts / "protocol.json").write_text(
        json.dumps({"handler": "fixture"}), encoding="utf-8"
    )
    build_experiment_manifest(
        archive,
        {
            "experiment_id": archive.name,
            "status": "COMPLETE",
            "holdout_accessed": False,
        },
    )
    return archive


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _registry(handler: RecordingHandler) -> ExperimentRegistry:
    registry = ExperimentRegistry()
    registry.register(handler)
    return registry


def test_frozen_experiment_is_rejected_before_handler_run(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    handler = RecordingHandler()

    with pytest.raises(SafetyError, match="frozen"):
        run_experiment(_context(tmp_path), archive, _registry(handler))

    assert handler.runs == []


def test_replay_publishes_outside_source_without_mutating_archive(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    before = _digest_tree(archive)
    handler = RecordingHandler()
    output = tmp_path / "replays" / "0901_EX01"

    result = replay_experiment(
        _context(tmp_path), archive, output, _registry(handler)
    )

    assert result.status == "PASS"
    assert Path(result.artifacts["output_dir"]) == output.resolve()
    assert Path(result.result["experiment_dir"]) == output.resolve()
    assert (output / "replay.json").is_file()
    replay_manifest = json.loads(
        (output / "experiment_manifest.json").read_text(encoding="utf-8")
    )
    assert replay_manifest["experiment_id"] == "0901_EX01"
    assert replay_manifest["run_type"] == "experiment_replay"
    assert replay_manifest["source_experiment"] == "0901_EX01"
    assert len(replay_manifest["source_manifest_sha256"]) == 64
    assert _digest_tree(archive) == before


def test_replay_rejects_output_inside_frozen_source(tmp_path: Path) -> None:
    archive = _archive(tmp_path)

    with pytest.raises(SafetyError, match="outside"):
        replay_experiment(
            _context(tmp_path), archive, archive / "runtime" / "replay", _registry(RecordingHandler())
        )
