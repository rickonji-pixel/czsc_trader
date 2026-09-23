from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from strategy_runtime.contracts import DataPreparationResult


EXPERIMENT_ID = "20260923_S008_EX58"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    overlay = _read(experiment / "artifacts/protocol.json")
    predecessor = repo / "experiments/S008/20260923_S008_EX57"
    paths = {
        "ex57_manifest_sha256": predecessor / "experiment_manifest.json",
        "ex57_protocol_sha256": predecessor / "artifacts/protocol.json",
        "ex57_script_sha256": predecessor / "run_experiment.py",
    }
    for key, path in paths.items():
        if _sha256(path) != overlay["sources"][key]:
            raise ValueError(f"frozen EX57 successor source differs: {path}")
    validate_experiment_archive(predecessor)

    specification = importlib.util.spec_from_file_location(
        "s008_ex57_base", predecessor / "run_experiment.py"
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load frozen EX57 implementation gate")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    module.EXPERIMENT_ID = EXPERIMENT_ID
    module.__file__ = str(Path(__file__).resolve())

    if hasattr(DataPreparationResult, "dataset_identity"):
        raise ValueError("fixture alias unexpectedly exists before EX58")
    DataPreparationResult.dataset_identity = property(lambda value: value.data_identity)
    try:
        module.main()
    finally:
        delattr(DataPreparationResult, "dataset_identity")

    for name in ("03_execution.md", "04_conclusion.md"):
        path = experiment / name
        path.write_text(
            path.read_text(encoding="utf-8").replace("# S008 EX57", "# S008 EX58"),
            encoding="utf-8",
        )
    evidence = _read(experiment / "artifacts/implementation_evidence.json")
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": overlay["experiment_type"],
            "strategy_id": overlay["strategy_id"],
            "credential_id": overlay["credential_id"],
            "symbol": overlay["symbol"],
            "development_cutoff": overlay["development_cutoff"],
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
