from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX57"


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
    overlay_path = experiment / "artifacts/protocol.json"
    overlay = _read(overlay_path)
    predecessor = repo / "experiments/S008/20260923_S008_EX56"
    base_protocol_path = predecessor / "artifacts/protocol.json"
    base_script_path = predecessor / "run_experiment.py"
    runtime_root = predecessor / "runtime/strategy_runtime"
    runtime_path = runtime_root / "strategies/s008_precious_metal_preference.py"
    paths = {
        "ex56_manifest_sha256": predecessor / "experiment_manifest.json",
        "ex56_protocol_sha256": base_protocol_path,
        "ex56_script_sha256": base_script_path,
        "ex56_runtime_file_sha256": runtime_path,
    }
    for key, path in paths.items():
        if _sha256(path) != overlay["sources"][key]:
            raise ValueError(f"frozen EX56 successor source differs: {path}")
    validate_experiment_archive(predecessor)

    specification = importlib.util.spec_from_file_location("s008_ex56_base", base_script_path)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load frozen EX56 implementation gate")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    base_read = module._read
    base_candidate = module._candidate
    base_implementation_sha256 = module.implementation_sha256

    def successor_read(path: Path) -> dict[str, object]:
        if path.resolve() != overlay_path.resolve():
            return base_read(path)
        protocol = base_read(base_protocol_path)
        protocol["experiment_id"] = EXPERIMENT_ID
        protocol["experiment_type"] = str(overlay["experiment_type"])
        protocol["predecessor_experiment_id"] = str(overlay["predecessor_experiment_id"])
        protocol["allowed_datasets"] = list(overlay["allowed_datasets"])
        return protocol

    def successor_candidate(protocol, source_root):
        del source_root
        return base_candidate(protocol, runtime_root)

    def successor_implementation_sha256(source_files, *, source_root=None):
        if source_root is not None and Path(source_root).resolve() == (
            experiment / "runtime/strategy_runtime"
        ).resolve():
            source_root = runtime_root
        return base_implementation_sha256(source_files, source_root=source_root)

    module._read = successor_read
    module._candidate = successor_candidate
    module.implementation_sha256 = successor_implementation_sha256
    module.EXPERIMENT_ID = EXPERIMENT_ID
    module.__file__ = str(Path(__file__).resolve())
    module.main()

    for name in ("03_execution.md", "04_conclusion.md"):
        path = experiment / name
        path.write_text(
            path.read_text(encoding="utf-8").replace("# S008 EX56", "# S008 EX57"),
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
