from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260914_S005_EX65"


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
    if protocol.get("reads_post_event_prices"):
        raise ValueError("EX65 may not read post-event prices")

    source_spec = protocol["source"]
    source = repo / "experiments/S005" / str(source_spec["experiment_id"])
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["manifest_sha256"],
        source / "artifacts/semiconductor_moneyflow_panel.csv.gz": source_spec["panel_sha256"],
        source / "artifacts/data_gate_evidence.json": source_spec["quality_sha256"],
        source / "artifacts/members_outside_star50.csv": source_spec["outside_members_sha256"],
        repo / "data/raw/588080_manifest.json": protocol["calendar_manifest_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read(source / "artifacts/data_gate_evidence.json")["passed"]:
        raise ValueError("EX64 data gate did not pass")

    panel = pd.read_csv(
        source / "artifacts/semiconductor_moneyflow_panel.csv.gz",
        usecols=["trade_date", "ts_code", "net_mf_amount", "gross_order_amount"],
        parse_dates=["trade_date"],
    )
    outside = set(
        pd.read_csv(source / "artifacts/members_outside_star50.csv")["ts_code"].astype(str)
    )
    panel = panel.loc[panel["ts_code"].astype(str).isin(outside)].copy()
    if panel.empty:
        raise ValueError("outside-STAR50 semiconductor panel is empty")
    panel["observed"] = panel["net_mf_amount"].notna()
    panel["positive"] = panel["net_mf_amount"].gt(0.0)
    grouped = panel.groupby("trade_date", sort=True, observed=True)
    ledger = grouped.agg(
        active_members=("ts_code", "nunique"),
        observed_members=("observed", "sum"),
        member_coverage=("observed", "mean"),
        positive_member_ratio=("positive", "mean"),
        net_mf_amount=("net_mf_amount", "sum"),
        gross_order_amount=("gross_order_amount", "sum"),
    )
    ledger["net_flow_ratio"] = ledger["net_mf_amount"].div(
        ledger["gross_order_amount"].replace(0.0, pd.NA)
    )

    market = load_market_data(repo / "data/raw", str(protocol["symbol"])).daily
    market["dt"] = pd.to_datetime(market["dt"]).dt.normalize()
    target_close = market.set_index("dt")["close"].astype(float)
    ledger["target_same_day_return"] = target_close.pct_change(fill_method=None).reindex(
        ledger.index
    )
    feature = protocol["feature"]
    lookback = int(feature["rolling_reference_sessions"])
    minimum = int(feature["minimum_reference_observations"])
    target_threshold = (
        ledger["target_same_day_return"]
        .shift(1)
        .rolling(lookback, min_periods=minimum)
        .quantile(float(feature["target_response_quantile"]))
    )
    ledger["target_response_threshold"] = target_threshold
    density = protocol["density"]
    summaries: list[dict[str, object]] = []
    for quantile in [float(value) for value in feature["strength_quantiles"]]:
        suffix = f"Q{int(quantile * 100)}"
        breadth_threshold = (
            ledger["positive_member_ratio"]
            .shift(1)
            .rolling(lookback, min_periods=minimum)
            .quantile(quantile)
        )
        flow_threshold = (
            ledger["net_flow_ratio"]
            .shift(1)
            .rolling(lookback, min_periods=minimum)
            .quantile(quantile)
        )
        ledger[f"breadth_threshold_{suffix.lower()}"] = breadth_threshold
        ledger[f"flow_threshold_{suffix.lower()}"] = flow_threshold
        paths = {
            f"COORDINATED_EXTERNAL_DEMAND_{suffix}": (
                ledger["positive_member_ratio"].ge(breadth_threshold)
                & ledger["net_flow_ratio"].ge(flow_threshold)
                & ledger["target_same_day_return"].le(target_threshold)
            ),
            f"EXTERNAL_FLOW_PRICE_DIVERGENCE_{suffix}": (
                ledger["positive_member_ratio"].gt(0.5)
                & ledger["net_flow_ratio"].ge(flow_threshold)
                & ledger["target_same_day_return"].le(0.0)
            ),
        }
        first_ready = pd.concat([breadth_threshold, flow_threshold, target_threshold], axis=1).dropna().index.min()
        for name, raw in paths.items():
            accepted = _cooldown(raw, int(density["cooldown_sessions"]))
            ledger[name] = accepted
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
            p10 = float(rolling.quantile(0.10))
            family = name.rsplit("_Q", maxsplit=1)[0]
            summaries.append(
                {
                    "mechanism": name,
                    "family": family,
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

    summary = pd.DataFrame(summaries)
    selected: str | None = None
    for family in protocol["selection_rule"]["family_priority"]:
        passing = summary.loc[summary["family"].eq(family) & summary["density_pass"]]
        if not passing.empty:
            selected = str(passing.sort_values("quantile", ascending=False).iloc[0]["mechanism"])
            break
    summary = summary.sort_values(
        ["density_pass", "rolling_60_p10", "rolling_60_median", "quantile"],
        ascending=[False, False, False, False],
    )
    ledger.reset_index().to_csv(
        artifacts / "external_industry_flow_ledger.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    summary.to_csv(artifacts / "density_summary.csv", index=False, lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "feature_sessions": int(len(ledger)),
        "outside_member_count": int(len(outside)),
        "paths_tested": int(len(summary)),
        "density_passing_paths": summary.loc[summary["density_pass"], "mechanism"].tolist(),
        "selected_for_fixed_return_test": selected,
        "reads_post_event_prices": False,
        "decision": (
            "PROCEED_TO_FIXED_RETURN_TEST"
            if selected
            else "STOP_EXTERNAL_INDUSTRY_FLOW_ON_DENSITY"
        ),
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
        "# S005 EX65 执行\n\n状态：`COMPLETE`。完成外围半导体资金扩散机制的无收益密度审计。\n\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX65 结论\n\n"
        f"裁决：`{evidence['decision']}`。唯一收益检验资格：`{selected or 'NONE'}`。"
        "本轮没有读取事件后收益；因子出现频率没有参与淘汰，密度门只作用于最终事件。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": evidence["decision"],
            "reads_post_event_prices": False,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
