from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260914_S005_EX67"


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
    if protocol.get("reads_target_forward_returns"):
        raise ValueError("EX67 may not read target forward returns")

    source_spec = protocol["source"]
    source = repo / "experiments/S005" / str(source_spec["experiment_id"])
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["manifest_sha256"],
        source / "artifacts/active_constituent_forecasts.csv.gz": source_spec["forecast_panel_sha256"],
        source / "artifacts/data_gate_evidence.json": source_spec["quality_sha256"],
        repo / "data/raw/588080_manifest.json": source_spec["calendar_manifest_sha256"],
    }
    for path, digest in expected.items():
        if raw_file_sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")
    if not _read(source / "artifacts/data_gate_evidence.json")["passed"]:
        raise ValueError("EX66 data gate did not pass")

    columns = [
        "ts_code",
        "report_date",
        "available_session",
        "org_name",
        "quarter",
        "np",
        "eps",
        "weight",
    ]
    reports = pd.read_csv(
        source / "artifacts/active_constituent_forecasts.csv.gz",
        usecols=columns,
        parse_dates=["report_date", "available_session"],
    )
    reports = reports.sort_values(
        ["ts_code", "org_name", "quarter", "report_date", "available_session"],
        kind="mergesort",
    ).reset_index(drop=True)
    revision = protocol["revision"]
    groups = reports.groupby(["ts_code", "org_name", "quarter"], dropna=False, sort=False)
    reports["previous_report_date"] = groups["report_date"].shift(1)
    reports["previous_np"] = groups["np"].shift(1)
    reports["previous_eps"] = groups["eps"].shift(1)
    reports["previous_age_days"] = (
        reports["report_date"] - reports["previous_report_date"]
    ).dt.days

    np_pair = reports["np"].notna() & reports["previous_np"].notna()
    current = reports["np"].where(np_pair, reports["eps"])
    previous = reports["previous_np"].where(np_pair, reports["previous_eps"])
    comparable = (
        current.notna()
        & previous.notna()
        & previous.abs().gt(1e-12)
        & reports["previous_age_days"].between(
            0, int(revision["maximum_previous_age_days"]), inclusive="both"
        )
    )
    reports["relative_revision"] = (
        current.sub(previous).div(previous.abs()).where(comparable)
    )
    threshold = float(revision["minimum_absolute_relative_change"])
    reports["revision_sign"] = np.select(
        [reports["relative_revision"].gt(threshold), reports["relative_revision"].lt(-threshold)],
        [1.0, -1.0],
        default=0.0,
    )
    reports.loc[~comparable, "revision_sign"] = np.nan

    comparable_reports = reports.loc[reports["revision_sign"].notna()].copy()
    broker_day = (
        comparable_reports.groupby(
            ["available_session", "ts_code", "org_name"], sort=True, observed=True
        )
        .agg(revision_sign=("revision_sign", "median"), weight=("weight", "first"))
        .reset_index()
    )
    company_day = (
        broker_day.groupby(["available_session", "ts_code"], sort=True, observed=True)
        .agg(revision_sign=("revision_sign", "mean"), weight=("weight", "first"))
        .reset_index()
    )
    company_day["signed_weight"] = company_day["revision_sign"] * company_day["weight"]
    company_day["observed_weight"] = company_day["weight"]

    market = load_market_data(repo / "data/raw", str(protocol["symbol"])).daily
    market["dt"] = pd.to_datetime(market["dt"]).dt.normalize()
    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    calendar = pd.DatetimeIndex(
        market.loc[market["dt"].le(cutoff), "dt"].drop_duplicates().sort_values()
    )
    daily = (
        company_day.groupby("available_session", sort=True, observed=True)
        .agg(
            signed_weight=("signed_weight", "sum"),
            observed_weight=("observed_weight", "sum"),
            revised_companies=("ts_code", "nunique"),
            revision_observations=("revision_sign", "size"),
        )
        .reindex(calendar, fill_value=0.0)
    )
    window = int(revision["rolling_sessions"])
    daily["rolling_signed_weight"] = daily["signed_weight"].rolling(window).sum()
    daily["rolling_observed_weight"] = daily["observed_weight"].rolling(window).sum()
    daily["revision_score"] = daily["rolling_signed_weight"].div(
        daily["rolling_observed_weight"].replace(0.0, np.nan)
    )
    presence = (
        company_day.assign(observed=1)
        .pivot_table(
            index="available_session",
            columns="ts_code",
            values="observed",
            aggfunc="max",
            fill_value=0,
        )
        .reindex(calendar, fill_value=0)
    )
    daily["rolling_distinct_companies"] = presence.rolling(window).max().sum(axis=1)

    close = market.set_index("dt")["close"].astype(float).reindex(calendar)
    daily["target_past_return"] = close.pct_change(
        int(revision["target_pullback_sessions"]), fill_method=None
    )
    density = protocol["density"]
    summaries: list[dict[str, object]] = []
    for quantile in [float(value) for value in revision["strength_quantiles"]]:
        suffix = f"Q{int(quantile * 100)}"
        score_threshold = (
            daily["revision_score"]
            .shift(1)
            .rolling(
                int(revision["threshold_reference_sessions"]),
                min_periods=int(revision["minimum_threshold_observations"]),
            )
            .quantile(quantile)
        )
        name = f"BROAD_EARNINGS_REVISION_PULLBACK_{suffix}"
        raw = (
            daily["rolling_distinct_companies"].ge(
                int(revision["minimum_distinct_companies"])
            )
            & daily["revision_score"].ge(score_threshold)
            & daily["target_past_return"].le(float(revision["target_maximum_return"]))
        )
        accepted = _cooldown(raw, int(density["cooldown_sessions"]))
        daily[f"score_threshold_{suffix.lower()}"] = score_threshold
        daily[name] = accepted
        first_ready = score_threshold.dropna().index.min()
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
        summaries.append(
            {
                "mechanism": name,
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

    summary = pd.DataFrame(summaries).sort_values("quantile", ascending=False)
    passing = summary.loc[summary["density_pass"]]
    selected = None if passing.empty else str(passing.iloc[0]["mechanism"])
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    daily.reset_index(names="trade_date").to_csv(
        artifacts / "expectation_revision_ledger.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
    )
    company_day.to_csv(
        artifacts / "company_revision_events.csv.gz",
        index=False,
        compression=compression,
        lineterminator="\n",
        date_format="%Y-%m-%d",
    )
    summary.to_csv(artifacts / "density_summary.csv", index=False, lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "comparable_report_rows": int(len(comparable_reports)),
        "company_revision_events": int(len(company_day)),
        "revision_factor_sessions": int(daily["revision_score"].notna().sum()),
        "paths_tested": int(len(summary)),
        "density_passing_paths": summary.loc[summary["density_pass"], "mechanism"].tolist(),
        "selected_for_fixed_return_test": selected,
        "reads_target_forward_returns": False,
        "decision": (
            "PROCEED_TO_FIXED_EXPECTATION_REVISION_RETURN_TEST"
            if selected
            else "STOP_EXPECTATION_REVISION_PULLBACK_ON_DENSITY"
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
        "# S005 EX67 执行\n\n状态：`COMPLETE`。完成卖方预期上修与目标回撤机制的无收益密度审计。\n\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX67 结论\n\n"
        f"裁决：`{evidence['decision']}`。唯一收益检验资格：`{selected or 'NONE'}`。"
        "慢速预期因子的出现频率没有参与淘汰，密度门只作用于最终事件；本轮没有读取事件后收益。\n",
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
            "reads_target_forward_returns": False,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
