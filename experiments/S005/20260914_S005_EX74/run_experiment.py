from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX74"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    return gains / losses if losses else (float("inf") if gains else 0.0)


def _fit_signal_scores(
    primary: pd.DataFrame,
    state_rows: pd.DataFrame,
    returns: pd.Series,
    train_mask: pd.Series,
    apply_index: pd.DatetimeIndex,
    prior_count: float,
) -> tuple[pd.DataFrame, float, list[dict[str, object]]]:
    baseline = float(returns.loc[train_mask].mean())
    output: dict[str, pd.Series] = {}
    edge_rows: list[dict[str, object]] = []
    for signal_id, states in state_rows.groupby("signal_id", sort=True):
        mapping: dict[str, float] = {}
        training_states = primary.loc[train_mask, signal_id]
        for row in states.itertuples(index=False):
            active = training_states.eq(row.state_primary)
            count = int(active.sum())
            raw_edge = float(returns.loc[train_mask].loc[active].mean() - baseline) if count else 0.0
            shrinkage = count / (count + prior_count) if count else 0.0
            edge = raw_edge * shrinkage
            mapping[str(row.state_primary)] = edge
            edge_rows.append({
                "signal_id": signal_id,
                "state_primary": row.state_primary,
                "training_occurrences": count,
                "raw_edge": raw_edge,
                "shrinkage": shrinkage,
                "shrunk_edge": edge,
            })
        output[str(signal_id)] = primary.loc[apply_index, signal_id].map(mapping).fillna(0.0).astype(float)
    return pd.DataFrame(output, index=apply_index), baseline, edge_rows


def _make_trades(
    decisions: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    opens: pd.Series,
    hold: int,
    cost: float,
) -> pd.DataFrame:
    positions = {session: index for index, session in enumerate(calendar)}
    rows: list[dict[str, object]] = []
    last_exit_index = -1
    for row in decisions.loc[decisions["trigger"]].sort_index().itertuples():
        signal_date = pd.Timestamp(row.Index)
        signal_index = positions[signal_date]
        entry_index = signal_index + 1
        exit_index = entry_index + hold
        if signal_index < last_exit_index or exit_index >= len(calendar):
            continue
        entry_date = calendar[entry_index]
        exit_date = calendar[exit_index]
        entry_price = float(opens.loc[entry_date])
        exit_price = float(opens.loc[exit_date])
        fraction = float(row.position_fraction)
        asset_stress = exit_price * (1.0 - cost) / (entry_price * (1.0 + cost)) - 1.0
        rows.append({
            "signal_date": signal_date,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "predicted_return": float(row.predicted_return),
            "position_fraction": fraction,
            "industry_score": float(row.industry_score),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_return": exit_price / entry_price - 1.0,
            "asset_stress_return": asset_stress,
            "account_trade_return": fraction * asset_stress,
        })
        last_exit_index = exit_index
    return pd.DataFrame(rows)


def _account(calendar: pd.DatetimeIndex, opens: pd.Series, trades: pd.DataFrame, cost: float) -> pd.DataFrame:
    position = pd.Series(0.0, index=calendar)
    for row in trades.itertuples(index=False):
        position.loc[(position.index >= row.entry_date) & (position.index < row.exit_date)] = row.position_fraction
    open_return = opens.pct_change().fillna(0.0)
    daily_factor = 1.0 + position.shift(1, fill_value=0.0) * open_return
    delta = position.diff().fillna(position.iloc[0])
    for session in calendar[delta > 0]:
        fraction = float(delta.loc[session])
        daily_factor.loc[session] *= (1.0 - fraction) + fraction / (1.0 + cost)
    for session in calendar[delta < 0]:
        fraction = float(-delta.loc[session])
        daily_factor.loc[session] *= 1.0 - fraction * cost
    return pd.DataFrame({
        "date": calendar,
        "position": position.to_numpy(),
        "open": opens.to_numpy(),
        "daily_return": daily_factor.to_numpy() - 1.0,
        "equity": daily_factor.cumprod().to_numpy(),
    })


def _metrics(trades: pd.DataFrame, account: pd.DataFrame, calendar: pd.DatetimeIndex, recent_sessions: int) -> dict[str, object]:
    asset_values = trades["asset_stress_return"].astype(float)
    account_values = trades["account_trade_return"].astype(float)
    equity = account["equity"].astype(float)
    drawdown = equity.div(equity.cummax()).sub(1.0)
    annual = account.assign(year=pd.to_datetime(account["date"]).dt.year).groupby("year")["daily_return"].apply(
        lambda values: float((1.0 + values).prod() - 1.0)
    )
    recent_start = calendar[-recent_sessions]
    recent = trades.loc[pd.to_datetime(trades["entry_date"]) >= recent_start, "asset_stress_return"]
    entries = pd.Series(0, index=calendar, dtype=int)
    entries.loc[pd.to_datetime(trades["signal_date"])] = 1
    rolling = entries.rolling(60, min_periods=60).sum().dropna()
    maximum_drawdown = float(drawdown.min())
    cagr = float(equity.iloc[-1] ** (252.0 / len(equity)) - 1.0)
    return {
        "closed_trades": int(len(trades)),
        "asset_stress_mean": float(asset_values.mean()),
        "asset_stress_median": float(asset_values.median()),
        "account_trade_mean": float(account_values.mean()),
        "win_rate": float(asset_values.gt(0).mean()),
        "profit_factor": _profit_factor(asset_values),
        "cagr": cagr,
        "maximum_drawdown": maximum_drawdown,
        "calmar": cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else None,
        "positive_years": int(annual.gt(0).sum()),
        "annual_returns": {str(int(key)): float(value) for key, value in annual.items()},
        "recent_closed_trades": int(len(recent)),
        "recent_mean": float(recent.mean()) if len(recent) else None,
        "rolling_60_cycle_median": float(rolling.median()) if not rolling.empty else 0.0,
        "rolling_60_cycle_p10": float(rolling.quantile(0.1)) if not rolling.empty else 0.0,
        "average_position_fraction": float(trades["position_fraction"].mean()),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX74 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex73 = repo / "experiments/S005" / str(sources["architecture_experiment_id"])
    ex72 = repo / "experiments/S005" / str(sources["state_scores_experiment_id"])
    ex49 = repo / "experiments/S005" / str(sources["state_matrix_experiment_id"])
    ex69 = repo / "experiments/S005" / str(sources["industry_experiment_id"])
    for source in (ex73, ex72, ex49, ex69):
        validate_experiment_archive(source)
    frozen_files = (
        (ex73 / "experiment_manifest.json", sources["architecture_manifest_sha256"]),
        (ex73 / "artifacts/architecture_plan.json", sources["architecture_plan_sha256"]),
        (ex73 / "artifacts/selected_components.csv", sources["selected_components_sha256"]),
        (ex72 / "artifacts/frozen_state_scores.csv.gz", sources["frozen_state_scores_sha256"]),
        (ex49 / "artifacts/primary_states.csv.gz", sources["primary_states_sha256"]),
        (ex69 / "artifacts/observation_ledger.csv.gz", sources["industry_ledger_sha256"]),
    )
    for path, expected in frozen_files:
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")

    plan = _read(ex73 / "artifacts/architecture_plan.json")
    selected = pd.read_csv(ex73 / "artifacts/selected_components.csv")
    state_rows = pd.read_csv(ex72 / "artifacts/frozen_state_scores.csv.gz")
    selected_ids = set(map(str, selected["signal_id"]))
    state_rows = state_rows.loc[state_rows["signal_id"].astype(str).isin(selected_ids)].copy()
    opportunity_ids = list(map(str, plan["opportunity_core"]))
    volatility_id = str(plan["volatility_environment"])

    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(
        context, str(protocol["dataset"]["name"]), str(protocol["symbol"]), str(protocol["asset_type"]),
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    if replay.fingerprint != str(protocol["dataset"]["fingerprint"]):
        raise ValueError("research dataset differs from frozen EX74 protocol")
    prices = replay.adjusted.daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.set_index("dt").sort_index()
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    calendar = prices.index
    opens = prices["open"].astype(float)
    hold = int(protocol["execution"]["forward_open_intervals"])
    forward_return = opens.shift(-(hold + 1)).div(opens.shift(-1)).sub(1.0)
    outcome_exit_date = pd.Series(calendar, index=calendar).shift(-(hold + 1))

    primary = pd.read_csv(ex49 / "artifacts/primary_states.csv.gz")
    primary["dt"] = pd.to_datetime(primary["dt"]).dt.normalize()
    primary = primary.set_index("dt").reindex(calendar)
    industry = pd.read_csv(ex69 / "artifacts/observation_ledger.csv.gz")
    industry = industry.loc[industry["factor_id"].eq(plan["industry_confidence_modulator"])].copy()
    industry["trade_date"] = pd.to_datetime(industry["trade_date"]).dt.normalize()
    industry_score = industry.drop_duplicates("trade_date").set_index("trade_date")["score"].astype(float)
    industry_score = industry_score.reindex(calendar).fillna(0.5).clip(0.0, 1.0)

    evaluation_mask = calendar >= pd.Timestamp(protocol["evaluation_start"])
    evaluation_calendar = calendar[evaluation_mask]
    architecture_decisions = {
        name: pd.DataFrame(index=calendar, columns=["predicted_return", "trigger", "position_fraction", "industry_score"])
        for name in protocol["architectures"]
    }
    fold_rows: list[dict[str, object]] = []
    edge_rows: list[dict[str, object]] = []
    quarters = evaluation_calendar.to_period("Q").unique()
    for quarter in quarters:
        quarter_start = quarter.start_time.normalize()
        apply_index = calendar[(calendar.to_period("Q") == quarter) & evaluation_mask]
        train_mask = forward_return.notna() & outcome_exit_date.lt(quarter_start)
        if int(train_mask.sum()) < int(protocol["model"]["minimum_training_sessions"]):
            raise ValueError(f"{quarter}: insufficient training sessions")
        component_scores, baseline, fold_edges = _fit_signal_scores(
            primary, state_rows, forward_return, train_mask, apply_index,
            float(protocol["model"]["state_shrinkage_prior_count"]),
        )
        for row in fold_edges:
            row["quarter"] = str(quarter)
        edge_rows.extend(fold_edges)
        for architecture, rules in protocol["architectures"].items():
            ids = opportunity_ids + ([volatility_id] if bool(rules["use_volatility"]) else [])
            predicted = baseline + component_scores.loc[:, ids].mean(axis=1)
            trigger = predicted.gt(float(protocol["model"]["entry_expected_gross_return_threshold"]))
            fraction = (
                0.5 + 0.5 * industry_score.loc[apply_index]
                if bool(rules["use_industry"])
                else pd.Series(1.0, index=apply_index)
            )
            target = architecture_decisions[architecture]
            target.loc[apply_index, "predicted_return"] = predicted
            target.loc[apply_index, "trigger"] = trigger
            target.loc[apply_index, "position_fraction"] = fraction
            target.loc[apply_index, "industry_score"] = industry_score.loc[apply_index]
        fold_rows.append({
            "quarter": str(quarter),
            "training_sessions": int(train_mask.sum()),
            "baseline_forward_return": baseline,
            "apply_sessions": int(len(apply_index)),
        })

    result_rows: list[dict[str, object]] = []
    all_trades: list[pd.DataFrame] = []
    all_accounts: list[pd.DataFrame] = []
    all_decisions: list[pd.DataFrame] = []
    acceptance = protocol["acceptance"]
    cost = float(protocol["execution"]["stress_one_way_cost"])
    for architecture, rules in protocol["architectures"].items():
        decisions = architecture_decisions[architecture].loc[evaluation_calendar].copy()
        decisions["predicted_return"] = pd.to_numeric(decisions["predicted_return"], errors="coerce")
        decisions["trigger"] = decisions["trigger"].fillna(False).astype(bool)
        decisions["position_fraction"] = pd.to_numeric(decisions["position_fraction"], errors="coerce").fillna(1.0)
        decisions["industry_score"] = pd.to_numeric(decisions["industry_score"], errors="coerce").fillna(0.5)
        trades = _make_trades(decisions, calendar, opens, hold, cost)
        if trades.empty:
            raise ValueError(f"{architecture}: no executable closed trades")
        trades.insert(0, "architecture", architecture)
        account = _account(evaluation_calendar, opens.reindex(evaluation_calendar), trades, cost)
        metrics = _metrics(trades, account, evaluation_calendar, int(acceptance["recent_sessions"]))
        checks = {
            "mean_trade": metrics["asset_stress_mean"] >= float(acceptance["minimum_mean_trade"]),
            "cagr": metrics["cagr"] >= float(acceptance["minimum_cagr"]),
            "maximum_drawdown": metrics["maximum_drawdown"] >= float(acceptance["maximum_drawdown_floor"]),
            "profit_factor": metrics["profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
            "positive_years": metrics["positive_years"] >= int(acceptance["positive_years_minimum"]),
            "recent": metrics["recent_mean"] is not None and metrics["recent_mean"] > float(acceptance["recent_mean_min_exclusive"]),
            "density_median": metrics["rolling_60_cycle_median"] >= float(acceptance["rolling_60_cycle_median_minimum"]),
            "density_p10": metrics["rolling_60_cycle_p10"] >= float(acceptance["rolling_60_cycle_p10_minimum"]),
        }
        result_rows.append({"architecture": architecture, **metrics, "checks": checks, "passed": bool(all(checks.values()))})
        all_trades.append(trades)
        account.insert(0, "architecture", architecture)
        all_accounts.append(account)
        decision_output = decisions.reset_index(names="signal_date")
        decision_output.insert(0, "architecture", architecture)
        all_decisions.append(decision_output)

    trade_counts = {str(row["architecture"]): int(row["closed_trades"]) for row in result_rows}
    industry_count_checks = {
        "core_pair": trade_counts["OPPORTUNITY_CORE"] == trade_counts["OPPORTUNITY_PLUS_INDUSTRY"],
        "volatility_pair": trade_counts["OPPORTUNITY_PLUS_VOLATILITY"]
        == trade_counts["OPPORTUNITY_PLUS_VOLATILITY_AND_INDUSTRY"],
    }
    if not all(industry_count_checks.values()):
        raise ValueError("industry confidence modulator changed core trigger count")
    passing = [str(row["architecture"]) for row in result_rows if row["passed"]]
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "evaluation_start": evaluation_calendar[0].strftime("%Y-%m-%d"),
        "evaluation_end": evaluation_calendar[-1].strftime("%Y-%m-%d"),
        "dataset_fingerprint": replay.fingerprint,
        "architectures": result_rows,
        "passing_architectures": passing,
        "industry_trigger_count_invariant": all(industry_count_checks.values()),
        "fold_count": int(len(fold_rows)),
        "return_path_count": protocol["return_path_count"],
        "prior_cumulative_return_path_count": protocol["prior_cumulative_return_path_count"],
        "cumulative_return_path_count": protocol["cumulative_return_path_count"],
        "selection_bias_note": "components were selected using the development pool; all 624 paths remain subject to SE search penalty",
        "decision": "PROCEED_TO_SE_FEASIBILITY_AUDIT" if passing else "STOP_SELECTED_ARCHITECTURE_ON_COMPLETE_STRATEGY_GATES",
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "strategy_evidence.json", evidence)

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    pd.concat(all_trades, ignore_index=True).to_csv(artifacts / "trades.csv.gz", index=False, encoding="utf-8", compression=compression, lineterminator="\n")
    pd.concat(all_accounts, ignore_index=True).to_csv(artifacts / "accounts.csv.gz", index=False, encoding="utf-8", compression=compression, lineterminator="\n")
    pd.concat(all_decisions, ignore_index=True).to_csv(artifacts / "decisions.csv.gz", index=False, encoding="utf-8", compression=compression, lineterminator="\n")
    pd.DataFrame(fold_rows).to_csv(artifacts / "folds.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    pd.DataFrame(edge_rows).to_csv(artifacts / "state_edges.csv.gz", index=False, encoding="utf-8", compression=compression, lineterminator="\n")
    pd.DataFrame([{key: value for key, value in row.items() if key != "checks"} for row in result_rows]).to_csv(
        artifacts / "architecture_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )

    (experiment / "03_execution.md").write_text(
        "# S005 EX74 执行\n\n"
        f"状态：`COMPLETE`。四条完整策略路径完成{len(fold_rows)}个季度的前向滚动，行业调节路径"
        "保持交易触发次数不变。四条路径均纳入完整策略频率与风险收益硬门。\n",
        encoding="utf-8",
    )
    lines = ["|路径|交易|60日中位/P10|年化|最大回撤|单笔均值|盈亏因子|通过|", "|---|---:|---:|---:|---:|---:|---:|---|"]
    for row in result_rows:
        lines.append(
            f"|{row['architecture']}|{row['closed_trades']}|{row['rolling_60_cycle_median']:.1f}/{row['rolling_60_cycle_p10']:.1f}|"
            f"{row['cagr']:.2%}|{row['maximum_drawdown']:.2%}|{row['asset_stress_mean']:.3%}|{row['profit_factor']:.2f}|"
            f"{'是' if row['passed'] else '否'}|"
        )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX74 结论\n\n" + "\n".join(lines) + "\n\n"
        f"裁决：`{evidence['decision']}`。通过路径：{('、'.join(passing) if passing else '无')}。"
        "频率门从本轮开始正式生效；组件层此前的频率只保留为诊断。任何通过结果仍需接受SE对"
        "620条既有路径和本轮4条路径的搜索惩罚。\n",
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
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
