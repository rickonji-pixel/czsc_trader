"""Build the immutable EX04 candidate bundle and run the unified evaluator."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
from strategy_manager import StrategyRegistry, canonical_sha256

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.evaluation_service import evaluate_experiment
from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import resolve_strategy_payload
from czsc_trader.data import load_market_data
from czsc_trader.factors import generate_factor_frame
from czsc_trader.identity import normalized_text_sha256


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
PROTOCOL_PATH = EXPERIMENT_DIR / "evaluation_protocol.json"
MANIFEST_PATH = EXPERIMENT_DIR / "candidate_manifest.json"
SOURCE_PATHS = (
    REPO_ROOT / "experiments" / "0903_EX03" / "artifacts" / "source_deployable_candidates.csv",
    REPO_ROOT / "experiments" / "0903_EX03" / "artifacts" / "corrective_candidates.csv",
)
WEIGHT_PREFIX = "weight__"
PREPARED_FROM_COMMIT = "5d8b4fbb178b91f6d25d8f89e4e7fa81d98d43eb"


def candidate_payload(source: dict[str, object], candidate_id: str, range_weights: dict[str, float]) -> dict[str, object]:
    payload = copy.deepcopy(source)
    payload.pop("legacy_identity", None)
    payload["research_source"] = {
        "experiment_id": "0903_EX04",
        "candidate_id": candidate_id,
        "protocol_path": "experiments/0903_EX04/evaluation_protocol.json",
    }
    rule = payload["rule"]
    rule["weights"]["range"] = dict(range_weights)
    rule["candidate_id"] = candidate_id
    rule["experiment"] = "0903_EX04"
    rule["research_end"] = "2026-09-02"
    return payload


def _source_candidates() -> pd.DataFrame:
    frames = [pd.read_csv(path) for path in SOURCE_PATHS]
    candidates = pd.concat(frames, ignore_index=True).sort_values("candidate_id", kind="stable")
    if len(candidates) != 1187 or candidates["candidate_id"].duplicated().any():
        raise ValueError("EX03 deployable source must contain 1,187 unique candidates")
    return candidates


def prepare_manifest() -> dict[str, object]:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    registry = StrategyRegistry(REPO_ROOT / "configs" / "strategies")
    release = registry.get_version("S001", "v1")
    if release.release_hash != protocol["incumbent_hash"]:
        raise ValueError("S001-v1 release differs from preregistration")
    execution = release.strategy_payload["rule"]["execution"]
    if canonical_sha256(execution) != protocol["execution_policy_hash"]:
        raise ValueError("execution policy differs from preregistration")

    table = _source_candidates()
    names = list(release.strategy_payload["rule"]["weights"]["range"])
    columns = [f"{WEIGHT_PREFIX}{name}" for name in names]
    if not set(columns) <= set(table.columns):
        raise ValueError("candidate source lacks frozen range factors")
    weights = table.loc[:, columns].astype(float)
    if (weights < 0.005 - 1e-12).any().any() or not weights.sum(axis=1).sub(1.0).abs().le(1e-9).all():
        raise ValueError("candidate weights violate deployable simplex")
    base_weights = release.strategy_payload["rule"]["weights"]["range"]
    control = table.loc[table["candidate_id"].eq(0), columns]
    if len(control) != 1 or any(abs(float(control.iloc[0][f"{WEIGHT_PREFIX}{name}"]) - float(base_weights[name])) > 1e-12 for name in names):
        raise ValueError("candidate zero is not the S001-v1 range control")

    data = load_market_data(REPO_ROOT / "data" / "raw", "588080.SH", "etf", cutoff="2026-09-02")
    factor_frame = generate_factor_frame(data).frame
    close = pd.Series(data.daily["close"].astype(float).to_numpy(), index=pd.DatetimeIndex(pd.to_datetime(data.daily["dt"]), name="dt"))
    baseline = resolve_strategy_payload(
        REPO_ROOT / "configs" / "rule_baselines", release.strategy_payload,
        release_id=release.release_id, release_hash=release.release_hash, symbol="588080.SH",
    )
    incumbent_target = apply_resolved_baseline(factor_frame, baseline, daily_close=close).target_position
    incumbent_behavior = canonical_sha256([float(value) for value in incumbent_target])
    descriptors: list[dict[str, object]] = [{
        "candidate_id": "S001-v1", "strategy_id": "S001", "strategy_name": "综合基线策略",
        "strategy_hash": release.release_hash, "execution_policy_hash": protocol["execution_policy_hash"],
        "behavior_hash": incumbent_behavior, "family": "S001-range-weights", "generation_stage": "incumbent",
        "parent_candidate_id": None, "parameter_group": "range_weights", "parameter_distance": 0.0,
        "is_incumbent": True, "strategy_payload": release.strategy_payload,
    }]
    trials: list[dict[str, object]] = [{
        "trial_id": "EX04-S001-v1", "candidate_id": "S001-v1", "strategy_hash": release.release_hash,
        "behavior_hash": incumbent_behavior, "status": "COMPLETED",
    }]
    for _, row in table.iterrows():
        source_id = int(row["candidate_id"])
        if source_id == 0:
            continue
        candidate_id = f"R{source_id:04d}"
        range_weights = {name: float(row[f"{WEIGHT_PREFIX}{name}"]) for name in names}
        payload = candidate_payload(release.strategy_payload, candidate_id, range_weights)
        candidate_hash = canonical_sha256(payload)
        resolved = resolve_strategy_payload(
            REPO_ROOT / "configs" / "rule_baselines", payload,
            release_id=candidate_id, release_hash=candidate_hash, symbol="588080.SH",
        )
        target = apply_resolved_baseline(factor_frame, resolved, daily_close=close).target_position
        behavior_hash = canonical_sha256([float(value) for value in target])
        distance = sum(abs(range_weights[name] - float(base_weights[name])) for name in names)
        descriptors.append({
            "candidate_id": candidate_id, "strategy_id": "S001", "strategy_name": "综合基线策略",
            "strategy_hash": candidate_hash, "execution_policy_hash": protocol["execution_policy_hash"],
            "behavior_hash": behavior_hash, "family": "S001-range-weights", "generation_stage": "EX03-reuse",
            "parent_candidate_id": "S001-v1", "parameter_group": "range_weights", "parameter_distance": distance,
            "is_incumbent": False, "strategy_payload": payload,
        })
        trials.append({
            "trial_id": f"EX04-{candidate_id}", "candidate_id": candidate_id, "strategy_hash": candidate_hash,
            "behavior_hash": behavior_hash, "status": "COMPLETED",
        })
    manifest = {
        "schema_version": 1, "experiment_id": "0903_EX04", "prepared_from_commit": PREPARED_FROM_COMMIT,
        "source_files": {path.relative_to(REPO_ROOT).as_posix(): normalized_text_sha256(path) for path in SOURCE_PATHS},
        "symbol": "588080.SH", "asset_type": "etf", "fee_rate": 0.0005, "init_cash": 1_000_000.0,
        "forward_start": "2026-09-03",
        "windows": {
            "full": {"start": "2021-01-01", "end": "2026-09-02"},
            "2021": {"start": "2021-01-01", "end": "2021-12-31"},
            "2022": {"start": "2022-01-01", "end": "2022-12-31"},
            "2023": {"start": "2023-01-01", "end": "2023-12-31"},
            "2024": {"start": "2024-01-01", "end": "2024-12-31"},
            "2025": {"start": "2025-01-01", "end": "2025-12-31"},
            "2026_ytd": {"start": "2026-01-01", "end": "2026-09-02"},
        },
        "candidates": descriptors, "trials": trials,
        "audits": {
            "candidate_count": len(descriptors), "trial_count": len(trials),
            "range_only_mutation": True, "minimum_factor_weight": 0.005,
            "trend_weights_frozen": True, "execution_policy_frozen": True,
        },
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def run() -> dict[str, object]:
    prepare_manifest()
    context = RepositoryContext.discover(REPO_ROOT, explicit_root=REPO_ROOT)
    return dict(evaluate_experiment(context, "0903_EX04").result)


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
