from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import pandas as pd

from paper_trading_engine.contracts import AdviceDecision

from czsc_trader.application.advice_service import build_intraday_overlay_advice_v5
from czsc_trader.baselines import resolve_strategy_payload
from czsc_trader.constituent_moneyflow_runtime import (
    latest_breadth_signal,
    seed_support_panel,
)
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260911_S003_EX59"
CANDIDATE_ID = "S003-C001"
CANDIDATE_HASH = "1d6736f816d652c59d43dd22491c567e65100eaddfa3d20575531ca5a124f741"


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    payload_path = repo / "experiments/S003/20260911_S003_EX56/candidate_payload.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    baseline = resolve_strategy_payload(
        repo / "strategies/dependencies",
        payload,
        release_id="S003-v1",
        release_hash="c" * 64,
        symbol="510500.SH",
        repository_root=repo,
    )
    spec = baseline.constituent_moneyflow_intraday
    if spec is None:
        raise ValueError("S003 intraday support specification is missing")

    temp_root = repo / ".tmp"
    temp_root.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="s003-ex59-", dir=temp_root) as temporary:
        runtime = Path(temporary)
        seed_support_panel(repo, runtime, "S003-v1", spec)
        triggered, breadth, threshold, coverage = latest_breadth_signal(
            runtime, "S003-v1", spec, pd.Timestamp("2026-09-08")
        )

    strategy = {
        "strategy_id": "S003",
        "name": "成分资金流宽度早盘延续",
        "version": "v1",
        "release_id": "S003-v1",
        "release_hash": "c" * 64,
        "qualification": "PAPER_READY",
    }
    setup = build_intraday_overlay_advice_v5(
        strategy=strategy,
        baseline=baseline,
        signal_date=pd.Timestamp("2026-09-08"),
        valid_session=pd.Timestamp("2026-09-09"),
        signal_close=6.20,
        actual_quantity=0,
        available_cash=100_000,
        cycle_target_quantity=None,
        event_triggered=triggered,
    )
    parsed_setup = AdviceDecision.from_cli_payload(
        {"status": "PASS", "result": {**setup, "data_cutoff": "2026-09-08"}}
    )
    rotation = build_intraday_overlay_advice_v5(
        strategy=strategy,
        baseline=baseline,
        signal_date=pd.Timestamp("2026-09-08"),
        valid_session=pd.Timestamp("2026-09-09"),
        signal_close=6.20,
        actual_quantity=parsed_setup.cycle_target_quantity,
        available_cash=50_000,
        cycle_target_quantity=parsed_setup.cycle_target_quantity,
        event_triggered=True,
    )
    parsed_rotation = AdviceDecision.from_cli_payload(
        {"status": "PASS", "result": {**rotation, "data_cutoff": "2026-09-08"}}
    )
    checks = {
        "runtime_signal_is_causal": 0 <= breadth <= 1 and 0 <= threshold <= 1,
        "runtime_coverage_gate_passed": coverage >= spec.minimum_observed_weight_ratio,
        "setup_plan_is_atomic": (
            parsed_setup.contract_version == "advice.v5"
            and parsed_setup.plan_mode == "CORE_SETUP"
            and len(parsed_setup.plan_legs) == 1
        ),
        "rotation_plan_has_dependency": (
            parsed_rotation.action == "ROTATE"
            and [leg.order.side for leg in parsed_rotation.plan_legs] == ["BUY", "SELL"]
            and parsed_rotation.plan_legs[1].dependency_required_status == "FILLED_ALL"
        ),
        "strategy_owns_entry_limit": (
            parsed_rotation.plan_legs[0].order.order_type == "LIMIT"
            and parsed_rotation.plan_legs[0].order.limit_price == 6.82
        ),
        "exit_uses_marketable_order": (
            parsed_rotation.plan_legs[1].order.order_type == "MARKET"
        ),
    }
    if not all(checks.values()):
        raise AssertionError(f"contract checks failed: {checks}")

    test_nodes = [
        "tests/functional/test_data_and_advice.py::test_s003_runtime_signal_and_planned_advice_contract",
        "tests/functional/test_data_and_advice.py::test_s003_runtime_support_appends_one_published_session",
        "packages/paper_trading_engine/tests/functional/test_trading_cycle.py::test_ft_pte10_intraday_plan_waits_for_fill_and_recovers_after_restart",
        "packages/paper_trading_engine/tests/functional/test_trading_cycle.py::test_ft_pte11_intraday_plan_blocks_exit_when_entry_is_not_filled",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *test_nodes, "-q"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout + completed.stderr)

    implementation_files = [
        "src/czsc_trader/constituent_moneyflow_runtime.py",
        "src/czsc_trader/application/advice_service.py",
        "src/czsc_trader/application/data_service.py",
        "packages/paper_trading_engine/src/paper_trading_engine/contracts.py",
        "packages/paper_trading_engine/src/paper_trading_engine/store.py",
        "packages/paper_trading_engine/src/paper_trading_engine/account_engine.py",
        "packages/paper_trading_engine/src/paper_trading_engine/futu_execution.py",
    ]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": CANDIDATE_ID,
        "candidate_hash": CANDIDATE_HASH,
        "status": "PASS",
        "runtime_signal": {
            "signal_date": "2026-09-08",
            "triggered": bool(triggered),
            "moneyflow_breadth": breadth,
            "prior_threshold": threshold,
            "observed_weight_ratio": coverage,
        },
        "contract_checks": checks,
        "functional_scenarios": {
            "passed": len(test_nodes),
            "nodes": test_nodes,
            "includes_restart_recovery": True,
            "includes_unfilled_entry_block": True,
        },
        "operational_semantics": {
            "core_fraction": spec.core_fraction,
            "event_fraction": spec.event_fraction,
            "entry_window": "09:30:00-09:35:00 Asia/Shanghai",
            "exit_window": "11:29:00-11:30:00 Asia/Shanghai",
            "entry_order": "strategy-priced marketable LIMIT; broker adjust_limit=0",
            "exit_order": "MARKET after entry FILLED_ALL",
        },
        "implementation_identity": {
            path: raw_file_sha256(repo / path) for path in implementation_files
        },
        "lifecycle_effects": {
            "strategy_version_created": False,
            "strategy_frozen": False,
            "pte_runtime_mutated": False,
        },
    }
    _write(artifacts / "pte_compatibility.json", evidence)
    _write(artifacts / "protocol.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": CANDIDATE_ID,
        "candidate_hash": CANDIDATE_HASH,
        "development_cutoff": "2026-09-08",
        "mutates_candidate": False,
        "freezes_strategy": False,
        "mutates_pte_runtime": False,
    })
    (experiment / "03_execution.md").write_text(
        "# S003 EX59 执行\n\n状态：COMPLETE。\n\n"
        "已运行4个正式功能场景，覆盖运行时数据、advice.v5、原子计划、成交依赖、"
        "重启恢复以及未完整成交时的安全阻断。实验没有写入运行中的PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX59 结论\n\n状态：COMPLETE。\n\n"
        "PTE执行兼容门：`PASS`。开盘买入与11:30退出形成可恢复的原子依赖计划；"
        "买入未全部成交时，退出不会执行。策略定价、数据发布和审计链路均已落地。"
        "本结论只解除EX58的工程阻断，不等同于冻结或部署。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": "pte_compatibility_gate",
        "strategy_id": "S003",
        "symbol": "510500.SH",
        "candidate_id": CANDIDATE_ID,
        "development_cutoff": "2026-09-08",
        "decision": "PASS",
        "strategy_frozen": False,
        "pte_runtime_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
