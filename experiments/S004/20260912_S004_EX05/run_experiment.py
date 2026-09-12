from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_regime import classify_lagged_daily_regime


EXPERIMENT_ID = "20260912_S004_EX05"


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


def _net(entry: pd.Series, exit_: pd.Series, cost: float) -> pd.Series:
    return exit_.astype(float) * (1.0 - cost) / (entry.astype(float) * (1.0 + cost)) - 1.0


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    if losses == 0.0:
        return float("inf") if gains > 0.0 else 0.0
    return gains / losses


def _price_series(bars: pd.DataFrame, clock: str, field: str) -> pd.Series:
    selected = bars.loc[bars["clock"].eq(clock)].set_index("trade_date")[field].astype(float)
    if selected.index.has_duplicates:
        raise ValueError(f"duplicate {clock} price")
    return selected


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(bool(protocol.get(key)) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("structural audit may not mutate lifecycle state")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    execution = protocol["execution"]
    acceptance = protocol["acceptance"]
    source_dir = repo / "experiments" / "S004" / str(dataset["evaluation_experiment"])
    source_manifest = validate_experiment_archive(source_dir)
    source = pd.read_csv(source_dir / "artifacts" / "episodes.csv.gz", parse_dates=["event_date", "entry_date", "exit_date"])
    source = source.loc[source["mechanism_id"].eq(str(target["mechanism_id"]))].copy().sort_values("event_date").reset_index(drop=True)
    loaded = load_intraday_research_data(repo / str(dataset["directory"]), str(target["symbol"]))
    bars = loaded.frames["5m"].copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    cost = float(execution["stress_one_way_cost"])

    opening = _price_series(bars, "09:35", "Open")
    close = _price_series(bars, "15:00", "Close")
    audit = source.loc[:, ["event_date", "entry_date", "exit_date", "entry_price", "exit_price", "stress_return"]].copy()
    audit["signal_close"] = close.reindex(audit["event_date"]).to_numpy()
    audit["entry_day_close"] = close.reindex(audit["entry_date"]).to_numpy()
    audit["signal_to_entry_gap"] = audit["entry_price"] / audit["signal_close"] - 1.0
    audit["entry_day_session_return"] = audit["entry_day_close"] / audit["entry_price"] - 1.0
    audit["exit_overnight_return"] = audit["exit_price"] / audit["entry_day_close"] - 1.0
    audit["reconstructed_stress_return"] = _net(audit["entry_price"], audit["exit_price"], cost)
    max_diff = float((audit["reconstructed_stress_return"] - audit["stress_return"]).abs().max())
    if max_diff > float(acceptance["primary_reconstruction_tolerance"]):
        raise ValueError(f"primary episode reconstruction differs by {max_diff}")

    timing_rows: list[dict[str, object]] = []
    timing_returns: list[pd.DataFrame] = []
    variants = [("09:35_OPEN", opening, opening)]
    for item in execution["entry_delay_clocks"]:
        clock, field = str(item).split("_")
        price = _price_series(bars, clock, field.title())
        variants.append((str(item), price, price))
    for name, entry_series, exit_series in variants:
        values = _net(
            entry_series.reindex(audit["entry_date"]).reset_index(drop=True),
            exit_series.reindex(audit["exit_date"]).reset_index(drop=True),
            cost,
        )
        timing_rows.append({
            "dimension": "ENTRY_DELAY",
            "variant": name,
            "episodes": int(len(values)),
            "stress_mean_return": float(values.mean()),
            "stress_profit_factor": _profit_factor(values),
        })
        timing_returns.append(pd.DataFrame({"dimension": "ENTRY_DELAY", "variant": name, "event_date": audit["event_date"], "stress_return": values}))
    for item in execution["exit_delay_clocks"]:
        clock, field = str(item).split("_")
        exit_series = _price_series(bars, clock, field.title())
        values = _net(
            opening.reindex(audit["entry_date"]).reset_index(drop=True),
            exit_series.reindex(audit["exit_date"]).reset_index(drop=True),
            cost,
        )
        timing_rows.append({
            "dimension": "EXIT_DELAY",
            "variant": str(item),
            "episodes": int(len(values)),
            "stress_mean_return": float(values.mean()),
            "stress_profit_factor": _profit_factor(values),
        })
        timing_returns.append(pd.DataFrame({"dimension": "EXIT_DELAY", "variant": str(item), "event_date": audit["event_date"], "stress_return": values}))
    timing = pd.DataFrame(timing_rows)

    one = loaded.frames["1m"].copy()
    one["Date"] = pd.to_datetime(one["Date"], errors="raise")
    one["trade_date"] = one["Date"].dt.normalize()
    daily_close = one.groupby("trade_date", sort=True, observed=True)["Close"].last().astype(float)
    regime_spec = protocol["regime"]
    regimes = classify_lagged_daily_regime(
        daily_close,
        lookback=int(regime_spec["lookback"]),
        er_threshold=float(regime_spec["er_threshold"]),
    )
    audit["regime"] = regimes["regime"].reindex(audit["entry_date"]).astype(str).to_numpy()
    regime_rows = []
    for label, group in audit.groupby("regime", observed=True):
        regime_rows.append({
            "regime": label,
            "episodes": int(len(group)),
            "stress_mean_return": float(group["stress_return"].mean()),
            "stress_profit_factor": _profit_factor(group["stress_return"]),
        })
    regime_metrics = pd.DataFrame(regime_rows)

    audit["entry_year"] = audit["entry_date"].dt.year
    loo_rows = []
    for year in sorted(audit["entry_year"].unique()):
        retained = audit.loc[audit["entry_year"].ne(year), "stress_return"]
        loo_rows.append({
            "excluded_year": int(year),
            "episodes": int(len(retained)),
            "stress_mean_return": float(retained.mean()),
            "stress_profit_factor": _profit_factor(retained),
        })
    leave_one_year_out = pd.DataFrame(loo_rows)

    audit.to_csv(artifacts / "path_decomposition.csv.gz", index=False, encoding="utf-8-sig", compression={"method": "gzip", "compresslevel": 9, "mtime": 0}, lineterminator="\n")
    timing.to_csv(artifacts / "timing_sensitivity.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    pd.concat(timing_returns, ignore_index=True).to_csv(artifacts / "timing_returns.csv.gz", index=False, encoding="utf-8-sig", compression={"method": "gzip", "compresslevel": 9, "mtime": 0}, lineterminator="\n")
    regime_metrics.to_csv(artifacts / "regime_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    leave_one_year_out.to_csv(artifacts / "leave_one_year_out.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    ten = timing.loc[timing["variant"].eq("10:00_CLOSE") & timing["dimension"].eq("ENTRY_DELAY")].iloc[0]
    checks = {
        "primary_reconstruction": max_diff <= float(acceptance["primary_reconstruction_tolerance"]),
        "leave_one_year_out": bool(leave_one_year_out["stress_mean_return"].gt(float(acceptance["leave_one_year_out_mean_min_exclusive"])).all()),
        "ten_o_clock_mean": float(ten["stress_mean_return"]) > float(acceptance["ten_o_clock_mean_min_exclusive"]),
        "ten_o_clock_profit_factor": float(ten["stress_profit_factor"]) > float(acceptance["ten_o_clock_profit_factor_min_exclusive"]),
    }
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "source_episode_sha256": source_manifest["files"]["artifacts/episodes.csv.gz"]["sha256"],
        "episodes": int(len(audit)),
        "primary_reconstruction_max_abs_diff": max_diff,
        "mean_signal_to_entry_gap": float(audit["signal_to_entry_gap"].mean()),
        "mean_entry_day_session_return": float(audit["entry_day_session_return"].mean()),
        "mean_exit_overnight_return": float(audit["exit_overnight_return"].mean()),
        "checks": checks,
        "route_decision": "CONTINUE_ATTRIBUTION" if all(checks.values()) else "STOP_OR_REVIEW",
        "candidate_created": False,
    }
    _write(artifacts / "structural_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。{len(audit)}笔固定事件完成路径拆分、"
        "入场与退出时点敏感性、S001滞后状态分层和逐年删除审计。主收益逐笔重建一致。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n"
        f"- 信号收盘至次日入场的未交易缺口均值：{summary['mean_signal_to_entry_gap']:.3%}；\n"
        f"- 入场日至收盘收益均值：{summary['mean_entry_day_session_return']:.3%}；\n"
        f"- 收盘至退出开盘收益均值：{summary['mean_exit_overnight_return']:.3%}；\n"
        f"- 10:00延迟入场压力收益：{float(ten['stress_mean_return']):.3%}，盈亏比{float(ten['stress_profit_factor']):.2f}；\n"
        f"- 删除任一年度后的最差压力收益均值：{leave_one_year_out['stress_mean_return'].min():.3%}。\n\n"
        f"结构审计结论：{'通过，可继续归因审计' if all(checks.values()) else '未通过或需复核'}。"
        "状态分层和延迟退出仅作解释，本轮没有创建候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": dataset["cutoff"],
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
