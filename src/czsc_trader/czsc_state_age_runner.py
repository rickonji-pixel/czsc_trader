"""Run the visible-history CZSC state-age diagnosis."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from .baselines import resolve_baseline
from .czsc_bi_layer import build_joint_factors
from .czsc_factor_stability import audit_causal_prefix, build_forward_outcomes
from .czsc_state_age import (
    classify_age_relationship,
    consecutive_state_age,
    fixed_age_bin_summary,
    yearly_age_correlations,
)
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factors import _run_signals


YEARS = (2021, 2022, 2023, 2024, 2025)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def validate_state_age_protocol(protocol: Mapping[str, object]) -> None:
    expected = {
        "experiment_id": "0901_EX03",
        "handler": "czsc_state_age_diagnosis",
        "symbol": "588080.SH",
        "signal": "cxt_bi_zdf_V230601",
        "signal_parameters": {"di": 1, "n": 5},
        "state_value": "向上",
        "visible_end": "2025-12-31",
        "horizons": [5, 20, 60],
        "primary_horizon": 20,
        "stable_min_abs_median_rho": 0.10,
        "output_status": "COMPLETE",
        "access_2026": False,
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"protocol {key} differs from preregistration")


def _state_and_age(data: object, protocol: Mapping[str, object]) -> pd.DataFrame:
    config = {"name": str(protocol["signal"]), "freq": "日线", **dict(protocol["signal_parameters"])}
    raw = _run_signals(data.daily, "日线", [config], 30, "daily")
    raw.index = raw.index.normalize()
    joint = build_joint_factors(raw.iloc[:, 0], (str(protocol["state_value"]),), tuple(map(str, ["第1层", "第2层", "第3层", "第4层", "第5层"])))
    state = joint.directions[str(protocol["state_value"])].rename("state")
    age = consecutive_state_age(state)
    return pd.concat([state.astype(float), age.astype(float)], axis=1)


def run_state_age_diagnosis(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    validate_state_age_protocol(protocol)
    baseline = resolve_baseline(Path(baseline_root), str(protocol["champion"]["version"]), symbol="588080.SH")
    if baseline.sha256 != str(protocol["champion"]["sha256"]):
        raise ValueError("champion hash differs from preregistration")

    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["visible_end"])))
    features = _state_and_age(data, protocol)
    horizons = tuple(map(int, protocol["horizons"]))
    outcomes = build_forward_outcomes(data.daily, pd.DatetimeIndex(features.index), horizons)
    yearly = yearly_age_correlations(features["state_age"], outcomes, years=YEARS, horizons=horizons)
    binned = fixed_age_bin_summary(features["state_age"], outcomes, protocol["age_bins"], years=YEARS, horizons=horizons)
    classification = classify_age_relationship(
        yearly,
        primary_horizon=int(protocol["primary_horizon"]),
        minimum_abs_median_rho=float(protocol["stable_min_abs_median_rho"]),
    )
    yearly.to_csv(artifacts / "yearly_correlations.csv", index=False, encoding="utf-8-sig")
    binned.to_csv(artifacts / "age_bin_metrics.csv", index=False, encoding="utf-8-sig")

    prefix_data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp("2023-12-31"))
    prefix = _state_and_age(prefix_data, protocol)
    causal = audit_causal_prefix(prefix, features, ("state", "state_age"))
    _write_json(artifacts / "causal_replay_audit.json", causal)
    metrics = {
        "status": "COMPLETE",
        "classification": classification,
        "active_day_count": int(features["state"].sum()),
        "maximum_state_age": int(features["state_age"].max()),
        "causal_replay_status": causal["status"],
        "visible_sample_end": "2025-12-31",
        "access_2026": False,
    }
    _write_json(artifacts / "metrics.json", metrics)
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
    (experiment_dir / "03_execution.md").write_text(
        "\n".join(
            [
                "# 0901_EX03 执行过程",
                "",
                f"- 执行提交：`{execution_commit}`",
                "- 2021—2025全部作为已见诊断样本。",
                f"- 向上状态有效日：{metrics['active_day_count']}；最长连续年龄：{metrics['maximum_state_age']}日。",
                f"- 因果前缀重放：`{causal['status']}`。",
                "- 未访问2026，未产生策略候选。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    primary = yearly[yearly["horizon"].eq(int(protocol["primary_horizon"]))]
    rho_lines = [f"- {int(row.year)}：{float(row.max_drawdown_rho):+.4f}" for row in primary.itertuples(index=False)]
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(
            [
                "# 0901_EX03 结论",
                "",
                "状态：`COMPLETE`。",
                "",
                f"诊断分类：`{classification}`。",
                "",
                "未来20日最大回撤与状态年龄的逐年Spearman相关：",
                *rho_lines,
                "",
                "本轮是已见历史归因，不是独立验证，也不产生交易规则。",
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
    return {**metrics, "experiment_dir": str(experiment_dir)}
