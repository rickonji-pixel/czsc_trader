from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260912_S004_EX11"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _net(entry: float, exit_: float, cost: float) -> float:
    return exit_ * (1.0 - cost) / (entry * (1.0 + cost)) - 1.0


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0.0].sum())
    losses = float(-values.loc[values < 0.0].sum())
    if losses == 0.0:
        return float("inf") if gains > 0.0 else 0.0
    return gains / losses


def _session_prices(one_minute: pd.DataFrame) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    bars = one_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    grouped = bars.groupby("trade_date", sort=True, observed=True)
    if not grouped.size().eq(240).all():
        raise ValueError("structural audit requires complete 240-bar sessions")
    prices = pd.DataFrame(index=pd.DatetimeIndex(grouped.size().index, name="trade_date"))
    prices["session_open"] = grouped["Open"].first().astype(float)
    prices["session_close"] = grouped["Close"].last().astype(float)
    for clock in ("10:00", "10:30"):
        selected = bars.loc[bars["clock"].eq(clock)].set_index("trade_date")
        prices[f"close_{clock.replace(':', '')}"] = selected["Close"].astype(float)
    return prices, pd.DatetimeIndex(prices.index, name="trade_date")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(bool(protocol.get(key)) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("corrected audit may not mutate lifecycle state")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    mechanisms = list(map(str, protocol["mechanisms"]))
    execution = protocol["execution"]
    acceptance = protocol["acceptance"]
    source_dir = repo / "experiments" / "S004" / str(dataset["source_experiment"])
    source_manifest = validate_experiment_archive(source_dir)
    source_summary = _read(source_dir / "artifacts" / "direction_summary.json")
    if not set(mechanisms).issubset(set(source_summary["feasible_mechanisms"])):
        raise ValueError("one or more corrected-audit inputs lost the direction prerequisite")
    source = pd.read_csv(source_dir / "artifacts" / "proxy_episodes.csv.gz", parse_dates=["event_date"])
    loaded = load_intraday_research_data(repo / str(dataset["directory"]), str(target["symbol"]))
    prices, calendar = _session_prices(loaded.frames["1m"])
    positions = {value: index for index, value in enumerate(calendar)}
    cost = float(execution["stress_one_way_cost"])
    tolerance = float(execution["source_reconstruction_tolerance"])

    date_sets = {
        mechanism: set(pd.DatetimeIndex(source.loc[source["mechanism_id"].eq(mechanism), "event_date"]))
        for mechanism in mechanisms
    }
    overlap_rows = []
    for left, right in combinations(mechanisms, 2):
        intersection = date_sets[left] & date_sets[right]
        union = date_sets[left] | date_sets[right]
        overlap_rows.append({"left": left, "right": right, "intersection": len(intersection), "union": len(union), "jaccard": len(intersection) / len(union)})
    overlap = pd.DataFrame(overlap_rows)

    path_rows: list[dict[str, object]] = []
    loo_rows: list[dict[str, object]] = []
    metric_rows: list[dict[str, object]] = []
    reconstruction_max = 0.0
    for mechanism in mechanisms:
        source_returns = source.loc[source["mechanism_id"].eq(mechanism)].set_index("event_date")["stress_return"].astype(float)
        records: list[dict[str, object]] = []
        for event_date in sorted(date_sets[mechanism]):
            position = positions.get(event_date)
            if position is None or position + 2 >= len(calendar):
                continue
            entry_date = calendar[position + 1]
            exit_date = calendar[position + 2]
            signal_close = float(prices.loc[event_date, "session_close"])
            entry_open = float(prices.loc[entry_date, "session_open"])
            entry_close = float(prices.loc[entry_date, "session_close"])
            exit_open = float(prices.loc[exit_date, "session_open"])
            primary = _net(entry_open, exit_open, cost)
            difference = abs(primary - float(source_returns.loc[event_date]))
            reconstruction_max = max(reconstruction_max, difference)
            row: dict[str, object] = {
                "mechanism_id": mechanism,
                "event_date": event_date,
                "entry_date": entry_date,
                "entry_year": entry_date.year,
                "signal_to_entry_gap": entry_open / signal_close - 1.0,
                "entry_day_session": entry_close / entry_open - 1.0,
                "post_close_overnight": exit_open / entry_close - 1.0,
                "primary_stress_return": primary,
                "source_stress_return": float(source_returns.loc[event_date]),
                "reconstruction_error": difference,
            }
            for clock in map(str, execution["delayed_entry_clocks"]):
                delayed_entry = float(prices.loc[entry_date, f"close_{clock.replace(':', '')}"])
                row[f"entry_{clock.replace(':', '')}_stress_return"] = _net(delayed_entry, exit_open, cost)
            records.append(row)
        frame = pd.DataFrame(records)
        path_rows.extend(records)
        mechanism_loo = []
        for excluded_year in sorted(frame["entry_year"].unique()):
            remaining = frame.loc[frame["entry_year"].ne(excluded_year), "primary_stress_return"]
            record = {"mechanism_id": mechanism, "excluded_year": int(excluded_year), "remaining_episodes": int(len(remaining)), "mean_return": float(remaining.mean())}
            loo_rows.append(record)
            mechanism_loo.append(record)
        loo_min = min(float(row["mean_return"]) for row in mechanism_loo)
        delayed = frame["entry_1000_stress_return"]
        checks = {
            "entry_day_session": float(frame["entry_day_session"].mean()) > float(acceptance["entry_day_session_mean_min_exclusive"]),
            "delayed_1000_mean": float(delayed.mean()) > float(acceptance["delayed_1000_mean_min_exclusive"]),
            "delayed_1000_profit_factor": _profit_factor(delayed) > float(acceptance["delayed_1000_profit_factor_min_exclusive"]),
            "leave_one_year_out": loo_min > float(acceptance["leave_one_year_out_min_mean_exclusive"]),
        }
        metric_rows.append(
            {
                "mechanism_id": mechanism,
                "episodes": int(len(frame)),
                "signal_to_entry_gap_mean": float(frame["signal_to_entry_gap"].mean()),
                "entry_day_session_mean": float(frame["entry_day_session"].mean()),
                "post_close_overnight_mean": float(frame["post_close_overnight"].mean()),
                "primary_stress_mean": float(frame["primary_stress_return"].mean()),
                "entry_1000_stress_mean": float(delayed.mean()),
                "entry_1000_profit_factor": _profit_factor(delayed),
                "entry_1030_stress_mean": float(frame["entry_1030_stress_return"].mean()),
                "leave_one_year_out_min_mean": loo_min,
                "checks": json.dumps(checks, ensure_ascii=False, sort_keys=True),
                "evidence": "STRUCTURE_SUPPORTED" if all(checks.values()) else "STRUCTURE_MIXED",
            }
        )
    if reconstruction_max > tolerance:
        raise ValueError(f"source execution reconstruction failed: {reconstruction_max}")

    paths = pd.DataFrame(path_rows)
    loo = pd.DataFrame(loo_rows)
    metrics = pd.DataFrame(metric_rows)
    supported = metrics.loc[metrics["evidence"].eq("STRUCTURE_SUPPORTED")]
    family_supported = len(supported) >= int(acceptance["structurally_supported_mechanisms_min"])
    paths.to_csv(artifacts / "path_decomposition.csv.gz", index=False, encoding="utf-8-sig", compression={"method": "gzip", "compresslevel": 9, "mtime": 0}, lineterminator="\n")
    loo.to_csv(artifacts / "leave_one_year_out.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    overlap.to_csv(artifacts / "event_overlap.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    metrics.to_csv(artifacts / "structural_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "source_sha256": source_manifest["files"]["artifacts/proxy_episodes.csv.gz"]["sha256"],
        "source_reconstruction_max_absolute_error": reconstruction_max,
        "supported_mechanisms": supported["mechanism_id"].tolist(),
        "event_overlap_jaccard_range": [float(overlap["jaccard"].min()), float(overlap["jaccard"].max())],
        "evidence": "MECHANISM_FAMILY_SUPPORTED" if family_supported else "MECHANISM_FAMILY_UNRESOLVED",
        "candidate_created": False,
    }
    _write(artifacts / "structural_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# 20260912_S004_EX11 执行\n\n状态：COMPLETE。逐笔重建EX09压力收益，最大绝对误差{reconstruction_max:.3e}；"
        "随后完成重叠、路径、延迟入场与逐年删除审计。\n",
        encoding="utf-8",
    )
    lines = ["# 20260912_S004_EX11 结论", "", "|代理|事件|次日盘中|10:00延迟|延迟盈亏比|逐年删除最差|标签|", "|---|---:|---:|---:|---:|---:|---|"]
    for row in metrics.itertuples(index=False):
        lines.append(f"|{row.mechanism_id}|{row.episodes}|{row.entry_day_session_mean:.3%}|{row.entry_1000_stress_mean:.3%}|{row.entry_1000_profit_factor:.2f}|{row.leave_one_year_out_min_mean:.3%}|{row.evidence}|")
    lines.extend(["", f"事件Jaccard重叠率：{overlap['jaccard'].min():.1%}—{overlap['jaccard'].max():.1%}。", f"机制家族标签：`{summary['evidence']}`。本轮没有创建候选。", ""])
    (experiment / "04_conclusion.md").write_text("\n".join(lines), encoding="utf-8")
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
