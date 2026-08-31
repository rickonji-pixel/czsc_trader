"""Run the preregistered direction-by-BI-power-layer diagnostic."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from .baselines import resolve_baseline
from .czsc_bi_layer import JointFactors, build_joint_factors
from .czsc_factor_stability import (
    audit_causal_prefix,
    build_forward_outcomes,
    evaluate_conditional_factor_stability,
    factor_redundancy_audit,
    select_discovery_candidates,
    validate_frozen_candidates,
)
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factors import _run_signals, generate_factor_frame
from .four_layer import normalized_signal_factors


DISCOVERY_YEARS = (2021, 2022, 2023)
VALIDATION_YEARS = (2024, 2025)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def validate_bi_layer_protocol(protocol: Mapping[str, object]) -> None:
    expected = {
        "experiment_id": "0901_EX02",
        "handler": "czsc_bi_layer_stability_diagnostic",
        "symbol": "588080.SH",
        "signal": "cxt_bi_zdf_V230601",
        "signal_parameters": {"di": 1, "n": 5},
        "directions": ["向上", "向下"],
        "layers": ["第1层", "第2层", "第3层", "第4层", "第5层"],
        "discovery_end": "2023-12-31",
        "validation_end": "2025-12-31",
        "horizons": [5, 20, 60],
        "primary_horizon": 20,
        "minimum_absolute_effect": 0.005,
        "control_policy": "same_direction_other_layers",
        "access_2026": False,
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"protocol {key} differs from preregistration")


def _generate(data: object, protocol: Mapping[str, object]) -> JointFactors:
    parameters = dict(protocol["signal_parameters"])
    config = {"name": str(protocol["signal"]), "freq": "日线", **parameters}
    raw = _run_signals(data.daily, "日线", [config], 30, "daily")
    raw.index = raw.index.normalize()
    return build_joint_factors(
        raw.iloc[:, 0],
        tuple(map(str, protocol["directions"])),
        tuple(map(str, protocol["layers"])),
    )


def _direction_from_name(name: str) -> str:
    parts = str(name).split("::")
    if len(parts) != 3:
        raise ValueError(f"invalid joint factor name: {name}")
    return parts[1]


def _evaluate(
    joint: JointFactors,
    daily: pd.DataFrame,
    factor_names: list[str],
    years: tuple[int, ...],
    horizons: tuple[int, ...],
) -> pd.DataFrame:
    outcomes = build_forward_outcomes(daily, pd.DatetimeIndex(joint.factors.index), horizons)
    pieces = [
        evaluate_conditional_factor_stability(
            joint.factors[name],
            joint.directions[_direction_from_name(name)],
            outcomes,
            years=years,
            horizons=horizons,
        )
        for name in factor_names
    ]
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()


def _discovery_supported(metrics: pd.DataFrame, protocol: Mapping[str, object]) -> set[str]:
    primary = metrics[metrics["horizon"].eq(int(protocol["primary_horizon"]))]
    supported: set[str] = set()
    for factor, group in primary.groupby("factor"):
        active = group.set_index("year").reindex(DISCOVERY_YEARS)["active_n"]
        if (
            active.notna().all()
            and active.ge(int(protocol["discovery_min_active_days_per_year"])).all()
            and active.sum() >= int(protocol["discovery_min_total_active_days"])
        ):
            supported.add(str(factor))
    return supported


def _validation_support(metrics: pd.DataFrame, protocol: Mapping[str, object]) -> dict[str, bool]:
    primary = metrics[metrics["horizon"].eq(int(protocol["primary_horizon"]))]
    result: dict[str, bool] = {}
    for factor, group in primary.groupby("factor"):
        active = group.set_index("year").reindex(VALIDATION_YEARS)["active_n"]
        result[str(factor)] = bool(
            active.notna().all()
            and active.ge(int(protocol["validation_min_active_days_per_year"])).all()
        )
    return result


def _write_docs(
    experiment_dir: Path,
    *,
    status: str,
    execution_commit: str,
    supported_count: int,
    selected_count: int,
    stable_count: int,
    causal_status: str,
) -> None:
    (experiment_dir / "03_execution.md").write_text(
        "\n".join(
            [
                "# 0901_EX02 执行过程",
                "",
                f"- 执行提交：`{execution_commit}`",
                "- 候选空间：方向×力度层级，共10项。",
                f"- 发现段支持度合格：{supported_count}项。",
                f"- 发现段冻结候选：{selected_count}项。",
                f"- 锁定验证稳定因子：{stable_count}项。",
                f"- 因果前缀重放：`{causal_status}`。",
                "- 未访问2026，未执行策略或仓位优化。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(
            [
                "# 0901_EX02 结论",
                "",
                f"状态：`{status}`。",
                "",
                f"10个固定组合中，{selected_count}项通过发现门槛，{stable_count}项通过锁定验证。",
                "本轮只判断力度层级相对同方向其他层级的增量信息，不形成交易策略。",
                "若结果为FAIL，不得事后合并层级、降低样本门槛或改用全市场控制组。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def run_bi_layer_stability_experiment(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    validate_bi_layer_protocol(protocol)
    baseline = resolve_baseline(Path(baseline_root), str(protocol["champion"]["version"]), symbol="588080.SH")
    if baseline.sha256 != str(protocol["champion"]["sha256"]):
        raise ValueError("champion hash differs from preregistration")
    horizons = tuple(map(int, protocol["horizons"]))

    discovery_data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["discovery_end"])))
    discovery = _generate(discovery_data, protocol)
    names = list(map(str, discovery.factors.columns))
    discovery_metrics = _evaluate(discovery, discovery_data.daily, names, DISCOVERY_YEARS, horizons)
    supported = _discovery_supported(discovery_metrics, protocol)
    selected = select_discovery_candidates(
        discovery_metrics[discovery_metrics["factor"].isin(supported)],
        years=DISCOVERY_YEARS,
        primary_horizon=int(protocol["primary_horizon"]),
        minimum_absolute_effect=float(protocol["minimum_absolute_effect"]),
    )
    discovery_metrics.to_csv(artifacts / "discovery_metrics.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(artifacts / "candidate_selection.csv", index=False, encoding="utf-8-sig")
    _write_json(
        artifacts / "factor_universe.json",
        {
            "signal": protocol["signal"],
            "candidate_count": len(names),
            "factor_names": names,
            "supported_factor_names": sorted(supported),
            "control_policy": protocol["control_policy"],
        },
    )
    champion_frame = generate_factor_frame(discovery_data).frame
    references = normalized_signal_factors(champion_frame[list(baseline.factor_names)])
    redundancy = factor_redundancy_audit(discovery.factors[selected["factor"].tolist()], references) if not selected.empty else []
    _write_json(artifacts / "redundancy_audit.json", redundancy)

    validation_accessed = not selected.empty
    validation_metrics = pd.DataFrame()
    validated = pd.DataFrame()
    causal = {"status": "NOT_RUN", "rows_checked": 0, "factors_checked": 0, "mismatch_count": 0, "mismatches_by_factor": {}}
    if validation_accessed:
        validation_data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["validation_end"])))
        validation = _generate(validation_data, protocol)
        selected_names = selected["factor"].tolist()
        causal = audit_causal_prefix(discovery.factors, validation.factors, selected_names)
        validation_metrics = _evaluate(validation, validation_data.daily, selected_names, VALIDATION_YEARS, horizons)
        support = _validation_support(validation_metrics, protocol)
        validated = validate_frozen_candidates(
            validation_metrics,
            selected,
            years=VALIDATION_YEARS,
            primary_horizon=int(protocol["primary_horizon"]),
            minimum_absolute_effect=float(protocol["minimum_absolute_effect"]),
        )
        validated["validation_support_pass"] = validated["factor"].map(support).fillna(False)
        validated["pass"] = validated["pass"] & validated["validation_support_pass"]
        validation_metrics.to_csv(artifacts / "validation_metrics.csv", index=False, encoding="utf-8-sig")
        validated.to_csv(artifacts / "validation_selection.csv", index=False, encoding="utf-8-sig")
    _write_json(artifacts / "causal_replay_audit.json", causal)
    stable = validated[validated["pass"]].copy() if not validated.empty else validated
    stable_payload = [] if stable.empty else stable.to_dict("records")
    _write_json(artifacts / "stable_factors.json", stable_payload)
    status = "ERROR" if causal["status"] == "FAIL" else "PASS" if stable_payload else "FAIL"
    summary = {
        "status": status,
        "candidate_count": len(names),
        "supported_factor_count": len(supported),
        "discovery_candidate_count": len(selected),
        "stable_factor_count": len(stable_payload),
        "validation_accessed": validation_accessed,
        "access_2026": False,
    }
    _write_json(artifacts / "metrics.json", summary)
    _write_json(
        artifacts / "identity_audit.json",
        {
            "status": "PASS",
            "champion_version": baseline.version,
            "champion_sha256": baseline.sha256,
            "discovery_data_hashes": discovery_data.hashes,
            "causal_replay_status": causal["status"],
            "access_2026": False,
        },
    )
    _write_docs(
        experiment_dir,
        status=status,
        execution_commit=execution_commit,
        supported_count=len(supported),
        selected_count=len(selected),
        stable_count=len(stable_payload),
        causal_status=str(causal["status"]),
    )
    protocol_sha = sha256((artifacts / "protocol.json").read_bytes()).hexdigest()
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": experiment_dir.name,
            "date": "2026-09-01",
            "status": status,
            "symbol": "588080.SH",
            "asset_type": "etf",
            "champion": protocol["champion"],
            "protocol_sha256": protocol_sha,
            "visible_sample_end": "2025-12-31" if validation_accessed else "2023-12-31",
            "validation_accessed": validation_accessed,
            "historical_2026_accessed": False,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {**summary, "experiment_dir": str(experiment_dir)}
