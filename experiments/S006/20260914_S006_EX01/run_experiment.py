from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from factor_signal_catalog import CatalogRegistry

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX01"


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


def _route(row: dict[str, object]) -> str:
    if row["kind"] == "signal":
        return "CATEGORICAL_SIGNAL_AUDIT"
    if row["information_family"] == "EVENT_CATALYST":
        return "EVENT_STUDY"
    return "CONTINUOUS_FACTOR_AUDIT"


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_new_returns"):
        raise ValueError("coverage lock must not read new returns")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("coverage lock cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    termination = repo / "experiments/S005" / str(sources["s005_termination_experiment_id"])
    validate_experiment_archive(termination)
    expected_hashes = {
        repo / "research/RESEARCH_MANDATE.md": sources["research_mandate_sha256"],
        termination / "experiment_manifest.json": sources["s005_termination_manifest_sha256"],
        repo / "experiments/S001/20260913_S001_EX01/artifacts/gap_summary.json": sources["s001_benchmark_sha256"],
        repo / "experiments/S004/20260913_S004_EX41/artifacts/candidate_metrics.json": sources["s004_frequency_benchmark_sha256"],
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")

    registry = CatalogRegistry(repo / "catalog")
    if registry.digest != protocol["catalog"]["expected_digest"]:
        raise ValueError("FSC differs from frozen S006 catalog digest")
    definitions = list(registry.list_definitions())
    ids = [str(row["id"]) for row in definitions]
    if len(ids) != len(set(ids)):
        raise ValueError("FSC snapshot contains duplicate definition IDs")

    ledger_path = artifacts / "definition_coverage_ledger.csv"
    fields = [
        "definition_id", "kind", "name", "information_family", "provider", "catalog_status",
        "evaluation_route", "s006_disposition", "source_experiment", "exclusion_reason",
    ]
    with ledger_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in definitions:
            writer.writerow({
                "definition_id": row["id"],
                "kind": row["kind"],
                "name": row["name"],
                "information_family": row["information_family"],
                "provider": row["provider"],
                "catalog_status": row["status"],
                "evaluation_route": _route(row),
                "s006_disposition": "PENDING_APPLICABILITY_REVIEW",
                "source_experiment": "",
                "exclusion_reason": "",
            })

    ledger_rows = list(csv.DictReader(ledger_path.open(encoding="utf-8")))
    ledger_ids = [row["definition_id"] for row in ledger_rows]
    missing = sorted(set(ids) - set(ledger_ids))
    unexpected = sorted(set(ledger_ids) - set(ids))
    if missing or unexpected or len(ledger_rows) != len(definitions):
        raise ValueError(f"incomplete FSC coverage: missing={missing[:3]}, unexpected={unexpected[:3]}")

    scope_counts = Counter("czsc" if row["provider"] == "czsc" else "project" for row in definitions)
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "catalog_digest": registry.digest,
        "definition_count": len(definitions),
        "kind_counts": dict(sorted(Counter(str(row["kind"]) for row in definitions).items())),
        "scope_counts": dict(sorted(scope_counts.items())),
        "evaluation_route_counts": dict(sorted(Counter(row["evaluation_route"] for row in ledger_rows).items())),
        "disposition_counts": dict(sorted(Counter(row["s006_disposition"] for row in ledger_rows).items())),
        "missing_definition_ids": missing,
        "unexpected_definition_ids": unexpected,
        "silent_exclusion_detected": False,
        "new_return_paths_read": 0,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "coverage_evidence.json", evidence)
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": "PROCEED_TO_FULL_CATALOG_APPLICABILITY_AUDIT",
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
