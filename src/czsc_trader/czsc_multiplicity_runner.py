"""Audit terminal-route CZSC candidates for global multiple testing."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .baselines import resolve_baseline
from .czsc_factor_stability import build_forward_outcomes, event_onsets
from .czsc_multiplicity import benjamini_hochberg, hac_incremental_test
from .czsc_route_runner import _build_route_family, _champion_references, _raw_signals
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _validate_protocol(protocol: dict[str, object]) -> None:
    expected = {
        "experiment_id": "0901_EX10",
        "handler": "czsc_route_multiplicity_audit",
        "symbol": "588080.SH",
        "visible_end": "2025-12-31",
        "access_2026": False,
        "parent_experiments": ["0901_EX06", "0901_EX07", "0901_EX08", "0901_EX09"],
        "endpoints": ["return_20", "max_drawdown_20"],
        "hac_max_lag": 20,
        "fdr_method": "benjamini_hochberg",
        "fdr_q": 0.10,
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"multiplicity protocol {key} differs from preregistration")


def run_multiplicity_audit(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    _validate_protocol(protocol)
    baseline = resolve_baseline(
        Path(baseline_root), str(protocol["champion"]["version"]), symbol="588080.SH"
    )
    if baseline.sha256 != str(protocol["champion"]["sha256"]):
        raise ValueError("multiplicity champion hash differs from preregistration")
    data = load_market_data(
        raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["visible_end"]))
    )
    references = _champion_references(data, baseline)
    outcomes = build_forward_outcomes(data.daily, references.index, [int(protocol["primary_horizon"])])
    parent_hashes = protocol.get("parent_manifests")
    if not isinstance(parent_hashes, dict):
        raise ValueError("multiplicity parent hashes are missing")
    raw_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], pd.DataFrame] = {}
    eligible_reasons = set(map(str, protocol["eligible_rejection_reasons"]))
    tests: list[dict[str, object]] = []
    formal: dict[tuple[str, str], dict[str, object]] = {}
    family_counts: dict[str, dict[str, int]] = {}
    for parent_name in map(str, protocol["parent_experiments"]):
        parent_dir = experiment_dir.parent / parent_name
        manifest_path = parent_dir / "experiment_manifest.json"
        actual_hash = sha256(manifest_path.read_bytes()).hexdigest()
        if actual_hash != str(parent_hashes.get(parent_name, "")):
            raise ValueError(f"multiplicity parent manifest hash differs: {parent_name}")
        parent_protocol = json.loads(
            (parent_dir / "artifacts" / "protocol.json").read_text(encoding="utf-8")
        )
        key = (
            tuple(map(str, parent_protocol["signal_names"])),
            tuple(map(str, parent_protocol["frequencies"])),
        )
        raw = raw_cache.get(key)
        if raw is None:
            raw = _raw_signals(data, parent_protocol)
            raw_cache[key] = raw
        factors, records = _build_route_family(
            raw, str(parent_protocol["family"]), parent_protocol, parent_dir
        )
        kinds = {
            str(row["factor"]): str(row["factor_kind"])
            for row in records
            if row["status"] == "candidate"
        }
        admitted = pd.read_csv(parent_dir / "artifacts" / "admitted_factors.csv")
        rejected = pd.read_csv(parent_dir / "artifacts" / "rejected_factors.csv")
        admitted_names = set(map(str, admitted["factor"]))
        rejected_eligible = set(
            map(str, rejected.loc[rejected["reason"].isin(eligible_reasons), "factor"])
        )
        eligible_names = sorted(admitted_names | rejected_eligible)
        for row in admitted.to_dict(orient="records"):
            formal[(parent_name, str(row["factor"]))] = row
        family_counts[parent_name] = {
            "canonical": len(factors.columns),
            "eligible": len(eligible_names),
            "formal_admitted": len(admitted_names),
        }
        reference_values = references.to_numpy(dtype=float)
        for name in eligible_names:
            indicator = factors[name]
            candidate = (
                event_onsets(indicator).astype(float)
                if kinds[name] == "event"
                else indicator.astype(float)
            )
            for endpoint in map(str, protocol["endpoints"]):
                result = hac_incremental_test(
                    outcomes[endpoint].to_numpy(dtype=float),
                    candidate.to_numpy(dtype=float),
                    reference_values,
                    max_lag=int(protocol["hac_max_lag"]),
                )
                tests.append(
                    {
                        "parent_experiment": parent_name,
                        "family": str(parent_protocol["family"]),
                        "factor": name,
                        "factor_kind": kinds[name],
                        "endpoint": endpoint,
                        **result,
                    }
                )
    test_frame = pd.DataFrame(tests)
    test_frame["q_value"] = benjamini_hochberg(test_frame["p_value"].to_numpy(dtype=float))
    survivors: list[dict[str, object]] = []
    for row in test_frame.itertuples(index=False):
        parent_key = (str(row.parent_experiment), str(row.factor))
        selected = formal.get(parent_key)
        if selected is None or str(selected["endpoint"]) != str(row.endpoint):
            continue
        direction_matches = int(np.sign(float(row.coefficient))) == int(selected["direction"])
        if float(row.q_value) <= float(protocol["fdr_q"]) and direction_matches:
            survivors.append(
                {
                    "parent_experiment": str(row.parent_experiment),
                    "family": str(row.family),
                    "factor": str(row.factor),
                    "factor_kind": str(row.factor_kind),
                    "endpoint": str(row.endpoint),
                    "original_direction": int(selected["direction"]),
                    "original_conditional_effect": float(selected["conditional_effect"]),
                    "hac_coefficient": float(row.coefficient),
                    "p_value": float(row.p_value),
                    "q_value": float(row.q_value),
                    "nobs": int(row.nobs),
                }
            )
    survivor_columns = [
        "parent_experiment", "family", "factor", "factor_kind", "endpoint",
        "original_direction", "original_conditional_effect", "hac_coefficient",
        "p_value", "q_value", "nobs",
    ]
    survivor_frame = pd.DataFrame(survivors, columns=survivor_columns)
    test_frame.to_csv(artifacts / "all_hac_tests.csv", index=False, encoding="utf-8-sig")
    survivor_frame.to_csv(
        artifacts / "multiplicity_survivors.csv", index=False, encoding="utf-8-sig"
    )
    status = "PASS" if not survivor_frame.empty else "FAIL"
    summary = {
        "status": status,
        "tested_hypothesis_count": len(test_frame),
        "formal_admitted_count": len(formal),
        "multiplicity_survivor_count": len(survivor_frame),
        "fdr_q": float(protocol["fdr_q"]),
        "family_counts": family_counts,
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
            "parent_manifest_hashes": parent_hashes,
            "access_2026": False,
        },
    )
    (experiment_dir / "03_execution.md").write_text(
        "\n".join(
            [
                "# 0901_EX10 执行过程",
                "",
                f"- 执行提交：`{execution_commit}`",
                f"- 全局HAC假设：{len(test_frame)}项。",
                f"- 父档案形式准入：{len(formal)}项。",
                f"- BH q≤0.10且方向一致：{len(survivor_frame)}项。",
                "- 控制冠军目标仓位和12个冻结因子；HAC最大滞后20日。",
                "- 未读取2026，未运行策略搜索。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(
            [
                "# 0901_EX10 结论",
                "",
                f"状态：`{status}`。",
                "",
                f"{len(formal)}个形式准入因子中，{len(survivor_frame)}个通过全局多重比较审计。",
                "本轮只决定后续研究资格，不重写父实验结论，也不形成策略。",
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
