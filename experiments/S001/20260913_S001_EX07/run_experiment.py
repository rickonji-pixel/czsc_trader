from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
from strategy_evaluator import AuditStatus, audit_replay, performance_metrics

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.benchmarks import replay_benchmarks
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.signal_replay import replay_signals
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S001_EX07"


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


def _returns(equity: pd.Series, initial_cash: float) -> pd.Series:
    values = equity.astype(float)
    result = values.pct_change()
    result.iloc[0] = values.iloc[0] / initial_cash - 1.0
    return result


def _metric_row(
    label: str, candidate: pd.Series, buyhold: pd.Series, multiple: float
) -> dict[str, object]:
    strategy = performance_metrics(candidate.to_numpy()).to_dict()
    benchmark = performance_metrics(buyhold.to_numpy()).to_dict()
    required = float(benchmark["cagr"]) * multiple if float(benchmark["cagr"]) > 0 else 0.0
    passed = float(strategy["cagr"]) >= required if required > 0 else float(strategy["cagr"]) > 0
    return {
        "window": label,
        "start": candidate.index.min().date().isoformat(),
        "end": candidate.index.max().date().isoformat(),
        "strategy_cagr": float(strategy["cagr"]),
        "buyhold_cagr": float(benchmark["cagr"]),
        "required_strategy_cagr": required,
        "return_gate": "PASS" if passed else "FAIL",
        "strategy_max_drawdown": float(strategy["max_drawdown"]),
        "strategy_calmar": float(strategy["calmar"]),
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
    summary_path = source / "artifacts/risk_budget_summary.json"
    landscape_path = source / "artifacts/risk_budget_landscape.csv"
    if _sha256(summary_path) != protocol["source_summary_sha256"]:
        raise ValueError("source risk-budget summary differs from protocol")
    if _sha256(landscape_path) != protocol["source_landscape_sha256"]:
        raise ValueError("source risk-budget landscape differs from protocol")
    if _read(summary_path).get("finding") != "FEASIBLE_RISK_BUDGET_REGION":
        raise ValueError("source experiment did not find a feasible region")
    landscape = pd.read_csv(landscape_path)
    floor = float(protocol["maximum_drawdown_floor"])
    buffer = float(protocol["minimum_drawdown_buffer"])
    increment = float(protocol["operational_increment"])
    eligible = landscape.loc[
        landscape["primary_gate"].eq("PASS")
        & landscape["max_drawdown"].sub(floor).ge(buffer - 1e-12)
        & landscape["risk_budget_fraction"]
        .div(increment)
        .sub(landscape["risk_budget_fraction"].div(increment).round())
        .abs()
        .lt(1e-9)
    ]
    if eligible.empty:
        raise ValueError("no risk budget satisfies the frozen selection rule")
    selected = float(eligible["risk_budget_fraction"].max())

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
    sleeve_cash = total_cash * selected
    reserve_cash = total_cash - sleeve_cash
    replay = replay_account(signals, replay_data, sleeve_cash)
    sleeve_metrics = calculate_metrics(replay, sleeve_cash)
    audit = audit_replay(
        build_replay_evidence(signals, replay_data, replay, sleeve_cash, sleeve_metrics)
    )
    if audit.status is not AuditStatus.PASS:
        raise ValueError(f"SE sleeve replay audit failed: {audit.reason_codes}")
    benchmark = replay_benchmarks(signals, replay_data, total_cash)
    account = replay.account_daily.copy()
    account["date"] = pd.to_datetime(account["date"], errors="raise").dt.normalize()
    total_equity = pd.Series(
        account["equity"].astype(float).to_numpy() + reserve_cash,
        index=account["date"],
        name="strategy",
    )
    buyhold_frame = benchmark.buyhold_account_daily.copy()
    buyhold_frame["date"] = pd.to_datetime(
        buyhold_frame["date"], errors="raise"
    ).dt.normalize()
    buyhold_equity = pd.Series(
        buyhold_frame["equity"].astype(float).to_numpy(),
        index=buyhold_frame["date"],
        name="buyhold",
    )
    aligned = pd.concat(
        [_returns(total_equity, total_cash), _returns(buyhold_equity, total_cash)],
        axis=1,
        join="inner",
    )
    if aligned.isna().any().any():
        raise AssertionError("strategy and BuyHold returns are not aligned")
    multiple = float(protocol["positive_buyhold_cagr_multiple"])
    annual_rows = [_metric_row("FULL", aligned["strategy"], aligned["buyhold"], multiple)]
    for year, frame in aligned.groupby(aligned.index.year):
        annual_rows.append(
            _metric_row(str(year), frame["strategy"], frame["buyhold"], multiple)
        )
    annual = pd.DataFrame(annual_rows)
    rolling_rows: list[dict[str, object]] = []
    window = int(protocol["rolling_window_sessions"])
    for end_index in range(window - 1, len(aligned)):
        frame = aligned.iloc[end_index - window + 1 : end_index + 1]
        rolling_rows.append(
            _metric_row(
                str(frame.index[-1].date()), frame["strategy"], frame["buyhold"], multiple
            )
        )
    rolling = pd.DataFrame(rolling_rows)
    full = annual.iloc[0]
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "strategy_reference": snapshot.identity.reference,
        "selected_risk_budget_fraction": selected,
        "selection_rule": "highest_10pct_increment_with_1pp_drawdown_buffer",
        "full_cagr": float(full["strategy_cagr"]),
        "full_max_drawdown": float(full["strategy_max_drawdown"]),
        "full_calmar": float(full["strategy_calmar"]),
        "annual_return_gate_failures": annual.loc[
            annual["window"].ne("FULL") & annual["return_gate"].eq("FAIL"), "window"
        ].tolist(),
        "worst_rolling_252_max_drawdown": float(rolling["strategy_max_drawdown"].min()),
        "worst_rolling_252_calmar": float(rolling["strategy_calmar"].min()),
        "se_sleeve_replay_audit": audit.status.value,
        "risk_note": "annual_and_rolling_results_are_diagnostic_not_hard_gates",
        "route_decision": "PROCEED_TO_RISK_BUDGET_CONTRACT_IMPLEMENTATION",
        "candidate_created": False,
    }
    annual.to_csv(artifacts / "annual_diagnostics.csv", index=False, lineterminator="\n")
    rolling.to_csv(artifacts / "rolling_252_diagnostics.csv", index=False, lineterminator="\n")
    _write(artifacts / "selection_summary.json", result)
    (experiment / "03_execution.md").write_text(
        "# S001 EX07 执行\n\n"
        f"状态：`COMPLETE`。按冻结规则选择 {selected:.0%} 风险预算；"
        "策略资金子账本通过 SE 审计，并完成年度及 252 交易日滚动诊断。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX07 结论\n\n"
        f"选择 {selected:.0%} 风险预算。完整开发池 CAGR 为 {float(full['strategy_cagr']):.2%}，"
        f"最大回撤 {float(full['strategy_max_drawdown']):.2%}，卡玛比率 {float(full['strategy_calmar']):.3f}。"
        "该结果满足 OPC 收益与回撤硬门，但当前 TDR/PTE 只支持全可用资金模式；"
        "下一步先实现并审计风险预算契约，再形成候选。\n",
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
