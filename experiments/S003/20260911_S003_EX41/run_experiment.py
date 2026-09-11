from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260911_S003_EX41"


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
    dates: pd.DatetimeIndex,
    event: pd.Series,
    *,
    evaluation_start: str,
    rolling_sessions: int,
) -> dict[str, float | int]:
    indicator = pd.Series(event.astype(int).to_numpy(), index=dates)
    rolling = indicator.rolling(rolling_sessions, min_periods=rolling_sessions).sum()
    rolling = rolling.loc[rolling.index >= pd.Timestamp(evaluation_start)]
    if rolling.empty:
        raise ValueError("no full density evaluation windows")
    return {
        "event_count": int(event.sum()),
        "rolling_60_median": float(rolling.median()),
        "rolling_60_p10": float(rolling.quantile(0.10)),
        "rolling_60_min": float(rolling.min()),
        "rolling_60_max": float(rolling.max()),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "conditional_return_analysis",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX41 may only inspect return-free ETF flow topology")

    dataset = protocol["dataset"]
    source = repo_root / "experiments" / dataset["source_experiment"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": dataset["source_manifest_sha256"],
        source / "artifacts" / "governed_etf_share_nav_panel.csv.gz": dataset[
            "source_panel_sha256"
        ],
        source / "artifacts" / "data_quality.json": dataset["source_quality_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read_json(source / "artifacts" / "data_quality.json")["passed"]:
        raise ValueError("EX40 corporate-action-aware data gate did not pass")

    panel = pd.read_csv(source / "artifacts" / "governed_etf_share_nav_panel.csv.gz")
    panel["dt"] = pd.to_datetime(panel["dt"]).dt.normalize()
    panel = panel.sort_values("dt").reset_index(drop=True)
    panel["share_flow_rate"] = panel["fd_share"] / panel["fd_share"].shift(1) - 1.0
    panel.loc[~panel["flow_feature_eligible"].astype(bool), "share_flow_rate"] = pd.NA
    panel["premium_rate"] = panel["close_nav_deviation"]
    panel.loc[~panel["premium_feature_eligible"].astype(bool), "premium_rate"] = pd.NA

    routes = protocol["route_order"]
    route_events: dict[str, pd.Series] = {}
    route_events[str(routes[0]["route_id"])] = panel["share_flow_rate"].lt(0).fillna(False)
    lookback = int(routes[1]["lookback"])
    quantile = float(routes[1]["quantile"])
    panel["prior_60_share_flow_q20"] = (
        panel["share_flow_rate"].shift(1).rolling(lookback, min_periods=lookback).quantile(quantile)
    )
    route_events[str(routes[1]["route_id"])] = panel["share_flow_rate"].le(
        panel["prior_60_share_flow_q20"]
    ).fillna(False)

    gate = protocol["density_gate"]
    route_rows: list[dict[str, object]] = []
    selected_route: str | None = None
    for route in routes:
        route_id = str(route["route_id"])
        metrics = _density(
            pd.DatetimeIndex(panel["dt"]),
            route_events[route_id],
            evaluation_start=dataset["density_evaluation_start"],
            rolling_sessions=int(gate["rolling_sessions"]),
        )
        eligible = bool(
            float(gate["median_min"])
            <= float(metrics["rolling_60_median"])
            <= float(gate["median_max"])
            and float(metrics["rolling_60_p10"]) >= float(gate["p10_min"])
        )
        route_rows.append({"route_id": route_id, **metrics, "density_eligible": eligible})
        if selected_route is None and eligible:
            selected_route = route_id

    valid_flow = panel["share_flow_rate"].dropna()
    valid_premium = panel["premium_rate"].dropna()
    profile = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "sessions": int(len(panel)),
        "valid_flow_sessions": int(len(valid_flow)),
        "negative_flow_sessions": int(valid_flow.lt(0).sum()),
        "zero_flow_sessions": int(valid_flow.eq(0).sum()),
        "positive_flow_sessions": int(valid_flow.gt(0).sum()),
        "share_flow_quantiles": {
            str(value): float(valid_flow.quantile(value))
            for value in (0.01, 0.10, 0.20, 0.50, 0.80, 0.90, 0.99)
        },
        "premium_quantiles": {
            str(value): float(valid_premium.quantile(value))
            for value in (0.01, 0.10, 0.20, 0.50, 0.80, 0.90, 0.99)
        },
        "contemporaneous_flow_premium_correlation": float(
            panel[["share_flow_rate", "premium_rate"]].corr().iloc[0, 1]
        ),
        "selected_route": selected_route,
        "conditional_return_analysis": False,
    }
    route_frame = pd.DataFrame(route_rows)
    feature_output = panel[
        [
            "dt",
            "fd_share",
            "share_flow_rate",
            "premium_rate",
            "prior_60_share_flow_q20",
            "corporate_action",
            "flow_feature_eligible",
            "premium_feature_eligible",
        ]
    ].copy()
    feature_output["dt"] = feature_output["dt"].dt.strftime("%Y-%m-%d")
    feature_output.to_csv(
        artifacts / "etf_flow_features.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    route_frame.to_csv(artifacts / "route_density.csv", index=False, lineterminator="\n")
    _write_json(artifacts / "feature_profile.json", profile)
    status = "PASS" if selected_route else "FAIL"
    selected = (
        route_frame.loc[route_frame["route_id"].eq(selected_route)].iloc[0]
        if selected_route
        else route_frame.iloc[0]
    )
    (experiment / "03_execution.md").write_text(
        "# S003 EX41 执行\n\n"
        f"纯数据形态与密度路由结果：`{status}`。有效份额变化{len(valid_flow)}日，其中净赎回"
        f"{profile['negative_flow_sessions']}日、零变化{profile['zero_flow_sessions']}日、净申购"
        f"{profile['positive_flow_sessions']}日。"
        f"路由结果为`{selected_route or 'NONE'}`，滚动60日中位数"
        f"{selected['rolling_60_median']:.1f}、第10百分位{selected['rolling_60_p10']:.1f}。\n\n"
        "本轮没有读取510500事件后收益。\n",
        encoding="utf-8",
    )
    conclusion = (
        f"冻结`{selected_route}`作为下一轮事件定义。"
        if selected_route
        else "两个预注册路由均未达到观察速度要求，停止ETF份额流机制。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX41 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "selected_route": selected_route,
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
