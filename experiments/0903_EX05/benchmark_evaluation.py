"""Benchmark exact candidate evaluation before resuming EX05."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
from time import perf_counter

import pandas as pd
from strategy_evaluator import CandidateDescriptor, EvaluationProtocol, screen_candidates

from czsc_trader.application.context import RepositoryContext
from czsc_trader.candidate_evaluation import (
    CandidateEvaluationContext,
    _evaluate_candidate_payloads_reference,
    evaluate_candidate_payloads,
)
from czsc_trader.evaluation_artifacts import _source_rows
from czsc_trader.identity import canonical_json_sha256


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
SOURCE_EXPERIMENT = REPO_ROOT / "experiments" / "0903_EX04"
OUTPUT_PATH = EXPERIMENT_DIR / "artifacts" / "evaluation_benchmark.json"


def representative_ids(candidates: list[dict[str, object]], count: int = 64) -> tuple[str, ...]:
    values = sorted(
        str(item["candidate_id"])
        for item in candidates
        if not bool(item.get("is_incumbent", False))
    )
    if len(values) < count or count < 2:
        raise ValueError("benchmark requires at least count candidates and count >= 2")
    return tuple(values[math.floor(index * (len(values) - 1) / (count - 1))] for index in range(count))


def _context(repository, manifest, protocol, workers):
    periods = tuple(
        (str(name), (pd.Timestamp(value["start"]), pd.Timestamp(value["end"])))
        for name, value in manifest["windows"].items()
    )
    return CandidateEvaluationContext(
        repository,
        str(manifest["symbol"]),
        str(manifest.get("asset_type", "etf")),
        periods,
        float(manifest.get("fee_rate", 0.0005)),
        float(manifest.get("init_cash", 1_000_000.0)),
        workers,
    )


def _descriptor(value):
    return CandidateDescriptor(
        str(value["candidate_id"]),
        str(value.get("strategy_hash", value.get("candidate_hash", ""))),
        str(value["execution_policy_hash"]),
        bool(value.get("is_incumbent", False)),
        str(value.get("behavior_hash", "")),
        float(value.get("parameter_distance", 0.0)),
        str(value.get("family", "")),
        str(value.get("generation_stage", "")),
        None if value.get("parent_candidate_id") is None else str(value["parent_candidate_id"]),
        str(value.get("parameter_group", "")),
    )


def source_screening_metric_hash() -> tuple[int, str]:
    screening = _source_rows(SOURCE_EXPERIMENT)[0]
    observations = [value[0].to_dict() for value in screening.values()]
    return len(observations), canonical_json_sha256({"observations": observations})


def run_benchmark() -> dict[str, object]:
    manifest = json.loads((SOURCE_EXPERIMENT / "candidate_manifest.json").read_text(encoding="utf-8"))
    protocol = EvaluationProtocol.from_dict(
        json.loads((SOURCE_EXPERIMENT / "evaluation_protocol.json").read_text(encoding="utf-8"))
    )
    ids = representative_ids(manifest["candidates"])
    by_id = {str(item["candidate_id"]): item for item in manifest["candidates"]}
    candidates = tuple(by_id[candidate_id] for candidate_id in ids)
    repository = RepositoryContext.discover(REPO_ROOT, explicit_root=REPO_ROOT)
    runs: list[dict[str, object]] = []

    started = perf_counter()
    reference = _evaluate_candidate_payloads_reference(
        _context(repository, manifest, protocol, 1), protocol, candidates, ids, "SCREENING",
    )
    reference_seconds = perf_counter() - started
    reference_rows = [item.to_dict() for item in reference]
    runs.append({
        "path": "reference",
        "workers": 1,
        "seconds": reference_seconds,
        "exact": True,
        "metric_hash": canonical_json_sha256({"observations": reference_rows}),
    })

    optimized_one_seconds = 0.0
    for workers in (1, 2, 4, 8):
        started = perf_counter()
        observations = evaluate_candidate_payloads(
            _context(repository, manifest, protocol, workers),
            protocol,
            candidates,
            ids,
            "SCREENING",
        )
        seconds = perf_counter() - started
        rows = [item.to_dict() for item in observations]
        exact = rows == reference_rows
        if workers == 1:
            optimized_one_seconds = seconds
        runs.append({
            "path": "optimized",
            "workers": workers,
            "seconds": seconds,
            "exact": exact,
            "metric_hash": canonical_json_sha256({"observations": rows}),
            "speedup_vs_reference": reference_seconds / seconds,
            "speedup_vs_optimized_one": None if workers == 1 else optimized_one_seconds / seconds,
        })

    eligible = [
        item for item in runs
        if item["path"] == "optimized"
        and item["workers"] > 1
        and item["exact"]
        and float(item["speedup_vs_reference"]) >= 3.0
    ]
    selected_workers = None
    if eligible:
        fastest = min(eligible, key=lambda item: float(item["seconds"]))
        near = [
            item for item in eligible
            if float(item["seconds"]) <= float(fastest["seconds"]) * 1.05
        ]
        selected_workers = min(int(item["workers"]) for item in near)
    result = {
        "schema_version": 1,
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
        },
        "candidate_ids": list(ids),
        "candidate_count": len(ids),
        "runs": runs,
        "required_parallel_speedup": 3.0,
        "selected_workers": selected_workers,
        "status": "PASS" if selected_workers is not None else "FAIL",
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_PATH)
    return result


def run_full_scale() -> dict[str, object]:
    benchmark = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    workers = benchmark.get("selected_workers")
    if benchmark.get("status") != "PASS" or workers not in {2, 4, 8}:
        raise ValueError("passing fixed benchmark is required before full-scale run")
    manifest = json.loads((SOURCE_EXPERIMENT / "candidate_manifest.json").read_text(encoding="utf-8"))
    protocol = EvaluationProtocol.from_dict(
        json.loads((SOURCE_EXPERIMENT / "evaluation_protocol.json").read_text(encoding="utf-8"))
    )
    payloads = tuple(manifest["candidates"])
    descriptors = tuple(_descriptor(item) for item in payloads)
    preliminary = screen_candidates(protocol, descriptors, ())
    ids = (protocol.incumbent_id, *preliminary.candidate_ids)
    repository = RepositoryContext.discover(REPO_ROOT, explicit_root=REPO_ROOT)
    started = perf_counter()
    observations = evaluate_candidate_payloads(
        _context(repository, manifest, protocol, int(workers)),
        protocol,
        payloads,
        ids,
        "SCREENING",
    )
    seconds = perf_counter() - started
    metric_hash = canonical_json_sha256({
        "observations": [item.to_dict() for item in observations],
    })
    source_count, source_metric_hash = source_screening_metric_hash()
    benchmark["full_scale_cold"] = {
        "candidate_pool_count": len(payloads),
        "evaluated_candidate_count": len(ids),
        "observation_count": len(observations),
        "workers": workers,
        "seconds": seconds,
        "maximum_seconds": 300.0,
        "metric_hash": metric_hash,
        "source_experiment": SOURCE_EXPERIMENT.name,
        "source_observation_count": source_count,
        "source_metric_hash": source_metric_hash,
        "matches_source": len(observations) == source_count and metric_hash == source_metric_hash,
        "status": "PASS" if seconds <= 300.0 else "FAIL",
    }
    temporary = OUTPUT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(benchmark, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_PATH)
    return benchmark


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-scale", action="store_true")
    arguments = parser.parse_args()
    result = run_full_scale() if arguments.full_scale else run_benchmark()
    print(json.dumps(result, ensure_ascii=False, indent=2))
