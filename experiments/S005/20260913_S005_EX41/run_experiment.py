from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX41"


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    return gains / losses if losses else (float("inf") if gains else 0.0)


def _bars(repo: Path, cutoff: pd.Timestamp) -> pd.DataFrame:
    frames = [pd.read_csv(path, parse_dates=["datetime"]) for path in sorted((repo / "data/raw").glob("588080_1m_*.csv"))]
    data = pd.concat(frames, ignore_index=True).sort_values("datetime")
    data = data.loc[data["datetime"].dt.normalize().le(cutoff)].copy()
    data["date"] = data["datetime"].dt.normalize()
    data["clock"] = data["datetime"].dt.strftime("%H:%M")
    if not data.groupby("date").size().eq(240).all():
        raise ValueError("incomplete minute sessions")
    return data


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    source = repo / "experiments/S005" / protocol["source"]["experiment_id"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": protocol["source"]["manifest_sha256"],
        source / "artifacts/frozen_signal_dates.csv": protocol["source"]["signal_dates_sha256"],
        source / "artifacts/selection_evidence.json": protocol["source"]["selection_evidence_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"preregistered source differs: {path.name}")

    signals = pd.read_csv(source / "artifacts/frozen_signal_dates.csv", parse_dates=["signal_date"])
    execution = protocol["execution"]
    bars = _bars(repo, pd.Timestamp(protocol["development_cutoff"]))
    calendar = pd.DatetimeIndex(sorted(bars["date"].unique()))
    positions = {date: position for position, date in enumerate(calendar)}
    clocks = [execution["entry_clock"], execution["exit_clock"]]
    prices = {
        (date, clock): float(group.loc[group["clock"].eq(clock), "open"].iloc[0])
        for date, group in bars.groupby("date")
        for clock in clocks
    }
    cost = float(execution["stress_one_way_cost"])
    rows: list[dict[str, object]] = []
    for signal in signals.itertuples(index=False):
        position = positions.get(signal.signal_date)
        exit_offset = int(execution["exit_session_offset"])
        if position is None or position + exit_offset >= len(calendar):
            continue
        entry_date = calendar[position + int(execution["entry_session_offset"])]
        exit_date = calendar[position + exit_offset]
        entry_price = prices[(entry_date, execution["entry_clock"])]
        exit_price = prices[(exit_date, execution["exit_clock"])]
        gross = exit_price / entry_price - 1.0
        stress = exit_price * (1.0 - cost) / (entry_price * (1.0 + cost)) - 1.0
        rows.append(
            {
                "signal_date": signal.signal_date,
                "unique_positive_events": signal.unique_positive_events,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "gross_return": gross,
                "stress_return": stress,
            }
        )
    trades = pd.DataFrame(rows)
    metrics = {
        "frozen_signal_dates": int(len(signals)),
        "closed_return_paths": int(len(trades)),
        "gross_mean": float(trades["gross_return"].mean()),
        "gross_median": float(trades["gross_return"].median()),
        "stress_mean": float(trades["stress_return"].mean()),
        "stress_median": float(trades["stress_return"].median()),
        "stress_win_rate": float(trades["stress_return"].gt(0).mean()),
        "stress_profit_factor": _profit_factor(trades["stress_return"]),
    }
    acceptance = protocol["directional_acceptance"]
    checks = {
        "stress_mean": metrics["stress_mean"] > float(acceptance["stress_mean_min_exclusive"]),
        "profit_factor": metrics["stress_profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
    }
    decision = (
        "DIRECTIONALLY_SUPPORTIVE_MORE_MANUAL_DATES_REQUIRED"
        if all(checks.values())
        else "STOP_GENERIC_POSITIVE_NEWS_CONTINUATION"
    )
    trades.to_csv(artifacts / "return_paths.csv", index=False, lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "mechanism": protocol["mechanism"],
        "metrics": metrics,
        "checks": checks,
        "temporal_coverage_sufficient_for_candidate_evaluation": False,
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    (artifacts / "return_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX41 执行\n\n"
        f"状态：`COMPLETE`。按EX40冻结规则完成{len(trades)}条闭合收益路径；压力均值"
        f"{metrics['stress_mean']:.3%}、中位数{metrics['stress_median']:.3%}、胜率"
        f"{metrics['stress_win_rate']:.2%}、盈亏比{metrics['stress_profit_factor']:.2f}。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX41 结论\n\n"
        f"裁决：`{decision}`。本轮只有{len(trades)}条方向性收益路径，不能评价交易频率、年度稳定性、"
        "最大回撤或候选资格，也不得依据结果切分事件类型、主体、年份或持有期。"
        "本轮不创建候选，不修改SM或PTE。\n",
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
            "reads_post_event_prices": True,
            "candidate_created": False,
        },
    )
    validate_experiment_archive(experiment)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
