"""Execute the preregistered CZSC factor-stability diagnostic."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from .baselines import resolve_baseline
from .czsc_factor_stability import (
    build_forward_outcomes,
    evaluate_factor_stability,
    select_discovery_candidates,
    validate_frozen_candidates,
)
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factor_discovery import generate_candidate_factors


DISCOVERY_YEARS = (2021, 2022, 2023)
VALIDATION_YEARS = (2024, 2025)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def validate_czsc_factor_stability_protocol(protocol: Mapping[str, object]) -> None:
    expected = {
        "experiment_id": "0901_EX01",
        "handler": "czsc_factor_stability_diagnostic",
        "symbol": "588080.SH",
        "discovery_start": "2021-01-01",
        "discovery_end": "2023-12-31",
        "validation_start": "2024-01-01",
        "validation_end": "2025-12-31",
        "horizons": [5, 20, 60],
        "primary_horizon": 20,
        "minimum_absolute_effect": 0.005,
        "candidate_scope": "new_daily_signals_only",
        "access_2026": False,
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"protocol {key} differs from preregistration")
    signals = protocol.get("new_daily_signals")
    if not isinstance(signals, list) or len(signals) != 21 or len(set(map(str, signals))) != 21:
        raise ValueError("protocol new_daily_signals must contain 21 unique names")
    if protocol.get("selection_endpoints") != ["return_delta", "max_drawdown_delta"]:
        raise ValueError("protocol selection_endpoints differs from preregistration")


def _new_signal_records(metadata: Mapping[str, Any], signals: set[str]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for raw_record in metadata.get("states", []):
        record = dict(raw_record)
        raw_name = str(record.get("raw_signal", ""))
        if record.get("status") != "candidate":
            continue
        if not any(f"__{signal}" in raw_name for signal in signals):
            continue
        records.append(record)
    return records


def _evaluate_records(
    factors: pd.DataFrame,
    records: list[dict[str, object]],
    daily: pd.DataFrame,
    years: tuple[int, ...],
    horizons: tuple[int, ...],
) -> pd.DataFrame:
    outcomes = build_forward_outcomes(daily, pd.DatetimeIndex(factors.index), horizons)
    pieces = [
        evaluate_factor_stability(
            factors[str(record["factor"])],
            outcomes,
            factor_kind=str(record["factor_kind"]),
            years=years,
            horizons=horizons,
        )
        for record in records
    ]
    if not pieces:
        return pd.DataFrame()
    return pd.concat(pieces, ignore_index=True)


def _supported_factors(
    metrics: pd.DataFrame,
    records: list[dict[str, object]],
    protocol: Mapping[str, object],
) -> set[str]:
    primary = metrics[metrics["horizon"].eq(int(protocol["primary_horizon"]))]
    supported: set[str] = set()
    kinds = {str(record["factor"]): str(record["factor_kind"]) for record in records}
    for factor, group in primary.groupby("factor"):
        indexed = group.set_index("year")
        if not set(DISCOVERY_YEARS) <= set(map(int, indexed.index)):
            continue
        active = indexed.loc[list(DISCOVERY_YEARS), "active_n"].astype(int)
        kind = kinds[str(factor)]
        if kind == "event":
            if active.sum() >= int(protocol["event_min_independent_occurrences"]) and active.gt(0).sum() >= int(protocol["event_min_years"]):
                supported.add(str(factor))
        elif active.ge(int(protocol["state_min_active_days_per_year"])).all():
            supported.add(str(factor))
    return supported


def _docs(
    experiment_dir: Path,
    *,
    status: str,
    execution_commit: str,
    record_count: int,
    supported_count: int,
    selected_count: int,
    stable_count: int,
    validation_accessed: bool,
) -> None:
    execution = [
        "# 0901_EX01 执行过程",
        "",
        f"- 执行提交：`{execution_commit}`",
        "- 发现段：2021—2023；主标签：未来20个交易日。",
        f"- 新信号拆分后的规范因子：{record_count}项。",
        f"- 满足状态/事件支持度：{supported_count}项。",
        f"- 发现段冻结候选：{selected_count}项。",
        f"- 是否访问2024—2025验证段：{str(validation_accessed).lower()}。",
        "- 未访问2026，未搜索权重、阈值或仓位。",
    ]
    conclusion = [
        "# 0901_EX01 结论",
        "",
        f"状态：`{status}`。",
        "",
        f"发现段冻结{selected_count}个候选，最终有{stable_count}个因子通过锁定验证。",
        "通过只代表因子具有下一轮统一策略研究价值，不代表已经形成可交易策略。",
        "若没有稳定因子，不应事后降低支持度、效应门槛或改变主周期。",
    ]
    (experiment_dir / "03_execution.md").write_text("\n".join(execution) + "\n", encoding="utf-8")
    (experiment_dir / "04_conclusion.md").write_text("\n".join(conclusion) + "\n", encoding="utf-8")


def run_czsc_factor_stability_experiment(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    validate_czsc_factor_stability_protocol(protocol)
    baseline = resolve_baseline(Path(baseline_root), str(protocol["champion"]["version"]), symbol="588080.SH")
    if baseline.sha256 != str(protocol["champion"]["sha256"]):
        raise ValueError("champion hash differs from preregistration")
    base_weights = pd.Series(baseline.factor_weights, index=baseline.factor_names, dtype=float)
    horizons = tuple(map(int, protocol["horizons"]))

    discovery_data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["discovery_end"])))
    discovery = generate_candidate_factors(discovery_data, protocol, base_weights)
    records = _new_signal_records(discovery.metadata, set(map(str, protocol["new_daily_signals"])))
    discovery_metrics = _evaluate_records(discovery.factors, records, discovery_data.daily, DISCOVERY_YEARS, horizons)
    supported = _supported_factors(discovery_metrics, records, protocol) if not discovery_metrics.empty else set()
    eligible_metrics = discovery_metrics[discovery_metrics["factor"].isin(supported)] if supported else discovery_metrics.iloc[0:0]
    selected = select_discovery_candidates(
        eligible_metrics,
        years=DISCOVERY_YEARS,
        primary_horizon=int(protocol["primary_horizon"]),
        minimum_absolute_effect=float(protocol["minimum_absolute_effect"]),
    )

    discovery_metrics.to_csv(artifacts / "discovery_metrics.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(artifacts / "candidate_selection.csv", index=False, encoding="utf-8-sig")
    _write_json(artifacts / "factor_universe.json", discovery.metadata)

    validation_accessed = not selected.empty
    validation_metrics = pd.DataFrame()
    validated = pd.DataFrame()
    if validation_accessed:
        validation_data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["validation_end"])))
        validation = generate_candidate_factors(
            validation_data,
            protocol,
            base_weights,
            frozen_names=selected["factor"].tolist(),
        )
        validation_records = selected[["factor", "factor_kind"]].to_dict("records")
        validation_metrics = _evaluate_records(validation.factors, validation_records, validation_data.daily, VALIDATION_YEARS, horizons)
        validated = validate_frozen_candidates(
            validation_metrics,
            selected,
            years=VALIDATION_YEARS,
            primary_horizon=int(protocol["primary_horizon"]),
            minimum_absolute_effect=float(protocol["minimum_absolute_effect"]),
        )
        validation_metrics.to_csv(artifacts / "validation_metrics.csv", index=False, encoding="utf-8-sig")
        validated.to_csv(artifacts / "validation_selection.csv", index=False, encoding="utf-8-sig")

    stable = validated[validated["pass"]].copy() if not validated.empty else validated
    stable_payload = [] if stable.empty else stable.to_dict("records")
    _write_json(artifacts / "stable_factors.json", stable_payload)
    status = "PASS" if stable_payload else "FAIL"
    identity = {
        "status": "PASS",
        "champion_version": baseline.version,
        "champion_sha256": baseline.sha256,
        "factor_count": len(records),
        "supported_factor_count": len(supported),
        "discovery_data_hashes": discovery_data.hashes,
        "discovery_end": str(protocol["discovery_end"]),
        "validation_end": str(protocol["validation_end"]) if validation_accessed else None,
        "validation_accessed": validation_accessed,
        "access_2026": False,
    }
    _write_json(artifacts / "identity_audit.json", identity)
    summary = {
        "status": status,
        "raw_signal_count": len(protocol["new_daily_signals"]),
        "canonical_factor_count": len(records),
        "supported_factor_count": len(supported),
        "discovery_candidate_count": len(selected),
        "stable_factor_count": len(stable_payload),
        "validation_accessed": validation_accessed,
        "access_2026": False,
    }
    _write_json(artifacts / "metrics.json", summary)
    _docs(
        experiment_dir,
        status=status,
        execution_commit=execution_commit,
        record_count=len(records),
        supported_count=len(supported),
        selected_count=len(selected),
        stable_count=len(stable_payload),
        validation_accessed=validation_accessed,
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
