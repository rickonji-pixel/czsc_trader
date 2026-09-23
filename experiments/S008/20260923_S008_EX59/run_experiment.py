from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from strategy_runtime import StrategyRuntime
from strategy_runtime.contracts import DataPreparationResult


EXPERIMENT_ID = "20260923_S008_EX59"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    overlay_path = experiment / "artifacts/protocol.json"
    overlay = _read(overlay_path)
    predecessor = repo / "experiments/S008/20260923_S008_EX58"
    base = repo / "experiments/S008/20260923_S008_EX56"
    base_protocol_path = base / "artifacts/protocol.json"
    base_script_path = base / "run_experiment.py"
    runtime_root = experiment / "runtime/strategy_runtime"
    runtime_path = runtime_root / "strategies/s008_precious_metal_preference.py"
    paths = {
        "ex58_manifest_sha256": predecessor / "experiment_manifest.json",
        "ex56_manifest_sha256": base / "experiment_manifest.json",
        "ex56_protocol_sha256": base_protocol_path,
        "ex56_script_sha256": base_script_path,
        "ex59_runtime_file_sha256": runtime_path,
    }
    for key, path in paths.items():
        if _sha256(path) != overlay["sources"][key]:
            raise ValueError(f"frozen EX59 source differs: {path}")
    validate_experiment_archive(predecessor)
    validate_experiment_archive(base)

    specification = importlib.util.spec_from_file_location("s008_ex56_base", base_script_path)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load frozen EX56 implementation gate")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    base_read = module._read

    def successor_read(path: Path) -> dict[str, object]:
        if path.resolve() != overlay_path.resolve():
            return base_read(path)
        protocol = base_read(base_protocol_path)
        protocol["experiment_id"] = EXPERIMENT_ID
        protocol["experiment_type"] = str(overlay["experiment_type"])
        protocol["predecessor_experiment_id"] = str(overlay["predecessor_experiment_id"])
        protocol["allowed_datasets"] = list(overlay["allowed_datasets"])
        protocol["sources"]["runtime_source_sha256"] = overlay["sources"][
            "runtime_source_sha256"
        ]
        protocol["synthetic_window"]["source_start"] = overlay["history_correction"][
            "new_required_input_start"
        ]
        return protocol

    module._read = successor_read
    module.EXPERIMENT_ID = EXPERIMENT_ID
    module.__file__ = str(Path(__file__).resolve())
    if hasattr(DataPreparationResult, "dataset_identity"):
        raise ValueError("fixture alias unexpectedly exists before EX59")
    DataPreparationResult.dataset_identity = property(lambda value: value.data_identity)
    try:
        module.main()
    finally:
        delattr(DataPreparationResult, "dataset_identity")

    effective_protocol = successor_read(overlay_path)
    candidate = module._candidate(effective_protocol, runtime_root)
    definition = StrategyRuntime().describe(candidate)
    history = definition.history
    if history.canonical_start != overlay["history_correction"]["new_canonical_start"]:
        raise ValueError("runtime canonical start differs from EX59 correction")
    if history.required_input_start != overlay["history_correction"]["new_required_input_start"]:
        raise ValueError("runtime required input start differs from EX59 correction")

    evidence_path = experiment / "artifacts/implementation_evidence.json"
    evidence = _read(evidence_path)
    evidence["history_scope"] = {
        "canonical_start": history.canonical_start,
        "required_input_start": history.required_input_start,
        "covers_full_etf_development_window": True,
    }
    _write(evidence_path, evidence)
    for name in ("03_execution.md", "04_conclusion.md"):
        path = experiment / name
        path.write_text(
            path.read_text(encoding="utf-8").replace("# S008 EX56", "# S008 EX59"),
            encoding="utf-8",
        )
    (experiment / "03_execution.md").write_text(
        (experiment / "03_execution.md").read_text(encoding="utf-8").rstrip()
        + " 规范回放起点已验证为2013-07-29，输入热身起点为2013-01-02。\n",
        encoding="utf-8",
    )
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
