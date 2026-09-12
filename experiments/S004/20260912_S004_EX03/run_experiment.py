from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260912_S004_EX03"


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


def _path_efficiency(frame: pd.DataFrame) -> float:
    returns = frame["Close"].pct_change(fill_method=None)
    first = float(frame.iloc[0]["Close"] / frame.iloc[0]["Open"] - 1.0)
    variation = float(returns.iloc[1:].abs().sum() + abs(first))
    displacement = abs(float(frame.iloc[-1]["Close"] / frame.iloc[0]["Open"] - 1.0))
    return displacement / variation if variation > 0 else 0.0


def _build_features(one_minute: pd.DataFrame) -> pd.DataFrame:
    bars = one_minute.copy()
    bars["Date"] = pd.to_datetime(bars["Date"], errors="raise")
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    rows: list[dict[str, object]] = []
    for trade_date, day in bars.groupby("trade_date", sort=True, observed=True):
        opening = day.loc[day["clock"].between("09:31", "10:30", inclusive="both")]
        late = day.loc[day["clock"].between("14:01", "15:00", inclusive="both")]
        if len(day) != 240 or len(opening) != 60 or len(late) != 60:
            raise ValueError(f"{trade_date.date()}: incomplete minute session")
        opening_return = float(opening.iloc[-1]["Close"] / opening.iloc[0]["Open"] - 1.0)
        late_return = float(late.iloc[-1]["Close"] / late.iloc[0]["Open"] - 1.0)
        rows.append(
            {
                "trade_date": trade_date,
                "opening_return": opening_return,
                "opening_efficiency": _path_efficiency(opening),
                "late_return": late_return,
                "late_efficiency": _path_efficiency(late),
                "opening_signal_time": opening.iloc[-1]["Date"],
                "late_signal_time": late.iloc[-1]["Date"],
            }
        )
    features = pd.DataFrame(rows).set_index("trade_date")
    features["OPENING_SELLOFF_REVERSAL_score"] = (-features["opening_return"]).clip(lower=0.0)
    features["OPENING_BREAKOUT_CONTINUATION_score"] = (
        features["opening_return"].clip(lower=0.0) * features["opening_efficiency"]
    )
    features["LATE_SELLOFF_REVERSAL_score"] = (-features["late_return"]).clip(lower=0.0)
    features["LATE_BREAKOUT_CONTINUATION_score"] = (
        features["late_return"].clip(lower=0.0) * features["late_efficiency"]
    )
    return features


def _cooldown_dates(
    raw_dates: pd.DatetimeIndex,
    calendar: pd.DatetimeIndex,
    cooldown_sessions: int,
) -> pd.DatetimeIndex:
    positions = {value: index for index, value in enumerate(calendar)}
    kept: list[pd.Timestamp] = []
    last_position = -10**9
    for value in raw_dates.sort_values():
        position = positions[value]
        if position - last_position > cooldown_sessions:
            kept.append(value)
            last_position = position
    return pd.DatetimeIndex(kept, name="trade_date")


def _rolling_density(
    dates: pd.DatetimeIndex,
    calendar: pd.DatetimeIndex,
    window: int,
) -> tuple[float, float, float, float]:
    flags = pd.Series(0, index=calendar, dtype="int64")
    flags.loc[flags.index.intersection(dates)] = 1
    rolling = flags.rolling(window, min_periods=window).sum().dropna()
    if rolling.empty:
        return 0.0, 0.0, 0.0, 0.0
    return (
        float(rolling.median()),
        float(rolling.quantile(0.10, interpolation="lower")),
        float(rolling.min()),
        float(rolling.max()),
    )


def _run(protocol: dict[str, object], bars: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    features = _build_features(bars)
    threshold_spec = protocol["threshold"]
    density_spec = protocol["density"]
    lookback = int(threshold_spec["lookback_sessions"])
    lag = int(threshold_spec["lag_sessions"])
    quantile = float(threshold_spec["quantile"])
    evaluation_start = pd.Timestamp(protocol["dataset"]["evaluation_start"])
    calendar = pd.DatetimeIndex(features.index, name="trade_date")
    event_frames: list[pd.DataFrame] = []
    density_rows: list[dict[str, object]] = []
    mechanisms = (
        "OPENING_SELLOFF_REVERSAL",
        "OPENING_BREAKOUT_CONTINUATION",
        "LATE_SELLOFF_REVERSAL",
        "LATE_BREAKOUT_CONTINUATION",
    )
    for mechanism in mechanisms:
        score_column = f"{mechanism}_score"
        threshold = (
            features[score_column]
            .shift(lag)
            .rolling(lookback, min_periods=lookback)
            .quantile(quantile)
        )
        raw = threshold.notna() & features[score_column].gt(0.0) & features[score_column].ge(threshold)
        raw_dates = pd.DatetimeIndex(features.index[raw & (features.index >= evaluation_start)])
        kept = _cooldown_dates(raw_dates, calendar, int(density_spec["cooldown_sessions"]))
        selected = features.loc[kept].copy().reset_index()
        selected.insert(0, "mechanism_id", mechanism)
        selected["score"] = selected[score_column]
        selected["threshold"] = threshold.reindex(kept).to_numpy()
        selected["signal_time"] = selected[
            "opening_signal_time" if mechanism.startswith("OPENING") else "late_signal_time"
        ]
        selected["planned_entry"] = "10:35 SAME_SESSION" if mechanism.startswith("OPENING") else "09:35 NEXT_SESSION"
        selected["planned_exit"] = "10:35 NEXT_SESSION" if mechanism.startswith("OPENING") else "09:35 SESSION_AFTER_ENTRY"
        event_frames.append(selected)
        density_calendar = calendar[calendar >= evaluation_start]
        median, p10, minimum, maximum = _rolling_density(
            kept, density_calendar, int(density_spec["window_sessions"])
        )
        capable = bool(
            int(density_spec["target_median_min"]) <= median <= int(density_spec["target_median_max"])
            and p10 >= int(density_spec["p10_min"])
        )
        density_rows.append(
            {
                "mechanism_id": mechanism,
                "raw_events": int(len(raw_dates)),
                "independent_events": int(len(kept)),
                "rolling_median_events": median,
                "rolling_p10_events": p10,
                "rolling_min_events": minimum,
                "rolling_max_events": maximum,
                "density_pass": capable,
                "evidence": "DENSITY_CAPABLE" if capable else "EVIDENCE_RATE_FAIL",
            }
        )
        features[f"{mechanism}_threshold"] = threshold
        features[f"{mechanism}_raw_event"] = raw
    events = pd.concat(event_frames, ignore_index=True)
    return features.reset_index(), events, pd.DataFrame(density_rows)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_event_prices"):
        raise ValueError("density census may not read post-event prices")
    if any(bool(protocol.get(key)) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("density census may not mutate lifecycle state")

    target = protocol["research_target"]
    loaded = load_intraday_research_data(repo / str(protocol["dataset"]["directory"]), str(target["symbol"]))
    bars = loaded.frames["1m"]
    features, events, density = _run(protocol, bars)

    boundary = pd.Timestamp("2026-01-05")
    mutated = bars.copy()
    mask = mutated["Date"].dt.normalize() >= boundary
    mutated.loc[mask, ["Open", "High", "Low", "Close"]] *= 1.1
    mutated_features, mutated_events, _ = _run(protocol, mutated)
    prefix = features.loc[features["trade_date"] < boundary].reset_index(drop=True)
    mutated_prefix = mutated_features.loc[mutated_features["trade_date"] < boundary].reset_index(drop=True)
    pd.testing.assert_frame_equal(prefix, mutated_prefix)
    event_prefix = events.loc[events["trade_date"] < boundary].reset_index(drop=True)
    mutated_event_prefix = mutated_events.loc[mutated_events["trade_date"] < boundary].reset_index(drop=True)
    pd.testing.assert_frame_equal(event_prefix, mutated_event_prefix)

    features.to_csv(artifacts / "mechanism_features.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    events.to_csv(artifacts / "mechanism_events.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    density.to_csv(artifacts / "density_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    capable = density.loc[density["density_pass"]]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "causal_prefix_mutation_audit": "PASS",
        "reads_post_event_prices": False,
        "mechanisms": int(len(density)),
        "density_capable": int(len(capable)),
        "eligible_for_return_test": capable["mechanism_id"].tolist(),
    }
    _write(artifacts / "census_summary.json", summary)
    table = [
        "|机制|原始事件|独立事件|60日中位/P10|最小/最大|证据|",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in density.itertuples(index=False):
        table.append(
            f"|{row.mechanism_id}|{row.raw_events}|{row.independent_events}|"
            f"{row.rolling_median_events:.0f}/{row.rolling_p10_events:.0f}|"
            f"{row.rolling_min_events:.0f}/{row.rolling_max_events:.0f}|{row.evidence}|"
        )
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。四个预注册机制中{len(capable)}个通过"
        "事件密度门；未来区间扰动不改变历史前缀，因果审计通过。未读取收益。\n",
        encoding="utf-8",
    )
    decision = "通过者进入独立收益方向实验。" if len(capable) else "没有机制达到目标，当前路线停止。"
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n" + "\n".join(table) + f"\n\n{decision} 本轮没有创建候选。\n",
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
