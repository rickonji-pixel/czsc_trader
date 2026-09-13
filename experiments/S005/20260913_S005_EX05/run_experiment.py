from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX05"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _daily(repo: Path, symbol: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    code = symbol.split(".", maxsplit=1)[0]
    frames = [pd.read_csv(path, parse_dates=["datetime"]) for path in sorted((repo / "data/raw").glob(f"{code}_1m_*.csv"))]
    if not frames:
        raise FileNotFoundError(f"no 1m files for {symbol}")
    minutes = pd.concat(frames, ignore_index=True).sort_values("datetime")
    minutes = minutes.loc[minutes["datetime"].dt.normalize() <= cutoff].copy()
    minutes["date"] = minutes["datetime"].dt.normalize()
    if not minutes.groupby("date").size().eq(240).all():
        raise ValueError(f"incomplete sessions for {symbol}")
    grouped = minutes.groupby("date", sort=True)
    daily = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
        }
    )
    daily["return"] = daily["close"].pct_change()
    daily["close_location"] = (daily["close"] - daily["low"]) / (daily["high"] - daily["low"]).replace(0.0, np.nan)
    return daily


def _cooldown(events: pd.Series, sessions: int) -> pd.Series:
    accepted = pd.Series(False, index=events.index)
    last_position = -sessions - 1
    for position, active in enumerate(events.fillna(False).astype(bool).to_numpy()):
        if active and position - last_position > sessions:
            accepted.iloc[position] = True
            last_position = position
    return accepted


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    return gains / losses if losses else (float("inf") if gains else 0.0)


def _cagr(returns: pd.Series) -> float:
    return float((1.0 + returns).prod() ** (252.0 / len(returns)) - 1.0)


def _maximum_drawdown(returns: pd.Series) -> float:
    equity = (1.0 + returns).cumprod()
    return float((equity / equity.cummax() - 1.0).min())


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    validate_experiment_archive(repo / "experiments/S005/20260913_S005_EX04")

    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    start = pd.Timestamp(str(protocol["development_start"]))
    primary = _daily(repo, str(protocol["symbol"]), cutoff)
    context = _daily(repo, str(protocol["context_symbol"]), cutoff)
    frame = primary.join(context[["return"]].rename(columns={"return": "context_return"}), how="inner")
    frame["relative_return"] = frame["return"] - frame["context_return"]
    frame["threshold"] = frame["relative_return"].shift(1).rolling(int(protocol["threshold_reference_sessions"])).quantile(
        float(protocol["relative_return_quantile"])
    )
    frame = frame.loc[(frame.index >= start) & (frame.index <= cutoff)].copy()
    raw = (frame["relative_return"] <= frame["threshold"]) & (frame["close_location"] <= float(protocol["maximum_close_location"]))
    density_rule = protocol["density"]
    accepted = _cooldown(raw, int(density_rule["cooldown_sessions"]))
    rolling = accepted.astype(int).rolling(int(density_rule["rolling_window_sessions"]), min_periods=int(density_rule["rolling_window_sessions"])).sum().dropna()
    density = {
        "raw_events": int(raw.fillna(False).sum()),
        "independent_events": int(accepted.sum()),
        "rolling_60_median": float(rolling.median()),
        "rolling_60_p10": float(rolling.quantile(0.10)),
        "rolling_60_minimum": float(rolling.min()),
        "rolling_60_maximum": float(rolling.max()),
    }
    density_pass = density["rolling_60_median"] >= float(density_rule["minimum_median"]) and density["rolling_60_p10"] >= float(
        density_rule["minimum_p10"]
    )
    ledger = frame[["return", "context_return", "relative_return", "threshold", "close_location"]].copy()
    ledger["raw_event"] = raw.fillna(False).astype(int)
    ledger["accepted_event"] = accepted.astype(int)
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    ledger.reset_index(names="date").to_csv(artifacts / "event_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")

    metrics: dict[str, float | int] | None = None
    checks: dict[str, bool] = {"density": bool(density_pass)}
    trades = pd.DataFrame()
    annual = pd.DataFrame()
    if density_pass:
        calendar = pd.DatetimeIndex(frame.index)
        positions = {date: position for position, date in enumerate(calendar)}
        rows: list[dict[str, object]] = []
        cost = float(protocol["execution"]["stress_one_way_cost"])
        for signal_date in pd.DatetimeIndex(accepted.index[accepted]):
            position = positions[signal_date]
            if position + 1 >= len(calendar):
                continue
            trade_date = calendar[position + 1]
            entry = float(frame.loc[trade_date, "open"])
            exit_ = float(frame.loc[trade_date, "close"])
            rows.append(
                {
                    "signal_date": signal_date,
                    "trade_date": trade_date,
                    "entry_price": entry,
                    "exit_price": exit_,
                    "gross_return": exit_ / entry - 1.0,
                    "stress_return": exit_ * (1.0 - cost) / (entry * (1.0 + cost)) - 1.0,
                }
            )
        trades = pd.DataFrame(rows)
        trades["trade_year"] = pd.to_datetime(trades["trade_date"]).dt.year
        annual = trades.groupby("trade_year", observed=True).agg(
            trades=("stress_return", "size"),
            stress_mean=("stress_return", "mean"),
            total_return=("stress_return", lambda values: float((1.0 + values).prod() - 1.0)),
        )
        account_returns = pd.Series(0.0, index=calendar)
        account_returns.loc[pd.DatetimeIndex(trades["trade_date"])] = trades["stress_return"].to_numpy()
        recent_start = calendar[-int(protocol["acceptance"]["recent_sessions"])]
        recent = trades.loc[pd.to_datetime(trades["trade_date"]) >= recent_start, "stress_return"]
        metrics = {
            "closed_trades": int(len(trades)),
            "terminal_signals_skipped": int(accepted.sum() - len(trades)),
            "gross_mean": float(trades["gross_return"].mean()),
            "stress_mean": float(trades["stress_return"].mean()),
            "stress_profit_factor": _profit_factor(trades["stress_return"]),
            "positive_years": int(annual["stress_mean"].gt(0.0).sum()),
            "recent_trades": int(len(recent)),
            "recent_stress_mean": float(recent.mean()),
            "proxy_cagr": _cagr(account_returns),
            "proxy_maximum_drawdown": _maximum_drawdown(account_returns),
        }
        acceptance = protocol["acceptance"]
        checks.update(
            {
                "stress_mean": metrics["stress_mean"] > float(acceptance["stress_mean_min_exclusive"]),
                "profit_factor": metrics["stress_profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
                "positive_years": metrics["positive_years"] >= int(acceptance["positive_years_minimum"]),
                "recent": metrics["recent_stress_mean"] > float(acceptance["recent_stress_mean_min_exclusive"]),
                "proxy_cagr": metrics["proxy_cagr"] >= float(acceptance["proxy_cagr_minimum"]),
                "proxy_maximum_drawdown": metrics["proxy_maximum_drawdown"] >= float(acceptance["proxy_maximum_drawdown_limit"]),
            }
        )
        trades.to_csv(artifacts / "trades.csv.gz", index=False, compression=compression, lineterminator="\n")
        annual.reset_index().to_csv(artifacts / "annual_metrics.csv", index=False, lineterminator="\n")

    passed = density_pass and all(checks.values())
    decision = "PROCEED_TO_MECHANISM_ATTRIBUTION" if passed else (
        "STOP_RELATIVE_DISLOCATION_FAMILY_ON_RETURN" if density_pass else "STOP_RELATIVE_DISLOCATION_FAMILY_ON_DENSITY"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "mechanism": protocol["mechanism"],
        "density": density,
        "density_pass": bool(density_pass),
        "metrics": metrics,
        "checks": checks,
        "decision": decision,
        "cumulative_return_path_count": int(protocol["cumulative_return_path_count"]),
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    _write(artifacts / "falsification_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S005 EX05 执行\n\n状态：COMPLETE。已按协议先执行密度门；密度通过后才读取唯一固定路径的未来收益。\n",
        encoding="utf-8",
    )
    metric_text = "密度未通过，因此没有读取未来收益。" if metrics is None else (
        f"压力成本后单笔均值{metrics['stress_mean']:.3%}、盈亏比{metrics['stress_profit_factor']:.2f}、"
        f"正收益年度{metrics['positive_years']}/6、最近252日均值{metrics['recent_stress_mean']:.3%}；"
        f"代理年化{metrics['proxy_cagr']:.2%}、最大回撤{metrics['proxy_maximum_drawdown']:.2%}。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX05 结论\n\n"
        f"独立事件{density['independent_events']}个，滚动60日中位数{density['rolling_60_median']:.1f}、"
        f"P10为{density['rolling_60_p10']:.1f}，密度门{'通过' if density_pass else '未通过'}。"
        f"{metric_text}\n\n裁决：`{decision}`。本轮不生成候选，不修改SM或PTE。\n",
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
