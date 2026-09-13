from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX25"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


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
        source / "artifacts/margin_feature_ledger.csv.gz": source_spec["ledger_sha256"],
        source / "artifacts/selection_evidence.json": source_spec["selection_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    selection = _read(source / "artifacts/selection_evidence.json")
    for mechanism in protocol["mechanisms"]:
        if mechanism not in selection["density_passing_paths"]:
            raise ValueError(f"mechanism did not pass density gate: {mechanism}")

    ledger = pd.read_csv(
        source / "artifacts/margin_feature_ledger.csv.gz", parse_dates=["dt"]
    ).set_index("dt")
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
    cost = float(execution["stress_one_way_cost"])
    acceptance = protocol["acceptance"]
    all_trades: list[pd.DataFrame] = []
    all_annual: list[pd.DataFrame] = []
    evidence_rows: list[dict[str, object]] = []
    for mechanism in protocol["mechanisms"]:
        signal_dates = pd.DatetimeIndex(ledger.index[ledger[mechanism].astype(bool)])
        rows: list[dict[str, object]] = []
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
                    "mechanism": mechanism,
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
        annual.insert(0, "mechanism", mechanism)
        account_returns = pd.Series(0.0, index=calendar)
        account_returns.loc[pd.DatetimeIndex(trades["exit_date"])] = trades[
            "stress_return"
        ].to_numpy()
        equity = (1.0 + account_returns).cumprod()
        recent_start = calendar[-int(acceptance["recent_sessions"])]
        recent = trades.loc[
            pd.to_datetime(trades["entry_date"]) >= recent_start, "stress_return"
        ]
        metrics = {
            "mechanism": mechanism,
            "closed_trades": int(len(trades)),
            "terminal_signals_skipped": int(len(signal_dates) - len(trades)),
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
        metrics["passed"] = all(checks.values())
        metrics["checks"] = checks
        evidence_rows.append(metrics)
        all_trades.append(trades)
        all_annual.append(annual.reset_index())
    passing = [str(row["mechanism"]) for row in evidence_rows if row["passed"]]
    decision = "PROCEED_TO_MECHANISM_ATTRIBUTION" if passing else "STOP_MARGIN_MECHANISMS_ON_RETURN"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "mechanisms": evidence_rows,
        "passing_mechanisms": passing,
        "decision": decision,
        "cumulative_return_path_count": int(protocol["cumulative_return_path_count"]),
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    pd.concat(all_trades, ignore_index=True).to_csv(
        artifacts / "trades.csv.gz", index=False, compression=compression, lineterminator="\n"
    )
    pd.concat(all_annual, ignore_index=True).to_csv(
        artifacts / "annual_metrics.csv", index=False, lineterminator="\n"
    )
    (artifacts / "return_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX25 执行\n\n状态：`COMPLETE`。两条同步融资路径均按统一三交易日口径完成。\n",
        encoding="utf-8",
    )
    lines = ["|机制|闭合交易|压力均值|盈亏比|正收益年|最近均值|年化|回撤|硬门|", "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in evidence_rows:
        lines.append(
            f"|{row['mechanism']}|{row['closed_trades']}|{row['stress_mean']:.3%}|"
            f"{row['stress_profit_factor']:.2f}|{row['positive_years']}/6|"
            f"{row['recent_stress_mean']:.3%}|{row['proxy_cagr']:.2%}|"
            f"{row['proxy_maximum_drawdown']:.2%}|{'PASS' if row['passed'] else 'FAIL'}|"
        )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX25 结论\n\n" + "\n".join(lines) + "\n\n"
        f"裁决：`{decision}`。通过路径：{', '.join(passing) if passing else 'NONE'}。"
        "本轮不生成候选，不修改SM或PTE。\n",
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
