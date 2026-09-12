from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX34"


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


def _density_metrics(events: pd.Series, window: int) -> dict[str, float | int]:
    rolling = events.astype(int).rolling(window, min_periods=window).sum().dropna()
    return {
        "events": int(events.sum()),
        "rolling60_median": float(rolling.median()),
        "rolling60_p10": float(rolling.quantile(0.10)),
        "rolling60_minimum": int(rolling.min()),
        "rolling60_maximum": int(rolling.max()),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "conditional_return_analysis",
        "reads_588080_price_or_return",
        "parameter_selection_by_return",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX34 may only select event paths by density")

    upstream = protocol["upstream"]
    us_path = repo / str(upstream["us_daily"])
    mapping_path = repo / str(upstream["session_mapping"])
    if _sha256(us_path) != str(upstream["us_daily_sha256"]):
        raise ValueError("upstream US daily evidence hash differs")
    if _sha256(mapping_path) != str(upstream["session_mapping_sha256"]):
        raise ValueError("upstream session mapping evidence hash differs")

    us_daily = pd.read_csv(us_path, compression="gzip")
    us_daily["trade_date"] = pd.to_datetime(us_daily["trade_date"]).dt.normalize()
    returns = us_daily.pivot(index="trade_date", columns="ts_code", values="pct_change")
    required = ["QQQ", "SMH", "SOXX", "NVDA"]
    if returns[required].isna().any().any():
        raise ValueError("required US return panel contains null values")
    features = pd.DataFrame(index=returns.index)
    features["smh_excess"] = returns["SMH"] - returns["QQQ"]
    features["soxx_excess"] = returns["SOXX"] - returns["QQQ"]
    features["semi_excess"] = (returns["SMH"] + returns["SOXX"]) / 2.0 - returns["QQQ"]
    features["nvda_excess"] = returns["NVDA"] - returns["QQQ"]
    features["positive_consensus"] = (
        features["smh_excess"].gt(0) & features["soxx_excess"].gt(0)
    )
    features["negative_consensus"] = (
        features["smh_excess"].lt(0) & features["soxx_excess"].lt(0)
    )

    feature_cfg = protocol["feature"]
    lookback = int(feature_cfg["rolling_us_sessions"])
    mapping = pd.read_csv(mapping_path, compression="gzip")
    mapping["a_share_date"] = pd.to_datetime(mapping["a_share_date"]).dt.normalize()
    mapping["us_trade_date"] = pd.to_datetime(mapping["us_trade_date"]).dt.normalize()
    mapping = mapping.sort_values("a_share_date").reset_index(drop=True)
    mapping["fresh_us_session"] = ~mapping["us_trade_date"].duplicated(keep="first")

    signals = mapping.merge(
        features.reset_index().rename(columns={"trade_date": "us_trade_date"}),
        on="us_trade_date",
        how="left",
        validate="many_to_one",
    )
    path_metrics: list[dict[str, object]] = []
    path_columns: list[str] = []
    window = int(protocol["density_gate"]["rolling_a_share_sessions"])
    for quantile in [float(value) for value in feature_cfg["quantiles"]]:
        positive_threshold = (
            features["semi_excess"].rolling(lookback, min_periods=lookback).quantile(quantile).shift(1)
        )
        negative_threshold = (
            features["semi_excess"]
            .rolling(lookback, min_periods=lookback)
            .quantile(1.0 - quantile)
            .shift(1)
        )
        thresholds = pd.DataFrame(
            {
                "us_trade_date": features.index,
                "positive_threshold": positive_threshold,
                "negative_threshold": negative_threshold,
            }
        )
        threshold_map = thresholds.set_index("us_trade_date")
        pos_name = f"POS_Q{int(quantile * 100)}"
        neg_name = f"NEG_Q{int(quantile * 100)}"
        signals[f"positive_threshold_q{int(quantile * 100)}"] = signals["us_trade_date"].map(
            threshold_map["positive_threshold"]
        )
        signals[f"negative_threshold_q{int(quantile * 100)}"] = signals["us_trade_date"].map(
            threshold_map["negative_threshold"]
        )
        signals[pos_name] = (
            signals["fresh_us_session"]
            & signals["positive_consensus"].fillna(False)
            & signals["semi_excess"].ge(signals[f"positive_threshold_q{int(quantile * 100)}"])
        )
        signals[neg_name] = (
            signals["fresh_us_session"]
            & signals["negative_consensus"].fillna(False)
            & signals["semi_excess"].le(signals[f"negative_threshold_q{int(quantile * 100)}"])
        )
        for name, direction in ((pos_name, "POSITIVE_UNDERREACTION"), (neg_name, "NEGATIVE_OVERREACTION")):
            metrics = _density_metrics(signals[name], window)
            path_metrics.append(
                {
                    "path_id": name,
                    "direction": direction,
                    "quantile": quantile,
                    **metrics,
                }
            )
            path_columns.append(name)

    census = pd.DataFrame(path_metrics)
    gate = protocol["density_gate"]
    census["density_eligible"] = (
        census["rolling60_median"].between(
            float(gate["median_minimum"]), float(gate["median_maximum"]), inclusive="both"
        )
        & census["rolling60_p10"].ge(float(gate["p10_minimum"]))
    )
    selected: dict[str, str] = {}
    for direction, group in census.loc[census["density_eligible"]].groupby("direction"):
        ranked = group.assign(
            distance=(group["rolling60_median"] - float(gate["selection_target"])).abs()
        ).sort_values(["distance", "quantile", "path_id"])
        selected[str(direction)] = str(ranked.iloc[0]["path_id"])

    annual_rows: list[dict[str, object]] = []
    for path_id in path_columns:
        yearly = signals.groupby(signals["a_share_date"].dt.year)[path_id].sum()
        annual_rows.extend(
            {"path_id": path_id, "year": int(year), "events": int(count)}
            for year, count in yearly.items()
        )
    annual = pd.DataFrame(annual_rows)
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "paths_evaluated": int(len(census)),
        "eligible_paths": int(census["density_eligible"].sum()),
        "selected_paths": selected,
        "conditional_returns_read": False,
        "reads_588080_price_or_return": False,
        "route_decision": (
            "PROCEED_TO_FIXED_COMPETING_HYPOTHESIS_EVALUATION"
            if selected
            else "STOP_OVERSEAS_TRANSMISSION_DENSITY_ROUTE"
        ),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    signal_out = signals.copy()
    for column in ("a_share_date", "us_trade_date"):
        signal_out[column] = signal_out[column].dt.strftime("%Y-%m-%d")
    signal_out.to_csv(artifacts / "event_panel.csv.gz", index=False, compression=compression)
    census.to_csv(artifacts / "density_census.csv", index=False, lineterminator="\n")
    annual.to_csv(artifacts / "annual_event_counts.csv", index=False, lineterminator="\n")
    _write_json(artifacts / "selection.json", result)

    selected_text = "、".join(f"{key}={value}" for key, value in selected.items()) or "无"
    (experiment / "03_execution.md").write_text(
        "# S004 EX34 执行\n\n"
        f"状态：`COMPLETE`。共普查{len(census)}条预注册密度路径，"
        f"{int(census['density_eligible'].sum())}条满足密度门；冻结路径：{selected_text}。\n\n"
        "本轮没有读取588080价格或后续收益，NVDA没有参与路径选择。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX34 结论\n\n"
        f"结论：`{result['route_decision']}`。事件阈值仅由密度决定；"
        f"固定路径为{selected_text}。下一轮才允许按T+1开盘至11:30的可执行口径"
        "同时检验延续、反转和完全定价三种竞争解释。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": "588080.SH",
            "development_cutoff": "2026-09-02",
            "status": "COMPLETE",
            "route_decision": result["route_decision"],
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()

