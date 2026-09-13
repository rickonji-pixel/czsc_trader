from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX24"


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
    source_spec = protocol["source"]
    data_source = repo / "experiments/S005" / str(source_spec["data_experiment"])
    review_source = repo / "experiments/S005" / str(source_spec["review_experiment"])
    validate_experiment_archive(data_source)
    validate_experiment_archive(review_source)
    expected = {
        data_source / "experiment_manifest.json": source_spec["data_manifest_sha256"],
        data_source / "artifacts/component_margin_panel.csv.gz": source_spec[
            "component_panel_sha256"
        ],
        data_source / "artifacts/margin_detail.csv.gz": source_spec["margin_sha256"],
        review_source / "experiment_manifest.json": source_spec["review_manifest_sha256"],
        review_source / "artifacts/reviewed_data_quality.json": source_spec[
            "review_quality_sha256"
        ],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read(review_source / "artifacts/reviewed_data_quality.json")["passed"]:
        raise ValueError("reviewed margin data gate did not pass")

    component = pd.read_csv(data_source / "artifacts/component_margin_panel.csv.gz")
    component["dt"] = pd.to_datetime(component["dt"])
    component["net_financing"] = component["rzmre"] - component["rzche"]
    component["positive_financing_weight"] = component["weight"].where(
        component["net_financing"].gt(0), 0.0
    )
    daily = component.groupby("dt", sort=True, observed=True).agg(
        total_weight=("weight", "sum"),
        positive_financing_weight=("positive_financing_weight", "sum"),
    )
    daily["component_financing_breadth"] = (
        daily["positive_financing_weight"] / daily["total_weight"]
    )
    margin = pd.read_csv(data_source / "artifacts/margin_detail.csv.gz")
    margin["trade_date"] = pd.to_datetime(margin["trade_date"])
    etf = margin.loc[margin["ts_code"].eq(protocol["symbol"])].set_index("trade_date")
    etf = etf.sort_index()
    etf["net_financing"] = etf["rzmre"] - etf["rzche"]
    etf["etf_financing_intensity"] = etf["net_financing"] / etf["rzye"].shift(1)
    daily["etf_financing_intensity"] = etf["etf_financing_intensity"].reindex(daily.index)

    feature = protocol["feature"]
    lookback = int(feature["rolling_reference_sessions"])
    minimum = int(feature["minimum_reference_observations"])
    density = protocol["density"]
    summaries: list[dict[str, object]] = []
    for quantile in [float(value) for value in feature["strength_quantiles"]]:
        suffix = f"Q{int(quantile * 100)}"
        breadth_upper = (
            daily["component_financing_breadth"]
            .shift(1)
            .rolling(lookback, min_periods=minimum)
            .quantile(quantile)
        )
        breadth_lower = (
            daily["component_financing_breadth"]
            .shift(1)
            .rolling(lookback, min_periods=minimum)
            .quantile(1.0 - quantile)
        )
        intensity_upper = (
            daily["etf_financing_intensity"]
            .shift(1)
            .rolling(lookback, min_periods=minimum)
            .quantile(quantile)
        )
        intensity_lower = (
            daily["etf_financing_intensity"]
            .shift(1)
            .rolling(lookback, min_periods=minimum)
            .quantile(1.0 - quantile)
        )
        daily[f"breadth_upper_{suffix.lower()}"] = breadth_upper
        daily[f"breadth_lower_{suffix.lower()}"] = breadth_lower
        daily[f"intensity_upper_{suffix.lower()}"] = intensity_upper
        daily[f"intensity_lower_{suffix.lower()}"] = intensity_lower
        paths = {
            f"JOINT_LEVERAGE_BUILD_{suffix}": (
                daily["component_financing_breadth"].ge(breadth_upper)
                & daily["etf_financing_intensity"].gt(0)
                & daily["etf_financing_intensity"].ge(intensity_upper)
            ),
            f"COMPONENT_LEVERAGE_BUILD_{suffix}": (
                daily["component_financing_breadth"].ge(breadth_upper)
                & daily["etf_financing_intensity"].ge(0)
            ),
            f"JOINT_DELEVERAGING_{suffix}": (
                daily["component_financing_breadth"].le(breadth_lower)
                & daily["etf_financing_intensity"].lt(0)
                & daily["etf_financing_intensity"].le(intensity_lower)
            ),
            f"COMPONENT_DELEVERAGING_{suffix}": (
                daily["component_financing_breadth"].le(breadth_lower)
                & daily["etf_financing_intensity"].le(0)
            ),
        }
        first_ready = breadth_upper.dropna().index.min()
        for name, raw in paths.items():
            accepted = _cooldown(raw, int(density["cooldown_sessions"]))
            daily[name] = accepted
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
    output = daily.reset_index()
    output["dt"] = output["dt"].dt.strftime("%Y-%m-%d")
    output.to_csv(
        artifacts / "margin_feature_ledger.csv.gz",
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
        "paths_tested": int(len(summary)),
        "density_passing_paths": passing,
        "reads_post_event_prices": False,
        "decision": "PROCEED_TO_FIXED_RETURN_TEST" if passing else "STOP_MARGIN_MECHANISMS_ON_DENSITY",
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
        "# S005 EX24 执行\n\n状态：`COMPLETE`。已完成融资机制无收益密度普查。\n\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX24 结论\n\n"
        f"裁决：`{selection['decision']}`。通过路径："
        f"{', '.join(passing) if passing else 'NONE'}。本轮未读取事件后收益。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "status": "COMPLETE",
            "reads_post_event_prices": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
