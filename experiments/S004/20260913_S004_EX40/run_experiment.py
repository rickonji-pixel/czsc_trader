from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX40"


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    return gains / losses if losses > 0 else float("inf")


def _metrics(frame: pd.DataFrame, recent_start: pd.Timestamp) -> dict[str, object]:
    annual = frame.groupby(frame["entry_date"].dt.year)["stress_return"].mean()
    low_vol = frame.loc[frame["low_volatility"].astype(bool), "stress_return"]
    recent = frame.loc[frame["event_date"].ge(recent_start), "stress_return"]
    return {
        "episodes": int(len(frame)),
        "stress_mean": float(frame["stress_return"].mean()),
        "profit_factor": _profit_factor(frame["stress_return"]),
        "positive_years": int(annual.gt(0).sum()),
        "evaluated_years": int(len(annual)),
        "low_vol_episodes": int(len(low_vol)),
        "low_vol_stress_mean": float(low_vol.mean()),
        "low_vol_profit_factor": _profit_factor(low_vol),
        "recent_252_episodes": int(len(recent)),
        "recent_252_stress_mean": float(recent.mean()),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX40 may only evaluate one fixed overlay")

    dataset = protocol["dataset"]
    upstream = protocol["upstream"]
    expected_hashes = {
        repo / str(dataset["market_manifest"]): str(dataset["market_manifest_sha256"]),
        repo / str(upstream["base_episodes"]): str(upstream["base_episodes_sha256"]),
        repo / str(upstream["base_metrics"]): str(upstream["base_metrics_sha256"]),
        repo / str(upstream["risk_event_panel"]): str(upstream["risk_event_panel_sha256"]),
        repo / str(upstream["replication_manifest"]): str(upstream["replication_manifest_sha256"]),
        repo / str(upstream["replication_evaluation"]): str(upstream["replication_evaluation_sha256"]),
    }
    for path, digest in expected_hashes.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")
    validate_experiment_archive((repo / str(upstream["replication_manifest"])).parent)
    replication = _read_json(repo / str(upstream["replication_evaluation"]))
    if replication.get("route_decision") != "MARGIN_ABSORBED_BUYING_RISK_FILTER_REPLICATED":
        raise ValueError("risk mechanism did not pass external replication")

    base = pd.read_csv(repo / str(upstream["base_episodes"]), compression="gzip")
    for column in ("event_date", "entry_date", "exit_date"):
        base[column] = pd.to_datetime(base[column]).dt.normalize()
    risk_panel = pd.read_csv(repo / str(upstream["risk_event_panel"]), compression="gzip")
    risk_panel["source_date"] = pd.to_datetime(risk_panel["source_date"]).dt.normalize()
    path_id = str(protocol["research_target"]["risk_path"])
    risk_dates = set(risk_panel.loc[risk_panel[path_id].astype(bool), "source_date"])
    base["risk_denied"] = base["event_date"].isin(risk_dates)

    symbol = str(protocol["research_target"]["symbol"])
    cutoff = pd.Timestamp(str(dataset["development_cutoff"]))
    market = load_market_data(repo / str(dataset["market_directory"]), symbol).daily.copy()
    market["event_date"] = pd.to_datetime(market["dt"]).dt.normalize()
    market = market.loc[market["event_date"].le(cutoff)].sort_values("event_date")
    daily_return = market["close"].astype(float).pct_change(fill_method=None)
    market["known_volatility"] = daily_return.rolling(20, min_periods=20).std(ddof=1)
    market["known_volatility_median"] = market["known_volatility"].rolling(
        120, min_periods=120
    ).median().shift(1)
    market["low_volatility"] = market["known_volatility"].le(
        market["known_volatility_median"]
    )
    regime = market.set_index("event_date")["low_volatility"]
    base["low_volatility"] = base["event_date"].map(regime)

    filtered = base.loc[~base["risk_denied"]].copy()
    removed = base.loc[base["risk_denied"]].copy()
    calendar = pd.DatetimeIndex(market["event_date"].drop_duplicates())
    density_cfg = protocol["density_gate"]
    density_calendar = calendar[calendar >= pd.Timestamp(str(density_cfg["evaluation_start"]))]
    flags = pd.Series(0, index=density_calendar, dtype="int64")
    flags.loc[flags.index.intersection(pd.DatetimeIndex(filtered["event_date"]))] = 1
    rolling = flags.rolling(
        int(density_cfg["rolling_sessions"]),
        min_periods=int(density_cfg["rolling_sessions"]),
    ).sum().dropna()
    density_median = float(rolling.median())
    density_p10 = float(rolling.quantile(0.10, interpolation="lower"))
    recent_start = pd.Timestamp(calendar[-252])
    base_metrics = _metrics(base, recent_start)
    filtered_metrics = _metrics(filtered, recent_start)
    removed_stress_mean = float(removed["stress_return"].mean()) if not removed.empty else None

    gate = protocol["acceptance"]
    checks = {
        "density_median": float(density_cfg["median_minimum"]) <= density_median <= float(density_cfg["median_maximum"]),
        "density_p10": density_p10 >= float(density_cfg["p10_minimum"]),
        "removed_stress_mean": (
            removed_stress_mean is not None
            and removed_stress_mean < float(gate["removed_stress_mean_max_exclusive"])
        ),
        "filtered_stress_mean_exceeds_base": filtered_metrics["stress_mean"] > base_metrics["stress_mean"],
        "filtered_profit_factor_exceeds_base": filtered_metrics["profit_factor"] > base_metrics["profit_factor"],
        "low_vol_stress_mean": filtered_metrics["low_vol_stress_mean"] > float(gate["low_vol_stress_mean_min_exclusive"]),
        "low_vol_profit_factor": filtered_metrics["low_vol_profit_factor"] > float(gate["low_vol_profit_factor_min_exclusive"]),
        "positive_years": filtered_metrics["positive_years"] >= int(gate["positive_years_minimum"]),
        "recent_252_stress_mean": filtered_metrics["recent_252_stress_mean"] > float(gate["recent_252_stress_mean_min_exclusive"]),
    }
    passed = all(checks.values())
    route = "PROCEED_TO_S004_C002_CONSTRUCTION" if passed else "STOP_MARGIN_RISK_OVERLAY"
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS" if passed else "FAIL",
        "route_decision": route,
        "risk_overlap_episodes": int(len(removed)),
        "removed_stress_mean": removed_stress_mean,
        "rolling_60_median": density_median,
        "rolling_60_p10": density_p10,
        "base_metrics": base_metrics,
        "filtered_metrics": filtered_metrics,
        "checks": checks,
        "candidate_created": False,
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    output = base.copy()
    for column in ("event_date", "entry_date", "exit_date"):
        output[column] = output[column].dt.strftime("%Y-%m-%d")
    output.to_csv(artifacts / "overlay_episodes.csv.gz", index=False, compression=compression)
    _write_json(artifacts / "overlay_summary.json", summary)

    removed_text = "不可用" if removed_stress_mean is None else f"{removed_stress_mean:.3%}"
    (experiment / "03_execution.md").write_text(
        "# S004 EX40 执行\n\n"
        f"固定风险事件与S004-C001重叠{len(removed)}笔，被过滤交易压力均值"
        f"{removed_text}。过滤后保留{len(filtered)}笔，滚动60日中位数/P10为"
        f"{density_median:.0f}/{density_p10:.0f}；压力均值由{base_metrics['stress_mean']:.3%}"
        f"变为{filtered_metrics['stress_mean']:.3%}，盈亏比由{base_metrics['profit_factor']:.2f}"
        f"变为{filtered_metrics['profit_factor']:.2f}；低波动均值"
        f"{filtered_metrics['low_vol_stress_mean']:.3%}。\n",
        encoding="utf-8",
    )
    conclusion = (
        "固定风险过滤通过全部门槛，只获得进入S004-C002候选构造的资格。"
        if passed
        else "风险过滤准确剔除了亏损交易，但交易密度和低波动质量门失败，停止构造S004-C002。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX40 结论\n\n"
        f"结论：`{route}`。{conclusion}本轮未创建或修改任何候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": symbol,
            "development_cutoff": str(dataset["development_cutoff"]),
            "status": "COMPLETE",
            "route_decision": route,
            "conditional_return_analysis": True,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
