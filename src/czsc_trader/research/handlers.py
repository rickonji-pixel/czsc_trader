from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess

from czsc_trader.application.context import RepositoryContext
from czsc_trader.experiment_archive import MANIFEST_NAME


Runner = Callable[[RepositoryContext, Path], dict[str, object]]


@contextmanager
def _repository_cwd(root: Path):
    previous = Path.cwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(previous)


def _protocol(experiment_dir: Path) -> dict[str, object]:
    return json.loads(
        (experiment_dir / "artifacts" / "protocol.json").read_text(encoding="utf-8")
    )


class FunctionHandler:
    """Adapt one deterministic research runner to the common contract."""

    def __init__(self, handler_id: str, runner: Runner) -> None:
        self.handler_id = handler_id
        self._runner = runner

    def validate_protocol(self, protocol: dict[str, object]) -> None:
        declared = protocol.get("handler") or protocol.get("experiment_type")
        if declared is not None and str(declared) != self.handler_id:
            raise ValueError(
                f"protocol handler {declared!r} differs from {self.handler_id!r}"
            )

    def run(
        self,
        context: RepositoryContext,
        experiment_dir: Path,
    ) -> dict[str, object]:
        return self._runner(context, experiment_dir)

    def replay(
        self,
        context: RepositoryContext,
        source_dir: Path,
        output_dir: Path,
    ) -> dict[str, object]:
        shutil.copytree(source_dir, output_dir, dirs_exist_ok=True)
        (output_dir / MANIFEST_NAME).unlink(missing_ok=True)
        return self.run(context, output_dir)


def _completed_archive(
    context: RepositoryContext,
    experiment_dir: Path,
    function: Callable[[Path], Path],
) -> dict[str, object]:
    with _repository_cwd(context.root):
        completed = function(experiment_dir)
    manifest = json.loads(
        (completed / MANIFEST_NAME).read_text(encoding="utf-8")
    )
    return {
        "status": manifest.get("status", "COMPLETE"),
        "experiment_dir": str(completed),
    }


def _champion_challenge(
    context: RepositoryContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.experiments import run_pre2026_experiment

    return run_pre2026_experiment(
        context.raw_dir,
        context.baseline_root,
        experiment_dir / "artifacts",
    )


def _four_layer(context: RepositoryContext, experiment_dir: Path) -> dict[str, object]:
    from czsc_trader.four_layer_runner import run_four_layer_experiment

    return run_four_layer_experiment(
        context.raw_dir, context.baseline_root, experiment_dir
    )


def _return_only(context: RepositoryContext, experiment_dir: Path) -> dict[str, object]:
    from czsc_trader.return_only_runner import run_return_only_experiment

    return run_return_only_experiment(
        context.raw_dir, context.baseline_root, experiment_dir
    )


def _factor_discovery(
    context: RepositoryContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.factor_discovery_runner import run_factor_discovery_experiment

    protocol = _protocol(experiment_dir)
    ex05_path = (
        context.experiments_root / "0824_EX05" / "artifacts" / "frozen_challenger.json"
        if protocol.get("experiment_type") == "event_aware_parallel_factor_discovery"
        else None
    )
    return run_factor_discovery_experiment(
        context.raw_dir,
        context.baseline_root,
        context.experiments_root / "0824_EX04" / "artifacts" / "frozen_challenger.json",
        experiment_dir,
        ex05_path=ex05_path,
    )


def _git_head(root: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, encoding="utf-8"
    ).strip()


def _optuna(context: RepositoryContext, experiment_dir: Path) -> dict[str, object]:
    from czsc_trader.optuna_runner import (
        run_optuna_experiment,
        validate_ex07_protocol,
        validate_ex08_protocol,
    )

    protocol = _protocol(experiment_dir)
    memory = protocol.get("experiment_type") == "optuna_inmemory_full_joint_strategy_search"
    return run_optuna_experiment(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        protocol_validator=validate_ex08_protocol if memory else validate_ex07_protocol,
        storage_mode="memory" if memory else "sqlite",
        recover_runtime=not memory,
        collect_batch_timings=memory,
        require_full_trial_count=memory,
        execution_commit=_git_head(context.root),
    )


def _top3(context: RepositoryContext, experiment_dir: Path) -> dict[str, object]:
    from czsc_trader.top3_holdout_runner import run_tournament_experiment

    return run_tournament_experiment(
        context.root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def registered_handlers() -> tuple[FunctionHandler, ...]:
    from . import preregistered

    diagnostic = {
        "champion_attribution": preregistered.run_preregistered_attribution,
        "ex04_mechanism_attribution": preregistered.run_preregistered_ex04_attribution,
        "ex04_path_attribution": preregistered.run_preregistered_ex04_path_attribution,
        "ex04_exit_signal_diagnosis": preregistered.run_preregistered_exit_signal_diagnosis,
        "dominant_exit_path_anatomy": preregistered.run_preregistered_dominant_exit_anatomy,
        "new_exit_representation_diagnosis": preregistered.run_preregistered_new_exit_representation,
        "ex04_decision_boundary_diagnosis": preregistered.run_preregistered_decision_boundary,
    }
    handlers = [
        FunctionHandler("champion_challenge", _champion_challenge),
        FunctionHandler("fixed_factor_four_layer_challenger", _four_layer),
        FunctionHandler("return_only_fixed_factor_challenger", _return_only),
        FunctionHandler("state_expanded_factor_discovery", _factor_discovery),
        FunctionHandler("event_aware_parallel_factor_discovery", _factor_discovery),
        FunctionHandler("optuna_joint_strategy_search", _optuna),
        FunctionHandler("optuna_inmemory_full_joint_strategy_search", _optuna),
        FunctionHandler("ex08_top3_holdout_tournament", _top3),
    ]
    handlers.extend(
        FunctionHandler(
            handler_id,
            lambda context, experiment_dir, function=function: _completed_archive(
                context, experiment_dir, function
            ),
        )
        for handler_id, function in diagnostic.items()
    )
    return tuple(handlers)
