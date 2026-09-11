from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.intraday_volume_imbalance import census_first_hour_volume_imbalance


EXPERIMENT_ID = "20260910_S003_EX11"


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
    event = protocol["event"]
    density = protocol["density"]
    dataset = protocol["dataset"]
    return census_first_hour_volume_imbalance(
        bars,
        evaluation_start=str(dataset["evaluation_start"]),
        observation_start_clock=str(event["observation_start_clock"]),
        observation_end_clock=str(event["observation_end_clock"]),
        expected_bars=int(event["expected_one_minute_bars"]),
        threshold_lookback_sessions=int(event["threshold_lookback_sessions"]),
        threshold_quantile=float(event["threshold_quantile"]),
        threshold_lag_sessions=int(event["threshold_lag_sessions"]),
        density_window_sessions=int(density["window_sessions"]),
        median_events_required=int(density["median_independent_events_required"]),
        p10_events_required=int(density["p10_independent_events_required"]),
        distinct_event_days_required=int(density["distinct_event_days_required"]),
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_event_prices") or protocol.get("assigns_trade_direction"):
        raise ValueError("EX11 must remain a pre-return event census")
    if any(
        bool(protocol.get(key))
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX11 may not mutate strategy lifecycle state")

    target = protocol["research_target"]
    intraday = load_intraday_research_data(repo / "data" / "raw", str(target["symbol"]))
    bars = intraday.frames["1m"]
    result = _run(protocol, bars)

    mutation_boundary = pd.Timestamp("2026-01-05")
    mutated = bars.copy()
    mask = mutated["Date"].dt.normalize() >= mutation_boundary
    mutated.loc[mask, "Volume"] *= 2.0
    mutated.loc[mask, "Close"] *= 1.1
    rerun = _run(protocol, mutated)
    prefix = result.features.loc[result.features["trade_date"] < mutation_boundary].reset_index(drop=True)
    mutated_prefix = rerun.features.loc[rerun.features["trade_date"] < mutation_boundary].reset_index(drop=True)
    pd.testing.assert_frame_equal(prefix, mutated_prefix)

    result.features.to_csv(
        artifacts / "first_hour_features.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    result.events.to_csv(
        artifacts / "volume_imbalance_events.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    _write(artifacts / "density_summary.json", result.density)
    _write(
        artifacts / "run_evidence.json",
        {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "status": "PASS",
            "causal_prefix_mutation_audit": "PASS",
            "reads_post_event_prices": False,
            "assigns_trade_direction": False,
            "input_manifest": intraday.manifest,
            "density": result.density,
        },
    )
    density = result.density
    (experiment / "03_execution.md").write_text(
        "# S003 EX11 执行\n\n"
        f"状态：COMPLETE。共覆盖{density['calendar_sessions']}个正式观察交易日，形成"
        f"{density['independent_events']}个首小时量价失衡事件；滚动60日中位数"
        f"{density['rolling_median_events']:.0f}、P10为{density['rolling_p10_events']:.0f}。"
        "未来区间扰动不改变历史前缀，因果审计通过。\n",
        encoding="utf-8",
    )
    conclusion = (
        "事件供给达到现行证据速度，可以进入延续与反转方向竞争。"
        if density["density_pass"]
        else "事件供给未达到现行证据速度，本机制路线停止。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX11 结论\n\n"
        f"共{density['independent_events']}个事件，买压{density['buy_pressure_events']}个、"
        f"卖压{density['sell_pressure_events']}个；量差与首小时价格方向一致率为"
        f"{density['price_direction_agreement']:.1%}。{conclusion}\n\n"
        "本轮没有读取10:30之后价格、没有赋予买卖方向，也没有创建候选。\n",
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
