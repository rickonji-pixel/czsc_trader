"""Run the preregistered EX13 score monotonicity diagnosis."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from .baselines import resolve_baseline
from .czsc_factor_stability import build_forward_outcomes
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .score_tier_diagnostic import diagnose_score_monotonicity
from .score_tier_position import classify_score_tiers
from .czsc_strategy_integration_runner import _strategy_inputs, _target


EXPECTED_CHALLENGER_MANIFEST = "7f381ff0e6cefffd77149cab93d759b46a1b6ef06d046b1169ffd3506a011589"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def validate_protocol(protocol: Mapping[str, object]) -> None:
    expected = {
        "handler": "ex13_score_monotonicity_diagnostic",
        "symbol": "588080.SH",
        "visible_end": "2025-12-31",
        "access_2026": False,
        "challenger_experiment": "0901_EX13",
        "challenger_manifest_sha256": EXPECTED_CHALLENGER_MANIFEST,
        "tier_boundaries": [0.025, 0.075, 0.125, 0.175],
        "minimum_annual_support": 30,
        "hac_max_lag": 20,
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"score-tier diagnostic protocol {key} differs from preregistration")
    experiment_id = protocol.get("experiment_id")
    if experiment_id == "0901_EX16":
        return
    retry_expected = {
        "experiment_id": "0901_EX17",
        "technical_retry_of": "0901_EX16",
        "technical_retry_manifest_sha256": "1203b1cd18a7e0bb6a45e110332b7a1ea33012c36a16d8ffc696e9586f892b8f",
        "numeric_prefix_absolute_tolerance": 1e-12,
    }
    for key, value in retry_expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"score-tier diagnostic retry {key} differs from preregistration")


def _safe_json(value: object) -> object:
    if isinstance(value, (np.floating, float)) and not np.isfinite(float(value)):
        return None
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (np.integer,)):
        return int(value)
    return value


def numeric_prefix_equal(
    left: pd.Series,
    right: pd.Series,
    *,
    absolute_tolerance: float = 1e-12,
) -> bool:
    """Compare causal numeric replays while allowing machine-scale roundoff."""
    if not left.index.equals(right.index):
        return False
    return bool(
        np.allclose(
            left.to_numpy(dtype=float),
            right.to_numpy(dtype=float),
            rtol=0.0,
            atol=float(absolute_tolerance),
            equal_nan=True,
        )
    )


def run_score_tier_diagnostic(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    validate_protocol(protocol)
    challenger_dir = experiment_dir.parent / str(protocol["challenger_experiment"])
    actual_parent_hash = sha256((challenger_dir / "experiment_manifest.json").read_bytes()).hexdigest()
    if actual_parent_hash != str(protocol["challenger_manifest_sha256"]):
        raise ValueError("score-tier diagnostic challenger identity differs")
    frozen = json.loads(
        (challenger_dir / "artifacts" / "frozen_historical_challenger.json").read_text(encoding="utf-8")
    )
    baseline = resolve_baseline(
        Path(baseline_root), str(frozen["champion"]["version"]), symbol="588080.SH"
    )
    if baseline.sha256 != str(frozen["champion"]["sha256"]):
        raise ValueError("score-tier diagnostic champion identity differs")
    source_dir = experiment_dir.parent / "0901_EX08"
    source_protocol = json.loads(
        (source_dir / "artifacts" / "protocol.json").read_text(encoding="utf-8")
    )
    data = load_market_data(
        raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["visible_end"]))
    )
    factors, weights, event, champion = _strategy_inputs(
        data, baseline, source_protocol, source_dir, str(frozen["source_factor"])
    )
    scores, target = _target(
        factors, weights, event, baseline, float(frozen["event_weight"])
    )
    evaluation = scores.index.year.astype(int) >= 2021
    evaluation &= scores.index.year.astype(int) <= 2025
    eval_index = scores.index[evaluation]
    outcomes = build_forward_outcomes(data.daily, scores.index, (5, 10, 20)).reindex(eval_index)
    diagnostic = diagnose_score_monotonicity(
        scores.reindex(eval_index),
        target.reindex(eval_index),
        event.reindex(eval_index),
        outcomes,
        minimum_annual_support=int(protocol["minimum_annual_support"]),
        hac_max_lag=int(protocol["hac_max_lag"]),
    )

    prefix_data = load_market_data(
        raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp("2023-12-31")
    )
    prefix_factors, prefix_weights, prefix_event, _ = _strategy_inputs(
        prefix_data, baseline, source_protocol, source_dir, str(frozen["source_factor"])
    )
    prefix_scores, prefix_target = _target(
        prefix_factors,
        prefix_weights,
        prefix_event,
        baseline,
        float(frozen["event_weight"]),
    )
    prefix_tiers = classify_score_tiers(prefix_scores)
    full_tiers = classify_score_tiers(scores)
    causal = {
        "status": "PASS",
        "prefix_cutoff": "2023-12-31",
        "score_equal": numeric_prefix_equal(
            prefix_scores,
            scores.reindex(prefix_scores.index),
            absolute_tolerance=float(protocol.get("numeric_prefix_absolute_tolerance", 0.0)),
        ),
        "tier_equal": prefix_tiers.equals(full_tiers.reindex(prefix_tiers.index)),
        "event_equal": prefix_event.equals(event.reindex(prefix_event.index)),
        "target_equal": prefix_target.equals(target.reindex(prefix_target.index)),
        "access_2026": False,
    }
    if not all(bool(causal[key]) for key in ("score_equal", "tier_equal", "event_equal", "target_equal")):
        causal["status"] = "FAIL"
    status = str(diagnostic.summary["status"]) if causal["status"] == "PASS" else "ERROR"
    diagnostic.tier_metrics.to_csv(artifacts / "tier_metrics.csv", index=False, encoding="utf-8-sig")
    diagnostic.yearly_metrics.to_csv(artifacts / "yearly_metrics.csv", index=False, encoding="utf-8-sig")
    diagnostic.influence_audit.to_csv(artifacts / "influence_audit.csv", index=False, encoding="utf-8-sig")
    _write_json(artifacts / "hac_result.json", _safe_json(diagnostic.hac_result))
    _write_json(artifacts / "causal_replay_audit.json", causal)
    summary = {
        **diagnostic.summary,
        "status": status,
        "evaluation_start": "2021-01-01",
        "evaluation_end": "2025-12-31",
        "observation_count": int(len(eval_index)),
        "held_observation_count": int(target.reindex(eval_index).eq(1.0).sum()),
        "causal_replay_status": causal["status"],
        "access_2026": False,
    }
    _write_json(artifacts / "metrics.json", _safe_json(summary))
    _write_json(
        artifacts / "identity_audit.json",
        {
            "status": "PASS",
            "challenger_manifest_sha256": actual_parent_hash,
            "champion_version": baseline.version,
            "champion_sha256": baseline.sha256,
            "visible_data_hashes": data.hashes,
            "access_2026": False,
        },
    )
    experiment_id = str(protocol["experiment_id"])
    (experiment_dir / "03_execution.md").write_text(
        "\n".join(
            [
                f"# {experiment_id} 执行过程",
                "",
                f"- 执行提交：`{execution_commit}`。",
                f"- 评价交易日：{len(eval_index)}；EX13持仓日：{int(target.reindex(eval_index).eq(1.0).sum())}。",
                f"- 年度支持：{diagnostic.summary['supported_years']}/5；收益与回撤同向年度：{diagnostic.summary['same_direction_years']}/5。",
                f"- 20日回撤HAC系数/p值：{float(diagnostic.hac_result['coefficient']):.8f} / {float(diagnostic.hac_result['p_value']):.8f}。",
                f"- 因果前缀重放：`{causal['status']}`；未读取2026。",
                "- 本轮只诊断计分单调性，没有生成仓位候选。",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(
            [
                f"# {experiment_id} 结论",
                "",
                f"状态：`{status}`。",
                "",
                f"S4相对S1/S2的20日收益差为{float(diagnostic.summary['aggregate_return_effect']):.6f}，路径最大回撤差为{float(diagnostic.summary['aggregate_drawdown_effect']):.6f}。",
                f"满足共同方向的年度为{diagnostic.summary['same_direction_years']}/5。",
                "PASS才允许启动四个固定分档仓位候选；FAIL则直接终止，不以回测结果挽救诊断。",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_id,
            "date": "2026-09-01",
            "status": status,
            "symbol": "588080.SH",
            "asset_type": "etf",
            "challenger_experiment": "0901_EX13",
            "protocol_sha256": sha256((artifacts / "protocol.json").read_bytes()).hexdigest(),
            "visible_sample_end": "2025-12-31",
            "validation_accessed": False,
            "historical_2026_accessed": False,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {**summary, "experiment_dir": str(experiment_dir)}
