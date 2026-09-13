from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260913_S005_EX15"


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
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    source = repo / "experiments/S005" / str(protocol["source_experiment"])
    validate_experiment_archive(source)

    quotes = pd.read_csv(source / "artifacts/option_daily.csv.gz", parse_dates=["trade_date", "maturity_date"])
    intraday = load_intraday_research_data(repo / "data/raw", str(protocol["symbol"])).frames["1m"]
    spot = (
        pd.DataFrame(
            {
                "trade_date": pd.to_datetime(intraday["Date"]).dt.normalize(),
                "close": pd.to_numeric(intraday["Close"]),
            }
        )
        .groupby("trade_date", sort=True)["close"]
        .last()
    )
    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    start = pd.Timestamp(str(protocol["development_start"]))
    quotes = quotes.loc[quotes["trade_date"].between(start, cutoff)].copy()
    quotes["spot"] = quotes["trade_date"].map(spot)
    quotes["days_to_maturity"] = (quotes["maturity_date"] - quotes["trade_date"]).dt.days
    quotes["moneyness"] = quotes["exercise_price"] / quotes["spot"]
    eligible = protocol["eligible_contracts"]
    quotes = quotes.loc[
        quotes["standard_contract"].astype(bool)
        & quotes["valid_quote"].astype(bool)
        & quotes["days_to_maturity"].between(
            int(eligible["minimum_days_to_maturity"]), int(eligible["maximum_days_to_maturity"])
        )
        & quotes["moneyness"].between(
            float(eligible["minimum_moneyness"]), float(eligible["maximum_moneyness"])
        )
    ].copy()
    grouped = quotes.groupby(["trade_date", "call_put"], observed=True).agg(vol=("vol", "sum"), oi=("oi", "sum"))
    volume = grouped["vol"].unstack("call_put").reindex(columns=["C", "P"])
    open_interest = grouped["oi"].unstack("call_put").reindex(columns=["C", "P"])
    dates = spot.loc[spot.index.to_series().between(start, cutoff)].index
    feature = pd.DataFrame(index=dates)
    feature["call_volume"] = volume["C"]
    feature["put_volume"] = volume["P"]
    feature["call_open_interest"] = open_interest["C"]
    feature["put_open_interest"] = open_interest["P"]
    feature["put_volume_share"] = feature["put_volume"] / (feature["put_volume"] + feature["call_volume"])
    feature["put_open_interest_share"] = feature["put_open_interest"] / (
        feature["put_open_interest"] + feature["call_open_interest"]
    )
    feature["direction_score"] = (
        0.5 - feature["put_volume_share"] + 0.5 - feature["put_open_interest_share"]
    )
    feature["call_consensus"] = (
        feature["put_volume_share"].lt(0.5) & feature["put_open_interest_share"].lt(0.5)
    )
    feature["put_consensus"] = (
        feature["put_volume_share"].gt(0.5) & feature["put_open_interest_share"].gt(0.5)
    )

    lookback = int(protocol["feature"]["rolling_reference_sessions"])
    quantiles = [float(value) for value in protocol["feature"]["strength_quantiles"]]
    density = protocol["density"]
    ledger = feature.copy()
    summaries: list[dict[str, object]] = []
    for quantile in quantiles:
        upper = feature["direction_score"].shift(1).rolling(lookback).quantile(quantile)
        lower = feature["direction_score"].shift(1).rolling(lookback).quantile(1.0 - quantile)
        paths = {
            f"CALL_DOMINANCE_Q{int(quantile * 100)}": (
                feature["call_consensus"] & feature["direction_score"].ge(upper),
                upper.notna(),
            ),
            f"PUT_FEAR_Q{int(quantile * 100)}": (
                feature["put_consensus"] & feature["direction_score"].le(lower),
                lower.notna(),
            ),
        }
        ledger[f"upper_q{int(quantile * 100)}"] = upper
        ledger[f"lower_q{int(quantile * 100)}"] = lower
        for name, (raw, threshold_ready) in paths.items():
            accepted = _cooldown(raw, int(density["cooldown_sessions"]))
            first_ready = threshold_ready.loc[threshold_ready].index.min()
            accepted_for_density = accepted.loc[accepted.index >= first_ready]
            rolling = (
                accepted_for_density.astype(int)
                .rolling(int(density["rolling_window_sessions"]), min_periods=int(density["rolling_window_sessions"]))
                .sum()
                .dropna()
            )
            item = {
                "mechanism": name,
                "direction": name.rsplit("_Q", maxsplit=1)[0],
                "quantile": quantile,
                "raw_events": int(raw.sum()),
                "independent_events": int(accepted.sum()),
                "rolling_60_median": float(rolling.median()),
                "rolling_60_p10": float(rolling.quantile(0.1)),
                "rolling_60_minimum": float(rolling.min()),
                "rolling_60_maximum": float(rolling.max()),
            }
            item["density_pass"] = bool(
                item["rolling_60_median"] >= float(density["minimum_median"])
                and item["rolling_60_p10"] >= float(density["minimum_p10"])
            )
            summaries.append(item)
            ledger[f"{name}__accepted"] = accepted.astype(int)

    summary = pd.DataFrame(summaries)
    selected: dict[str, str] = {}
    for direction, group in summary.loc[summary["density_pass"]].groupby("direction", observed=True):
        selected[str(direction)] = str(group.sort_values("quantile", ascending=False).iloc[0]["mechanism"])
    decision = "PROCEED_TO_FIXED_OPTION_RETURN_TEST" if selected else "STOP_OPTION_DIRECTIONAL_DEMAND_ON_DENSITY"
    coverage = {
        "feature_sessions": int(feature[["put_volume_share", "put_open_interest_share"]].notna().all(axis=1).sum()),
        "total_sessions": int(len(feature)),
        "coverage": float(feature[["put_volume_share", "put_open_interest_share"]].notna().all(axis=1).mean()),
        "direction_score_median": float(feature["direction_score"].median()),
        "direction_score_p10": float(feature["direction_score"].quantile(0.1)),
        "direction_score_p90": float(feature["direction_score"].quantile(0.9)),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    ledger.reset_index(names="trade_date").to_csv(
        artifacts / "option_feature_ledger.csv.gz", index=False, compression=compression, lineterminator="\n"
    )
    summary.to_csv(artifacts / "density_summary.csv", index=False, lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "feature_coverage": coverage,
        "selected_mechanisms": selected,
        "decision": decision,
        "reads_post_event_prices": False,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    (artifacts / "selection_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# S005 EX15 结论",
        "",
        "|路径|独立事件|60日中位数|P10|密度门|",
        "|---|---:|---:|---:|---|",
    ]
    lines += [
        f"|{item['mechanism']}|{item['independent_events']}|{item['rolling_60_median']:.1f}|"
        f"{item['rolling_60_p10']:.1f}|{'PASS' if item['density_pass'] else 'FAIL'}|"
        for item in summaries
    ]
    lines += ["", f"冻结路径：`{selected or 'NONE'}`；裁决：`{decision}`。本轮未读取事件后收益。", ""]
    (experiment / "03_execution.md").write_text(
        "# S005 EX15 执行\n\n状态：COMPLETE。已完成期权方向需求特征与无收益密度筛选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text("\n".join(lines), encoding="utf-8")
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "selected_mechanisms": selected,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
