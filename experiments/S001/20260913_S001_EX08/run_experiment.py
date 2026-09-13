from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import (
    BacktestRequestV2,
    load_replay_data,
    resolve_candidate_snapshot,
    resolve_registered_strategy,
    run_backtest_v2,
)
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260913_S001_EX08"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    source = repo / str(protocol["source_experiment"])
    validate_experiment_archive(source)
    summary_path = source / "artifacts/selection_summary.json"
    if _sha256(summary_path) != protocol["source_summary_sha256"]:
        raise ValueError("risk-budget selection differs from protocol")
    selection = _read(summary_path)
    if selection.get("route_decision") != "PROCEED_TO_RISK_BUDGET_CONTRACT_IMPLEMENTATION":
        raise ValueError("source experiment did not authorize contract implementation")
    if float(selection["selected_risk_budget_fraction"]) != float(
        protocol["allocation_fraction"]
    ):
        raise ValueError("implemented allocation differs from selected allocation")

    context = RepositoryContext.discover(repo)
    registered = resolve_registered_strategy(
        context,
        str(protocol["source_strategy_id"]),
        str(protocol["source_strategy_version"]),
    )
    if registered.source_hash != protocol["source_strategy_release_hash"]:
        raise ValueError("source strategy release differs from protocol")
    candidate_payload = deepcopy(registered.strategy_payload)
    candidate_payload.update(
        {
            "schema_version": 1,
            "candidate_id": str(protocol["candidate_id"]),
            "name": "综合基线策略60%风险预算",
            "source_release": registered.identity.reference,
            "source_experiment": EXPERIMENT_ID,
        }
    )
    capital = candidate_payload["rule"]["execution"]["capital"]
    capital["mode"] = str(protocol["capital_mode"])
    capital["allocation_fraction"] = float(protocol["allocation_fraction"])
    candidate_hash = canonical_json_sha256(candidate_payload)
    candidate = resolve_candidate_snapshot(
        context,
        str(protocol["candidate_id"]),
        candidate_payload,
        candidate_hash,
        f"experiments/S001/{EXPERIMENT_ID}/candidate_payload.json",
    )
    cutoff = pd.Timestamp(str(protocol["development_cutoff"])).date()
    replay_data = load_replay_data(
        context,
        str(protocol["dataset"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        cutoff,
    )
    start = pd.Timestamp(str(protocol["evaluation_start"])).date()
    end = pd.Timestamp(str(protocol["evaluation_end"])).date()
    registered_signals = replay_signals(registered, replay_data, start, end)
    candidate_signals = replay_signals(candidate, replay_data, start, end)
    if not registered_signals.decisions["target_position"].equals(
        candidate_signals.decisions["target_position"]
    ):
        raise AssertionError("risk-budget candidate changed the strategy signal sequence")

    output_root = repo / ".tmp/s001-ex08-backtest"
    shutil.rmtree(output_root, ignore_errors=True)
    summary = run_backtest_v2(
        snapshot=candidate,
        replay_data=replay_data,
        request=BacktestRequestV2(
            symbol=str(protocol["symbol"]),
            asset_type=str(protocol["asset_type"]),
            dataset=replay_data.dataset,
            start=start,
            end=end,
            initial_cash=float(protocol["initial_cash"]),
        ),
        outputs_root=output_root,
        run_date=cutoff,
        repository_root=repo,
    )
    metrics = summary.metrics["strategy"]["metrics"]
    account = pd.read_csv(summary.output_dir / "account_daily.csv")
    sessions = len(account)
    cagr = float((1.0 + float(metrics["return"])) ** (252.0 / sessions) - 1.0)
    required_cagr = float(protocol["buyhold_cagr"]) * float(
        protocol["positive_buyhold_cagr_multiple"]
    )
    return_pass = cagr >= required_cagr
    drawdown_pass = float(metrics["max_drawdown"]) >= float(
        protocol["maximum_drawdown_floor"]
    )
    fills = pd.read_csv(summary.output_dir / "fills.csv")
    first_buy = fills.loc[fills["side"].eq("BUY")].iloc[0]
    first_buy_cost = float(first_buy["quantity"]) * float(first_buy["price"]) + float(
        first_buy["fees"]
    )
    passed = return_pass and drawdown_pass and summary.manifest["audit"]["status"] == "PASS"
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "candidate_id": protocol["candidate_id"],
        "candidate_hash": candidate_hash,
        "source_strategy_reference": registered.identity.reference,
        "signal_sequence_unchanged": True,
        "capital_mode": protocol["capital_mode"],
        "allocation_fraction": protocol["allocation_fraction"],
        "first_buy_cash_utilization": first_buy_cost / float(protocol["initial_cash"]),
        "cagr": cagr,
        "total_return": float(metrics["return"]),
        "max_drawdown": float(metrics["max_drawdown"]),
        "calmar": float(metrics["calmar"]),
        "win_loss_ratio": float(metrics["win_loss_ratio"]),
        "closed_trades": int(metrics["closed_trades"]),
        "required_strategy_cagr": required_cagr,
        "return_gate": "PASS" if return_pass else "FAIL",
        "drawdown_gate": "PASS" if drawdown_pass else "FAIL",
        "se_replay_audit": summary.manifest["audit"]["status"],
        "primary_gate": "PASS" if passed else "FAIL",
        "route_decision": "PROCEED_TO_CANDIDATE_EVALUATION" if passed else "REJECT_CONTRACT",
        "candidate_created": passed,
    }
    if passed:
        _write(experiment / "candidate_payload.json", candidate_payload)
    for name in ("metrics.json", "audit.json", "report.md", "trades.csv"):
        shutil.copy2(summary.output_dir / name, artifacts / name)
    _write(artifacts / "contract_result.json", result)
    (experiment / "03_execution.md").write_text(
        "# S001 EX08 执行\n\n"
        f"状态：`COMPLETE`。60% 风险预算正式契约回放 `{result['primary_gate']}`；"
        f"SE 账本审计 `{result['se_replay_audit']}`。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX08 结论\n\n"
        f"正式契约口径 CAGR {cagr:.2%}，最大回撤 {float(metrics['max_drawdown']):.2%}，"
        f"卡玛比率 {float(metrics['calmar']):.3f}，盈亏比 {float(metrics['win_loss_ratio']):.3f}。"
        f"裁决：`{result['route_decision']}`。"
        + ("已生成 `S001-C001`，下一步进入统一候选评估。\n" if passed else "不生成候选。\n"),
        encoding="utf-8",
    )
    shutil.rmtree(output_root, ignore_errors=True)
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["source_strategy_id"],
            "strategy_version": protocol["source_strategy_version"],
            "candidate_id": protocol["candidate_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": result["route_decision"],
            "candidate_generation": passed,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
