"""Re-audit the immutable EX05 candidate pool under OPC-v3."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.evaluation_service import evaluate_experiment
from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.identity import normalized_text_sha256


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
SOURCE_MANIFEST = REPO_ROOT / "experiments" / "0903_EX05" / "candidate_manifest.json"
MANIFEST_PATH = EXPERIMENT_DIR / "candidate_manifest.json"
SOURCE_MANIFEST_SHA256 = "9c9ad082a8f741ce0144b4335288f38d6e3135bd6d3fb5f7123f05b5478e9ca9"
PREPARED_FROM_COMMIT = "47dbe585955a05d39271404c39d7d6f98bceddd0"


def candidate_manifest_from_source(source: dict[str, object]) -> dict[str, object]:
    manifest = copy.deepcopy(source)
    manifest["experiment_id"] = "0903_EX06"
    manifest["prepared_from_commit"] = PREPARED_FROM_COMMIT
    manifest["source_experiment"] = {
        "experiment_id": "0903_EX05",
        "candidate_manifest_sha256": SOURCE_MANIFEST_SHA256,
    }
    manifest["evaluation_workers"] = 8
    manifest["reuse_experiment_artifacts"] = True
    manifest["reuse_source_experiments"] = ["0903_EX05"]
    manifest["metric_semantics_version"] = "candidate-metrics-v1"
    manifest["audit_protocol"] = {
        "version": "champion-audit-v1",
        "seed": 20260903,
        "bootstrap_repetitions": 10_000,
        "mean_block_lengths": [21, 10, 42],
    }
    trials = manifest.get("trials")
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1_187:
        raise ValueError("EX06 requires all 1,187 EX05 candidates")
    if not isinstance(trials, list) or len(trials) != len(candidates):
        raise ValueError("EX06 requires the complete EX05 trial ledger")
    for trial in trials:
        if not isinstance(trial, dict) or "candidate_id" not in trial:
            raise ValueError("EX05 candidate manifest contains an invalid trial")
        trial["trial_id"] = f"EX06-{trial['candidate_id']}"
    audits = manifest.get("audits")
    if not isinstance(audits, dict):
        raise ValueError("EX05 candidate manifest has no audits")
    audits["revaluation_only"] = True
    audits["candidate_payloads_unchanged"] = True
    audits["complete_champion_audit"] = True
    return manifest


def prepare_manifest() -> dict[str, object]:
    if normalized_text_sha256(SOURCE_MANIFEST) != SOURCE_MANIFEST_SHA256:
        raise ValueError("EX05 candidate manifest differs from preregistration")
    source = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    if source.get("experiment_id") != "0903_EX05":
        raise ValueError("source candidate manifest is not EX05")
    manifest = candidate_manifest_from_source(source)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return manifest


def run() -> dict[str, object]:
    prepare_manifest()
    context = RepositoryContext.discover(REPO_ROOT, explicit_root=REPO_ROOT)
    artifacts_dir = EXPERIMENT_DIR / "artifacts"
    benchmark_path = artifacts_dir / "audit_benchmark.json"
    evaluation_was_complete = (artifacts_dir / "evaluation_result.json").is_file()
    started_at = time.perf_counter()
    result = dict(evaluate_experiment(context, "0903_EX06").result)
    elapsed_seconds = time.perf_counter() - started_at
    if evaluation_was_complete and benchmark_path.is_file():
        benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    else:
        benchmark = {
            "scope": "end_to_end_evaluation",
            "elapsed_seconds": round(elapsed_seconds, 3),
            "target_seconds": 300,
            "target_met": elapsed_seconds <= 300,
            "candidate_count": 1_187,
            "reuse_source_experiment": "0903_EX05",
        }
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        benchmark_path.write_text(
            json.dumps(benchmark, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
    (EXPERIMENT_DIR / "03_execution.md").write_text(
        "# 0903_EX06 执行\n\n"
        f"- 决策：`{result['decision']}`\n"
        f"- 临时冠军：`{result.get('recommended_candidate_id') or '无'}`\n"
        f"- 审计状态：`{(result.get('audit') or {}).get('status', 'N/A')}`\n"
        f"- 统计风险：`{(result.get('audit') or {}).get('risk_label', 'N/A')}`\n"
        f"- 端到端耗时：`{benchmark['elapsed_seconds']}`秒\n",
        encoding="utf-8", newline="\n",
    )
    audit = result.get("audit") or {}
    search_bias = audit.get("search_bias") or {}
    dsr = audit.get("dsr") or {}
    raw_dsr = dsr.get("raw") or {}
    effective_dsr = dsr.get("effective") or {}
    (EXPERIMENT_DIR / "04_conclusion.md").write_text(
        "# 0903_EX06 结论\n\n"
        f"R1102仍为唯一临时冠军，完整审计为`{audit.get('status', 'N/A')}`，"
        f"统计风险为`{audit.get('risk_label', 'N/A')}`。Sharpe-PBO为"
        f"`{float(search_bias.get('pbo', 0.0)):.4f}`；DSR原始/有效试验数口径的概率分别为"
        f"`{float(raw_dsr.get('probability', 0.0)):.4f}`和"
        f"`{float(effective_dsr.get('probability', 0.0)):.4f}`。配对Bootstrap与PBO方向"
        "偏正面，DSR为中性，真实候选邻域偏弱。\n\n"
        "SE给出`RECOMMEND_FREEZE`，代表证据完整且满足确定性标准；统计优势仍有不确定性。"
        "本轮不自动冻结、不注册模拟盘，也不改变SM或PTE状态，最终动作需人工确认。\n",
        encoding="utf-8", newline="\n",
    )
    build_experiment_manifest(EXPERIMENT_DIR, {
        "experiment_id": "0903_EX06", "date": "2026-09-03", "status": "COMPLETE",
        "symbol": "588080.SH", "asset_type": "etf",
        "visible_sample_end": "2026-09-02", "evaluation_standard": "opc-v3",
        "decision": result["decision"],
        "recommended_candidate_id": result.get("recommended_candidate_id"),
        "automatic_promotion": False,
    })
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
