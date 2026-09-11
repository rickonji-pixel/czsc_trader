from __future__ import annotations

import hashlib
import json
from pathlib import Path

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260911_S003_EX55"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in ("mutates_trader", "mutates_strategy_manager", "mutates_pte", "freezes_strategy")
    ):
        raise ValueError("EX55 is a read-only compatibility replay")

    candidate_experiment = repo / "experiments/20260911_S003_EX53"
    prior_gate = repo / "experiments/20260911_S003_EX54"
    validate_experiment_archive(candidate_experiment)
    validate_experiment_archive(prior_gate)

    sources = {
        "candidate_manifest_sha256": candidate_experiment / "experiment_manifest.json",
        "candidate_payload_raw_sha256": candidate_experiment / "candidate_payload.json",
        "candidate_readiness_sha256": candidate_experiment / "artifacts/candidate_readiness.json",
        "prior_gate_protocol_sha256": prior_gate / "artifacts/protocol.json",
        "prior_gate_manifest_sha256": prior_gate / "experiment_manifest.json",
    }
    for key, path in sources.items():
        if _sha256(path) != protocol["source"][key]:
            raise ValueError(f"source hash differs: {path}")

    prior_protocol = _read_json(prior_gate / "artifacts/protocol.json")
    prior_mismatch = {
        "candidate_manifest": {
            "expected": prior_protocol["source"]["candidate_manifest_sha256"],
            "actual": _sha256(candidate_experiment / "experiment_manifest.json"),
        },
        "candidate_readiness": {
            "expected": prior_protocol["source"]["candidate_readiness_sha256"],
            "actual": _sha256(candidate_experiment / "artifacts/candidate_readiness.json"),
        },
    }
    if all(item["expected"] == item["actual"] for item in prior_mismatch.values()):
        raise ValueError("EX54 source mismatch is no longer reproducible")

    candidate = _read_json(candidate_experiment / "candidate_payload.json")
    readiness = _read_json(candidate_experiment / "artifacts/candidate_readiness.json")
    if canonical_json_sha256(candidate) != protocol["candidate_hash"]:
        raise ValueError("candidate identity differs")
    if readiness["result"]["decision"] != "RECOMMEND_REGISTRATION":
        raise ValueError("candidate was not recommended for registration")

    resolution_error = ""
    try:
        resolve_candidate_snapshot(
            RepositoryContext.discover(repo),
            str(protocol["candidate_id"]),
            candidate,
            str(protocol["candidate_hash"]),
            "experiments/20260911_S003_EX53/candidate_payload.json",
        )
        resolution_supported = True
    except ValueError as exc:
        resolution_supported = False
        resolution_error = str(exc)

    capabilities = {
        "tdr_strategy_kind_resolution": resolution_supported,
        "tdr_core_overlay_account": False,
        "tdr_timed_intraday_exit": False,
        "pte_timed_intraday_exit": False,
        "runtime_point_in_time_moneyflow_data": False,
    }
    passed = all(capabilities.values())
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": protocol["candidate_id"],
        "passed": passed,
        "capabilities": capabilities,
        "resolution_error": resolution_error,
        "prior_gate_source_mismatch": prior_mismatch,
        "formal_pk_allowed": passed,
        "research_candidate_retained": True,
        "strategy_frozen": False,
        "pte_mutated": False,
    }
    _write_json(artifacts / "execution_compatibility.json", result)

    status = "PASS" if passed else "BLOCKED"
    (experiment / "03_execution.md").write_text(
        "# S003 EX55 执行\n\n"
        "EX53与EX54档案自身完整；EX54的两个跨实验源哈希与最终EX53文件不一致，"
        "因此EX54不能从干净检出状态重放。EX55使用最终提交哈希纠正来源后成功重放。\n\n"
        f"正式执行兼容门：`{status}`。TDR候选解析：{capabilities['tdr_strategy_kind_resolution']}；"
        f"核心/事件双仓位：{capabilities['tdr_core_overlay_account']}；TDR 11:30计划退出："
        f"{capabilities['tdr_timed_intraday_exit']}；PTE 11:30计划退出："
        f"{capabilities['pte_timed_intraday_exit']}；运行时历史成分资金流："
        f"{capabilities['runtime_point_in_time_moneyflow_data']}。\n\n"
        f"当前解析错误：`{resolution_error}`。本轮没有修改TDR、SM或PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX55 结论\n\n"
        f"结论：`{status}`。EX54原始失败证据保持不变；EX55恢复执行兼容门的可复现性。"
        "S003-C001研究候选继续有效，正式PK暂缓。下一步仍需建设统一日内事件策略契约、"
        "历史时点资金流运行时数据、核心/事件双仓位回放和PTE计划退出能力。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "candidate_id": "S003-C001",
            "development_cutoff": "2026-09-08",
            "status": status,
            "corrects_experiment": "20260911_S003_EX54",
            "formal_pk_allowed": passed,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
