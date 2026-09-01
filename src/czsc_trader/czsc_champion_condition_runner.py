"""Confirm the route survivor inside frozen champion position states."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .baselines import resolve_baseline
from .czsc_factor_stability import audit_causal_prefix, build_forward_outcomes, event_onsets
from .czsc_multiplicity import benjamini_hochberg, hac_incremental_test
from .czsc_route import admit_factors, build_champion_position_factors
from .czsc_route_runner import _build_route_family, _champion_references, _raw_signals
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _validate(protocol: dict[str, object]) -> None:
    expected = {
        "experiment_id": "0901_EX11",
        "handler": "czsc_champion_position_condition",
        "symbol": "588080.SH",
        "visible_end": "2025-12-31",
        "access_2026": False,
        "multiplicity_parent": "0901_EX10",
        "source_experiment": "0901_EX08",
        "source_factor_kind": "event",
        "source_endpoint": "max_drawdown_20",
        "source_direction": 1,
        "candidate_target_positions": [0, 1],
        "hac_max_lag": 20,
        "fdr_q": 0.10,
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"champion-condition protocol {key} differs from preregistration")


def run_champion_position_condition(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    _validate(protocol)
    baseline = resolve_baseline(
        Path(baseline_root), str(protocol["champion"]["version"]), symbol="588080.SH"
    )
    if baseline.sha256 != str(protocol["champion"]["sha256"]):
        raise ValueError("champion-condition baseline identity differs")
    multiplicity_dir = experiment_dir.parent / str(protocol["multiplicity_parent"])
    actual_parent_hash = sha256((multiplicity_dir / "experiment_manifest.json").read_bytes()).hexdigest()
    if actual_parent_hash != str(protocol["multiplicity_parent_manifest"]):
        raise ValueError("champion-condition multiplicity parent identity differs")
    survivors = pd.read_csv(multiplicity_dir / "artifacts" / "multiplicity_survivors.csv")
    source_factor = str(protocol["source_factor"])
    match = survivors[survivors["factor"].eq(source_factor)]
    if len(match) != 1 or str(match.iloc[0]["endpoint"]) != str(protocol["source_endpoint"]):
        raise ValueError("champion-condition source is not the unique multiplicity survivor")

    data = load_market_data(
        raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp(str(protocol["visible_end"]))
    )
    source_dir = experiment_dir.parent / str(protocol["source_experiment"])
    source_protocol = json.loads(
        (source_dir / "artifacts" / "protocol.json").read_text(encoding="utf-8")
    )
    raw = _raw_signals(data, source_protocol)
    source_factors, _ = _build_route_family(
        raw, str(source_protocol["family"]), source_protocol, source_dir
    )
    if source_factor not in source_factors:
        raise ValueError("champion-condition source factor cannot be replayed")
    references = _champion_references(data, baseline).reindex(source_factors.index)
    factors, records = build_champion_position_factors(
        source_factors[source_factor].rename(source_factor), references["target_position"]
    )
    kinds = {str(row["factor"]): str(row["factor_kind"]) for row in records}
    outcomes = build_forward_outcomes(
        data.daily, factors.index, tuple(map(int, protocol["horizons"]))
    )
    prefix_data = load_market_data(
        raw_dir, "588080.SH", "etf", cutoff=pd.Timestamp("2023-12-31")
    )
    prefix_raw = _raw_signals(prefix_data, source_protocol)
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

    hac_rows: list[dict[str, object]] = []
    reference_values = references.to_numpy(dtype=float)
    for name in map(str, factors.columns):
        event = event_onsets(factors[name]).astype(float)
        for endpoint in ("return_20", "max_drawdown_20"):
            result = hac_incremental_test(
                outcomes[endpoint].to_numpy(dtype=float),
                event.to_numpy(dtype=float),
                reference_values,
                max_lag=int(protocol["hac_max_lag"]),
            )
            hac_rows.append({"factor": name, "endpoint": endpoint, **result})
    hac = pd.DataFrame(hac_rows)
    hac["q_value"] = benjamini_hochberg(hac["p_value"].to_numpy(dtype=float))
    common = admission.admitted.set_index("factor") if not admission.admitted.empty else pd.DataFrame()
    confirmed: list[dict[str, object]] = []
    for row in hac.itertuples(index=False):
        if common.empty or str(row.factor) not in common.index:
            continue
        admitted = common.loc[str(row.factor)]
        if str(admitted["endpoint"]) != str(row.endpoint):
            continue
        if float(row.q_value) <= float(protocol["fdr_q"]) and int(np.sign(float(row.coefficient))) == int(admitted["direction"]):
            confirmed.append(
                {
                    "factor": str(row.factor),
                    "factor_kind": "event",
                    "champion_target_position": int(str(row.factor).rsplit("_", 1)[1]),
                    "endpoint": str(row.endpoint),
                    "direction": int(admitted["direction"]),
                    "conditional_effect": float(admitted["conditional_effect"]),
                    "hac_coefficient": float(row.coefficient),
                    "p_value": float(row.p_value),
                    "q_value": float(row.q_value),
                    "nobs": int(row.nobs),
                }
            )
    confirmed_frame = pd.DataFrame(
        confirmed,
        columns=[
            "factor", "factor_kind", "champion_target_position", "endpoint", "direction",
            "conditional_effect", "hac_coefficient", "p_value", "q_value", "nobs",
        ],
    )
    _write_json(artifacts / "factor_universe.json", records)
    admission.admitted.to_csv(artifacts / "common_admitted.csv", index=False, encoding="utf-8-sig")
    admission.rejected.to_csv(artifacts / "rejected_factors.csv", index=False, encoding="utf-8-sig")
    admission.conditional_audit.to_csv(artifacts / "conditional_audit.csv", index=False, encoding="utf-8-sig")
    admission.influence_audit.to_csv(artifacts / "influence_audit.csv", index=False, encoding="utf-8-sig")
    admission.redundancy_audit.to_csv(artifacts / "redundancy_audit.csv", index=False, encoding="utf-8-sig")
    hac.to_csv(artifacts / "confirmatory_hac.csv", index=False, encoding="utf-8-sig")
    confirmed_frame.to_csv(artifacts / "confirmed_factors.csv", index=False, encoding="utf-8-sig")
    _write_json(artifacts / "causal_replay_audit.json", causal)
    status = "PASS" if not confirmed_frame.empty else "FAIL"
    summary = {
        "status": status,
        "candidate_count": len(factors.columns),
        "common_admitted_count": len(admission.admitted),
        "confirmed_factor_count": len(confirmed_frame),
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
            "multiplicity_parent_manifest": actual_parent_hash,
            "visible_data_hashes": data.hashes,
            "access_2026": False,
        },
    )
    confirmed_text = ", ".join(map(str, confirmed_frame["champion_target_position"])) or "无"
    (experiment_dir / "03_execution.md").write_text(
        "\n".join(
            [
                "# 0901_EX11 执行过程",
                "",
                f"- 执行提交：`{execution_commit}`",
                f"- 固定父事件：`{source_factor}`。",
                f"- 共同准入：{len(admission.admitted)}/2。",
                f"- 确认性BH后通过仓位：{confirmed_text}。",
                f"- 因果前缀重放：`{causal['status']}`。",
                "- 未读取2026，未运行策略搜索。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(
            [
                "# 0901_EX11 结论",
                "",
                f"状态：`{status}`。",
                "",
                f"父风险事件按冠军仓位拆分后，有{len(confirmed_frame)}个条件事件完成确认。",
                "本轮只确定事件适用的冠军仓位上下文，不形成交易策略。",
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
