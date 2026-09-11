from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_inventory import evaluate_inventory_mechanisms


EXPERIMENT_ID = "20260910_S003_EX06"


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


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in (
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX06 may not mutate strategy lifecycle state")
    target = protocol["research_target"]
    dataset = protocol["dataset"]
    execution = protocol["execution"]
    density = protocol["density"]
    control = protocol["matched_random_control"]
    census_dir = repo / "experiments" / str(dataset["census_experiment"])
    regime_dir = repo / "experiments" / str(dataset["regime_experiment"])
    hypothesis_dir = repo / "experiments" / str(dataset["hypothesis_experiment"])
    census_manifest = validate_experiment_archive(census_dir)
    regime_manifest = validate_experiment_archive(regime_dir)
    hypothesis_manifest = validate_experiment_archive(hypothesis_dir)
    previous_protocol = _read(hypothesis_dir / "artifacts" / "protocol.json")
    if previous_protocol["hypotheses"] != protocol["hypotheses"]:
        raise ValueError("EX06 hypotheses differ from the pre-return EX05 registration")
    events = pd.read_csv(census_dir / "artifacts" / "signal_events.csv.gz")
    regimes = pd.read_csv(regime_dir / "artifacts" / "daily_regimes.csv")
    daily_regime = regimes.set_index(pd.to_datetime(regimes["date"]))["regime"]
    intraday = load_intraday_research_data(repo / "data" / "raw", str(target["symbol"]))

    result = evaluate_inventory_mechanisms(
        events,
        intraday.frames["15m"],
        intraday.frames["5m"],
        daily_regime,
        protocol["hypotheses"],
        evaluation_start=str(dataset["evaluation_start"]),
        exit_bars=execution["exit_bars"],
        baseline_one_way_cost=float(execution["baseline_one_way_cost"]),
        stress_one_way_cost=float(execution["stress_one_way_cost"]),
        base_fraction=float(execution["base_fraction"]),
        density_window_sessions=int(density["window_sessions"]),
        median_episodes_required=int(density["median_closed_episodes_required"]),
        p10_episodes_required=int(density["p10_closed_episodes_required"]),
        matched_random_simulations=int(control["simulations"]),
        random_seed=int(control["seed"]),
    )
    outputs = {
        "episodes.csv.gz": result.episodes,
        "mechanism_metrics.csv": result.metrics,
        "annual_metrics.csv": result.annual,
        "regime_metrics.csv": result.regime,
        "account_metrics.csv": result.account,
    }
    for name, frame in outputs.items():
        compression = (
            {"method": "gzip", "compresslevel": 9, "mtime": 0}
            if name.endswith(".gz")
            else None
        )
        frame.to_csv(
            artifacts / name,
            index=False,
            encoding="utf-8-sig",
            compression=compression,
            lineterminator="\n",
        )
    counts = result.metrics["evidence"].value_counts().sort_index().to_dict()
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "input_manifests": {
            "census": census_manifest["files"]["artifacts/signal_events.csv.gz"]["sha256"],
            "regime": regime_manifest["files"]["artifacts/daily_regimes.csv"]["sha256"],
            "hypothesis": hypothesis_manifest["files"]["artifacts/protocol.json"]["sha256"],
        },
        "evaluated_combinations": len(result.metrics),
        "evidence_counts": {str(key): int(value) for key, value in counts.items()},
        "candidate_created": False,
    }
    _write(artifacts / "inventory_summary.json", summary)
    strongest = result.metrics.sort_values(
        ["density_pass", "matched_random_percentile", "mean_return"],
        ascending=[False, False, False],
    ).head(7)
    table = [
        "|机制|持有分钟|标签|30日中位/P10|压力均值|盈亏比|随机分位|相对静态终值|",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in strongest.itertuples(index=False):
        table.append(
            f"|{row.hypothesis_id}|{row.exit_trading_minutes}|{row.evidence}|"
            f"{row.rolling_30d_median_episodes:.0f}/{row.rolling_30d_p10_episodes:.0f}|"
            f"{row.mean_return:.3%}|{row.profit_factor:.2f}|"
            f"{row.matched_random_percentile:.1%}|"
            f"{row.incremental_terminal_return_vs_static:.1%}|"
        )
    supported = int((result.metrics["evidence"] == "SUPPORTED").sum())
    rate_pass = int(result.metrics["density_pass"].sum())
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n"
        f"状态：COMPLETE。7个预注册机制与4个固定日内持有时长形成28个组合；"
        f"{rate_pass}个组合通过证据速度，{supported}个组合取得SUPPORTED标签。"
        "底仓、现金、T+1库存轮换及静态基准均按协议计算。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX06 结论\n\n"
        + "\n".join(table)
        + "\n\n"
        + f"本轮共有{rate_pass}/28个组合满足OPC证据速度，{supported}/28个组合取得"
        "SUPPORTED。表中展示预注册口径下证据最强的固定组合。\n\n"
        "SUPPORTED仍只表示底仓日内滚动架构值得继续形成简化原型。若结果为零，则当前"
        "7个信号家族在12bp压力成本下不具备可执行日内增量，S003应停止围绕它们继续调参。\n",
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
