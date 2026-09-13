from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX06"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _session_frame(repo: Path, symbol: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    code = symbol.split(".", maxsplit=1)[0]
    frames = [pd.read_csv(path, parse_dates=["datetime"]) for path in sorted((repo / "data/raw").glob(f"{code}_1m_*.csv"))]
    if not frames:
        raise FileNotFoundError(f"no 1m files for {symbol}")
    minutes = pd.concat(frames, ignore_index=True).sort_values("datetime")
    minutes = minutes.loc[minutes["datetime"].dt.normalize() <= cutoff].copy()
    minutes["date"] = minutes["datetime"].dt.normalize()
    minutes["clock"] = minutes["datetime"].dt.strftime("%H:%M")
    if not minutes.groupby("date").size().eq(240).all():
        raise ValueError(f"incomplete sessions for {symbol}")
    grouped = minutes.groupby("date", sort=True)
    sessions = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "close": grouped["close"].last(),
        }
    )
    for clock in ("09:36", "14:56"):
        values = minutes.loc[minutes["clock"].eq(clock)].set_index("date")["open"].astype(float)
        if len(values) != len(sessions):
            raise ValueError(f"missing {clock} bars for {symbol}")
        sessions[f"open_{clock.replace(':', '')}"] = values
    sessions["previous_close"] = sessions["close"].shift(1)
    sessions["gap"] = sessions["open"] / sessions["previous_close"] - 1.0
    return sessions


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


def _random_percentile(
    selected: pd.DatetimeIndex, available: pd.Series, actual_mean: float, iterations: int, seed: int
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
    return float((values <= actual_mean).mean() * 100.0), float(np.median(values)), float(np.quantile(values, 0.90))


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    validate_experiment_archive(repo / "experiments/S005/20260913_S005_EX05")

    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    start = pd.Timestamp(str(protocol["development_start"]))
    primary = _session_frame(repo, str(protocol["symbol"]), cutoff)
    context = _session_frame(repo, str(protocol["context_symbol"]), cutoff)
    frame = primary.join(context[["gap"]].rename(columns={"gap": "context_gap"}), how="inner")
    frame["relative_gap"] = frame["gap"] - frame["context_gap"]
    frame["threshold"] = frame["relative_gap"].shift(1).rolling(int(protocol["threshold_reference_sessions"])).quantile(
        float(protocol["relative_gap_quantile"])
    )
    frame = frame.loc[(frame.index >= start) & (frame.index <= cutoff)].copy()
    raw = frame["relative_gap"] <= frame["threshold"]
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
    ledger = frame[["gap", "context_gap", "relative_gap", "threshold"]].copy()
    ledger["raw_event"] = raw.fillna(False).astype(int)
    ledger["accepted_event"] = accepted.astype(int)
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    ledger.reset_index(names="date").to_csv(artifacts / "event_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")

    metrics: dict[str, float | int] | None = None
    checks: dict[str, bool] = {"density": bool(density_pass)}
    if density_pass:
        entry_column = "open_" + str(protocol["execution"]["entry_clock"]).replace(":", "")
        exit_column = "open_" + str(protocol["execution"]["exit_clock"]).replace(":", "")
        cost = float(protocol["execution"]["stress_one_way_cost"])
        gross = frame[exit_column] / frame[entry_column] - 1.0
        stress = frame[exit_column] * (1.0 - cost) / (frame[entry_column] * (1.0 + cost)) - 1.0
        event_dates = pd.DatetimeIndex(accepted.index[accepted])
        trades = frame.loc[event_dates, [entry_column, exit_column]].copy()
        trades.index.name = "trade_date"
        trades["gross_return"] = gross.reindex(event_dates)
        trades["stress_return"] = stress.reindex(event_dates)
        trades["trade_year"] = trades.index.year
        annual = trades.groupby("trade_year", observed=True).agg(
            trades=("stress_return", "size"),
            stress_mean=("stress_return", "mean"),
            total_return=("stress_return", lambda values: float((1.0 + values).prod() - 1.0)),
        )
        account_returns = pd.Series(0.0, index=frame.index)
        account_returns.loc[event_dates] = trades["stress_return"].to_numpy()
        recent_start = frame.index[-int(protocol["acceptance"]["recent_sessions"])]
        recent = trades.loc[trades.index >= recent_start, "stress_return"]
        percentile, random_median, random_p90 = _random_percentile(
            event_dates,
            stress,
            float(trades["stress_return"].mean()),
            int(protocol["random_control"]["iterations"]),
            int(protocol["random_control"]["seed"]),
        )
        equity = (1.0 + account_returns).cumprod()
        metrics = {
            "closed_trades": int(len(trades)),
            "gross_mean": float(trades["gross_return"].mean()),
            "stress_mean": float(trades["stress_return"].mean()),
            "stress_profit_factor": _profit_factor(trades["stress_return"]),
            "positive_years": int(annual["stress_mean"].gt(0.0).sum()),
            "recent_trades": int(len(recent)),
            "recent_stress_mean": float(recent.mean()),
            "random_percentile": percentile,
            "random_median": random_median,
            "random_p90": random_p90,
            "proxy_cagr": float(equity.iloc[-1] ** (252.0 / len(account_returns)) - 1.0),
            "proxy_maximum_drawdown": float((equity / equity.cummax() - 1.0).min()),
        }
        acceptance = protocol["acceptance"]
        checks.update(
            {
                "stress_mean": metrics["stress_mean"] > float(acceptance["stress_mean_min_exclusive"]),
                "profit_factor": metrics["stress_profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
                "positive_years": metrics["positive_years"] >= int(acceptance["positive_years_minimum"]),
                "recent": metrics["recent_stress_mean"] > float(acceptance["recent_stress_mean_min_exclusive"]),
                "random_control": metrics["random_percentile"] >= float(acceptance["random_percentile_minimum"]),
                "proxy_cagr": metrics["proxy_cagr"] >= float(acceptance["proxy_cagr_minimum"]),
                "proxy_maximum_drawdown": metrics["proxy_maximum_drawdown"] >= float(acceptance["proxy_maximum_drawdown_limit"]),
            }
        )
        trades.reset_index().to_csv(artifacts / "trades.csv.gz", index=False, compression=compression, lineterminator="\n")
        annual.reset_index().to_csv(artifacts / "annual_metrics.csv", index=False, lineterminator="\n")

    passed = density_pass and all(checks.values())
    decision = "PROCEED_TO_MECHANISM_ATTRIBUTION" if passed else (
        "STOP_OPENING_AUCTION_DISLOCATION_ON_RETURN" if density_pass else "STOP_OPENING_AUCTION_DISLOCATION_ON_DENSITY"
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
        "# S005 EX06 执行\n\n状态：COMPLETE。已按协议先执行密度门；通过后才读取09:36至14:56的收益。\n",
        encoding="utf-8",
    )
    metric_text = "密度未通过，未读取后续价格。" if metrics is None else (
        f"压力单笔均值{metrics['stress_mean']:.3%}、盈亏比{metrics['stress_profit_factor']:.2f}、"
        f"正收益年度{metrics['positive_years']}/6、随机分位{metrics['random_percentile']:.1f}%；"
        f"代理年化{metrics['proxy_cagr']:.2%}、最大回撤{metrics['proxy_maximum_drawdown']:.2%}。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX06 结论\n\n"
        f"独立事件{density['independent_events']}个，滚动60日中位数{density['rolling_60_median']:.1f}、"
        f"P10为{density['rolling_60_p10']:.1f}。{metric_text}\n\n"
        f"裁决：`{decision}`。本轮不生成候选，不修改SM或PTE。\n",
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
