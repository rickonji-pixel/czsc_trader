from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX11"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _bars(repo: Path, symbol: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    code = symbol.split(".", maxsplit=1)[0]
    paths = sorted((repo / "data/raw").glob(f"{code}_1m_*.csv"))
    if not paths:
        raise FileNotFoundError(f"minute research data unavailable: {symbol}")
    frames = [pd.read_csv(path, parse_dates=["datetime"]) for path in paths]
    data = pd.concat(frames, ignore_index=True).sort_values("datetime")
    data = data.loc[data["datetime"].dt.normalize() <= cutoff].copy()
    data["date"] = data["datetime"].dt.normalize()
    data["clock"] = data["datetime"].dt.strftime("%H:%M")
    if not data.groupby("date").size().eq(240).all():
        raise ValueError("incomplete minute sessions")
    return data


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    return gains / losses if losses else (float("inf") if gains else 0.0)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")

    source_dir = repo / "experiments/S005" / str(protocol["source_experiment"])
    validate_experiment_archive(source_dir)
    selection = _read(source_dir / "artifacts/selection_evidence.json")
    if selection["selected_mechanism"] != protocol["mechanism"]:
        raise ValueError("selected mechanism differs from protocol")

    ledger = pd.read_csv(source_dir / "artifacts/event_ledger.csv.gz", parse_dates=["date"])
    event_column = f"{protocol['mechanism']}__accepted"
    event_dates = pd.DatetimeIndex(ledger.loc[ledger[event_column].eq(1), "date"])
    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    start = pd.Timestamp(str(protocol["development_start"]))
    bars = _bars(repo, str(protocol["symbol"]), cutoff)
    calendar = pd.DatetimeIndex(sorted(bars.loc[bars["date"].ge(start), "date"].unique()))
    positions = {date: position for position, date in enumerate(calendar)}

    execution = protocol["execution"]
    entry_clock = str(execution["entry_clock"])
    exit_clock = str(execution["exit_clock"])
    prices = {
        (clock, date): float(group.loc[group["clock"].eq(clock), "open"].iloc[0])
        for date, group in bars.groupby("date")
        for clock in (entry_clock, exit_clock)
    }
    cost = float(execution["stress_one_way_cost"])
    rows: list[dict[str, object]] = []
    for signal_date in event_dates:
        position = positions.get(signal_date)
        exit_offset = int(execution["exit_offset_sessions"])
        if position is None or position + exit_offset >= len(calendar):
            continue
        entry_date = calendar[position + int(execution["entry_offset_sessions"])]
        exit_date = calendar[position + exit_offset]
        entry = prices[(entry_clock, entry_date)]
        exit_ = prices[(exit_clock, exit_date)]
        rows.append(
            {
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_price": entry,
                "exit_price": exit_,
                "gross_return": exit_ / entry - 1.0,
                "stress_return": exit_ * (1.0 - cost) / (entry * (1.0 + cost)) - 1.0,
            }
        )

    trades = pd.DataFrame(rows)
    trades["entry_year"] = pd.to_datetime(trades["entry_date"]).dt.year
    annual = trades.groupby("entry_year", observed=True).agg(
        trades=("stress_return", "size"),
        stress_mean=("stress_return", "mean"),
        total_return=("stress_return", lambda x: float((1.0 + x).prod() - 1.0)),
    )
    account_returns = pd.Series(0.0, index=calendar)
    account_returns.loc[pd.DatetimeIndex(trades["exit_date"])] = trades["stress_return"].to_numpy()
    equity = (1.0 + account_returns).cumprod()
    recent_start = calendar[-int(protocol["acceptance"]["recent_sessions"])]
    recent = trades.loc[pd.to_datetime(trades["entry_date"]) >= recent_start, "stress_return"]
    metrics = {
        "closed_trades": int(len(trades)),
        "terminal_signals_skipped": int(len(event_dates) - len(trades)),
        "gross_mean": float(trades["gross_return"].mean()),
        "stress_mean": float(trades["stress_return"].mean()),
        "stress_profit_factor": _profit_factor(trades["stress_return"]),
        "positive_years": int(annual["stress_mean"].gt(0).sum()),
        "recent_trades": int(len(recent)),
        "recent_stress_mean": float(recent.mean()),
        "proxy_cagr": float(equity.iloc[-1] ** (252.0 / len(account_returns)) - 1.0),
        "proxy_maximum_drawdown": float((equity / equity.cummax() - 1.0).min()),
    }
    acceptance = protocol["acceptance"]
    checks = {
        "stress_mean_budget": metrics["stress_mean"] >= float(acceptance["stress_mean_minimum"]),
        "profit_factor": metrics["stress_profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
        "positive_years": metrics["positive_years"] >= int(acceptance["positive_years_minimum"]),
        "recent": metrics["recent_stress_mean"] > float(acceptance["recent_stress_mean_min_exclusive"]),
        "proxy_cagr": metrics["proxy_cagr"] >= float(acceptance["proxy_cagr_minimum"]),
        "proxy_maximum_drawdown": metrics["proxy_maximum_drawdown"] >= float(acceptance["proxy_maximum_drawdown_limit"]),
    }
    decision = "PROCEED_TO_MECHANISM_ATTRIBUTION" if all(checks.values()) else "STOP_VOLATILITY_IGNITION_ON_RETURN"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "mechanism": protocol["mechanism"],
        "metrics": metrics,
        "checks": checks,
        "decision": decision,
        "cumulative_return_path_count": int(protocol["cumulative_return_path_count"]),
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    trades.to_csv(artifacts / "trades.csv.gz", index=False, compression=compression, lineterminator="\n")
    annual.reset_index().to_csv(artifacts / "annual_metrics.csv", index=False, lineterminator="\n")
    (artifacts / "return_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX11 执行\n\n状态：COMPLETE。已完成唯一三交易日固定路径复算。\n", encoding="utf-8"
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S005 EX11 结论\n\n闭合交易{metrics['closed_trades']}笔；压力单笔均值{metrics['stress_mean']:.3%}、"
        f"盈亏比{metrics['stress_profit_factor']:.2f}、正收益年度{metrics['positive_years']}/6、"
        f"最近252日均值{metrics['recent_stress_mean']:.3%}；代理年化{metrics['proxy_cagr']:.2%}、"
        f"最大回撤{metrics['proxy_maximum_drawdown']:.2%}。\n\n裁决：`{decision}`。"
        "本轮不生成候选，不修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
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
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
