"""Diagnose cxt_bi_zdf validity conditional on the champion BI direction."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from .baselines import resolve_baseline
from .czsc_bi_layer import parse_signal_values
from .czsc_factor_stability import (
    audit_causal_prefix,
    build_forward_outcomes,
    classify_yearly_effects,
    evaluate_conditional_factor_stability,
    factor_redundancy_audit,
)
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factors import _run_signals, generate_factor_frame
from .four_layer import normalized_signal_factors


YEARS = (2021, 2022, 2023, 2024, 2025)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def validate_incremental_validity_protocol(protocol: Mapping[str, object]) -> None:
    expected = {
        "experiment_id": "0901_EX04",
        "handler": "czsc_incremental_validity_diagnosis",
        "symbol": "588080.SH",
        "signal": "cxt_bi_zdf_V230601",
        "signal_parameters": {"di": 1, "n": 5},
        "reference_factor": "raw__daily__cxt_bi_status_V230101",
        "directions": [
            {"name": "向上", "reference_value": 1.0},
            {"name": "向下", "reference_value": -1.0},
        ],
        "visible_end": "2025-12-31",
        "horizons": [5, 20, 60],
        "primary_horizon": 20,
        "primary_endpoint": "max_drawdown_delta",
        "minimum_absolute_effect": 0.005,
        "output_status": "COMPLETE",
        "access_2026": False,
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"protocol {key} differs from preregistration")


def _features(data: object, protocol: Mapping[str, object]) -> tuple[pd.DataFrame, pd.DataFrame]:
    config = {"name": str(protocol["signal"]), "freq": "日线", **dict(protocol["signal_parameters"])}
    raw = _run_signals(data.daily, "日线", [config], 30, "daily")
    raw.index = raw.index.normalize()
    primary = raw.iloc[:, 0].map(lambda value: parse_signal_values(value)[0])
    champion = generate_factor_frame(data).frame.reindex(raw.index)
    reference_name = str(protocol["reference_factor"])
    reference = normalized_signal_factors(champion[[reference_name]])[reference_name]
    factors: dict[str, pd.Series] = {}
    conditions: dict[str, pd.Series] = {}
    for spec in protocol["directions"]:
        direction = str(spec["name"])
        name = f"valid__cxt_bi_zdf::{direction}"
        factors[name] = primary.eq(direction).astype(float)
        conditions[name] = reference.eq(float(spec["reference_value"]))
    return pd.DataFrame(factors, index=raw.index), pd.DataFrame(conditions, index=raw.index)


def run_incremental_validity_diagnosis(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    validate_incremental_validity_protocol(protocol)
    baseline = resolve_baseline(Path(baseline_root), str(protocol["champion"]["version"]), symbol="588080.SH")
    if baseline.sha256 != str(protocol["champion"]["sha256"]):
        raise ValueError("champion hash differs from preregistration")

    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["visible_end"])))
    factors, conditions = _features(data, protocol)
    horizons = tuple(map(int, protocol["horizons"]))
    outcomes = build_forward_outcomes(data.daily, pd.DatetimeIndex(factors.index), horizons)
    metrics = pd.concat(
        [
            evaluate_conditional_factor_stability(
                factors[name],
                conditions[name],
                outcomes,
                years=YEARS,
                horizons=horizons,
            )
            for name in factors.columns
        ],
        ignore_index=True,
    )
    metrics.to_csv(artifacts / "conditional_metrics.csv", index=False, encoding="utf-8-sig")
    primary = metrics[metrics["horizon"].eq(int(protocol["primary_horizon"]))]
    classifications = []
    for factor, group in primary.groupby("factor", sort=True):
        classification = classify_yearly_effects(
            group.sort_values("year")[str(protocol["primary_endpoint"])],
            minimum_abs_median=float(protocol["minimum_absolute_effect"]),
        )
        classifications.append(
            {
                "factor": str(factor),
                "classification": classification,
                "yearly_effects": {
                    str(int(row.year)): float(getattr(row, str(protocol["primary_endpoint"])))
                    for row in group.sort_values("year").itertuples(index=False)
                },
                "median_absolute_effect": float(group[str(protocol["primary_endpoint"])].abs().median()),
            }
        )
    _write_json(artifacts / "classification.json", classifications)

    prefix_data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp("2023-12-31"))
    prefix_factors, prefix_conditions = _features(prefix_data, protocol)
    prefix = pd.concat(
        [
            prefix_factors,
            prefix_conditions.rename(columns=lambda name: f"condition::{name}"),
        ],
        axis=1,
    )
    full = pd.concat(
        [factors, conditions.rename(columns=lambda name: f"condition::{name}")],
        axis=1,
    )
    causal = audit_causal_prefix(prefix, full, tuple(prefix.columns))
    _write_json(artifacts / "causal_replay_audit.json", causal)
    reference_frame = generate_factor_frame(data).frame.reindex(factors.index)
    reference = normalized_signal_factors(reference_frame[[str(protocol["reference_factor"])]])
    _write_json(artifacts / "redundancy_audit.json", factor_redundancy_audit(factors, reference))
    summary = {
        "status": "COMPLETE",
        "classifications": {row["factor"]: row["classification"] for row in classifications},
        "stable_incremental_factor_count": sum(row["classification"] == "stable_incremental_validity" for row in classifications),
        "causal_replay_status": causal["status"],
        "visible_sample_end": "2025-12-31",
        "access_2026": False,
    }
    _write_json(artifacts / "metrics.json", summary)
    _write_json(
        artifacts / "identity_audit.json",
        {
            "status": "PASS",
            "champion_version": baseline.version,
            "champion_sha256": baseline.sha256,
            "visible_data_hashes": data.hashes,
            "all_history_is_diagnostic": True,
            "access_2026": False,
        },
    )
    lines = []
    for row in classifications:
        effects = ", ".join(f"{year}:{value:+.4f}" for year, value in row["yearly_effects"].items())
        lines.append(f"- `{row['factor']}`：`{row['classification']}`；{effects}")
    (experiment_dir / "03_execution.md").write_text(
        "\n".join(
            [
                "# 0901_EX04 执行过程",
                "",
                f"- 执行提交：`{execution_commit}`",
                "- 2021—2025全部作为已见诊断样本。",
                f"- 因果前缀重放：`{causal['status']}`。",
                "- 未访问2026，未产生策略候选。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(
            [
                "# 0901_EX04 结论",
                "",
                "状态：`COMPLETE`。",
                "",
                *lines,
                "",
                "本轮只检验相对冠军笔方向的增量，不是独立验证，也不形成交易规则。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    protocol_sha = sha256((artifacts / "protocol.json").read_bytes()).hexdigest()
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": "2026-09-01",
            "status": "COMPLETE",
            "symbol": "588080.SH",
            "asset_type": "etf",
            "champion": protocol["champion"],
            "protocol_sha256": protocol_sha,
            "visible_sample_end": "2025-12-31",
            "validation_accessed": False,
            "historical_2026_accessed": False,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {**summary, "experiment_dir": str(experiment_dir)}
