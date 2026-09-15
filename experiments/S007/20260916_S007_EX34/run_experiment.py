from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from strategy_evaluator import (
    AuditIdentity,
    CandidateDescriptor,
    CandidateProfile,
    ChampionAuditRequest,
    ExecutionEvidence,
    ExecutionOrder,
    ExternalReplayEvidence,
    FactorEvent,
    MachineEvaluationCase,
    MachineEvaluationPolicy,
    MetricObservation,
    MetricStatus,
    ParameterPoint,
    ReturnMatrixEvidence,
    RiskLabel,
    StressScenarioResult,
    TrialRecord,
    evaluate_machine_eligibility,
    hash_audit_data,
    hash_candidate_pool,
    hash_execution_evidence,
    hash_return_matrix,
    performance_metrics,
)

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.causal_feature_gate_replay import build_causal_feature_gate_signals
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260916_S007_EX34"
SELECTED_TRIAL = "S007-EX27-DECOUPLED-ENTRY-BOUNDARY-02821"
EXTERNAL_START = pd.Timestamp("2024-01-02")


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


def _write_gzip_json(path: Path, value: object) -> None:
    raw = (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    with path.open("wb") as handle:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=handle,
            compresslevel=9,
            mtime=0,
        ) as compressed:
            compressed.write(raw)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _daily_returns(account: pd.DataFrame, initial_cash: float) -> pd.Series:
    values = account.copy()
    values["date"] = pd.to_datetime(values["date"], errors="raise").dt.normalize()
    equity = values.set_index("date").sort_index()["equity"].astype(float)
    output = equity.pct_change()
    output.iloc[0] = equity.iloc[0] / initial_cash - 1.0
    return output


def _profit_factor(trades: pd.DataFrame) -> tuple[float | None, MetricStatus]:
    if trades.empty:
        return None, MetricStatus.NO_CLOSED_TRADES
    returns = pd.to_numeric(trades["net_return"], errors="raise")
    wins = float(returns.clip(lower=0.0).sum())
    losses = float(-returns.clip(upper=0.0).sum())
    if wins == 0.0:
        return 0.0, MetricStatus.NO_WINS
    if losses == 0.0:
        return None, MetricStatus.NO_LOSSES
    return wins / losses, MetricStatus.VALID


def _observation(
    candidate_id: str,
    scenario_id: str,
    returns: pd.Series,
    total_return: float,
    trades: pd.DataFrame | None,
    *,
    tier: str,
) -> MetricObservation:
    performance = performance_metrics(returns.to_numpy(dtype=float))
    profit_factor, pf_status = (
        (None, MetricStatus.UNAVAILABLE) if trades is None else _profit_factor(trades)
    )
    return MetricObservation(
        candidate_id,
        "full",
        scenario_id,
        tier,
        performance.cagr,
        total_return,
        performance.max_drawdown,
        performance.calmar,
        MetricStatus.VALID,
        profit_factor,
        pf_status,
        0 if trades is None else len(trades),
    )


def _return_evidence(frame: pd.DataFrame) -> ReturnMatrixEvidence:
    raw = ReturnMatrixEvidence(
        tuple(pd.to_datetime(frame.index).strftime("%Y-%m-%d")),
        tuple(map(str, frame.columns)),
        tuple(tuple(float(value) for value in row) for row in frame.to_numpy(dtype=float)),
        "",
    )
    return replace(raw, content_hash=hash_return_matrix(raw))


def _execution_evidence(ex31: Path) -> ExecutionEvidence:
    decisions = pd.read_csv(ex31 / "decisions.csv")
    decisions = decisions.loc[decisions["factor_score"].notna()].reset_index(drop=True)
    orders = pd.read_csv(ex31 / "orders.csv")
    fills = pd.read_csv(ex31 / "fills.csv")
    fill_by_order = fills.set_index("order_id")
    targets = decisions["target_position"].astype(float)
    previous = targets.shift(1, fill_value=float(targets.iloc[0]))
    changed = decisions.loc[targets.ne(previous)].copy()
    changed["before"] = previous.loc[changed.index].to_numpy(dtype=float)
    events = tuple(
        FactorEvent(
            str(row.decision_id),
            str(row.signal_date),
            "Entry" if float(row.target_position) > float(row.before) else "Exit",
            float(row.before),
            float(row.target_position),
            float(row.factor_score),
        )
        for row in changed.itertuples(index=False)
    )
    execution_orders = []
    for row in orders.itertuples(index=False):
        fill = fill_by_order.loc[row.order_id]
        execution_orders.append(
            ExecutionOrder(
                str(row.signal_date),
                str(row.execution_date),
                str(row.side).title(),
                float(fill["price"]),
                float(fill["quantity"]),
                float(fill["fees"]),
                str(row.decision_id),
            )
        )
    evidence = ExecutionEvidence(
        tuple(decisions["signal_date"].astype(str)),
        tuple(targets),
        tuple(decisions["factor_score"].astype(float)),
        events,
        tuple(execution_orders),
        "",
    )
    return replace(evidence, content_hash=hash_execution_evidence(evidence))


def _adjusted_fill_replay(
    account: pd.DataFrame,
    fills: pd.DataFrame,
    initial_cash: float,
    *,
    fee_multiplier: float,
    slippage_bp: int,
) -> tuple[pd.Series, pd.DataFrame, float]:
    daily = account.copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.normalize()
    fills = fills.copy()
    fills["fill_time"] = pd.to_datetime(fills["fill_time"], errors="raise").dt.normalize()
    by_date = {key: value for key, value in fills.groupby("fill_time", sort=True)}
    cash = initial_cash
    quantity = 0.0
    entry_price: float | None = None
    entry_fee = 0.0
    trade_rows: list[dict[str, float]] = []
    equity: list[float] = []
    slip = slippage_bp / 10_000.0
    for row in daily.itertuples(index=False):
        for fill in by_date.get(row.date, pd.DataFrame()).itertuples(index=False):
            price = float(fill.price) * (1.0 + slip if fill.side == "BUY" else 1.0 - slip)
            fee = float(fill.fees) * fee_multiplier
            size = float(fill.quantity)
            if fill.side == "BUY":
                cash -= price * size + fee
                quantity += size
                entry_price = price
                entry_fee = fee
            else:
                cash += price * size - fee
                quantity -= size
                if entry_price is None:
                    raise ValueError("stress replay encountered sell without entry")
                cost = entry_price * size + entry_fee
                proceeds = price * size - fee
                trade_rows.append({"net_return": proceeds / cost - 1.0})
                entry_price = None
                entry_fee = 0.0
        equity.append(cash + quantity * float(row.close))
    values = pd.Series(equity, index=daily["date"], dtype=float)
    returns = values.pct_change()
    returns.iloc[0] = values.iloc[0] / initial_cash - 1.0
    return returns, pd.DataFrame(trade_rows), float(values.iloc[-1] / initial_cash - 1.0)


def _buyhold_stress(
    account: pd.DataFrame,
    open_prices: pd.Series,
    initial_cash: float,
    *,
    base_fee: float,
    fee_multiplier: float,
    slippage_bp: int,
) -> tuple[pd.Series, float]:
    daily = account.copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.normalize()
    open_prices.index = pd.to_datetime(open_prices.index, errors="raise").normalize()
    open_price = float(open_prices.loc[daily["date"].iloc[0]])
    effective = open_price * (1.0 + slippage_bp / 10_000.0) * (1.0 + base_fee * fee_multiplier)
    shares = initial_cash / effective
    values = daily["close"].astype(float) * shares
    returns = values.pct_change()
    returns.iloc[0] = values.iloc[0] / initial_cash - 1.0
    return pd.Series(returns.to_numpy(), index=daily["date"]), float(values.iloc[-1] / initial_cash - 1.0)


def _candidate_for_trial(candidate: dict[str, object], row: pd.Series) -> dict[str, object]:
    value = deepcopy(candidate)
    score = value["rule"]["score"]
    score["base_weights"] = {
        name.removeprefix("metric.weights."): float(row[name])
        for name in row.index
        if name.startswith("metric.weights.")
    }
    share = float(json.loads(str(row["params"]))["confirm_share_fraction"])
    score["confirmation_weights"] = {
        "micro_share_change_5d_lag1": share,
        "tsfresh__log_volume_change__mean__lb20": 1.0 - share,
    }
    score["entry_threshold"] = float(row["metric.entry_threshold"])
    score["exit_threshold"] = float(row["metric.exit_threshold"])
    score["confirmation_threshold"] = float(row["metric.confirmation_threshold"])
    return value


def _neighbor_points(
    repo: Path,
    protocol: dict[str, object],
    candidate: dict[str, object],
    candidate_observation: MetricObservation,
) -> tuple[ParameterPoint, ...]:
    feasible = pd.read_csv(repo / "experiments/S007/20260915_S007_EX27/artifacts/feasible_trials.csv")
    feasible = feasible.loc[feasible["feasible"].eq(True)].copy()
    selected = _read(repo / "experiments/S007/20260915_S007_EX28/artifacts/selected_configuration.json")
    names = tuple(selected["params"])
    center = np.asarray([float(selected["params"][name]) for name in names])
    feasible["distance"] = feasible["params"].map(
        lambda raw: float(np.abs(np.asarray([json.loads(raw)[name] for name in names]) - center).sum())
    )
    neighbors = feasible.loc[feasible["trial_id"].ne(SELECTED_TRIAL)].sort_values(
        ["distance", "trial_id"]
    ).drop_duplicates("behavior_hash").head(20)
    context = RepositoryContext.discover(repo)
    replay_data = load_replay_data(
        context, "research", str(protocol["symbol"]), "etf", date.fromisoformat(str(protocol["development_cutoff"]))
    )
    points = [
        ParameterPoint(
            str(protocol["candidate_id"]),
            str(selected["behavior_hash"]),
            tuple((name, float(selected["params"][name])) for name in names),
            True,
            1,
            (
                ("net_cagr", candidate_observation.net_cagr),
                ("max_drawdown", candidate_observation.max_drawdown),
                ("calmar", float(candidate_observation.calmar)),
                ("profit_factor", float(candidate_observation.profit_factor)),
            ),
        )
    ]
    rows = []
    for _, series in neighbors.iterrows():
        payload = _candidate_for_trial(candidate, series)
        payload_hash = canonical_json_sha256(payload)
        trial_id = str(series["trial_id"])
        snapshot = resolve_candidate_snapshot(context, trial_id, payload, payload_hash, "EX34/neighbor")
        signals = build_causal_feature_gate_signals(
            snapshot,
            replay_data,
            pd.Timestamp(protocol["evaluation_start"]),
            pd.Timestamp(protocol["development_cutoff"]),
            repo,
        )
        result = replay_account(signals, replay_data, float(protocol["initial_cash"]))
        metrics = calculate_metrics(result, float(protocol["initial_cash"]))
        returns = _daily_returns(result.account_daily, float(protocol["initial_cash"]))
        perf = performance_metrics(returns.to_numpy(dtype=float))
        params = json.loads(str(series["params"]))
        rows.append({
            "trial_id": trial_id,
            "distance": float(series["distance"]),
            "cagr": perf.cagr,
            "maximum_drawdown": perf.max_drawdown,
            "calmar": perf.calmar,
            "profit_factor": float(metrics["win_loss_ratio"]),
        })
        points.append(
            ParameterPoint(
                trial_id,
                str(series["behavior_hash"]),
                tuple((name, float(params[name])) for name in names),
                True,
                1 if bool(series["pareto"]) else 2,
                (
                    ("net_cagr", perf.cagr),
                    ("max_drawdown", perf.max_drawdown),
                    ("calmar", perf.calmar),
                    ("profit_factor", float(metrics["win_loss_ratio"])),
                ),
            )
        )
    pd.DataFrame(rows).to_csv(
        repo / "experiments/S007/20260916_S007_EX34/artifacts/neighbor_replays.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    return tuple(points)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("EX34 protocol identity differs")
    if any(protocol.get(key) for key in ("creates_strategy_version", "strategy_frozen", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX34 cannot freeze or mutate SM/PTE")
    for relative, expected in protocol["sources"].items():
        path = repo / relative
        if _sha256(path) != expected:
            raise ValueError(f"frozen evidence differs: {relative}")

    ex31 = repo / "experiments/S007/20260915_S007_EX31/artifacts"
    ex32 = repo / "experiments/S007/20260915_S007_EX32/artifacts"
    candidate = _read(repo / "experiments/S007/20260915_S007_EX31/candidate_payload.json")
    candidate_hash = canonical_json_sha256(candidate)
    initial_cash = float(protocol["initial_cash"])

    comparison = pd.read_csv(ex32 / "daily_return_matrix.csv")
    comparison["date"] = pd.to_datetime(comparison["date"], errors="raise").dt.normalize()
    comparison = comparison.set_index("date")
    candidate_returns = comparison[str(protocol["candidate_id"])]
    benchmark_returns = comparison[str(protocol["benchmark_id"])]
    candidate_account = pd.read_csv(ex31 / "account_daily.csv")
    candidate_trades = pd.read_csv(ex31 / "trades.csv")
    buyhold_account = pd.read_csv(ex32 / "buyhold_account_daily.csv")
    formal_candidate = _observation(
        str(protocol["candidate_id"]), "standard", candidate_returns,
        float(candidate_account["equity"].iloc[-1] / initial_cash - 1.0), candidate_trades,
        tier="formal_tdr",
    )
    formal_benchmark = _observation(
        str(protocol["benchmark_id"]), "standard", benchmark_returns,
        float(buyhold_account["equity"].iloc[-1] / initial_cash - 1.0), None,
        tier="formal_tdr",
    )

    search = pd.read_csv(
        repo / "experiments/S007/20260915_S007_EX29/artifacts/final_family_daily_return_matrix.csv.gz"
    )
    search["date"] = pd.to_datetime(search["date"], errors="raise").dt.normalize()
    search = search.set_index("date").loc[comparison.index]
    search = search.rename(columns={SELECTED_TRIAL: str(protocol["candidate_id"])})
    search_evidence = _return_evidence(search)
    comparison_evidence = _return_evidence(comparison)

    behavior_ledger = pd.read_csv(
        repo / "experiments/S007/20260915_S007_EX29/artifacts/final_family_behavior_ledger.csv"
    )
    feasible = pd.read_csv(repo / "experiments/S007/20260915_S007_EX27/artifacts/feasible_trials.csv")
    source = feasible.sort_values("trial_number").drop_duplicates("trial_id").set_index("trial_id")
    execution_hash = canonical_json_sha256(candidate["rule"]["execution"])
    descriptors = []
    trials = []
    for row in behavior_ledger.itertuples(index=False):
        candidate_id = str(protocol["candidate_id"]) if row.trial_id == SELECTED_TRIAL else str(row.trial_id)
        strategy_hash = candidate_hash if row.trial_id == SELECTED_TRIAL else str(source.at[row.trial_id, "strategy_hash"])
        descriptors.append(CandidateDescriptor(
            candidate_id, strategy_hash, execution_hash, False, str(row.behavior_hash),
            family="S007-DECOUPLED-ENTRY", generation_stage="EX27", parameter_group="EX27",
        ))
        trials.append(TrialRecord(candidate_id, candidate_id, strategy_hash, str(row.behavior_hash), "COMPLETE"))
    benchmark_hash = canonical_json_sha256({"benchmark": protocol["benchmark_id"], "execution": candidate["rule"]["execution"]})
    descriptors.append(CandidateDescriptor(str(protocol["benchmark_id"]), benchmark_hash, execution_hash, True, family="BUYHOLD"))
    trials.append(TrialRecord(str(protocol["benchmark_id"]), str(protocol["benchmark_id"]), benchmark_hash, "", "COMPLETE"))
    descriptors_tuple = tuple(descriptors)

    parameters = _neighbor_points(repo, protocol, candidate, formal_candidate)
    profile = CandidateProfile(
        str(protocol["candidate_id"]), True, True,
        (
            ("net_cagr", formal_candidate.net_cagr),
            ("max_drawdown", formal_candidate.max_drawdown),
            ("calmar", float(formal_candidate.calmar)),
            ("profit_factor", float(formal_candidate.profit_factor)),
        ),
        1,
        float(formal_candidate.calmar),
    )

    fills = pd.read_csv(ex31 / "fills.csv")
    context = RepositoryContext.discover(repo)
    replay_data = load_replay_data(
        context,
        "research",
        str(protocol["symbol"]),
        "etf",
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    daily_prices = replay_data.adjusted.daily.copy()
    daily_prices["dt"] = pd.to_datetime(daily_prices["dt"], errors="raise").dt.normalize()
    open_prices = daily_prices.set_index("dt")["open"].astype(float)
    scenarios = (
        ("fee_x2", 2.0, 0),
        ("slippage_15bp", 1.0, 15),
        ("slippage_30bp", 1.0, 30),
        ("slippage_50bp", 1.0, 50),
    )
    stress_results = []
    stress_rows = []
    for scenario_id, fee_multiplier, slippage_bp in scenarios:
        c_returns, c_trades, c_total = _adjusted_fill_replay(
            candidate_account, fills, initial_cash,
            fee_multiplier=fee_multiplier, slippage_bp=slippage_bp,
        )
        b_returns, b_total = _buyhold_stress(
            buyhold_account, open_prices, initial_cash, base_fee=0.001,
            fee_multiplier=fee_multiplier, slippage_bp=slippage_bp,
        )
        c_obs = _observation(str(protocol["candidate_id"]), scenario_id, c_returns, c_total, c_trades, tier="stress_replay")
        b_obs = _observation(str(protocol["benchmark_id"]), scenario_id, b_returns, b_total, None, tier="stress_replay")
        stress_results.append(StressScenarioResult(scenario_id, execution_hash, (c_obs, b_obs)))
        stress_rows.append({
            "scenario_id": scenario_id,
            "cagr": c_obs.net_cagr,
            "maximum_drawdown": c_obs.max_drawdown,
            "calmar": c_obs.calmar,
            "profit_factor": c_obs.profit_factor,
            "closed_trades": c_obs.closed_trades,
        })
    pd.DataFrame(stress_rows).to_csv(
        artifacts / "stress_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )

    external_daily = pd.read_csv(
        repo / "experiments/S007/20260915_S007_EX30/artifacts/588300_daily_hfq.csv.gz"
    )
    external_daily["date"] = pd.to_datetime(external_daily["date"], errors="raise").dt.normalize()
    external_daily = external_daily.set_index("date")
    external_panel = pd.read_csv(
        repo / "experiments/S007/20260915_S007_EX30/artifacts/588300_fixed_formula_panel.csv.gz"
    )
    external_panel["date"] = pd.to_datetime(external_panel["date"], errors="raise").dt.normalize()
    external_panel = external_panel.set_index("date")
    mask = external_daily.index >= EXTERNAL_START
    ext_prices = external_daily.loc[mask]
    ext_target = external_panel["execution_target"].reindex(ext_prices.index).fillna(0.0)
    cash, shares, previous = 1.0, 0.0, 0.0
    ext_values = []
    for flag, open_price, close_price in zip(ext_target, ext_prices["open"], ext_prices["close"], strict=True):
        if flag != previous:
            if flag == 1.0:
                shares, cash = cash / (float(open_price) * 1.001), 0.0
            else:
                cash, shares = shares * float(open_price) * 0.999, 0.0
            previous = float(flag)
        ext_values.append(cash + shares * float(close_price))
    ext_values = pd.Series(ext_values, index=ext_prices.index)
    ext_returns = ext_values.pct_change()
    ext_returns.iloc[0] = ext_values.iloc[0] - 1.0
    external = ExternalReplayEvidence(
        "S007-C001-FIXED-ON-588300", "588300.SH", str(protocol["candidate_id"]), candidate_hash,
        tuple(ext_returns.index.strftime("%Y-%m-%d")), tuple(ext_returns.astype(float)),
    )

    execution = _execution_evidence(ex31)
    identity = AuditIdentity(
        EXPERIMENT_ID,
        hash_candidate_pool(descriptors_tuple),
        str(protocol["development_cutoff"]),
        hash_audit_data(search_evidence, comparison_evidence),
        execution_hash,
        "opc-v3",
        "champion-audit-v1",
        73401,
    )
    request = ChampionAuditRequest(
        identity,
        str(protocol["candidate_id"]),
        str(protocol["benchmark_id"]),
        (),
        search_evidence,
        comparison_evidence,
        parameters,
        execution,
        (formal_candidate, formal_benchmark),
        (formal_candidate,),
        descriptors_tuple,
        tuple(trials),
        (profile,),
        tuple(stress_results),
        10_000,
        (21, 10, 42),
    )
    policy_data = protocol["policy"]
    policy = MachineEvaluationPolicy(
        str(policy_data["policy_id"]),
        str(policy_data["policy_version"]),
        (RiskLabel.FAVORABLE, RiskLabel.MIXED),
        float(policy_data["minimum_bootstrap_probability"]),
        int(policy_data["required_external_replays"]),
        float(policy_data["external_minimum_cagr"]),
        float(policy_data["external_max_drawdown_floor"]),
        float(policy_data["stress_minimum_cagr"]),
        float(policy_data["stress_max_drawdown_floor"]),
        float(policy_data["stress_minimum_calmar"]),
    )
    report = evaluate_machine_eligibility(
        MachineEvaluationCase(EXPERIMENT_ID, candidate_hash, policy, request, (external,))
    )
    _write(artifacts / "machine_evaluation_report.json", report.to_dict())
    _write_gzip_json(artifacts / "machine_evaluation_input.json.gz", MachineEvaluationCase(
        EXPERIMENT_ID, candidate_hash, policy, request, (external,)
    ).to_dict())
    checks = {item.check_id: item.status.value for item in report.checks}
    (experiment / "03_execution.md").write_text(
        "# S007 EX34 执行\n\n"
        f"状态：`COMPLETE`。SE从850个真实搜索行为、20个TDR参数邻域回放、"
        f"{len(execution.orders)}笔订单、四档成本压力和588300固定公式回放中自行计算体检。"
        f"各项结果：`{json.dumps(checks, ensure_ascii=False, sort_keys=True)}`。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX34 结论\n\n"
        f"SE机器裁决：`{report.machine_verdict.value}`；统计综合标签："
        f"`{None if report.risk_label is None else report.risk_label.value}`。"
        f"原因码：`{json.dumps(report.reason_codes, ensure_ascii=False)}`。\n\n"
        "该结论只决定候选是否具备人工冻结评审资格。本轮没有创建策略版本、冻结策略或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "candidate_id": protocol["candidate_id"],
            "candidate_hash": candidate_hash,
            "machine_verdict": report.machine_verdict.value,
            "risk_label": None if report.risk_label is None else report.risk_label.value,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
