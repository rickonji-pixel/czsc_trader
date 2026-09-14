from __future__ import annotations

import hashlib
import json
from pathlib import Path

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX02"


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
        raise ValueError("mandate amendment must not read new returns")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("mandate amendment cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    initial = repo / "experiments/S006" / str(sources["initial_contract_experiment_id"])
    validate_experiment_archive(initial)
    benchmark = repo / str(sources["s001_benchmark_path"])
    expected_hashes = {
        initial / "experiment_manifest.json": sources["initial_contract_manifest_sha256"],
        benchmark: sources["s001_benchmark_sha256"],
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")

    initial_protocol = _read(initial / "artifacts/protocol.json")
    benchmark_data = _read(benchmark)
    old_limit = float(protocol["amendment"]["old_value"])
    new_limit = float(protocol["amendment"]["new_value"])
    benchmark_drawdown = float(benchmark_data["full_window"]["strategy_max_drawdown"])
    if float(initial_protocol["objectives"]["maximum_drawdown_limit"]) != old_limit:
        raise ValueError("declared old drawdown limit differs from EX01")
    if new_limit <= benchmark_drawdown:
        raise ValueError("new drawdown limit is not strictly better than S001-v2")

    _write(
        experiment / "artifacts/amendment_evidence.json",
        {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "old_maximum_drawdown_limit": old_limit,
            "new_maximum_drawdown_limit": new_limit,
            "s001_v2_maximum_drawdown": benchmark_drawdown,
            "strictly_better_than_s001_v2": new_limit > benchmark_drawdown,
            "new_return_paths_read": 0,
            "candidate_created": False,
            "strategy_manager_mutated": False,
            "pte_mutated": False,
        },
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": "AMEND_MAXIMUM_DRAWDOWN_LIMIT_TO_20_PERCENT",
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
