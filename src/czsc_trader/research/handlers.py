from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from czsc_trader.experiment_archive import MANIFEST_NAME

from .contracts import ResearchContext


Runner = Callable[[ResearchContext, Path], dict[str, object]]


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
        context: ResearchContext,
        experiment_dir: Path,
    ) -> dict[str, object]:
        return self._runner(context, experiment_dir)

    def replay(
        self,
        context: ResearchContext,
        source_dir: Path,
        output_dir: Path,
    ) -> dict[str, object]:
        shutil.copytree(source_dir, output_dir, dirs_exist_ok=True)
        (output_dir / MANIFEST_NAME).unlink(missing_ok=True)
        return self.run(context, output_dir)


def _completed_archive(
    context: ResearchContext,
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
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from . import preregistered

    match = re.fullmatch(r"(\d{2})(\d{2})_EX\d{2}", experiment_dir.name)
    if match is None:
        raise ValueError("champion challenge directory must use MMDD_EXXX")
    run_date = date(datetime.now().year, int(match.group(1)), int(match.group(2)))
    protocol_path = (experiment_dir / "artifacts" / "protocol.json").resolve()
    with tempfile.TemporaryDirectory(
        prefix=".champion_challenge_", dir=experiment_dir.parent
    ) as temporary:
        with _repository_cwd(context.root):
            generated = preregistered.main(
                Path(temporary),
                run_date=run_date,
                protocol_path=protocol_path,
            )
        shutil.rmtree(experiment_dir)
        shutil.copytree(generated, experiment_dir)
    manifest = json.loads(
        (experiment_dir / MANIFEST_NAME).read_text(encoding="utf-8")
    )
    return {
        "status": str(manifest.get("status", "COMPLETE")),
        "experiment_dir": str(experiment_dir),
    }


def _four_layer(context: ResearchContext, experiment_dir: Path) -> dict[str, object]:
    from czsc_trader.four_layer_runner import run_four_layer_experiment

    return run_four_layer_experiment(
        context.raw_dir, context.baseline_root, experiment_dir
    )


def _return_only(context: ResearchContext, experiment_dir: Path) -> dict[str, object]:
    from czsc_trader.return_only_runner import run_return_only_experiment

    return run_return_only_experiment(
        context.raw_dir, context.baseline_root, experiment_dir
    )


def _factor_discovery(
    context: ResearchContext, experiment_dir: Path
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


def _optuna(context: ResearchContext, experiment_dir: Path) -> dict[str, object]:
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


def _top3(context: ResearchContext, experiment_dir: Path) -> dict[str, object]:
    from czsc_trader.top3_holdout_runner import run_tournament_experiment

    return run_tournament_experiment(
        context.root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _position_sizing(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.position_sizing_runner import run_position_sizing_experiment

    return run_position_sizing_experiment(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _dynamic_position_sizing(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.dynamic_position_sizing_runner import (
        run_dynamic_position_sizing_experiment,
    )

    return run_dynamic_position_sizing_experiment(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _downside_risk_position(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.downside_risk_runner import run_downside_risk_experiment

    return run_downside_risk_experiment(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _position_risk_program(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.position_risk_runner import run_position_risk_program

    return run_position_risk_program(
        context.raw_dir,
        context.baseline_root,
        context.experiments_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _czsc_factor_stability(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.czsc_factor_stability_runner import (
        run_czsc_factor_stability_experiment,
    )

    return run_czsc_factor_stability_experiment(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _czsc_bi_layer_stability(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.czsc_bi_layer_runner import run_bi_layer_stability_experiment

    return run_bi_layer_stability_experiment(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _czsc_state_age(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.czsc_state_age_runner import run_state_age_diagnosis

    return run_state_age_diagnosis(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _czsc_incremental_validity(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.czsc_incremental_validity_runner import (
        run_incremental_validity_diagnosis,
    )

    return run_incremental_validity_diagnosis(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _czsc_route_family(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.czsc_route_runner import run_czsc_route_family

    return run_czsc_route_family(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _czsc_route_multiplicity(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.czsc_multiplicity_runner import run_multiplicity_audit

    return run_multiplicity_audit(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _czsc_champion_condition(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.czsc_champion_condition_runner import (
        run_champion_position_condition,
    )

    return run_champion_position_condition(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _czsc_pairwise_interaction(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.czsc_pairwise_runner import run_pairwise_interaction

    return run_pairwise_interaction(
        context.root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _czsc_unified_factor_integration(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.czsc_strategy_integration_runner import (
        run_unified_factor_integration,
    )

    return run_unified_factor_integration(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _ex13_score_monotonicity(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.score_tier_diagnostic_runner import run_score_tier_diagnostic

    return run_score_tier_diagnostic(
        context.raw_dir,
        context.baseline_root,
        experiment_dir,
        execution_commit=_git_head(context.root),
    )


def _regime_conditioned_weight(
    context: ResearchContext, experiment_dir: Path
) -> dict[str, object]:
    from czsc_trader.regime_weight_runner import run_regime_weight_experiment

    return run_regime_weight_experiment(
        context.raw_dir,
        context.baseline_root,
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
        FunctionHandler("entry_fixed_position_sizing", _position_sizing),
        FunctionHandler(
            "dynamic_score_zone_position_sizing", _dynamic_position_sizing
        ),
        FunctionHandler(
            "downside_risk_position_optimization", _downside_risk_position
        ),
        FunctionHandler("position_risk_five_rounds", _position_risk_program),
        FunctionHandler("czsc_factor_stability_diagnostic", _czsc_factor_stability),
        FunctionHandler("czsc_bi_layer_stability_diagnostic", _czsc_bi_layer_stability),
        FunctionHandler("czsc_state_age_diagnosis", _czsc_state_age),
        FunctionHandler("czsc_incremental_validity_diagnosis", _czsc_incremental_validity),
        FunctionHandler("czsc_route_family_diagnostic", _czsc_route_family),
        FunctionHandler("czsc_route_multiplicity_audit", _czsc_route_multiplicity),
        FunctionHandler("czsc_champion_position_condition", _czsc_champion_condition),
        FunctionHandler("czsc_route_pairwise_interaction", _czsc_pairwise_interaction),
        FunctionHandler("czsc_unified_factor_integration", _czsc_unified_factor_integration),
        FunctionHandler("ex13_score_monotonicity_diagnostic", _ex13_score_monotonicity),
        FunctionHandler("regime_conditioned_weight_challenge", _regime_conditioned_weight),
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
