from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


EXPERIMENT_ID = "20260913_S005_EX47"


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


def _bars(repo: Path, cutoff: pd.Timestamp) -> pd.DataFrame:
    paths = sorted((repo / "data/raw").glob("588080_1m_*.csv"))
    if not paths:
        raise FileNotFoundError("no 588080 1m files")
    frame = pd.concat(
        [pd.read_csv(path, parse_dates=["datetime"]) for path in paths],
        ignore_index=True,
    ).sort_values("datetime")
    frame = frame[frame["datetime"].dt.normalize() <= cutoff].copy()
    frame["date"] = frame["datetime"].dt.normalize()
    frame["clock"] = frame["datetime"].dt.strftime("%H:%M")
    counts = frame.groupby("date").size()
    if not counts.eq(240).all():
        raise ValueError("588080 contains incomplete 1m sessions")
    return frame


def _profit_factor(values: pd.Series) -> float:
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    return gains / losses if losses else (float("inf") if gains else 0.0)


def _random_percentile(
    selected: pd.DatetimeIndex,
    available: pd.Series,
    actual_mean: float,
    iterations: int,
    seed: int,
) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    dates = pd.DatetimeIndex(available.dropna().index)
    controls: list[float] = []
    for _ in range(iterations):
        shifted: list[pd.Timestamp] = []
        for year in sorted(selected.year.unique()):
            year_dates = dates[dates.year == year]
            source = selected[selected.year == year]
            positions = {date: position for position, date in enumerate(year_dates)}
            source = pd.DatetimeIndex([date for date in source if date in positions])
            offset = int(rng.integers(1, len(year_dates)))
            shifted.extend(year_dates[(positions[date] + offset) % len(year_dates)] for date in source)
        controls.append(float(available.reindex(pd.DatetimeIndex(shifted)).mean()))
    values = np.asarray(controls, dtype=float)
    return (
        float((values <= actual_mean).mean() * 100.0),
        float(np.median(values)),
        float(np.quantile(values, 0.90)),
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    sys.path.insert(0, str(repo / "src"))
    from czsc_trader.experiment_archive import (  # noqa: PLC0415
        build_experiment_manifest,
        validate_experiment_archive,
    )
    from czsc_trader.identity import raw_file_sha256  # noqa: PLC0415

    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    source_dir = repo / "experiments/S005" / str(protocol["source"]["experiment_id"])
    validate_experiment_archive(source_dir)
    for name, relative in (
        ("manifest_sha256", "experiment_manifest.json"),
        ("ledger_sha256", "artifacts/event_ledger.csv"),
        ("selection_sha256", "artifacts/selection.json"),
    ):
        if raw_file_sha256(source_dir / relative) != protocol["source"][name]:
            raise ValueError(f"source {relative} differs from frozen protocol")
    market_manifest = repo / str(protocol["market_data"]["manifest"])
    if raw_file_sha256(market_manifest) != protocol["market_data"]["manifest_sha256"]:
        raise ValueError("588080 intraday data differs from frozen protocol")

    selection = _read(source_dir / "artifacts/selection.json")
    if selection["selected_mechanism"] != protocol["mechanism"]:
        raise ValueError("selected mechanism differs from frozen protocol")
    ledger = pd.read_csv(source_dir / "artifacts/event_ledger.csv", parse_dates=["signal_date"])
    signal_dates = pd.DatetimeIndex(
        ledger.loc[ledger["quantile"].eq(float(selection["selected_quantile"])), "signal_date"]
    )

    cutoff = pd.Timestamp(protocol["development_cutoff"])
    start = pd.Timestamp(protocol["development_start"])
    bars = _bars(repo, cutoff)
    calendar = pd.DatetimeIndex(sorted(bars.loc[bars["date"].ge(start), "date"].unique()))
    positions = {date: position for position, date in enumerate(calendar)}
    execution = protocol["execution"]
    entry_clock = str(execution["entry_clock"])
    exit_clock = str(execution["exit_clock"])
    price = {
        (clock, date): float(group.loc[group["clock"].eq(clock), "open"].iloc[0])
        for date, group in bars.groupby("date")
        for clock in (entry_clock, exit_clock)
    }
    rows: list[dict[str, object]] = []
    cost = float(execution["stress_one_way_cost"])
    for signal_date in signal_dates:
        position = positions.get(signal_date)
        if position is None or position + int(execution["exit_offset_sessions"]) >= len(calendar):
            continue
        entry_date = calendar[position + int(execution["entry_offset_sessions"])]
        exit_date = calendar[position + int(execution["exit_offset_sessions"])]
        entry = price[(entry_clock, entry_date)]
        exit_ = price[(exit_clock, exit_date)]
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
    if trades.empty:
        raise ValueError("selected mechanism produced no executable trades")
    trades["entry_year"] = pd.to_datetime(trades["entry_date"]).dt.year
    annual = trades.groupby("entry_year", observed=True).agg(
        trades=("stress_return", "size"),
        stress_mean=("stress_return", "mean"),
        total_return=("stress_return", lambda values: float((1.0 + values).prod() - 1.0)),
    )
    account_returns = pd.Series(0.0, index=calendar)
    account_returns.loc[pd.DatetimeIndex(trades["exit_date"])] = trades["stress_return"].to_numpy()
    equity = (1.0 + account_returns).cumprod()
    available = pd.Series(index=calendar, dtype=float)
    for position, signal_date in enumerate(calendar[:-1]):
        trade_date = calendar[position + 1]
        entry = price[(entry_clock, trade_date)]
        exit_ = price[(exit_clock, trade_date)]
        available.loc[signal_date] = exit_ * (1.0 - cost) / (entry * (1.0 + cost)) - 1.0
    random_percentile, random_median, random_p90 = _random_percentile(
        pd.DatetimeIndex(trades["signal_date"]),
        available,
        float(trades["stress_return"].mean()),
        int(protocol["random_control"]["iterations"]),
        int(protocol["random_control"]["seed"]),
    )
    acceptance = protocol["acceptance"]
    recent_start = calendar[-int(acceptance["recent_sessions"])]
    recent = trades[pd.to_datetime(trades["entry_date"]) >= recent_start]["stress_return"]
    metrics = {
        "closed_trades": int(len(trades)),
        "terminal_signals_skipped": int(len(signal_dates) - len(trades)),
        "gross_mean": float(trades["gross_return"].mean()),
        "stress_mean": float(trades["stress_return"].mean()),
        "stress_profit_factor": _profit_factor(trades["stress_return"]),
        "positive_years": int(annual["stress_mean"].gt(0.0).sum()),
        "recent_trades": int(len(recent)),
        "recent_stress_mean": float(recent.mean()),
        "random_percentile": random_percentile,
        "random_median": random_median,
        "random_p90": random_p90,
        "proxy_cagr": float(equity.iloc[-1] ** (252.0 / len(account_returns)) - 1.0),
        "proxy_maximum_drawdown": float((equity / equity.cummax() - 1.0).min()),
    }
    checks = {
        "stress_mean": metrics["stress_mean"] >= float(acceptance["stress_mean_minimum"]),
        "profit_factor": metrics["stress_profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
        "positive_years": metrics["positive_years"] >= int(acceptance["positive_years_minimum"]),
        "recent": metrics["recent_stress_mean"] > float(acceptance["recent_stress_mean_min_exclusive"]),
        "random_control": metrics["random_percentile"] >= float(protocol["random_control"]["minimum_percentile"]),
        "proxy_cagr": metrics["proxy_cagr"] >= float(acceptance["proxy_cagr_minimum"]),
        "proxy_maximum_drawdown": metrics["proxy_maximum_drawdown"] >= float(acceptance["proxy_maximum_drawdown_limit"]),
    }
    passed = all(checks.values())
    decision = "PROCEED_TO_MECHANISM_ATTRIBUTION" if passed else "STOP_CROSS_ETF_TRANSMISSION_ON_RETURN"
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
    _write(artifacts / "return_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S005 EX47 执行\n\n"
        f"状态：`COMPLETE`。按唯一冻结路径完成{len(trades)}笔次日09:36至14:56闭合交易，"
        "使用单边6bp压力成本。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX47 结论\n\n"
        f"闭合交易{len(trades)}笔；压力均值{metrics['stress_mean']:.3%}、盈亏比"
        f"{metrics['stress_profit_factor']:.2f}、正收益年度{metrics['positive_years']}/6、"
        f"最近252日均值{metrics['recent_stress_mean']:.3%}、随机对照分位"
        f"{metrics['random_percentile']:.1f}%；代理年化{metrics['proxy_cagr']:.2%}、"
        f"最大回撤{metrics['proxy_maximum_drawdown']:.2%}。\n\n"
        f"裁决：`{decision}`。本轮不创建候选、不修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": str(protocol["development_cutoff"]),
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
