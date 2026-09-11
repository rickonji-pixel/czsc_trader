from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.early_session_events import census_early_session_events
from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260911_S003_EX17"


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


def _run(protocol: dict[str, object], bars: pd.DataFrame):
    threshold = protocol["threshold"]
    density = protocol["density"]
    return census_early_session_events(
        bars,
        protocol["mechanisms"],
        evaluation_start=str(protocol["dataset"]["evaluation_start"]),
        threshold_lookback_sessions=int(threshold["lookback_sessions"]),
        threshold_quantile=float(threshold["quantile"]),
        threshold_lag_sessions=int(threshold["lag_sessions"]),
        density_window_sessions=int(density["window_sessions"]),
        target_median_min=int(density["target_median_min"]),
        target_median_max=int(density["target_median_max"]),
        median_events_required=int(density["median_events_required"]),
        p10_events_required=int(density["p10_events_required"]),
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_information_prices"):
        raise ValueError("EX17 must not read post-information prices")
    if any(
        bool(protocol.get(key))
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX17 may not mutate strategy lifecycle state")

    target = protocol["research_target"]
    intraday = load_intraday_research_data(repo / "data" / "raw", str(target["symbol"]))
    bars = intraday.frames[str(protocol["dataset"]["frequency"])]
    result = _run(protocol, bars)

    mutation_boundary = pd.Timestamp("2026-01-05")
    mutated = bars.copy()
    mask = mutated["Date"].dt.normalize() >= mutation_boundary
    mutated.loc[mask, ["Open", "Close"]] *= 1.1
    rerun = _run(protocol, mutated)
    prefix = result.features.loc[result.features["trade_date"] < mutation_boundary]
    mutated_prefix = rerun.features.loc[rerun.features["trade_date"] < mutation_boundary]
    pd.testing.assert_frame_equal(
        prefix.reset_index(drop=True),
        mutated_prefix.reset_index(drop=True),
    )

    result.features.to_csv(
        artifacts / "early_session_features.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    result.events.to_csv(
        artifacts / "mechanism_events.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    result.density.to_csv(
        artifacts / "density_metrics.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    capable = result.density.loc[result.density["density_pass"]]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "causal_prefix_mutation_audit": "PASS",
        "reads_post_information_prices": False,
        "mechanisms": int(len(result.density)),
        "density_capable": int(len(capable)),
        "inside_target_band": int(result.density["inside_target_band"].sum()),
        "eligible_for_direction_test": capable["mechanism_id"].tolist(),
    }
    _write(artifacts / "census_summary.json", summary)
    table = [
        "|机制|事件数|多头/减仓|60日中位/P10|最小/最大|目标带|证据|",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for row in result.density.itertuples(index=False):
        table.append(
            f"|{row.mechanism_id}|{row.events}|{row.long_events}/{row.short_overlay_events}|"
            f"{row.rolling_median_events:.0f}/{row.rolling_p10_events:.0f}|"
            f"{row.rolling_min_events:.0f}/{row.rolling_max_events:.0f}|"
            f"{'是' if row.inside_target_band else '否'}|{row.evidence}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S003 EX17 执行\n\n"
        f"状态：COMPLETE。3个机制中{len(capable)}个通过密度底线，"
        f"{int(result.density['inside_target_band'].sum())}个进入12—20笔目标带。"
        "未来区间扰动不改变历史前缀，因果审计通过。\n",
        encoding="utf-8",
    )
    decision = (
        "密度合格机制可按冻结方向和执行时点进入EX18收益检验。"
        if len(capable)
        else "没有机制达到中频密度底线，本轮停止。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX17 结论\n\n"
        + "\n".join(table)
        + "\n\n"
        + decision
        + " 本实验没有读取执行后价格，也没有创建候选。\n",
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
            "development_cutoff": protocol["dataset"]["cutoff"],
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
