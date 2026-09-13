from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX04"


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _load_daily(repo: Path, symbol: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    code = symbol.split(".", maxsplit=1)[0]
    paths = sorted((repo / "data/raw").glob(f"{code}_1m_*.csv"))
    frames = [pd.read_csv(path, parse_dates=["datetime"]) for path in paths]
    if not frames:
        raise FileNotFoundError(f"no 1m files for {symbol}")
    minutes = pd.concat(frames, ignore_index=True).sort_values("datetime")
    minutes = minutes.loc[minutes["datetime"].dt.normalize() <= cutoff].copy()
    minutes["date"] = minutes["datetime"].dt.normalize()
    counts = minutes.groupby("date").size()
    if not counts.eq(240).all():
        raise ValueError(f"incomplete sessions for {symbol}")
    grouped = minutes.groupby("date", sort=True)
    return pd.DataFrame({"open": grouped["open"].first(), "close": grouped["close"].last()})


def _net_long(entry: pd.Series, exit_: pd.Series, cost: float) -> pd.Series:
    return exit_ * (1.0 - cost) / (entry * (1.0 + cost)) - 1.0


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    if losses == 0.0:
        return float("inf") if gains > 0.0 else 0.0
    return gains / losses


def _maximum_drawdown(returns: pd.Series) -> float:
    equity = (1.0 + returns.fillna(0.0)).cumprod()
    return float((equity / equity.cummax() - 1.0).min())


def _cagr(returns: pd.Series) -> float:
    years = len(returns) / 252.0
    return float((1.0 + returns.fillna(0.0)).prod() ** (1.0 / years) - 1.0)


def _random_percentile(
    selected_dates: pd.DatetimeIndex,
    available_returns: pd.Series,
    actual_mean: float,
    iterations: int,
    seed: int,
) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    available_dates = pd.DatetimeIndex(available_returns.dropna().index)
    controls: list[float] = []
    selected_by_year = pd.Series(selected_dates, index=selected_dates).groupby(selected_dates.year)
    for _ in range(iterations):
        shifted: list[pd.Timestamp] = []
        for year, group in selected_by_year:
            year_dates = available_dates[available_dates.year == year]
            positions = {date: position for position, date in enumerate(year_dates)}
            source = [date for date in group.index if date in positions]
            if not source:
                continue
            offset = int(rng.integers(1, len(year_dates)))
            shifted.extend(year_dates[(positions[date] + offset) % len(year_dates)] for date in source)
        sample = available_returns.reindex(pd.DatetimeIndex(shifted)).dropna()
        controls.append(float(sample.mean()))
    values = np.asarray(controls, dtype=float)
    if values.size != iterations:
        raise ValueError("random control did not produce every iteration")
    return float((values <= actual_mean).mean() * 100.0), float(np.median(values)), float(np.quantile(values, 0.90))


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read_json(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    source_dir = repo / "experiments/S005" / str(protocol["source_experiment"])
    validate_experiment_archive(source_dir)
    source = _read_json(source_dir / "artifacts/selection_evidence.json")
    if source.get("selected_mechanism") != protocol["mechanism"]:
        raise ValueError("selected mechanism differs from frozen return protocol")

    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    start = pd.Timestamp(str(protocol["development_start"]))
    daily = _load_daily(repo, str(protocol["symbol"]), cutoff)
    daily = daily.loc[(daily.index >= start) & (daily.index <= cutoff)].copy()
    event_ledger_path = source_dir / "artifacts/event_ledger.csv.gz"
    event_ledger = pd.read_csv(event_ledger_path, parse_dates=["date"])
    event_column = f"{protocol['mechanism']}__accepted"
    event_dates = pd.DatetimeIndex(event_ledger.loc[event_ledger[event_column].eq(1), "date"])
    calendar = pd.DatetimeIndex(daily.index)
    positions = {date: position for position, date in enumerate(calendar)}
    rows: list[dict[str, object]] = []
    execution = protocol["execution"]
    baseline_cost = float(execution["baseline_one_way_cost"])
    stress_cost = float(execution["stress_one_way_cost"])
    for signal_date in event_dates:
        position = positions.get(signal_date)
        if position is None or position + 1 >= len(calendar):
            continue
        trade_date = calendar[position + 1]
        entry = float(daily.loc[trade_date, "open"])
        exit_ = float(daily.loc[trade_date, "close"])
        rows.append(
            {
                "signal_date": signal_date,
                "trade_date": trade_date,
                "entry_price": entry,
                "exit_price": exit_,
                "gross_return": exit_ / entry - 1.0,
                "baseline_return": exit_ * (1.0 - baseline_cost) / (entry * (1.0 + baseline_cost)) - 1.0,
                "stress_return": exit_ * (1.0 - stress_cost) / (entry * (1.0 + stress_cost)) - 1.0,
            }
        )
    trades = pd.DataFrame(rows)
    if trades.empty:
        raise ValueError("selected mechanism produced no executable trades")
    trades["trade_year"] = pd.to_datetime(trades["trade_date"]).dt.year

    available_gross = daily["close"] / daily["open"] - 1.0
    available_stress = _net_long(daily["open"], daily["close"], stress_cost)
    valid_signal_dates = pd.DatetimeIndex(trades["signal_date"])
    signal_date_forward_returns = available_stress.shift(-1)
    random_percentile, random_median, random_p90 = _random_percentile(
        valid_signal_dates,
        signal_date_forward_returns,
        float(trades["stress_return"].mean()),
        int(protocol["random_control"]["iterations"]),
        int(protocol["random_control"]["seed"]),
    )
    account_returns = pd.Series(0.0, index=calendar, name="stress_return")
    account_returns.loc[pd.DatetimeIndex(trades["trade_date"])] = trades["stress_return"].to_numpy()
    annual = trades.groupby("trade_year", observed=True).agg(
        trades=("stress_return", "size"),
        stress_mean=("stress_return", "mean"),
        total_return=("stress_return", lambda values: float((1.0 + values).prod() - 1.0)),
    )
    recent_start = calendar[-int(protocol["acceptance"]["recent_sessions"])]
    recent = trades.loc[pd.to_datetime(trades["trade_date"]) >= recent_start, "stress_return"]
    metrics = {
        "closed_trades": int(len(trades)),
        "terminal_signals_skipped": int(len(event_dates) - len(trades)),
        "gross_mean": float(trades["gross_return"].mean()),
        "baseline_mean": float(trades["baseline_return"].mean()),
        "stress_mean": float(trades["stress_return"].mean()),
        "stress_profit_factor": _profit_factor(trades["stress_return"]),
        "positive_years": int(annual["stress_mean"].gt(0.0).sum()),
        "recent_trades": int(len(recent)),
        "recent_stress_mean": float(recent.mean()),
        "random_percentile": random_percentile,
        "random_median": random_median,
        "random_p90": random_p90,
        "proxy_cagr": _cagr(account_returns),
        "proxy_maximum_drawdown": _maximum_drawdown(account_returns),
        "available_session_gross_mean": float(available_gross.mean()),
    }
    acceptance = protocol["acceptance"]
    checks = {
        "stress_mean": metrics["stress_mean"] > float(acceptance["stress_mean_min_exclusive"]),
        "profit_factor": metrics["stress_profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
        "positive_years": metrics["positive_years"] >= int(acceptance["positive_years_minimum"]),
        "recent": metrics["recent_stress_mean"] > float(acceptance["recent_stress_mean_min_exclusive"]),
        "random_control": metrics["random_percentile"] >= float(acceptance["random_percentile_minimum"]),
        "proxy_cagr": metrics["proxy_cagr"] >= float(acceptance["proxy_cagr_minimum"]),
        "proxy_maximum_drawdown": metrics["proxy_maximum_drawdown"] >= float(acceptance["proxy_maximum_drawdown_limit"]),
    }
    passed = all(checks.values())
    decision = "PROCEED_TO_MECHANISM_ATTRIBUTION" if passed else "STOP_RELATIVE_FLOW_FAMILY_ON_RETURN"
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "mechanism": protocol["mechanism"],
        "return_path_count": 1,
        "metrics": metrics,
        "checks": checks,
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "source_event_ledger_sha256": raw_file_sha256(event_ledger_path),
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    trades.to_csv(artifacts / "trades.csv.gz", index=False, compression=compression, lineterminator="\n")
    annual.reset_index().to_csv(artifacts / "annual_metrics.csv", index=False, lineterminator="\n")
    account_returns.rename_axis("date").reset_index().to_csv(artifacts / "account_returns.csv.gz", index=False, compression=compression, lineterminator="\n")
    _write_json(artifacts / "return_evidence.json", result)
    (experiment / "03_execution.md").write_text(
        "# S005 EX04 执行\n\n状态：COMPLETE。已按唯一固定路径完成181个信号的次日开盘至收盘收益证伪。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX04 结论\n\n"
        f"可执行闭合交易{metrics['closed_trades']}笔；压力成本后单笔均值{metrics['stress_mean']:.3%}，"
        f"盈亏比{metrics['stress_profit_factor']:.2f}，正收益年度{metrics['positive_years']}/6，"
        f"最近252日均值{metrics['recent_stress_mean']:.3%}，随机对照分位{metrics['random_percentile']:.1f}%。\n\n"
        f"代理账户年化收益{metrics['proxy_cagr']:.2%}，最大回撤{metrics['proxy_maximum_drawdown']:.2%}。"
        f"裁决：`{decision}`。本轮没有生成候选，也没有修改SM或PTE。\n",
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
