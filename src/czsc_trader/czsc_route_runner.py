"""Run one preregistered family in the terminal CZSC research route."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from .baseline_execution import apply_resolved_baseline
from .baselines import resolve_baseline
from .czsc_factor_stability import audit_causal_prefix, build_forward_outcomes, event_onsets
from .czsc_route import (
    admit_factors,
    build_cross_frequency_factors,
    build_dynamic_factors,
    build_family_factors,
    replay_family_factors,
    validate_route_family_protocol,
)
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factors import _backward_align, _run_signals, generate_factor_frame
from .four_layer import normalized_signal_factors


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _raw_signals(data: object, protocol: Mapping[str, object]) -> pd.DataFrame:
    target = pd.DatetimeIndex(data.daily["dt"], name="dt")
    pieces: list[pd.DataFrame] = []
    sources = {
        "30分钟": (data.intraday, 500, "30m"),
        "日线": (data.daily, 30, "daily"),
        "周线": (data.weekly, 10, "weekly"),
    }
    for frequency in map(str, protocol["frequencies"]):
        frame, init_n, tag = sources[frequency]
        configs = [
            {"name": str(name), "freq": frequency, "di": 1}
            for name in protocol["signal_names"]
        ]
        generated = _run_signals(frame, frequency, configs, init_n, tag)
        if frequency == "30分钟":
            generated = generated.loc[generated.index.strftime("%H:%M") == "15:00"]
            generated.index = generated.index.normalize()
            generated = generated.reindex(target)
        elif frequency == "日线":
            generated.index = generated.index.normalize()
            generated = generated.reindex(target)
        else:
            generated.index = generated.index.normalize()
            generated = _backward_align(generated, target)
        pieces.append(generated)
    raw = pd.concat(pieces, axis=1)
    raw.index = target
    if raw.columns.duplicated().any():
        raise ValueError("route raw signal identities are not unique")
    return raw


def _champion_references(data: object, baseline: object) -> pd.DataFrame:
    champion_frame = generate_factor_frame(data).frame
    names = list(map(str, baseline.factor_names))
    factors = normalized_signal_factors(champion_frame[names]).astype(float)
    applied = apply_resolved_baseline(champion_frame, baseline)
    references = factors.copy()
    references.insert(0, "target_position", applied.target_position.reindex(factors.index).astype(float))
    return references


def _event_ledger(factors: pd.DataFrame, kinds: Mapping[str, str]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for name in map(str, factors.columns):
        if kinds[name] != "event":
            continue
        onset = event_onsets(factors[name])
        rows.extend(
            {"factor": name, "event_date": pd.Timestamp(dt).date().isoformat()}
            for dt in onset.index[onset]
        )
    return pd.DataFrame(rows, columns=["factor", "event_date"])


def _parent_factor_frame(raw: pd.DataFrame, experiment_dir: Path, protocol: Mapping[str, object]) -> tuple[pd.DataFrame, dict[str, str]]:
    pieces: list[pd.DataFrame] = []
    kinds: dict[str, str] = {}
    parent_hashes = protocol.get("parent_manifests")
    if not isinstance(parent_hashes, dict):
        raise ValueError("dynamic family parent manifest hashes are missing")
    for parent_name in map(str, protocol.get("parent_experiments", [])):
        parent_dir = experiment_dir.parent / parent_name
        manifest_path = parent_dir / "experiment_manifest.json"
        if sha256(manifest_path.read_bytes()).hexdigest() != str(parent_hashes.get(parent_name, "")):
            raise ValueError(f"dynamic parent manifest hash differs: {parent_name}")
        parent_artifacts = parent_dir / "artifacts"
        records = json.loads((parent_artifacts / "factor_universe.json").read_text(encoding="utf-8"))
        canonical = [row for row in records if row["status"] == "candidate"]
        frame = replay_family_factors(raw, canonical)
        pieces.append(frame)
        kinds.update({str(row["factor"]): str(row["factor_kind"]) for row in canonical})
    if not pieces:
        raise ValueError("dynamic family requires frozen parent experiments")
    base = pd.concat(pieces, axis=1)
    if base.columns.duplicated().any():
        raise ValueError("dynamic parent factor identities are not unique")
    return base, kinds


def _build_route_family(
    raw: pd.DataFrame,
    family: str,
    protocol: Mapping[str, object],
    experiment_dir: Path,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    if family in {"atomic_structure", "sparse_event"}:
        return build_family_factors(raw, family, dict(protocol))
    if family == "state_dynamic":
        base, kinds = _parent_factor_frame(raw, experiment_dir, protocol)
        return build_dynamic_factors(base, kinds)
    if family == "cross_frequency":
        return build_cross_frequency_factors(raw)
    raise ValueError(f"route family runner cannot execute family {family}")


def _write_docs(
    experiment_dir: Path,
    *,
    status: str,
    family: str,
    execution_commit: str,
    raw_count: int,
    candidate_count: int,
    admitted_count: int,
    rejection_counts: Mapping[str, int],
    causal_status: str,
) -> None:
    rejected = ", ".join(f"{key}={value}" for key, value in sorted(rejection_counts.items())) or "无"
    (experiment_dir / "03_execution.md").write_text(
        "\n".join(
            [
                f"# {experiment_dir.name} 执行过程",
                "",
                f"- 执行提交：`{execution_commit}`",
                f"- 信息家族：`{family}`。",
                f"- 原始信号配置：{raw_count}项。",
                f"- 去重后规范候选：{candidate_count}项。",
                f"- 通过统一准入：{admitted_count}项。",
                f"- 因果前缀重放：`{causal_status}`。",
                f"- 淘汰原因汇总：{rejected}。",
                "- 评价历史为2021—2025；未读取2026，未搜索策略参数。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(
            [
                f"# {experiment_dir.name} 结论",
                "",
                f"状态：`{status}`。",
                "",
                f"{candidate_count}个规范候选中有{admitted_count}个通过终局计划的全部准入门槛。",
                "PASS仅代表因子获得统一策略集成资格；FAIL表示本信息家族没有可晋升因子。",
                "完整候选、逐年条件效应、影响点、冗余和因果证据见artifacts。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def run_czsc_route_family(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    validate_route_family_protocol(protocol)
    family = str(protocol["family"])
    if family not in {"atomic_structure", "sparse_event", "state_dynamic", "cross_frequency"}:
        raise ValueError(f"route family runner cannot execute family {family}")
    baseline = resolve_baseline(
        Path(baseline_root), str(protocol["champion"]["version"]), symbol="588080.SH"
    )
    if baseline.sha256 != str(protocol["champion"]["sha256"]):
        raise ValueError("route family champion hash differs from preregistration")

    data = load_market_data(
        raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["visible_end"]))
    )
    raw = _raw_signals(data, protocol)
    factors, records = _build_route_family(raw, family, protocol, experiment_dir)
    canonical = [row for row in records if row["status"] == "candidate"]
    if list(factors.columns) != [str(row["factor"]) for row in canonical]:
        raise AssertionError("canonical factor metadata differs from factor frame")
    kinds = {str(row["factor"]): str(row["factor_kind"]) for row in canonical}
    references = _champion_references(data, baseline).reindex(factors.index)
    outcomes = build_forward_outcomes(
        data.daily, pd.DatetimeIndex(factors.index), tuple(map(int, protocol["horizons"]))
    )

    prefix_data = load_market_data(
        raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp("2023-12-31")
    )
    prefix_raw = _raw_signals(prefix_data, protocol)
    if family in {"atomic_structure", "sparse_event"}:
        prefix_factors = replay_family_factors(prefix_raw, canonical)
        causal = audit_causal_prefix(prefix_factors, factors, tuple(map(str, factors.columns)))
        causal_passed = {
            name
            for name, mismatches in causal["mismatches_by_factor"].items()
            if int(mismatches) == 0
        }
    else:
        causal = audit_causal_prefix(prefix_raw, raw, tuple(map(str, raw.columns)))
        causal_passed = set(map(str, factors.columns)) if causal["status"] == "PASS" else set()
    admission = admit_factors(
        factors,
        references,
        outcomes,
        protocol,
        factor_kinds=kinds,
        causal_passed=causal_passed,
    )
    status = "PASS" if not admission.admitted.empty else "FAIL"
    rejection_counts = Counter(map(str, admission.rejected["reason"]))

    _write_json(artifacts / "factor_universe.json", records)
    admission.admitted.to_csv(artifacts / "admitted_factors.csv", index=False, encoding="utf-8-sig")
    admission.rejected.to_csv(artifacts / "rejected_factors.csv", index=False, encoding="utf-8-sig")
    admission.yearly_metrics.to_csv(artifacts / "yearly_metrics.csv", index=False, encoding="utf-8-sig")
    admission.conditional_audit.to_csv(artifacts / "conditional_audit.csv", index=False, encoding="utf-8-sig")
    admission.influence_audit.to_csv(artifacts / "influence_audit.csv", index=False, encoding="utf-8-sig")
    admission.redundancy_audit.to_csv(artifacts / "redundancy_audit.csv", index=False, encoding="utf-8-sig")
    _write_json(artifacts / "causal_replay_audit.json", causal)
    if family == "sparse_event":
        _event_ledger(factors, kinds).to_csv(artifacts / "event_ledger.csv", index=False, encoding="utf-8-sig")
    summary = {
        "status": status,
        "family": family,
        "raw_signal_config_count": len(protocol["signal_names"]) * len(protocol["frequencies"]),
        "canonical_factor_count": len(factors.columns),
        "admitted_factor_count": len(admission.admitted),
        "rejection_counts": dict(sorted(rejection_counts.items())),
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
            "visible_sample_end": "2025-12-31",
            "access_2026": False,
        },
    )
    _write_docs(
        experiment_dir,
        status=status,
        family=family,
        execution_commit=execution_commit,
        raw_count=int(summary["raw_signal_config_count"]),
        candidate_count=len(factors.columns),
        admitted_count=len(admission.admitted),
        rejection_counts=rejection_counts,
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
            "visible_sample_end": "2025-12-31",
            "validation_accessed": False,
            "historical_2026_accessed": False,
        },
    )
    validate_experiment_archive(experiment_dir)
    return {**summary, "experiment_dir": str(experiment_dir)}
