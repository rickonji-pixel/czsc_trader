from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX20"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _cooldown(events: pd.Series, sessions: int) -> pd.Series:
    accepted = pd.Series(False, index=events.index)
    last = -sessions - 1
    for position, active in enumerate(events.fillna(False).to_numpy(dtype=bool)):
        if active and position - last > sessions:
            accepted.iloc[position] = True
            last = position
    return accepted


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    source_spec = protocol["sources"]
    panel_source = repo / "experiments/S005" / str(source_spec["panel_experiment"])
    review_source = repo / "experiments/S005" / str(source_spec["review_experiment"])
    validate_experiment_archive(panel_source)
    validate_experiment_archive(review_source)
    expected = {
        panel_source / "experiment_manifest.json": source_spec["panel_manifest_sha256"],
        panel_source / "artifacts/constituent_panel.csv.gz": source_spec["panel_sha256"],
        review_source / "experiment_manifest.json": source_spec["review_manifest_sha256"],
        review_source / "artifacts/reviewed_data_quality.json": source_spec[
            "review_quality_sha256"
        ],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read(review_source / "artifacts/reviewed_data_quality.json")["passed"]:
        raise ValueError("reviewed constituent data gate did not pass")

    panel = pd.read_csv(panel_source / "artifacts/constituent_panel.csv.gz")
    panel["dt"] = pd.to_datetime(panel["dt"])
    panel["weight"] = pd.to_numeric(panel["weight"], errors="raise")
    panel["positive_price_weight"] = panel["weight"].where(panel["pct_chg"].gt(0), 0.0)
    panel["positive_flow_weight"] = panel["weight"].where(panel["net_mf_amount"].gt(0), 0.0)
    panel["observed_price_weight"] = panel["weight"].where(panel["observed_daily"], 0.0)
    panel["observed_flow_weight"] = panel["weight"].where(panel["observed_moneyflow"], 0.0)
    daily = panel.groupby("dt", sort=True, observed=True).agg(
        positive_price_weight=("positive_price_weight", "sum"),
        positive_flow_weight=("positive_flow_weight", "sum"),
        observed_price_weight=("observed_price_weight", "sum"),
        observed_flow_weight=("observed_flow_weight", "sum"),
        positive_price_members=("pct_chg", lambda values: int(values.gt(0).sum())),
        observed_price_members=("observed_daily", "sum"),
    )
    daily["price_breadth"] = daily["positive_price_weight"] / daily["observed_price_weight"]
    daily["flow_breadth"] = daily["positive_flow_weight"] / daily["observed_flow_weight"]
    daily["equal_price_breadth"] = (
        daily["positive_price_members"] / daily["observed_price_members"]
    )
    daily["joint_breadth"] = daily[["price_breadth", "flow_breadth"]].min(axis=1)
    daily["weight_drag_gap"] = daily["equal_price_breadth"] - daily["price_breadth"]

    market = load_market_data(repo / "data/raw", str(protocol["symbol"])).daily.copy()
    market["dt"] = pd.to_datetime(market["dt"]).dt.normalize()
    market = market.set_index("dt").sort_index()
    market_close = pd.to_numeric(market["close"], errors="coerce")
    daily["etf_return_pct"] = market_close.pct_change(fill_method=None).mul(100).reindex(
        daily.index
    )

    feature = protocol["feature"]
    lookback = int(feature["rolling_reference_sessions"])
    minimum = int(feature["minimum_reference_observations"])
    density = protocol["density"]
    summaries: list[dict[str, object]] = []
    event_columns: list[str] = []
    for quantile in [float(value) for value in feature["strength_quantiles"]]:
        suffix = f"Q{int(quantile * 100)}"
        upper = {
            name: daily[name].shift(1).rolling(lookback, min_periods=minimum).quantile(quantile)
            for name in ("flow_breadth", "joint_breadth", "weight_drag_gap")
        }
        lower_joint = (
            daily["joint_breadth"]
            .shift(1)
            .rolling(lookback, min_periods=minimum)
            .quantile(1.0 - quantile)
        )
        for name, threshold in upper.items():
            daily[f"{name.lower()}_{suffix.lower()}"] = threshold
        daily[f"joint_breadth_lower_{suffix.lower()}"] = lower_joint
        paths = {
            f"INTERNAL_ACCUMULATION_{suffix}": (
                daily["flow_breadth"].ge(upper["flow_breadth"])
                & daily["price_breadth"].ge(0.5)
                & daily["etf_return_pct"].le(0)
            ),
            f"BROAD_PARTICIPATION_THRUST_{suffix}": (
                daily["joint_breadth"].ge(upper["joint_breadth"])
                & daily["etf_return_pct"].gt(0)
                & daily["etf_return_pct"].le(float(feature["maximum_thrust_etf_return_pct"]))
            ),
            f"HEALTHY_MAJORITY_HEAVYWEIGHT_DRAG_{suffix}": (
                daily["weight_drag_gap"].gt(0)
                & daily["weight_drag_gap"].ge(upper["weight_drag_gap"])
                & daily["equal_price_breadth"].ge(0.5)
                & daily["etf_return_pct"].le(0)
            ),
            f"BROAD_WASHOUT_{suffix}": (
                daily["joint_breadth"].le(lower_joint) & daily["etf_return_pct"].lt(0)
            ),
        }
        first_ready = upper["joint_breadth"].dropna().index.min()
        for name, raw in paths.items():
            accepted = _cooldown(raw, int(density["cooldown_sessions"]))
            daily[name] = accepted
            event_columns.append(name)
            rolling = (
                accepted.loc[accepted.index >= first_ready]
                .astype(int)
                .rolling(
                    int(density["rolling_window_sessions"]),
                    min_periods=int(density["rolling_window_sessions"]),
                )
                .sum()
                .dropna()
            )
            median = float(rolling.median())
            p10 = float(rolling.quantile(0.1))
            summaries.append(
                {
                    "mechanism": name,
                    "family": name.rsplit("_Q", maxsplit=1)[0],
                    "quantile": quantile,
                    "raw_events": int(raw.sum()),
                    "independent_events": int(accepted.sum()),
                    "rolling_60_median": median,
                    "rolling_60_p10": p10,
                    "rolling_60_minimum": float(rolling.min()),
                    "rolling_60_maximum": float(rolling.max()),
                    "density_pass": bool(
                        median >= float(density["minimum_median"])
                        and p10 >= float(density["minimum_p10"])
                    ),
                }
            )

    summary = pd.DataFrame(summaries).sort_values(
        ["density_pass", "rolling_60_p10", "rolling_60_median", "independent_events"],
        ascending=[False, False, False, False],
    )
    feature_out = daily.reset_index()
    feature_out["dt"] = feature_out["dt"].dt.strftime("%Y-%m-%d")
    feature_out.to_csv(
        artifacts / "constituent_feature_ledger.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    summary.to_csv(artifacts / "density_summary.csv", index=False, lineterminator="\n")
    passing = summary.loc[summary["density_pass"], "mechanism"].tolist()
    selection = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "feature_sessions": int(len(daily)),
        "first_feature_session": daily.index.min().date().isoformat(),
        "last_feature_session": daily.index.max().date().isoformat(),
        "paths_tested": int(len(summary)),
        "density_passing_paths": passing,
        "reads_post_event_prices": False,
        "decision": (
            "PROCEED_TO_FIXED_RETURN_TEST" if passing else "STOP_CONSTITUENT_MECHANISMS_ON_DENSITY"
        ),
    }
    (artifacts / "selection_evidence.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    rows = ["|路径|独立事件|60日中位数|P10|密度门|", "|---|---:|---:|---:|---|"]
    for row in summary.itertuples(index=False):
        rows.append(
            f"|{row.mechanism}|{row.independent_events}|{row.rolling_60_median:.1f}|"
            f"{row.rolling_60_p10:.1f}|{'PASS' if row.density_pass else 'FAIL'}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S005 EX20 执行\n\n状态：`COMPLETE`。已完成成分机制的无收益密度普查，共测试"
        f"{len(summary)}条预注册路径；{len(passing)}条通过。\n\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX20 结论\n\n"
        f"裁决：`{selection['decision']}`。通过路径："
        f"{', '.join(passing) if passing else 'NONE'}。本轮未读取事件后收益。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": protocol["development_cutoff"],
            "status": "COMPLETE",
            "reads_post_event_prices": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
