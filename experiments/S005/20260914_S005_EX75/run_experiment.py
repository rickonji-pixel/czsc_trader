from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from factor_signal_catalog import CatalogRegistry

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX75"


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
    protocol = _read(experiment / "artifacts/protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_new_returns"):
        raise ValueError("termination audit must not read new returns")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("termination audit cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    source_experiments: dict[str, Path] = {
        "EX72": repo / "experiments/S005" / str(sources["categorical_information_experiment_id"]),
        "EX74": repo / "experiments/S005" / str(sources["complete_strategy_experiment_id"]),
    }
    for source in source_experiments.values():
        validate_experiment_archive(source)
    expected_hashes = {
        source_experiments["EX72"] / "experiment_manifest.json": sources["categorical_information_manifest_sha256"],
        source_experiments["EX74"] / "experiment_manifest.json": sources["complete_strategy_manifest_sha256"],
        repo / "catalog/factors/project.json": sources["project_factor_catalog_sha256"],
        repo / "catalog/signals/project.json": sources["project_signal_catalog_sha256"],
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")

    definitions = CatalogRegistry(repo / "catalog").list_definitions()
    provider_counts = Counter("czsc" if row["provider"] == "czsc" else "project" for row in definitions)
    kind_counts = Counter(str(row["kind"]) for row in definitions)
    ex72_protocol = _read(source_experiments["EX72"] / "artifacts/protocol.json")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "termination_status": protocol["termination"]["status"],
        "termination_reason": protocol["termination"]["reason"],
        "fsc_definition_count": len(definitions),
        "fsc_kind_counts": dict(sorted(kind_counts.items())),
        "fsc_scope_counts": dict(sorted(provider_counts.items())),
        "ex72_experiment_type": ex72_protocol["experiment_type"],
        "ex72_unit": ex72_protocol["model"]["unit"],
        "ex72_return_path_count": ex72_protocol["return_path_count"],
        "ex74_decision": _read(source_experiments["EX74"] / "experiment_manifest.json")["decision"],
        "valid_conclusion_scope": "SELECTED_CZSC_DOMINANT_ARCHITECTURE_ONLY",
        "invalid_scope_expansion": "FULL_FSC_POTENTIAL_EXHAUSTED",
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(experiment / "artifacts/termination_evidence.json", evidence)
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": protocol["termination"]["status"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
