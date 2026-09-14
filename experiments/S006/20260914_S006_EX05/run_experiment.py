from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX05"


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
        raise ValueError("execution anchor amendment must not read returns")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("execution anchor amendment cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    source = repo / "experiments/S006" / str(sources["evaluation_lock_experiment_id"])
    validate_experiment_archive(source)
    if _sha256(source / "experiment_manifest.json") != str(sources["evaluation_lock_manifest_sha256"]):
        raise ValueError("frozen EX04 source differs")
    ex04 = _read(source / "artifacts/protocol.json")
    if ex04["evaluation"]["entry"] != protocol["amendment"]["old_value"]:
        raise ValueError("declared old execution anchor differs from EX04")
    clocks = pd.to_datetime(pd.Series(["09:00:00", "09:30:00", "10:30:00"]), format="%H:%M:%S").dt.time
    open_clock = pd.Timestamp(protocol["amendment"]["market_open_clock"]).time()
    same_day = [clock <= open_clock for clock in clocks]
    if same_day != [True, True, False]:
        raise ValueError("first tradable open boundary behaves unexpectedly")

    _write(
        experiment / "artifacts/amendment_evidence.json",
        {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "market_open_clock": str(open_clock),
            "boundary_examples": {
                "09:00:00": "SAME_DAY_OPEN",
                "09:30:00": "SAME_DAY_OPEN",
                "10:30:00": "NEXT_SESSION_OPEN",
            },
            "preregistered_return_paths": int(protocol["unchanged"]["preregistered_return_paths"]),
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
            "decision": "USE_FIRST_TRADABLE_OPEN_CAUSAL_ANCHOR",
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
