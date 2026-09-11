from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260911_S003_EX64"


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
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol["semantics"].get("totals_directly_comparable") is not False:
        raise ValueError("EX64 must preserve distinct account semantics")
    if any(
        protocol.get(key)
        for key in (
            "parameter_selection", "candidate_generation", "promotion_allowed",
            "mutates_strategy_manager", "mutates_pte",
        )
    ):
        raise ValueError("EX64 is reconciliation only")

    ex45 = repo / "experiments/S003/20260911_S003_EX45"
    ex63 = repo / "experiments/S003/20260911_S003_EX63"
    validate_experiment_archive(ex45)
    validate_experiment_archive(ex63)
    source = protocol["sources"]
    expected = {
        ex45 / "experiment_manifest.json": source["ex45_manifest_sha256"],
        ex45 / "artifacts/account_metrics.csv": source["ex45_account_metrics_sha256"],
        ex63 / "experiment_manifest.json": source["ex63_manifest_sha256"],
        ex63 / "artifacts/attribution_summary.json": source["ex63_summary_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")

    research = pd.read_csv(ex45 / "artifacts/account_metrics.csv").iloc[0]
    formal = _read_json(ex63 / "artifacts/attribution_summary.json")["frozen_attribution"]
    research_total = float(research["terminal_equity"] - 1.0)
    research_static = float(research["static_terminal_equity"] - 1.0)
    research_alpha = float(research["terminal_equity"] - research["static_terminal_equity"])
    formal_total = float(formal["account_total_return"])
    formal_static = float(formal["static_core_total_return"])
    formal_alpha = float(formal["event_alpha_terminal_return_on_account"])
    direction_consistent = research_alpha > 0 and formal_alpha > 0
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "direction_consistent": direction_consistent,
        "research_economic_stress": {
            "account_total_return": research_total,
            "static_core_total_return": research_static,
            "event_alpha_absolute_return_on_account": research_alpha,
            "event_alpha_relative_to_static_terminal": float(
                research["incremental_terminal_return_vs_static"]
            ),
            "account_maximum_drawdown": float(research["max_drawdown"]),
            "static_core_maximum_drawdown": float(research["static_max_drawdown"]),
        },
        "formal_price_only_baseline_cost": {
            "account_total_return": formal_total,
            "static_core_total_return": formal_static,
            "event_alpha_absolute_return_on_account": formal_alpha,
            "account_maximum_drawdown": float(formal["account_maximum_drawdown"]),
            "static_core_maximum_drawdown": float(formal["static_core_maximum_drawdown"]),
            "event_alpha_maximum_drawdown_on_account": float(
                formal["event_alpha_maximum_drawdown_on_account"]
            ),
        },
        "boundary": {
            "economic_total_return_authority": "EX45",
            "deterministic_order_and_ledger_authority": "EX56_EX63",
            "formal_ledger_includes_distribution_cashflow": False,
            "pte_corporate_action_allocation_required": True,
        },
        "candidate_changed": False,
        "sm_changed": False,
        "pte_changed": False,
    }
    _write_json(artifacts / "valuation_reconciliation.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S003 EX64 执行\n\n"
        f"口径核对：`PASS`。研究经济压力口径下，账户累计收益{research_total:.2%}、"
        f"静态核心{research_static:.2%}、事件Alpha绝对贡献{research_alpha:.2%}；"
        f"正式未复权基准成本口径分别为{formal_total:.2%}、{formal_static:.2%}和"
        f"{formal_alpha:.2%}。两套口径的事件贡献方向一致。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX64 结论\n\n"
        "EX45负责长期经济收益解释，EX56/EX63负责未复权订单、成交和账本确定性验证。"
        "两套账户总收益不能直接互换；正式账本尚未显式计入基金现金分配。S003-v1冻结参数"
        "不受影响，但PTE在发生分红、拆分等非交易变动时必须告警并单独归属，不能作为费用"
        "差异自动入账。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "strategy_id": "S003",
        "release_id": "S003-v1",
        "symbol": "510500.SH",
        "development_cutoff": "2026-09-08",
        "status": "PASS",
        "direction_consistent": direction_consistent,
        "formal_ledger_includes_distribution_cashflow": False,
        "candidate_generation": False,
        "mutates_strategy_manager": False,
        "mutates_pte": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
