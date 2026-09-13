from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S005_EX30"


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


def _event_density(events: pd.Series, first_ready: pd.Timestamp, protocol: dict[str, object]) -> tuple[pd.Series, dict[str, float]]:
    density = protocol["density"]
    accepted = _cooldown(events, int(density["cooldown_sessions"]))
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
    return accepted, {
        "rolling_60_median": float(rolling.median()),
        "rolling_60_p10": float(rolling.quantile(0.1)),
        "rolling_60_minimum": float(rolling.min()),
        "rolling_60_maximum": float(rolling.max()),
    }


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
        source / "artifacts/global_technology_panel.csv.gz": source_spec["data_sha256"],
        source / "artifacts/data_quality.json": source_spec["quality_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read(source / "artifacts/data_quality.json")["passed"]:
        raise ValueError("global technology data gate did not pass")
    if protocol.get("reads_post_event_prices"):
        raise ValueError("EX30 may not read 588080 post-event returns")

    panel = pd.read_csv(
        source / "artifacts/global_technology_panel.csv.gz",
        parse_dates=["a_share_session", "us_trade_date"],
    )
    close = panel.pivot(index="a_share_session", columns="source_code", values="close").sort_index()
    us_dates = panel.pivot(index="a_share_session", columns="source_code", values="us_trade_date").sort_index()
    interval_returns = close.pct_change(fill_method=None)
    fresh = us_dates.ne(us_dates.shift(1)).any(axis=1)
    feature = protocol["feature"]
    semis = list(feature["semiconductor_codes"])
    missing = sorted(set(semis + [feature["broad_market_code"], feature["technology_index_code"]]).difference(close.columns))
    if missing:
        raise ValueError(f"global panel missing sources: {missing}")
    ledger = pd.DataFrame(index=close.index)
    ledger["fresh_us_observation"] = fresh
    ledger["semi_basket_return"] = interval_returns[semis].mean(axis=1)
    ledger["semi_positive_breadth"] = interval_returns[semis].gt(0).sum(axis=1)
    ledger["spx_return"] = interval_returns[str(feature["broad_market_code"])]
    ledger["ixic_return"] = interval_returns[str(feature["technology_index_code"])]
    ledger["semi_excess_spx"] = ledger["semi_basket_return"] - ledger["spx_return"]
    ledger["ixic_excess_spx"] = ledger["ixic_return"] - ledger["spx_return"]

    lookback = int(feature["rolling_reference_sessions"])
    minimum = int(feature["minimum_reference_observations"])
    min_breadth = int(feature["minimum_positive_semiconductor_breadth"])
    summaries: list[dict[str, object]] = []
    for quantile in [float(value) for value in feature["strength_quantiles"]]:
        suffix = f"Q{int(quantile * 100)}"
        thresholds = {
            "SEMI_SPECIFIC_RISK_ON": ledger["semi_excess_spx"].shift(1).rolling(lookback, min_periods=minimum).quantile(quantile),
            "SEMI_BROAD_RISK_ON": ledger["semi_basket_return"].shift(1).rolling(lookback, min_periods=minimum).quantile(quantile),
        }
        strengths = {
            "SEMI_SPECIFIC_RISK_ON": ledger["semi_excess_spx"],
            "SEMI_BROAD_RISK_ON": ledger["semi_basket_return"],
        }
        for family, threshold in thresholds.items():
            name = f"{family}_{suffix}"
            ledger[f"{name}_threshold"] = threshold
            raw = (
                ledger["fresh_us_observation"]
                & ledger["semi_positive_breadth"].ge(min_breadth)
                & strengths[family].gt(0)
                & strengths[family].ge(threshold)
            )
            first_ready = threshold.dropna().index.min()
            accepted, metrics = _event_density(raw, first_ready, protocol)
            ledger[name] = accepted
            density = protocol["density"]
            density_pass = bool(
                metrics["rolling_60_median"] >= float(density["minimum_median"])
                and metrics["rolling_60_p10"] >= float(density["minimum_p10"])
            )
            summaries.append(
                {
                    "mechanism": name,
                    "family": family,
                    "quantile": quantile,
                    "raw_events": int(raw.sum()),
                    "independent_events": int(accepted.sum()),
                    **metrics,
                    "density_pass": density_pass,
                }
            )

    summary = pd.DataFrame(summaries)
    priorities = list(protocol["selection_rule"]["family_priority"])
    selected: str | None = None
    for family in priorities:
        passing = summary[(summary["family"] == family) & summary["density_pass"]]
        if not passing.empty:
            selected = str(passing.sort_values("quantile", ascending=False).iloc[0]["mechanism"])
            break
    summary = summary.sort_values(
        ["density_pass", "rolling_60_p10", "rolling_60_median", "quantile"],
        ascending=[False, False, False, False],
    )
    output = ledger.reset_index()
    output["a_share_session"] = output["a_share_session"].dt.strftime("%Y-%m-%d")
    output.to_csv(
        artifacts / "global_semiconductor_feature_ledger.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    summary.to_csv(artifacts / "density_summary.csv", index=False, lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "feature_sessions": int(len(ledger)),
        "fresh_us_observation_sessions": int(fresh.sum()),
        "paths_tested": int(len(summary)),
        "density_passing_paths": summary.loc[summary["density_pass"], "mechanism"].tolist(),
        "selected_for_fixed_return_test": selected,
        "reads_post_event_prices": False,
        "decision": "PROCEED_TO_FIXED_RETURN_TEST" if selected else "STOP_GLOBAL_SEMI_MECHANISMS_ON_DENSITY",
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
        "# S005 EX30 执行\n\n状态：`COMPLETE`。已完成全球半导体风险偏好的无收益密度普查。\n\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    selected_text = selected or "NONE"
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX30 结论\n\n"
        f"裁决：`{evidence['decision']}`。按预注册优先级获得唯一收益检验资格的路径："
        f"`{selected_text}`。本轮未读取588080事件后收益。\n",
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
