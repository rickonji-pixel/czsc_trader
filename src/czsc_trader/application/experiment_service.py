from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

from czsc_trader.experiment_archive import MANIFEST_NAME, validate_experiment_archive
from czsc_trader.research.registry import ExperimentRegistry

from .context import RepositoryContext
from .errors import ExecutionError, SafetyError, ValidationError
from .results import CommandResult


def _load_protocol(experiment_dir: Path) -> dict[str, object]:
    path = experiment_dir / "artifacts" / "protocol.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "experiment_protocol_invalid",
            f"cannot read experiment protocol: {exc}",
            context={"path": str(path)},
        ) from exc
    if not isinstance(payload, dict):
        raise ValidationError("experiment_protocol_invalid", "protocol must be a JSON object")
    return payload


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def run_experiment(
    context: RepositoryContext,
    experiment_dir: Path,
    registry: ExperimentRegistry,
) -> CommandResult:
    experiment_dir = Path(experiment_dir).resolve()
    if (experiment_dir / MANIFEST_NAME).exists():
        raise SafetyError(
            "frozen_experiment_run_forbidden",
            f"frozen experiment cannot run in place: {experiment_dir.name}",
        )
    protocol = _load_protocol(experiment_dir)
    handler = registry.resolve(protocol, experiment_dir.name)
    handler.validate_protocol(protocol)
    try:
        summary = handler.run(context, experiment_dir)
    except Exception as exc:
        raise ExecutionError("experiment_failed", str(exc)) from exc
    return CommandResult(
        status="PASS",
        command="experiment.run",
        result=summary,
        artifacts={"output_dir": str(experiment_dir)},
    )


def replay_experiment(
    context: RepositoryContext,
    source_dir: Path,
    output_dir: Path,
    registry: ExperimentRegistry,
) -> CommandResult:
    source = Path(source_dir).resolve()
    output = Path(output_dir).resolve()
    if output == source or _is_within(output, source):
        raise SafetyError(
            "replay_output_not_isolated",
            "replay output must be outside the frozen source archive",
        )
    if output.exists():
        raise SafetyError("replay_output_exists", f"replay output already exists: {output}")
    try:
        validate_experiment_archive(source)
    except (OSError, ValueError) as exc:
        raise ValidationError("experiment_archive_invalid", str(exc)) from exc
    protocol = _load_protocol(source)
    handler = registry.resolve(protocol, source.name)
    handler.validate_protocol(protocol)
    before = _tree_hashes(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=".replay_", dir=output.parent))
    source_copy = workspace / "source"
    staging_output = workspace / "output"
    try:
        shutil.copytree(source, source_copy)
        staging_output.mkdir()
        summary = handler.replay(context, source_copy, staging_output)
        if _tree_hashes(source) != before:
            raise SafetyError(
                "frozen_experiment_modified",
                "frozen source archive changed during replay",
            )
        staging_output.replace(output)
    except SafetyError:
        raise
    except Exception as exc:
        raise ExecutionError("experiment_replay_failed", str(exc)) from exc
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return CommandResult(
        status="PASS",
        command="experiment.replay",
        result=summary,
        artifacts={"output_dir": str(output)},
    )
