from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260920_S007_EX44"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_hash(path: Path, expected: str) -> None:
    observed = _sha256(path)
    if observed != expected:
        raise ValueError(f"source hash differs: {path}: {observed} != {expected}")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    protocol = _read_json(experiment / "02_protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("EX44 protocol identity differs")
    forbidden = (
        "reads_forward_data",
        "runs_new_statistics",
        "simulates_new_policy",
        "searches_parameters",
        "creates_candidate",
        "changes_strategy_parameters",
        "mutates_pte",
    )
    if any(protocol.get(field) for field in forbidden):
        raise ValueError("EX44 must remain a stage-closure audit")

    evidence_by_experiment: dict[str, dict[str, Any]] = {}
    for experiment_id, source in protocol["sources"].items():
        source_archive = repo / "experiments" / "S007" / experiment_id
        validate_experiment_archive(source_archive)
        _require_hash(
            source_archive / "experiment_manifest.json",
            str(source["manifest_sha256"]),
        )
        evidence_path = source_archive / str(source["evidence"])
        _require_hash(evidence_path, str(source["evidence_sha256"]))
        source_evidence = _read_json(evidence_path)
        if source_evidence.get("status") != "PASS":
            raise ValueError(f"source experiment did not pass integrity execution: {experiment_id}")
        if source_evidence.get("credential_id") != protocol["credential_id"]:
            raise ValueError(f"source credential differs: {experiment_id}")
        if source_evidence.get("development_cutoff") != protocol["development_cutoff"]:
            raise ValueError(f"source development cutoff differs: {experiment_id}")
        for field in (
            "forward_data_read",
            "strategy_parameters_changed",
            "candidate_created",
            "pte_mutated",
        ):
            if source_evidence.get(field):
                raise ValueError(f"source crossed closure boundary: {experiment_id}:{field}")
        evidence_by_experiment[experiment_id] = source_evidence

    ex39 = evidence_by_experiment["20260920_S007_EX39"]
    ex40 = evidence_by_experiment["20260920_S007_EX40"]
    ex41 = evidence_by_experiment["20260920_S007_EX41"]
    ex42 = evidence_by_experiment["20260920_S007_EX42"]
    ex43 = evidence_by_experiment["20260920_S007_EX43"]
    if (
        ex39["trade_count"] != 139
        or ex39["short_trade_count"] != 121
        or ex41["overlap"]["price_risk_incremental_count"] != 43
        or ex42["overlay_trigger_count"] != 43
        or ex43["sample"]["trade_count"] != 43
    ):
        raise ValueError("S007 attribution evidence chain does not close")
    if ex43["decision"] != "CHECKPOINT_INFORMATION_INSUFFICIENT":
        raise ValueError("EX43 decision differs from the approved closure basis")

    first_close = next(
        item
        for item in ex40["early_path_comparisons"]
        if item["feature"] == "close_return_s1"
    )
    closure = {
        "schema_version": 1,
        "status": "PASS",
        "experiment_id": EXPERIMENT_ID,
        "credential_id": protocol["credential_id"],
        "strategy_release": "S007-v1",
        "development_cutoff": protocol["development_cutoff"],
        "decision": "BATCH_CLOSED_NO_NEW_CANDIDATE",
        "human_review": protocol["human_review"],
        "evidence_chain": {
            "EX39": {
                "closed_trades": int(ex39["trade_count"]),
                "short_trades": int(ex39["short_trade_count"]),
                "short_losses": int(ex39["short_loss_count"]),
                "sustained_trades": int(ex39["sustained_trade_count"]),
                "top_1_positive_share": ex39["concentration"]["top_1_positive_share"],
                "top_3_positive_share": ex39["concentration"]["top_3_positive_share"],
                "top_5_positive_share": ex39["concentration"]["top_5_positive_share"],
            },
            "EX40": {
                "first_close_short_win_auc": first_close["short_win_auc"],
                "first_close_bh_fdr_qvalue": first_close["bh_fdr_qvalue"],
                "interpretation": "POST_ENTRY_PATH_ASSOCIATION_ONLY",
            },
            "EX41": {
                "price_risk_count": ex41["overlap"]["price_risk_count"],
                "factor_exit_count": ex41["overlap"]["factor_exit_count"],
                "intersection_count": ex41["overlap"]["intersection_count"],
                "price_risk_incremental_count": ex41["overlap"][
                    "price_risk_incremental_count"
                ],
            },
            "EX42": {
                "baseline_return": ex42["baseline"]["metrics"]["return"],
                "overlay_return": ex42["overlay"]["metrics"]["return"],
                "baseline_max_drawdown": ex42["baseline"]["metrics"]["max_drawdown"],
                "overlay_max_drawdown": ex42["overlay"]["metrics"]["max_drawdown"],
                "improved_trades": ex42["triggered_trade_return_delta"]["improved_count"],
                "worsened_trades": ex42["triggered_trade_return_delta"]["worsened_count"],
                "observed_as_no_trade": ex42["no_trade_degeneration"][
                    "observed_as_no_trade"
                ],
                "overlay_one_session_trade_rate": ex42["overlay"]["diagnostics"][
                    "one_session_trade_rate"
                ],
            },
            "EX43": {
                "hold_better_count": ex43["sample"]["hold_better_count"],
                "exit_better_count": ex43["sample"]["exit_better_count"],
                "fdr_significant_count": ex43["univariate"]["fdr_significant_count"],
                "cross_year_auc": ex43["fixed_cross_year_model"]["auc"],
                "cross_year_balanced_accuracy": ex43["fixed_cross_year_model"][
                    "balanced_accuracy"
                ],
                "cross_year_two_sided_pvalue": ex43["fixed_cross_year_model"][
                    "two_sided_permutation_pvalue"
                ],
            },
        },
        "retained_findings": [
            "S007-v1 formal ledger and frozen execution contract remain the baseline",
            "existing factor-score exit retains an independent profit-taking role",
            "post-entry price path is useful for attribution and forward monitoring",
            "all post-cutoff observations remain outside this development batch",
        ],
        "rejected_directions": [
            "holding-period length as a standalone pre-trade filter",
            "universal first-close cost-adjusted breakeven exit",
            "additional threshold search at the first-close checkpoint",
        ],
        "next_boundary": {
            "research_family_state": "PAUSED",
            "operational_state": "S007-v1_PTE_FORWARD_OBSERVATION_CONTINUES",
            "new_formula_or_parameter_change": "REQUIRES_NEW_RESEARCH_BATCH_AND_CANDIDATE",
            "development_data_reuse": "FORBIDDEN_FOR_POST_CUTOFF_FORWARD_OBSERVATIONS",
        },
        "forward_data_read": False,
        "new_statistics_run": False,
        "new_policy_simulated": False,
        "strategy_parameters_changed": False,
        "candidate_created": False,
        "pte_mutated": False,
    }

    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    _write_json(artifacts / "stage_closure.json", closure)
    experiment.joinpath("03_execution.md").write_text(
        "# S007 EX44 执行\n\n"
        "状态：`COMPLETE`。EX39—EX43五个不可变档案及核心证据哈希全部通过校验，"
        "样本数量从139笔正式交易、43笔增量价格风险交易到43笔固定反事实与机制诊断"
        "完整闭合。\n\n"
        "本轮只汇总既有证据和人工评审结论，没有读取前瞻数据、追加统计、模拟新规则、"
        "创建候选、修改冻结策略或改变PTE。\n",
        encoding="utf-8",
    )
    experiment.joinpath("04_conclusion.md").write_text(
        "# S007 EX44 结论\n\n"
        "状态：`COMPLETE`；裁决：`BATCH_CLOSED_NO_NEW_CANDIDATE`。\n\n"
        "固定v1归因确认：短交易同时包含大量盈利和亏损；首日价格状态与结果存在关联，"
        "但固定次日退出使累计收益从219.54%降至100.28%，最大回撤从-10.81%恶化至"
        "-11.12%，43笔中只有13笔改善、30笔恶化。首日可见信息也未形成经多重校正或"
        "跨年验证支持的稳定分层。\n\n"
        "因此保留S007-v1及现有基础分退出，否决通用首日价格风险退出，并结束首日退出"
        "阈值研究。本批次不形成新候选；研究状态转为暂停，S007-v1继续按既有方案进行"
        "独立PTE前瞻观察。任何公式或参数变化必须使用新的研究批次和候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "strategy_version": protocol["strategy_version"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": "BATCH_CLOSED_NO_NEW_CANDIDATE",
            "research_family_state_target": "PAUSED",
            "strategy_parameters_changed": False,
            "candidate_created": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
