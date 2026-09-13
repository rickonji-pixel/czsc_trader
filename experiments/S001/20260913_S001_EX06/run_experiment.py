from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
from strategy_evaluator import AuditStatus, audit_replay

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S001_EX06"


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


def _portfolio_metrics(equity: pd.Series, initial_cash: float) -> dict[str, float]:
    values = equity.astype(float).reset_index(drop=True)
    total_return = float(values.iloc[-1] / initial_cash - 1.0)
    cagr = float((values.iloc[-1] / initial_cash) ** (252.0 / len(values)) - 1.0)
    max_drawdown = float(values.div(values.cummax()).sub(1.0).min())
    return {
        "cagr": cagr,
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "calmar": cagr / abs(max_drawdown) if abs(max_drawdown) > 1e-12 else 0.0,
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    source = repo / str(protocol["source_experiment"])
    validate_experiment_archive(source)
    source_summary = source / "artifacts/feasibility_summary.json"
    if _sha256(source_summary) != protocol["source_summary_sha256"]:
        raise ValueError("source feasibility summary differs from protocol")
    if _read(source_summary).get("finding") != "NO_FEASIBLE_REGION":
        raise ValueError("profit-protection experiment did not route to risk-budget test")

    context = RepositoryContext.discover(repo)
    snapshot = resolve_registered_strategy(
        context, str(protocol["strategy_id"]), str(protocol["strategy_version"])
    )
    if snapshot.source_hash != protocol["strategy_release_hash"]:
        raise ValueError("strategy release differs from protocol")
    cutoff = pd.Timestamp(str(protocol["development_cutoff"])).date()
    replay_data = load_replay_data(
        context,
        str(protocol["dataset"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        cutoff,
    )
    signals = replay_signals(
        snapshot,
        replay_data,
        pd.Timestamp(str(protocol["evaluation_start"])).date(),
        pd.Timestamp(str(protocol["evaluation_end"])).date(),
    )
    total_cash = float(protocol["total_initial_cash"])
    required_cagr = float(protocol["buyhold_cagr"]) * float(
        protocol["positive_buyhold_cagr_multiple"]
    )
    drawdown_floor = float(protocol["maximum_drawdown_floor"])
    rows: list[dict[str, object]] = []
    for fraction in [float(value) for value in protocol["risk_budget_fractions"]]:
        sleeve_cash = total_cash * fraction
        reserve_cash = total_cash - sleeve_cash
        replay = replay_account(signals, replay_data, sleeve_cash)
        sleeve_metrics = calculate_metrics(replay, sleeve_cash)
        evidence = build_replay_evidence(
            signals, replay_data, replay, sleeve_cash, sleeve_metrics
        )
        audit = audit_replay(evidence)
        if audit.status is not AuditStatus.PASS:
            raise ValueError(
                f"SE replay audit failed for risk budget {fraction:.2f}: {audit.reason_codes}"
            )
        total_equity = replay.account_daily["equity"].astype(float) + reserve_cash
        metrics = _portfolio_metrics(total_equity, total_cash)
        return_pass = metrics["cagr"] >= required_cagr
        drawdown_pass = metrics["max_drawdown"] >= drawdown_floor
        rows.append(
            {
                "risk_budget_fraction": fraction,
                "strategy_sleeve_cash": sleeve_cash,
                "cash_reserve": reserve_cash,
                **metrics,
                "closed_trades": int(sleeve_metrics["closed_trades"]),
                "return_gate": "PASS" if return_pass else "FAIL",
                "drawdown_gate": "PASS" if drawdown_pass else "FAIL",
                "primary_gate": "PASS" if return_pass and drawdown_pass else "FAIL",
                "se_sleeve_replay_audit": audit.status.value,
            }
        )
    landscape = pd.DataFrame(rows)
    feasible = landscape.loc[landscape["primary_gate"].eq("PASS")].copy()
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "strategy_reference": snapshot.identity.reference,
        "data_fingerprint": replay_data.fingerprint,
        "required_strategy_cagr": required_cagr,
        "maximum_drawdown_floor": drawdown_floor,
        "scenario_count": len(landscape),
        "feasible_scenario_count": len(feasible),
        "feasible_risk_budget_fractions": feasible["risk_budget_fraction"].tolist(),
        "finding": "FEASIBLE_RISK_BUDGET_REGION" if len(feasible) >= 2 else "NO_STABLE_REGION",
        "route_decision": (
            "PROCEED_TO_RISK_BUDGET_POLICY_SELECTION"
            if len(feasible) >= 2
            else "RETURN_TO_SIGNAL_MECHANISM_RESEARCH"
        ),
        "candidate_created": False,
    }
    landscape.to_csv(artifacts / "risk_budget_landscape.csv", index=False, lineterminator="\n")
    _write(artifacts / "risk_budget_summary.json", result)
    (experiment / "03_execution.md").write_text(
        "# S001 EX06 执行\n\n"
        f"状态：`COMPLETE`。完成 {len(landscape)} 个风险预算情景；"
        "各策略资金子账本均通过 SE 审计。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX06 结论\n\n"
        f"{len(landscape)} 个风险预算情景中有 {len(feasible)} 个同时满足收益门和 15% 最大回撤门。"
        f"可行比例为 {feasible['risk_budget_fraction'].tolist()}。"
        f"裁决：`{result['route_decision']}`。本轮不选择正式比例、不生成候选。\n",
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
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": result["route_decision"],
            "candidate_generation": False,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
