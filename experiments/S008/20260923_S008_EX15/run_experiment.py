from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX15"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _monthly_with_development_boundary(
    frame: pd.DataFrame, sessions: pd.DatetimeIndex
) -> pd.DataFrame:
    values = frame.copy().sort_values("Date")
    available_calendar = values["Date"] + pd.offsets.MonthBegin(2)
    effective_positions = sessions.searchsorted(available_calendar)
    within_development = effective_positions < len(sessions)
    values = values.loc[within_development].copy()
    effective_positions = effective_positions[within_development]
    values.index = sessions[effective_positions]
    values = values.drop(columns=["Date"])
    if values.index.duplicated().any():
        raise ValueError("monthly effective sessions are not unique")
    return values.reindex(sessions).ffill()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    overlay_path = experiment / "artifacts/protocol.json"
    overlay = _read(overlay_path)
    base_protocol_path = repo / str(overlay["base_protocol_path"])
    base_script_path = repo / str(overlay["base_script_path"])
    predecessor_manifest = repo / "experiments/S008/20260923_S008_EX14/experiment_manifest.json"
    if _sha256(base_protocol_path) != overlay["base_protocol_sha256"]:
        raise ValueError("EX14 base protocol differs from frozen identity")
    if _sha256(base_script_path) != overlay["base_script_sha256"]:
        raise ValueError("EX14 base script differs from frozen identity")
    if _sha256(predecessor_manifest) != overlay["predecessor_manifest_sha256"]:
        raise ValueError("EX14 predecessor manifest differs from frozen identity")
    validate_experiment_archive(predecessor_manifest.parent)

    specification = importlib.util.spec_from_file_location("s008_ex14_base", base_script_path)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load frozen EX14 implementation")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    base_read = module._read

    def inherited_read(path: Path) -> dict[str, object]:
        if path.resolve() != overlay_path.resolve():
            return base_read(path)
        protocol = base_read(base_protocol_path)
        protocol["experiment_id"] = EXPERIMENT_ID
        protocol["experiment_type"] = str(overlay["experiment_type"])
        protocol["predecessor_experiment_id"] = str(overlay["predecessor_experiment_id"])
        return protocol

    module._read = inherited_read
    module._monthly = _monthly_with_development_boundary
    module.EXPERIMENT_ID = EXPERIMENT_ID
    module.__file__ = str(Path(__file__).resolve())
    module.main()

    for name in ("03_execution.md", "04_conclusion.md"):
        path = experiment / name
        path.write_text(
            path.read_text(encoding="utf-8").replace("# S008 EX14", "# S008 EX15"),
            encoding="utf-8",
        )
    evidence = _read(experiment / "artifacts/materialization_evidence.json")
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": overlay["experiment_type"],
            "strategy_id": overlay["strategy_id"],
            "credential_id": overlay["credential_id"],
            "symbol": overlay["symbol"],
            "development_cutoff": "2024-12-31",
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
