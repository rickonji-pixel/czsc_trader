from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX30"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
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
        raise ValueError("EX30 may only perform return-free density calibration")

    sources = protocol["sources"]
    moneyflow_source = repo / "experiments" / "S004" / sources["moneyflow_experiment"]
    price_source = repo / "experiments" / "S004" / sources["price_experiment"]
    validate_experiment_archive(moneyflow_source)
    validate_experiment_archive(price_source)
    expected = {
        moneyflow_source / "experiment_manifest.json": sources["moneyflow_manifest_sha256"],
        moneyflow_source / "artifacts" / "constituent_moneyflow_panel.csv.gz": sources[
            "moneyflow_panel_sha256"
        ],
        moneyflow_source / "artifacts" / "data_quality.json": sources[
            "moneyflow_quality_sha256"
        ],
        price_source / "experiment_manifest.json": sources["price_manifest_sha256"],
        price_source / "artifacts" / "constituent_daily_panel.csv.gz": sources[
            "price_panel_sha256"
        ],
        repo / sources["market_manifest"]: sources["market_manifest_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read_json(moneyflow_source / "artifacts" / "data_quality.json")["passed"]:
        raise ValueError("EX28 moneyflow data gate did not pass")

    moneyflow = pd.read_csv(
        moneyflow_source / "artifacts" / "constituent_moneyflow_panel.csv.gz"
    )
    price = pd.read_csv(price_source / "artifacts" / "constituent_daily_panel.csv.gz")
    moneyflow["dt"] = pd.to_datetime(moneyflow["dt"]).dt.normalize()
    price["dt"] = pd.to_datetime(price["dt"]).dt.normalize()
    frame = moneyflow.merge(
        price[["dt", "con_code", "pct_chg"]],
        on=["dt", "con_code"],
        how="left",
        validate="one_to_one",
    )
    if frame["pct_chg"].isna().any():
        raise ValueError("observed moneyflow member lacks constituent price return")
    observed = frame["observed_moneyflow"].astype(bool)
    frame["observed_weight"] = frame["weight"].where(observed, 0.0)
    frame["absorption_member"] = observed & frame["net_mf_amount"].gt(0) & frame[
        "pct_chg"
    ].le(0)
    frame["absorption_weight"] = frame["weight"].where(frame["absorption_member"], 0.0)
    grouped = frame.groupby("dt", sort=True)
    features = grouped[["weight", "observed_weight", "absorption_weight"]].sum()
    features["observed_weight_ratio"] = features["observed_weight"] / features["weight"]
    features["absorption_breadth"] = features["absorption_weight"] / features[
        "observed_weight"
    ]
    market = load_market_data(repo / "data" / "raw", "588080.SH").daily
    etf = market[["dt", "close"]].copy()
    etf["dt"] = pd.to_datetime(etf["dt"]).dt.normalize()
    etf = etf.sort_values("dt")
    etf["etf_return_pct"] = etf["close"].pct_change() * 100.0
    etf["etf_volatility_20"] = etf["etf_return_pct"].rolling(20, min_periods=20).std(ddof=1)
    etf["prior_volatility_median_120"] = etf["etf_volatility_20"].shift(1).rolling(
        120, min_periods=120
    ).median()
    etf["low_volatility"] = (
        etf["etf_volatility_20"] <= etf["prior_volatility_median_120"]
    )
    features = features.reset_index().merge(
        etf[
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
        threshold = features["absorption_breadth"].shift(1).rolling(
            int(spec["threshold_lookback_valid_sessions"]),
            min_periods=int(spec["threshold_lookback_valid_sessions"]),
        ).quantile(float(quantile))
        active = (
            features["absorption_breadth"].ge(threshold)
            & features["observed_weight_ratio"].ge(
                float(spec["minimum_observed_weight_ratio"])
            )
        ).fillna(False)
        signal_dates = calendar[active]
        selected_signals = signal_dates[next_session.reindex(signal_dates).notna()]
        events = features.set_index("dt").loc[selected_signals].reset_index()
        events["event_date"] = events["dt"].map(next_session)
        events = events.loc[events["event_date"].ge(evaluation_start)].copy()
        path_id = f"AB-Q{int(round(float(quantile) * 100)):02d}"
        events["path_id"] = path_id
        events["threshold_quantile"] = float(quantile)
        event_frames.append(events)
        median, p10, minimum = _density(
            calendar, pd.DatetimeIndex(events["event_date"]), evaluation_start
        )
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
        artifacts / "absorption_features.csv.gz",
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
        "# S004 EX30 执行\n\n"
        f"无收益成分吸筹密度门：`{status}`。3条分位路径中"
        f"{summary['density_eligible_paths']}条通过，固定选择`{selected_path or '无'}`。\n\n"
        "本轮只按事件密度选择，没有读取次日收益；累计收益搜索仍为68次。\n",
        encoding="utf-8",
    )
    conclusion = (
        f"`{selected_path}`获得一次固定收益评价资格。"
        if selected_path
        else "成分级吸筹事件密度不足，停止机制。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX30 结论\n\n"
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
