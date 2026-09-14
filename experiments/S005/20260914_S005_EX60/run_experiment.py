from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260914_S005_EX60"


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


def _account(
    calendar: pd.DatetimeIndex,
    closes: pd.Series,
    trades: pd.DataFrame,
    cost: float,
) -> pd.DataFrame:
    entries = trades.set_index("entry_date")
    exits = trades.set_index("exit_date")
    cash = 1.0
    shares = 0.0
    rows: list[dict[str, object]] = []
    for session in calendar:
        if session in entries.index:
            if shares != 0.0:
                raise ValueError("overlapping entry while position is open")
            entry = entries.loc[session]
            if isinstance(entry, pd.DataFrame):
                raise ValueError("multiple entries on one session")
            shares = cash / (float(entry["entry_price"]) * (1.0 + cost))
            cash = 0.0
        if session in exits.index:
            if shares == 0.0:
                raise ValueError("exit without an open position")
            exit_row = exits.loc[session]
            if isinstance(exit_row, pd.DataFrame):
                raise ValueError("multiple exits on one session")
            cash = shares * float(exit_row["exit_price"]) * (1.0 - cost)
            shares = 0.0
        equity = cash + shares * float(closes.loc[session])
        rows.append({"date": session, "cash": cash, "shares": shares, "close": float(closes.loc[session]), "equity": equity})
    if shares != 0.0:
        raise ValueError("terminal account retains an open position")
    account = pd.DataFrame(rows)
    account["daily_return"] = account["equity"].pct_change().fillna(account["equity"].iloc[0] - 1.0)
    return account


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX60 cannot create, promote, or deploy a candidate")
    if _sha256(repo / "data/raw/588080_intraday_manifest.json") != protocol["intraday_manifest_sha256"]:
        raise ValueError("intraday research generation differs from frozen protocol")

    source = repo / "experiments/S005" / str(protocol["source"]["experiment_id"])
    validate_experiment_archive(source)
    for path, expected in (
        (source / "experiment_manifest.json", protocol["source"]["manifest_sha256"]),
        (source / "artifacts/event_ledger.csv", protocol["source"]["event_ledger_sha256"]),
        (source / "artifacts/selection_evidence.json", protocol["source"]["selection_evidence_sha256"]),
    ):
        if _sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")
    selection = _read(source / "artifacts/selection_evidence.json")
    mechanism = str(protocol["mechanism"])
    if selection.get("selected_mechanism") != mechanism or selection.get("decision") != "PROCEED_TO_FIXED_COORDINATED_DEMAND_RETURN_TEST":
        raise ValueError("mechanism lacks frozen return-test eligibility")

    intraday = load_intraday_research_data(repo / "data/raw", str(protocol["symbol"]))
    bars = intraday.frames["1m"].copy()
    bars["date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    bars = bars.loc[bars["date"] <= pd.Timestamp(protocol["development_cutoff"])]
    if not bars.groupby("date").size().eq(240).all():
        raise ValueError("incomplete minute sessions")
    calendar = pd.DatetimeIndex(sorted(bars["date"].unique()))
    positions = {session: index for index, session in enumerate(calendar)}
    execution = protocol["execution"]
    entry_clock = str(execution["entry_clock"])
    exit_clock = str(execution["exit_clock"])
    entry_prices = bars.loc[bars["clock"].eq(entry_clock)].set_index("date")["Open"].astype(float)
    exit_prices = bars.loc[bars["clock"].eq(exit_clock)].set_index("date")["Open"].astype(float)
    closes = bars.groupby("date", sort=True)["Close"].last().astype(float)

    events = pd.read_csv(source / "artifacts/event_ledger.csv", parse_dates=["event_date"])
    events = events.loc[events["mechanism"].eq(mechanism)].sort_values("event_date")
    rows: list[dict[str, object]] = []
    for event in events.itertuples(index=False):
        event_date = pd.Timestamp(event.event_date).normalize()
        position = positions.get(event_date)
        if position is None:
            continue
        entry_index = position + int(execution["entry_offset_sessions"])
        exit_index = position + int(execution["exit_offset_sessions"])
        if exit_index >= len(calendar):
            continue
        entry_date = calendar[entry_index]
        exit_date = calendar[exit_index]
        entry = float(entry_prices.loc[entry_date])
        exit_ = float(exit_prices.loc[exit_date])
        cost = float(execution["stress_one_way_cost"])
        rows.append({
            "mechanism": mechanism,
            "event_date": event_date,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "score": float(event.coordinated_demand_score),
            "threshold": float(event.threshold),
            "entry_price": entry,
            "exit_price": exit_,
            "gross_return": exit_ / entry - 1.0,
            "stress_return": exit_ * (1.0 - cost) / (entry * (1.0 + cost)) - 1.0,
        })
    trades = pd.DataFrame(rows)
    if trades.empty:
        raise ValueError("no executable closed trades")
    ordered = trades.sort_values("entry_date")
    if (ordered["entry_date"].iloc[1:].reset_index(drop=True) <= ordered["exit_date"].iloc[:-1].reset_index(drop=True)).any():
        raise ValueError("frozen event ledger creates overlapping positions")
    account_calendar = calendar[(calendar >= trades["entry_date"].min()) & (calendar <= trades["exit_date"].max())]
    account = _account(account_calendar, closes, trades, float(execution["stress_one_way_cost"]))
    trade_product = float((1.0 + trades["stress_return"]).prod())
    if abs(float(account["equity"].iloc[-1]) - trade_product) > 1e-12:
        raise AssertionError("account equity differs from compounded trade returns")

    trades["entry_year"] = pd.to_datetime(trades["entry_date"]).dt.year
    annual = trades.groupby("entry_year", observed=True).agg(
        trades=("stress_return", "size"),
        stress_mean=("stress_return", "mean"),
        total_return=("stress_return", lambda values: float((1.0 + values).prod() - 1.0)),
    ).reset_index()
    equity = account["equity"].astype(float)
    drawdown = equity.div(equity.cummax()).sub(1.0)
    recent_start = account_calendar[-int(protocol["acceptance"]["recent_sessions"])]
    recent = trades.loc[trades["entry_date"].ge(recent_start), "stress_return"]
    metrics = {
        "closed_trades": int(len(trades)),
        "terminal_events_skipped": int(len(events) - len(trades)),
        "gross_mean": float(trades["gross_return"].mean()),
        "stress_mean": float(trades["stress_return"].mean()),
        "profit_factor": _profit_factor(trades["stress_return"]),
        "win_rate": float(trades["stress_return"].gt(0).mean()),
        "positive_years": int(annual["stress_mean"].gt(0).sum()),
        "observed_years": int(len(annual)),
        "recent_trades": int(len(recent)),
        "recent_stress_mean": float(recent.mean()),
        "total_return": float(equity.iloc[-1] - 1.0),
        "cagr": float(equity.iloc[-1] ** (252.0 / len(account)) - 1.0),
        "maximum_drawdown": float(drawdown.min()),
    }
    metrics["calmar"] = metrics["cagr"] / abs(metrics["maximum_drawdown"]) if metrics["maximum_drawdown"] < 0 else None
    acceptance = protocol["acceptance"]
    checks = {
        "stress_mean": metrics["stress_mean"] >= float(acceptance["stress_mean_minimum"]),
        "profit_factor": metrics["profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
        "positive_years": metrics["positive_years"] >= int(acceptance["positive_years_minimum"]),
        "recent": metrics["recent_stress_mean"] > float(acceptance["recent_stress_mean_min_exclusive"]),
        "cagr": metrics["cagr"] >= float(acceptance["cagr_minimum"]),
        "maximum_drawdown": metrics["maximum_drawdown"] >= float(acceptance["maximum_drawdown_floor"]),
    }
    passed = bool(all(checks.values()))
    decision = "PROCEED_TO_COORDINATED_DEMAND_ATTRIBUTION" if passed else "STOP_COORDINATED_DEMAND_ON_RETURN"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "mechanism": mechanism,
        "metrics": metrics,
        "checks": checks,
        "passed": passed,
        "prior_cumulative_return_path_count": protocol["prior_cumulative_return_path_count"],
        "cumulative_return_path_count": protocol["cumulative_return_path_count"],
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    trades.to_csv(artifacts / "trades.csv.gz", index=False, compression=compression, lineterminator="\n")
    account.to_csv(artifacts / "account_daily.csv.gz", index=False, compression=compression, lineterminator="\n")
    annual.to_csv(artifacts / "annual_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(artifacts / "return_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S005 EX60 执行\n\n状态：`COMPLETE`。唯一预注册协同需求路径完成收益与逐日盯市风险检验。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX60 结论\n\n"
        "|交易|压力均值|盈亏因子|胜率|正收益年|最近均值|年化|最大回撤|卡玛|硬门|\n"
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|\n"
        f"|{metrics['closed_trades']}|{metrics['stress_mean']:.3%}|{metrics['profit_factor']:.2f}|{metrics['win_rate']:.1%}|"
        f"{metrics['positive_years']}/{metrics['observed_years']}|{metrics['recent_stress_mean']:.3%}|{metrics['cagr']:.2%}|"
        f"{metrics['maximum_drawdown']:.2%}|{metrics['calmar']:.2f}|{'PASS' if passed else 'FAIL'}|\n\n"
        f"裁决：`{decision}`。累计收益路径增至{protocol['cumulative_return_path_count']}条。"
        "本轮不生成候选，不修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": "S005",
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
