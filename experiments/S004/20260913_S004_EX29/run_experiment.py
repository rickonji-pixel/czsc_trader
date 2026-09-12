from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX29"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _build_features(panel: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    frame = panel.copy()
    frame["dt"] = pd.to_datetime(frame["dt"]).dt.normalize()
    observed = frame["observed_moneyflow"].astype(bool)
    frame["observed_weight"] = frame["weight"].where(observed, 0.0)
    frame["positive_weight"] = frame["weight"].where(
        observed & frame["net_mf_amount"].gt(0), 0.0
    )
    grouped = frame.groupby("dt", sort=True)
    features = grouped[["weight", "observed_weight", "positive_weight"]].sum()
    features["observed_weight_ratio"] = features["observed_weight"] / features["weight"]
    features["moneyflow_breadth"] = features["positive_weight"] / features["observed_weight"]
    market = daily[["dt", "close"]].copy()
    market["dt"] = pd.to_datetime(market["dt"]).dt.normalize()
    market = market.sort_values("dt")
    market["etf_return_pct"] = market["close"].pct_change() * 100.0
    market["etf_volatility_20"] = market["etf_return_pct"].rolling(
        20, min_periods=20
    ).std(ddof=1)
    market["prior_volatility_median_120"] = market["etf_volatility_20"].shift(1).rolling(
        120, min_periods=120
    ).median()
    market["low_volatility"] = (
        market["etf_volatility_20"] <= market["prior_volatility_median_120"]
    )
    return features.reset_index().merge(
        market[
            [
                "dt",
                "etf_return_pct",
                "etf_volatility_20",
                "prior_volatility_median_120",
                "low_volatility",
            ]
        ],
        on="dt",
        how="left",
        validate="one_to_one",
    )


def _density(
    calendar: pd.DatetimeIndex,
    event_dates: pd.DatetimeIndex,
    evaluation_start: pd.Timestamp,
) -> tuple[float, float, float]:
    activation = pd.Series(calendar.isin(event_dates), index=calendar, dtype=int)
    rolling = activation.rolling(60, min_periods=60).sum()
    eligible = rolling.loc[rolling.index >= evaluation_start].dropna()
    return float(eligible.median()), float(eligible.quantile(0.10)), float(eligible.min())


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "conditional_return_analysis",
            "parameter_selection_using_returns",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX29 may only perform return-free density calibration")

    source_spec = protocol["source"]
    source = repo / "experiments" / "S004" / source_spec["experiment_id"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["experiment_manifest_sha256"],
        source / "artifacts" / "constituent_moneyflow_panel.csv.gz": source_spec[
            "panel_sha256"
        ],
        source / "artifacts" / "data_quality.json": source_spec["data_quality_sha256"],
        repo / source_spec["market_manifest"]: source_spec["market_manifest_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read_json(source / "artifacts" / "data_quality.json")["passed"]:
        raise ValueError("EX28 moneyflow data gate did not pass")

    panel = pd.read_csv(source / "artifacts" / "constituent_moneyflow_panel.csv.gz")
    market = load_market_data(repo / "data" / "raw", "588080.SH").daily
    features = _build_features(panel, market)
    cutoff = pd.Timestamp(protocol["dataset"]["development_cutoff"])
    evaluation_start = pd.Timestamp(protocol["dataset"]["evaluation_start"])
    features = features.loc[features["dt"].le(cutoff)].sort_values("dt").reset_index(drop=True)
    calendar = pd.DatetimeIndex(features["dt"])
    next_session = pd.Series(calendar, index=calendar).shift(-1)
    spec = protocol["feature"]
    gate = protocol["density_gate"]

    rows: list[dict[str, object]] = []
    event_frames: list[pd.DataFrame] = []
    for quantile in spec["threshold_quantiles"]:
        threshold = features["moneyflow_breadth"].shift(1).rolling(
            int(spec["threshold_lookback_valid_sessions"]),
            min_periods=int(spec["threshold_lookback_valid_sessions"]),
        ).quantile(float(quantile))
        active = (
            features["moneyflow_breadth"].ge(threshold)
            & features["moneyflow_breadth"].gt(
                float(spec["minimum_moneyflow_breadth_exclusive"])
            )
            & features["etf_return_pct"].le(float(spec["maximum_etf_signal_day_return_pct"]))
            & features["observed_weight_ratio"].ge(
                float(spec["minimum_observed_weight_ratio"])
            )
        ).fillna(False)
        signal_dates = calendar[active]
        selected_signals = signal_dates[next_session.reindex(signal_dates).notna()]
        events = features.set_index("dt").loc[selected_signals].reset_index()
        events["event_date"] = events["dt"].map(next_session)
        events = events.loc[events["event_date"].ge(evaluation_start)].copy()
        path_id = f"MFU-Q{int(round(float(quantile) * 100)):02d}"
        events["path_id"] = path_id
        events["threshold_quantile"] = float(quantile)
        event_frames.append(events)
        event_dates = pd.DatetimeIndex(events["event_date"])
        median, p10, minimum = _density(calendar, event_dates, evaluation_start)
        density_eligible = bool(
            float(gate["median_min"]) <= median <= float(gate["median_max"])
            and p10 >= float(gate["p10_min"])
        )
        low_vol_events = int(events["low_volatility"].sum())
        rows.append(
            {
                "path_id": path_id,
                "threshold_quantile": float(quantile),
                "event_count": int(len(events)),
                "rolling_60_median": median,
                "rolling_60_p10": p10,
                "rolling_60_min": minimum,
                "low_volatility_events": low_vol_events,
                "low_volatility_share": low_vol_events / len(events) if len(events) else 0.0,
                "density_eligible": density_eligible,
            }
        )

    density = pd.DataFrame(rows)
    eligible = density.loc[density["density_eligible"]].copy()
    selected_path: str | None = None
    if not eligible.empty:
        eligible["target_distance"] = (
            eligible["rolling_60_median"] - float(gate["target_median"])
        ).abs()
        selected_path = str(
            eligible.sort_values(["target_distance", "threshold_quantile"]).iloc[0]["path_id"]
        )
    events = pd.concat(event_frames, ignore_index=True)
    features_out = features.copy()
    features_out["dt"] = features_out["dt"].dt.strftime("%Y-%m-%d")
    for column in ("dt", "event_date"):
        events[column] = pd.to_datetime(events[column]).dt.strftime("%Y-%m-%d")
    features_out.to_csv(
        artifacts / "moneyflow_divergence_features.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    events.to_csv(
        artifacts / "mechanism_events.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    density.to_csv(artifacts / "density_metrics.csv", index=False, lineterminator="\n")
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "tested_density_paths": int(len(density)),
        "density_eligible_paths": int(density["density_eligible"].sum()),
        "selected_path": selected_path,
        "conditional_return_analysis": False,
        "cumulative_return_bearing_trials": int(protocol["prior_return_bearing_trials"]),
        "next_step": "PRE_REGISTER_RETURN_EVALUATION" if selected_path else "STOP_MECHANISM",
    }
    _write_json(artifacts / "census_summary.json", summary)

    status = "PASS" if selected_path else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S004 EX29 执行\n\n"
        f"无收益吸筹背离密度门：`{status}`。3条分位路径中"
        f"{summary['density_eligible_paths']}条通过，固定选择`{selected_path or '无'}`。\n\n"
        "本轮只按事件密度选择，没有读取次日收益；累计收益搜索仍为68次。\n",
        encoding="utf-8",
    )
    conclusion = (
        f"`{selected_path}`获得一次固定收益评价资格。"
        if selected_path
        else "吸筹背离事件密度不足，停止该固定机制定义。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX29 结论\n\n"
        f"结论：`{status}`。{conclusion}没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": "588080.SH",
            "development_cutoff": protocol["dataset"]["development_cutoff"],
            "status": status,
            "selected_path": selected_path,
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
