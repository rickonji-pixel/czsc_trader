from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX32"


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
    source = repo / "experiments/S005" / str(source_spec["experiment_id"])
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["manifest_sha256"],
        source / "artifacts/etf_share_size.csv.gz": source_spec["data_sha256"],
        source / "artifacts/data_quality.json": source_spec["quality_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read(source / "artifacts/data_quality.json")["passed"]:
        raise ValueError("ETF share data gate did not pass")
    if protocol.get("reads_post_event_prices"):
        raise ValueError("EX32 may not read 588080 post-event returns")

    data = pd.read_csv(source / "artifacts/etf_share_size.csv.gz", parse_dates=["trade_date"])
    data = data.set_index("trade_date").sort_index()
    data["nav_dislocation"] = data["close"] / data["nav"] - 1.0
    if not np.isfinite(data["nav_dislocation"].dropna()).all():
        raise ValueError("non-finite NAV dislocation")
    feature = protocol["feature"]
    lookback = int(feature["rolling_reference_sessions"])
    minimum = int(feature["minimum_reference_observations"])
    density = protocol["density"]
    summaries: list[dict[str, object]] = []
    for quantile in [float(value) for value in feature["strength_quantiles"]]:
        suffix = f"Q{int(quantile * 100)}"
        upper = data["nav_dislocation"].shift(1).rolling(lookback, min_periods=minimum).quantile(quantile)
        lower = data["nav_dislocation"].shift(1).rolling(lookback, min_periods=minimum).quantile(1.0 - quantile)
        data[f"premium_threshold_{suffix.lower()}"] = upper
        data[f"discount_threshold_{suffix.lower()}"] = lower
        paths = {
            f"DISCOUNT_REVERSION_{suffix}": data["nav_dislocation"].lt(0) & data["nav_dislocation"].le(lower),
            f"PREMIUM_DEMAND_{suffix}": data["nav_dislocation"].gt(0) & data["nav_dislocation"].ge(upper),
        }
        for name, raw in paths.items():
            accepted = _cooldown(raw, int(density["cooldown_sessions"]))
            data[name] = accepted
            first_ready = (lower if name.startswith("DISCOUNT") else upper).dropna().index.min()
            rolling = (
                accepted.loc[accepted.index >= first_ready]
                .astype(int)
                .rolling(int(density["rolling_window_sessions"]), min_periods=int(density["rolling_window_sessions"]))
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
                    "density_pass": bool(median >= float(density["minimum_median"]) and p10 >= float(density["minimum_p10"])),
                }
            )
    summary = pd.DataFrame(summaries)
    selected: str | None = None
    for family in protocol["selection_rule"]["family_priority"]:
        passing = summary[(summary["family"] == family) & summary["density_pass"]]
        if not passing.empty:
            selected = str(passing.sort_values("quantile", ascending=False).iloc[0]["mechanism"])
            break
    summary = summary.sort_values(
        ["density_pass", "rolling_60_p10", "rolling_60_median", "quantile"],
        ascending=[False, False, False, False],
    )
    output = data.reset_index()
    output["trade_date"] = output["trade_date"].dt.strftime("%Y-%m-%d")
    output.to_csv(
        artifacts / "nav_dislocation_feature_ledger.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    summary.to_csv(artifacts / "density_summary.csv", index=False, lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "feature_sessions": int(len(data)),
        "valid_dislocation_sessions": int(data["nav_dislocation"].notna().sum()),
        "paths_tested": int(len(summary)),
        "density_passing_paths": summary.loc[summary["density_pass"], "mechanism"].tolist(),
        "selected_for_fixed_return_test": selected,
        "reads_post_event_prices": False,
        "decision": "PROCEED_TO_FIXED_RETURN_TEST" if selected else "STOP_NAV_DISLOCATION_MECHANISMS_ON_DENSITY",
    }
    (artifacts / "selection_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    rows = ["|路径|独立事件|60日中位数|P10|密度门|", "|---|---:|---:|---:|---|"]
    for row in summary.itertuples(index=False):
        rows.append(
            f"|{row.mechanism}|{row.independent_events}|{row.rolling_60_median:.1f}|"
            f"{row.rolling_60_p10:.1f}|{'PASS' if row.density_pass else 'FAIL'}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S005 EX32 执行\n\n状态：`COMPLETE`。已完成ETF折溢价机制的无收益密度普查。\n\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX32 结论\n\n"
        f"裁决：`{evidence['decision']}`。按预注册优先级获得唯一收益检验资格的路径："
        f"`{selected or 'NONE'}`。本轮未读取588080事件后收益。\n",
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
