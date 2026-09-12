from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX26"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _build_features(panel: pd.DataFrame, etf_daily: pd.DataFrame) -> pd.DataFrame:
    panel = panel.copy()
    panel["dt"] = pd.to_datetime(panel["dt"]).dt.normalize()
    observed = panel["record_status"].eq("OBSERVED")
    panel["observed"] = observed
    panel["advance"] = observed & panel["pct_chg"].gt(0)
    panel["observed_weight"] = panel["weight"].where(observed, 0.0)
    panel["weighted_return_numerator"] = (
        panel["weight"] * panel["pct_chg"]
    ).where(observed, 0.0)
    grouped = panel.groupby("dt", sort=True)
    features = grouped[
        ["observed", "advance", "observed_weight", "weighted_return_numerator"]
    ].sum()
    features["membership_rows"] = grouped.size()
    features["observed_ratio"] = features["observed"] / features["membership_rows"]
    features["advance_ratio"] = features["advance"] / features["observed"]
    features["weighted_constituent_return_pct"] = (
        features["weighted_return_numerator"] / features["observed_weight"]
    )

    etf = etf_daily[["dt", "close"]].copy()
    etf["dt"] = pd.to_datetime(etf["dt"]).dt.normalize()
    etf = etf.sort_values("dt")
    etf["etf_return_pct"] = etf["close"].pct_change() * 100.0
    features = features.reset_index().merge(
        etf[["dt", "etf_return_pct"]], on="dt", how="left", validate="one_to_one"
    )
    features["lead_score_pct"] = (
        features["weighted_constituent_return_pct"] - features["etf_return_pct"]
    )
    features["etf_volatility_20"] = (
        features["etf_return_pct"].rolling(20, min_periods=20).std(ddof=1)
    )
    features["prior_volatility_median_120"] = (
        features["etf_volatility_20"].shift(1).rolling(120, min_periods=120).median()
    )
    features["low_volatility"] = (
        features["etf_volatility_20"] <= features["prior_volatility_median_120"]
    )
    return features


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
        raise ValueError("EX26 may only perform return-free event-density calibration")

    source_spec = protocol["source"]
    source = repo / "experiments" / "S004" / source_spec["experiment_id"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["experiment_manifest_sha256"],
        source / "artifacts" / "constituent_daily_panel.csv.gz": source_spec["panel_sha256"],
        source / "artifacts" / "data_quality.json": source_spec["data_quality_sha256"],
        repo / source_spec["market_manifest"]: source_spec["market_manifest_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read_json(source / "artifacts" / "data_quality.json")["passed"]:
        raise ValueError("EX25 data gate did not pass")

    panel = pd.read_csv(source / "artifacts" / "constituent_daily_panel.csv.gz")
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
        threshold = features["lead_score_pct"].shift(1).rolling(
            int(spec["threshold_lookback"]), min_periods=int(spec["threshold_lookback"])
        ).quantile(float(quantile))
        active = (
            features["lead_score_pct"].gt(threshold)
            & features["weighted_constituent_return_pct"].gt(0)
            & features["advance_ratio"].gt(float(spec["require_advance_ratio_above"]))
            & features["observed_ratio"].ge(float(spec["minimum_observed_ratio"]))
        ).fillna(False)
        signal_dates = calendar[active]
        event_dates = pd.DatetimeIndex(next_session.reindex(signal_dates).dropna())
        event_dates = event_dates[event_dates >= evaluation_start]
        median, p10, minimum = _density(calendar, event_dates, evaluation_start)
        density_eligible = bool(
            float(gate["median_min"]) <= median <= float(gate["median_max"])
            and p10 >= float(gate["p10_min"])
        )
        path_id = f"BL-Q{int(round(float(quantile) * 100)):02d}"
        selected_signals = signal_dates[next_session.reindex(signal_dates).notna()]
        event_frame = features.set_index("dt").loc[selected_signals].reset_index()
        event_frame["event_date"] = event_frame["dt"].map(next_session)
        event_frame = event_frame.loc[event_frame["event_date"].ge(evaluation_start)].copy()
        event_frame["path_id"] = path_id
        event_frame["threshold_quantile"] = float(quantile)
        event_frames.append(event_frame)
        low_vol_events = int(event_frame["low_volatility"].sum())
        rows.append(
            {
                "path_id": path_id,
                "threshold_quantile": float(quantile),
                "event_count": int(len(event_frame)),
                "rolling_60_median": median,
                "rolling_60_p10": p10,
                "rolling_60_min": minimum,
                "low_volatility_events": low_vol_events,
                "low_volatility_share": low_vol_events / len(event_frame) if len(event_frame) else 0.0,
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
    events = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame()
    features_out = features.copy()
    features_out["dt"] = features_out["dt"].dt.strftime("%Y-%m-%d")
    for column in ("dt", "event_date"):
        if column in events:
            events[column] = pd.to_datetime(events[column]).dt.strftime("%Y-%m-%d")
    features_out.to_csv(
        artifacts / "breadth_lead_features.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    density.to_csv(artifacts / "density_metrics.csv", index=False, lineterminator="\n")
    events.to_csv(
        artifacts / "mechanism_events.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    feature_stats = {
        "observed_ratio_min": float(features["observed_ratio"].min()),
        "advance_ratio_lead_score_correlation": float(
            features[["advance_ratio", "lead_score_pct"]].corr().iloc[0, 1]
        ),
        "weighted_return_lead_score_correlation": float(
            features[["weighted_constituent_return_pct", "lead_score_pct"]].corr().iloc[0, 1]
        ),
    }
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "tested_density_paths": int(len(density)),
        "density_eligible_paths": int(density["density_eligible"].sum()),
        "selected_path": selected_path,
        "feature_stats": feature_stats,
        "conditional_return_analysis": False,
        "cumulative_return_bearing_trials": int(protocol["prior_return_bearing_trials"]),
        "next_step": "PRE_REGISTER_RETURN_EVALUATION" if selected_path else "STOP_MECHANISM",
    }
    _write_json(artifacts / "census_summary.json", summary)

    status = "PASS" if selected_path else "FAIL"
    selected_text = selected_path or "无"
    (experiment / "03_execution.md").write_text(
        "# S004 EX26 执行\n\n"
        f"无收益事件密度门：`{status}`。3条分位路径中"
        f"{summary['density_eligible_paths']}条符合滚动60日目标，固定选择`{selected_text}`。"
        f"最低成分观测率{feature_stats['observed_ratio_min']:.2%}。\n\n"
        "本轮只使用事件日前已知信息并仅按密度选择，没有读取事件后收益；累计收益搜索仍为67次。\n",
        encoding="utf-8",
    )
    conclusion = (
        f"`{selected_path}`获得一次固定收益评价资格，执行口径预注册为次日开盘至11:30。"
        if selected_path
        else "该成分领先定义无法满足三个月观察所需密度，停止机制。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX26 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮没有创建候选或修改SM/PTE。\n",
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
