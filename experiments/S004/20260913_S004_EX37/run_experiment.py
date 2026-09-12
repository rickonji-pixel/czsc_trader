from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX37"


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


def _density_metrics(events: pd.Series, ready: pd.Series, window: int) -> dict[str, float | int]:
    eligible = events.loc[ready.astype(bool)].astype(int)
    rolling = eligible.rolling(window, min_periods=window).sum().dropna()
    if rolling.empty:
        raise ValueError("no complete rolling density window after feature warmup")
    return {
        "eligible_sessions": int(len(eligible)),
        "events": int(eligible.sum()),
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
        "reads_post_event_return",
        "conditional_return_analysis",
        "parameter_selection_by_return",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX37 may only select event paths by density")

    dataset = protocol["dataset"]
    upstream = protocol["upstream"]
    expected_hashes = {
        repo / str(dataset["market_manifest"]): str(dataset["market_manifest_sha256"]),
        repo / str(upstream["manifest"]): str(upstream["manifest_sha256"]),
        repo / str(upstream["margin_detail"]): str(upstream["margin_detail_sha256"]),
        repo / str(upstream["data_quality"]): str(upstream["data_quality_sha256"]),
    }
    for path, digest in expected_hashes.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")
    validate_experiment_archive((repo / str(upstream["manifest"])).parent)
    quality = _read_json(repo / str(upstream["data_quality"]))
    if quality.get("status") != "PASS":
        raise ValueError("upstream margin data gate did not pass")

    symbol = str(protocol["research_target"]["trade_symbol"])
    cutoff = pd.Timestamp(str(dataset["development_cutoff"]))
    market = load_market_data(repo / str(dataset["market_directory"]), symbol).daily.copy()
    market["source_date"] = pd.to_datetime(market["dt"]).dt.normalize()
    market = market.loc[market["source_date"].le(cutoff)].sort_values("source_date")
    market["same_day_return"] = market["close"].astype(float).pct_change(fill_method=None)
    calendar = pd.DatetimeIndex(market["source_date"].drop_duplicates())
    next_session = pd.Series(calendar[1:], index=calendar[:-1])

    margin = pd.read_csv(repo / str(upstream["margin_detail"]), compression="gzip")
    margin["source_date"] = pd.to_datetime(margin["trade_date"]).dt.normalize()
    margin = margin.sort_values("source_date").reset_index(drop=True)
    margin["net_financing_flow"] = (
        margin["rzmre"].astype(float) - margin["rzche"].astype(float)
    )
    margin["previous_rzye"] = margin["rzye"].astype(float).shift(1)
    margin["net_flow_ratio"] = margin["net_financing_flow"] / margin["previous_rzye"]
    margin["same_day_return"] = margin["source_date"].map(
        market.set_index("source_date")["same_day_return"]
    )
    margin["event_date"] = margin["source_date"].map(next_session)
    margin = margin.loc[
        margin["event_date"].notna()
        & margin["event_date"].le(cutoff)
        & margin["previous_rzye"].gt(0)
        & margin["same_day_return"].notna()
    ].copy()
    if margin[["net_flow_ratio", "same_day_return", "event_date"]].isna().any().any():
        raise ValueError("causal margin feature panel contains null values")

    cfg = protocol["feature"]
    lookback = int(cfg["rolling_sessions"])
    path_columns: list[str] = []
    metric_rows: list[dict[str, object]] = []
    ready_columns: dict[str, str] = {}
    window = int(protocol["density_gate"]["rolling_a_share_sessions"])
    for quantile in [float(value) for value in cfg["quantiles"]]:
        suffix = int(quantile * 100)
        upper_name = f"upper_q{suffix}"
        lower_name = f"lower_q{100 - suffix}"
        margin[upper_name] = (
            margin["net_flow_ratio"].rolling(lookback, min_periods=lookback).quantile(quantile).shift(1)
        )
        margin[lower_name] = (
            margin["net_flow_ratio"].rolling(lookback, min_periods=lookback).quantile(1.0 - quantile).shift(1)
        )
        upper_ready = margin[upper_name].notna()
        lower_ready = margin[lower_name].notna()
        definitions = {
            f"ACCUM_Q{suffix}": (
                upper_ready
                & margin["net_financing_flow"].gt(0)
                & margin["net_flow_ratio"].ge(margin[upper_name])
                & margin["same_day_return"].le(0)
            ),
            f"CHASE_Q{suffix}": (
                upper_ready
                & margin["net_financing_flow"].gt(0)
                & margin["net_flow_ratio"].ge(margin[upper_name])
                & margin["same_day_return"].gt(0)
            ),
            f"DELEV_Q{suffix}": (
                lower_ready
                & margin["net_financing_flow"].lt(0)
                & margin["net_flow_ratio"].le(margin[lower_name])
                & margin["same_day_return"].lt(0)
            ),
        }
        for path_id, events in definitions.items():
            family = path_id.split("_", 1)[0]
            ready = upper_ready if family in {"ACCUM", "CHASE"} else lower_ready
            margin[path_id] = events
            ready_name = f"{path_id}_ready"
            margin[ready_name] = ready
            ready_columns[path_id] = ready_name
            metrics = _density_metrics(events, ready, window)
            metric_rows.append(
                {
                    "path_id": path_id,
                    "family": family,
                    "quantile": quantile,
                    **metrics,
                }
            )
            path_columns.append(path_id)

    census = pd.DataFrame(metric_rows)
    gate = protocol["density_gate"]
    census["density_eligible"] = (
        census["rolling60_median"].between(
            float(gate["median_minimum"]), float(gate["median_maximum"]), inclusive="both"
        )
        & census["rolling60_p10"].ge(float(gate["p10_minimum"]))
    )
    selected: dict[str, str] = {}
    for family, group in census.loc[census["density_eligible"]].groupby("family"):
        ranked = group.assign(
            distance=(group["rolling60_median"] - float(gate["selection_target"])).abs()
        ).sort_values(["distance", "quantile", "path_id"])
        selected[str(family)] = str(ranked.iloc[0]["path_id"])

    annual_rows: list[dict[str, object]] = []
    for path_id in path_columns:
        ready = margin[ready_columns[path_id]].astype(bool)
        yearly = margin.loc[ready].groupby(margin.loc[ready, "event_date"].dt.year)[path_id].sum()
        annual_rows.extend(
            {"path_id": path_id, "year": int(year), "events": int(count)}
            for year, count in yearly.items()
        )
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "paths_evaluated": int(len(census)),
        "eligible_paths": int(census["density_eligible"].sum()),
        "selected_paths": selected,
        "post_event_returns_read": False,
        "route_decision": (
            "PROCEED_TO_FIXED_MARGIN_BEHAVIOR_EVALUATION"
            if selected
            else "STOP_MARGIN_BEHAVIOR_DENSITY_ROUTE"
        ),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    output = margin.copy()
    for column in ("trade_date", "source_date", "event_date"):
        output[column] = pd.to_datetime(output[column]).dt.strftime("%Y-%m-%d")
    output.to_csv(artifacts / "event_panel.csv.gz", index=False, compression=compression)
    census.to_csv(artifacts / "density_census.csv", index=False, lineterminator="\n")
    pd.DataFrame(annual_rows).to_csv(
        artifacts / "annual_event_counts.csv", index=False, lineterminator="\n"
    )
    _write_json(artifacts / "selection.json", result)

    selected_text = "、".join(f"{key}={value}" for key, value in selected.items()) or "无"
    (experiment / "03_execution.md").write_text(
        "# S004 EX37 执行\n\n"
        f"状态：`COMPLETE`。共普查{len(census)}条预注册密度路径，"
        f"{int(census['density_eligible'].sum())}条满足密度门；冻结路径：{selected_text}。\n\n"
        "本轮只使用融资交易日及其当日价格定义T日行为，未读取T+1价格或任何事件后收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX37 结论\n\n"
        f"结论：`{result['route_decision']}`。"
        f"固定路径为{selected_text}。下一轮才允许按统一执行口径检验竞争解释。\n",
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
            "route_decision": result["route_decision"],
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
