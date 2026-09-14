from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX56"


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


def _win_loss_ratio(values: pd.Series) -> float | None:
    wins = values.loc[values > 0]
    losses = values.loc[values < 0]
    if wins.empty or losses.empty:
        return None
    return float(wins.mean() / abs(losses.mean()))


def _account(calendar: pd.DatetimeIndex, opens: pd.Series, trades: pd.DataFrame, cost: float) -> pd.DataFrame:
    position = pd.Series(0.0, index=calendar)
    for row in trades.itertuples(index=False):
        position.loc[(position.index >= row.entry_date) & (position.index < row.exit_date)] = 1.0
    open_return = opens.pct_change().fillna(0.0)
    held_return = position.shift(1, fill_value=0.0) * open_return
    delta = position.diff().fillna(position.iloc[0])
    transaction_factor = pd.Series(1.0, index=calendar)
    transaction_factor.loc[delta > 0] = 1.0 / (1.0 + cost)
    transaction_factor.loc[delta < 0] = 1.0 - cost
    daily_factor = (1.0 + held_return) * transaction_factor
    equity = daily_factor.cumprod()
    return pd.DataFrame({"date": calendar, "position": position.to_numpy(), "open": opens.to_numpy(), "daily_return": daily_factor.to_numpy() - 1.0, "equity": equity.to_numpy()})


def _metrics(trades: pd.DataFrame, account: pd.DataFrame, recent_start: pd.Timestamp) -> dict[str, object]:
    values = trades["stress_return"].astype(float)
    equity = account["equity"].astype(float)
    drawdown = equity.div(equity.cummax()).sub(1.0)
    cagr = float(equity.iloc[-1] ** (252.0 / len(equity)) - 1.0)
    annual = trades.groupby(pd.to_datetime(trades["entry_date"]).dt.year)["stress_return"].mean()
    recent = trades.loc[pd.to_datetime(trades["entry_date"]) >= recent_start, "stress_return"]
    maximum_drawdown = float(drawdown.min())
    return {
        "closed_trades": int(len(trades)),
        "stress_mean": float(values.mean()),
        "stress_median": float(values.median()),
        "win_rate": float(values.gt(0).mean()),
        "profit_factor": _profit_factor(values),
        "win_loss_ratio": _win_loss_ratio(values),
        "positive_years": int(annual.gt(0).sum()),
        "observed_years": int(len(annual)),
        "recent_trades": int(len(recent)),
        "recent_mean": float(recent.mean()),
        "total_return": float(equity.iloc[-1] - 1.0),
        "cagr": cagr,
        "maximum_drawdown": maximum_drawdown,
        "calmar": cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else None,
        "exposure": float(account["position"].mean()),
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
        raise ValueError("EX56 may compare frozen return paths but cannot create, promote, or deploy a candidate")

    source = repo / "experiments/S005" / str(protocol["source"]["experiment_id"])
    validate_experiment_archive(source)
    for path, expected in (
        (source / "experiment_manifest.json", protocol["source"]["manifest_sha256"]),
        (source / "artifacts/cycles.csv", protocol["source"]["cycles_sha256"]),
        (source / "artifacts/density_evidence.json", protocol["source"]["density_evidence_sha256"]),
    ):
        if _sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")
    density = _read(source / "artifacts/density_evidence.json")
    if set(protocol["hypotheses"]) - set(density["density_passing_hypotheses"]):
        raise ValueError("return comparison includes a hypothesis that failed the density gate")

    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(context, str(protocol["dataset"]["name"]), str(protocol["symbol"]), str(protocol["asset_type"]), date.fromisoformat(str(protocol["development_cutoff"])))
    if replay.fingerprint != protocol["dataset"]["fingerprint"]:
        raise ValueError("research dataset differs from the frozen EX56 protocol")
    prices = replay.adjusted.daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.set_index("dt").sort_index()
    start = pd.Timestamp(protocol["evaluation_start"])
    cutoff = pd.Timestamp(protocol["development_cutoff"])
    calendar = prices.index[prices.index.to_series().between(start, cutoff)]
    opens = prices["open"].astype(float).reindex(calendar)
    next_session = {calendar[index]: calendar[index + 1] for index in range(len(calendar) - 1)}
    recent_start = calendar[-int(protocol["acceptance"]["recent_sessions"])]
    cost = float(protocol["execution"]["stress_one_way_cost"])

    source_cycles = pd.read_csv(source / "artifacts/cycles.csv", parse_dates=["entry_date", "exit_date"])
    result_rows: list[dict[str, object]] = []
    trade_frames: list[pd.DataFrame] = []
    account_frames: list[pd.DataFrame] = []
    annual_frames: list[pd.DataFrame] = []
    acceptance = protocol["acceptance"]
    for hypothesis in protocol["hypotheses"]:
        cycles = source_cycles.loc[source_cycles["hypothesis"].eq(hypothesis) & source_cycles["entry_date"].ge(start) & source_cycles["exit_date"].notna()].copy()
        rows: list[dict[str, object]] = []
        for row in cycles.itertuples(index=False):
            entry_date = next_session.get(pd.Timestamp(row.entry_date))
            exit_date = next_session.get(pd.Timestamp(row.exit_date))
            if entry_date is None or exit_date is None or exit_date <= entry_date:
                continue
            entry_price = float(opens.loc[entry_date])
            exit_price = float(opens.loc[exit_date])
            rows.append({
                "hypothesis": hypothesis, "entry_signal_date": row.entry_date, "exit_signal_date": row.exit_date,
                "entry_date": entry_date, "exit_date": exit_date, "entry_price": entry_price, "exit_price": exit_price,
                "gross_return": exit_price / entry_price - 1.0,
                "stress_return": exit_price * (1.0 - cost) / (entry_price * (1.0 + cost)) - 1.0,
            })
        trades = pd.DataFrame(rows)
        if trades.empty:
            raise ValueError(f"{hypothesis}: no executable closed trades")
        account = _account(calendar, opens, trades, cost)
        metrics = _metrics(trades, account, recent_start)
        checks = {
            "cagr": metrics["cagr"] >= float(acceptance["minimum_cagr"]),
            "maximum_drawdown": metrics["maximum_drawdown"] >= float(acceptance["maximum_drawdown_floor"]),
            "profit_factor": metrics["profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
            "positive_years": metrics["positive_years"] >= int(acceptance["positive_years_minimum"]),
            "recent": metrics["recent_mean"] > float(acceptance["recent_mean_min_exclusive"]),
        }
        result_rows.append({"hypothesis": hypothesis, **metrics, "passed": bool(all(checks.values())), "checks": checks})
        trade_frames.append(trades)
        account.insert(0, "hypothesis", hypothesis)
        account_frames.append(account)
        annual = trades.groupby(pd.to_datetime(trades["entry_date"]).dt.year, as_index=False).agg(trades=("stress_return", "size"), mean_return=("stress_return", "mean"), total_return=("stress_return", lambda values: float((1.0 + values).prod() - 1.0)))
        annual = annual.rename(columns={"entry_date": "year", "index": "year"})
        annual.insert(0, "hypothesis", hypothesis)
        annual_frames.append(annual)

    buyhold_trades = pd.DataFrame([{"entry_date": calendar[0], "exit_date": calendar[-1]}])
    buyhold_account = _account(calendar, opens, buyhold_trades, cost)
    buyhold_drawdown = buyhold_account["equity"].div(buyhold_account["equity"].cummax()).sub(1.0)
    buyhold = {
        "total_return": float(buyhold_account["equity"].iloc[-1] - 1.0),
        "cagr": float(buyhold_account["equity"].iloc[-1] ** (252.0 / len(buyhold_account)) - 1.0),
        "maximum_drawdown": float(buyhold_drawdown.min()),
    }
    buyhold["calmar"] = buyhold["cagr"] / abs(buyhold["maximum_drawdown"])
    passing = [str(row["hypothesis"]) for row in result_rows if row["passed"]]
    decision = "PROCEED_TO_FSC_MECHANISM_ATTRIBUTION" if passing else "STOP_FSC_COMBINATIONS_ON_RETURN"
    evidence = {
        "schema_version": 1, "experiment_id": EXPERIMENT_ID, "status": "COMPLETE",
        "evaluation_start": calendar[0].strftime("%Y-%m-%d"), "evaluation_end": calendar[-1].strftime("%Y-%m-%d"),
        "dataset_fingerprint": replay.fingerprint, "hypotheses": result_rows,
        "passing_hypotheses": passing, "buyhold": buyhold,
        "prior_cumulative_return_path_count": protocol["prior_cumulative_return_path_count"],
        "cumulative_return_path_count": protocol["cumulative_return_path_count"],
        "decision": decision, "candidate_created": False, "strategy_frozen": False, "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "return_evidence.json", evidence)
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    pd.concat(trade_frames, ignore_index=True).to_csv(artifacts / "trades.csv.gz", index=False, compression=compression, lineterminator="\n")
    pd.concat(account_frames, ignore_index=True).to_csv(artifacts / "account_daily.csv.gz", index=False, compression=compression, lineterminator="\n")
    pd.concat(annual_frames, ignore_index=True).to_csv(artifacts / "annual_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    table = ["|路径|交易|压力均值|盈亏因子|年化|最大回撤|卡玛|近252日均值|硬门|", "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in result_rows:
        table.append(f"|{row['hypothesis']}|{row['closed_trades']}|{row['stress_mean']:.3%}|{row['profit_factor']:.2f}|{row['cagr']:.2%}|{row['maximum_drawdown']:.2%}|{row['calmar']:.2f}|{row['recent_mean']:.3%}|{'PASS' if row['passed'] else 'FAIL'}|")
    (experiment / "03_execution.md").write_text("# S005 EX56 执行\n\n状态：`COMPLETE`。三条冻结路径采用共同窗口、次日开盘执行和单边6bp压力成本完成收益比较。\n\n" + "\n".join(table) + "\n", encoding="utf-8")
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX56 结论\n\n" + "\n".join(table) + "\n\n"
        f"同窗口BuyHold年化{buyhold['cagr']:.2%}、最大回撤{buyhold['maximum_drawdown']:.2%}、卡玛{buyhold['calmar']:.2f}。"
        f"裁决：`{decision}`；通过路径：{', '.join(passing) if passing else 'NONE'}。"
        "三条路径全部计入搜索审计；本轮只决定是否值得进入机制归因，不生成候选、不修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "experiment_type": protocol["experiment_type"],
        "strategy_id": "S005", "symbol": protocol["symbol"], "development_cutoff": protocol["development_cutoff"],
        "decision": decision, "candidate_created": False, "strategy_frozen": False, "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
