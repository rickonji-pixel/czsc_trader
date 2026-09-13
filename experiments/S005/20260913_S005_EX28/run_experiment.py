from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX28"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _cooldown(events: pd.Series, sessions: int) -> pd.Series:
    accepted = pd.Series(False, index=events.index)
    last = -sessions - 1
    for position, active in enumerate(events.fillna(False).to_numpy(dtype=bool)):
        if active and position - last > sessions:
            accepted.iloc[position] = True
            last = position
    return accepted


def _bars(repo: Path, symbol: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    code = symbol.split(".", maxsplit=1)[0]
    frames = [
        pd.read_csv(path, parse_dates=["datetime"])
        for path in sorted((repo / "data/raw").glob(f"{code}_1m_*.csv"))
    ]
    if not frames:
        raise FileNotFoundError(f"minute research data unavailable: {symbol}")
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
    source_spec = protocol["source"]
    source = repo / "experiments/S005" / str(source_spec["experiment_id"])
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["manifest_sha256"],
        source / "artifacts/share_flow_feature_ledger.csv.gz": source_spec["ledger_sha256"],
        source / "artifacts/selection_evidence.json": source_spec["selection_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    selection = _read(source / "artifacts/selection_evidence.json")
    if any(subtype not in selection["density_passing_paths"] for subtype in protocol["subtypes"]):
        raise ValueError("one or more subtypes did not pass the frozen density gate")

    ledger = pd.read_csv(
        source / "artifacts/share_flow_feature_ledger.csv.gz", parse_dates=["trade_date"]
    ).set_index("trade_date")
    dip_creation = (
        ledger["share_change"].gt(0)
        & ledger["share_change"].ge(ledger["creation_threshold_q70"])
        & ledger["etf_return"].le(0)
    )
    rally_redemption = (
        ledger["share_change"].lt(0)
        & ledger["share_change"].le(ledger["redemption_threshold_q70"])
        & ledger["etf_return"].ge(0)
    )
    raw = dip_creation | rally_redemption
    accepted = _cooldown(raw, int(protocol["density"]["cooldown_sessions"]))
    first_ready = ledger["creation_threshold_q70"].dropna().index.min()
    rolling = (
        accepted.loc[accepted.index >= first_ready]
        .astype(int)
        .rolling(
            int(protocol["density"]["rolling_window_sessions"]),
            min_periods=int(protocol["density"]["rolling_window_sessions"]),
        )
        .sum()
        .dropna()
    )
    density_median = float(rolling.median())
    density_p10 = float(rolling.quantile(0.1))
    density_pass = bool(
        density_median >= float(protocol["density"]["minimum_median"])
        and density_p10 >= float(protocol["density"]["minimum_p10"])
    )
    if not density_pass:
        raise ValueError("combined mechanism failed its preregistered density gate")
    event_ledger = ledger[["share_change", "etf_return"]].copy()
    event_ledger["subtype"] = ""
    event_ledger.loc[dip_creation, "subtype"] = "DIP_CREATION"
    event_ledger.loc[rally_redemption, "subtype"] = "RALLY_REDEMPTION"
    event_ledger[protocol["mechanism"]] = accepted

    cutoff = pd.Timestamp(protocol["development_cutoff"])
    start = pd.Timestamp(protocol["development_start"])
    bars = _bars(repo, str(protocol["symbol"]), cutoff)
    calendar = pd.DatetimeIndex(sorted(bars.loc[bars["date"].ge(start), "date"].unique()))
    positions = {date: position for position, date in enumerate(calendar)}
    execution = protocol["execution"]
    clocks = (str(execution["entry_clock"]), str(execution["exit_clock"]))
    prices = {
        (clock, date): float(group.loc[group["clock"].eq(clock), "open"].iloc[0])
        for date, group in bars.groupby("date")
        for clock in clocks
    }
    rows: list[dict[str, object]] = []
    cost = float(execution["stress_one_way_cost"])
    signal_dates = pd.DatetimeIndex(event_ledger.index[accepted])
    for signal_date in signal_dates:
        position = positions.get(signal_date)
        if position is None or position + int(execution["exit_offset_sessions"]) >= len(calendar):
            continue
        entry_date = calendar[position + int(execution["entry_offset_sessions"])]
        exit_date = calendar[position + int(execution["exit_offset_sessions"])]
        entry = prices[(clocks[0], entry_date)]
        exit_ = prices[(clocks[1], exit_date)]
        rows.append(
            {
                "mechanism": protocol["mechanism"],
                "subtype": event_ledger.loc[signal_date, "subtype"],
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
        total_return=("stress_return", lambda values: float((1.0 + values).prod() - 1.0)),
    )
    subtype = trades.groupby("subtype", observed=True).agg(
        trades=("stress_return", "size"),
        stress_mean=("stress_return", "mean"),
        profit_factor=("stress_return", _profit_factor),
    )
    account_returns = pd.Series(0.0, index=calendar)
    account_returns.loc[pd.DatetimeIndex(trades["exit_date"])] = trades[
        "stress_return"
    ].to_numpy()
    equity = (1.0 + account_returns).cumprod()
    acceptance = protocol["acceptance"]
    recent_start = calendar[-int(acceptance["recent_sessions"])]
    recent = trades.loc[pd.to_datetime(trades["entry_date"]) >= recent_start, "stress_return"]
    metrics = {
        "closed_trades": int(len(trades)),
        "terminal_signals_skipped": int(len(signal_dates) - len(trades)),
        "density_rolling_60_median": density_median,
        "density_rolling_60_p10": density_p10,
        "gross_mean": float(trades["gross_return"].mean()),
        "stress_mean": float(trades["stress_return"].mean()),
        "stress_profit_factor": _profit_factor(trades["stress_return"]),
        "positive_years": int(annual["stress_mean"].gt(0).sum()),
        "recent_trades": int(len(recent)),
        "recent_stress_mean": float(recent.mean()),
        "proxy_cagr": float(equity.iloc[-1] ** (252.0 / len(account_returns)) - 1.0),
        "proxy_maximum_drawdown": float((equity / equity.cummax() - 1.0).min()),
    }
    checks = {
        "stress_mean_budget": metrics["stress_mean"] >= float(acceptance["stress_mean_minimum"]),
        "profit_factor": metrics["stress_profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
        "positive_years": metrics["positive_years"] >= int(acceptance["positive_years_minimum"]),
        "recent": metrics["recent_stress_mean"] > float(acceptance["recent_stress_mean_min_exclusive"]),
        "proxy_cagr": metrics["proxy_cagr"] >= float(acceptance["proxy_cagr_minimum"]),
        "proxy_maximum_drawdown": metrics["proxy_maximum_drawdown"] >= float(acceptance["proxy_maximum_drawdown_limit"]),
    }
    decision = "PROCEED_TO_MECHANISM_ATTRIBUTION" if all(checks.values()) else "STOP_PRIMARY_FLOW_ABSORPTION_ON_RETURN"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "mechanism": protocol["mechanism"],
        "metrics": metrics,
        "checks": checks,
        "subtype_diagnostics_are_not_candidates": True,
        "decision": decision,
        "cumulative_return_path_count": int(protocol["cumulative_return_path_count"]),
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    event_output = event_ledger.reset_index()
    event_output["trade_date"] = event_output["trade_date"].dt.strftime("%Y-%m-%d")
    event_output.to_csv(
        artifacts / "event_ledger.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
    )
    trades.to_csv(
        artifacts / "trades.csv.gz", index=False, compression=compression, lineterminator="\n"
    )
    annual.reset_index().to_csv(artifacts / "annual_metrics.csv", index=False, lineterminator="\n")
    subtype.reset_index().to_csv(artifacts / "subtype_diagnostics.csv", index=False, lineterminator="\n")
    (artifacts / "return_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX28 执行\n\n"
        f"状态：`COMPLETE`。合并路径密度为{density_median:.1f}/{density_p10:.1f}，完成"
        f"{metrics['closed_trades']}笔固定三交易日回测。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX28 结论\n\n"
        f"闭合交易{metrics['closed_trades']}笔；压力均值{metrics['stress_mean']:.3%}、盈亏比"
        f"{metrics['stress_profit_factor']:.2f}、正收益年度{metrics['positive_years']}/6、最近252日"
        f"均值{metrics['recent_stress_mean']:.3%}；代理年化{metrics['proxy_cagr']:.2%}、最大回撤"
        f"{metrics['proxy_maximum_drawdown']:.2%}。\n\n裁决：`{decision}`。子场景只用于归因，"
        "不具备单独晋升资格；本轮不创建候选，不修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "status": "COMPLETE",
            "decision": decision,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
