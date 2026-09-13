from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256
from czsc_trader.intraday_data import load_intraday_research_data


EXPERIMENT_ID = "20260913_S005_EX16"


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


def _spot_close(repo: Path, symbol: str) -> pd.Series:
    intraday = load_intraday_research_data(repo / "data/raw", symbol).frames["1m"]
    return (
        pd.DataFrame(
            {
                "trade_date": pd.to_datetime(intraday["Date"]).dt.normalize(),
                "close": pd.to_numeric(intraday["Close"]),
            }
        )
        .groupby("trade_date", sort=True)["close"]
        .last()
    )


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

    start = pd.Timestamp(str(protocol["development_start"]))
    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    spot = _spot_close(repo, str(protocol["symbol"])).loc[start:cutoff]
    quotes = pd.read_csv(source / "artifacts/option_daily.csv.gz", parse_dates=["trade_date", "maturity_date"])
    quotes = quotes.loc[
        quotes["standard_contract"].astype(bool)
        & quotes["valid_quote"].astype(bool)
        & quotes["settle"].gt(0)
        & quotes["trade_date"].between(start, cutoff)
    ].copy()
    quotes["spot"] = quotes["trade_date"].map(spot)
    quotes["days_to_maturity"] = (quotes["maturity_date"] - quotes["trade_date"]).dt.days
    selection = protocol["contract_selection"]
    quotes = quotes.loc[
        quotes["days_to_maturity"].between(
            int(selection["minimum_days_to_maturity"]), int(selection["maximum_days_to_maturity"])
        )
    ].copy()
    pair_source = quotes[
        ["trade_date", "maturity_date", "days_to_maturity", "exercise_price", "call_put", "settle", "spot"]
    ]
    pairs = pair_source.pivot_table(
        index=["trade_date", "maturity_date", "days_to_maturity", "exercise_price", "spot"],
        columns="call_put",
        values="settle",
        aggfunc="last",
    ).reset_index()
    pairs = pairs.dropna(subset=["C", "P"])
    nearest_maturity = pairs.groupby("trade_date")["maturity_date"].transform("min")
    pairs = pairs.loc[pairs["maturity_date"].eq(nearest_maturity)].copy()
    pairs["strike_distance"] = (pairs["exercise_price"] / pairs["spot"] - 1.0).abs()
    selected = pairs.sort_values(["trade_date", "strike_distance", "exercise_price"]).drop_duplicates(
        "trade_date", keep="first"
    )
    selected["expected_move_proxy"] = (
        (selected["C"] + selected["P"])
        / selected["spot"]
        / np.sqrt(selected["days_to_maturity"] / 365.0)
    )
    feature = pd.DataFrame(index=spot.index)
    feature["spot_close"] = spot
    feature["spot_return"] = spot.pct_change()
    feature = feature.join(
        selected.set_index("trade_date")[
            ["maturity_date", "days_to_maturity", "exercise_price", "C", "P", "expected_move_proxy"]
        ]
    )

    lookback = int(protocol["feature"]["rolling_reference_sessions"])
    minimum_observations = int(protocol["feature"]["minimum_reference_observations"])
    quantiles = [float(value) for value in protocol["feature"]["strength_quantiles"]]
    density = protocol["density"]
    ledger = feature.copy()
    summaries: list[dict[str, object]] = []
    for quantile in quantiles:
        threshold = (
            feature["expected_move_proxy"]
            .shift(1)
            .rolling(lookback, min_periods=minimum_observations)
            .quantile(quantile)
        )
        paths = {
            f"HIGH_EXPECTED_MOVE_UP_Q{int(quantile * 100)}": feature["expected_move_proxy"].ge(threshold)
            & feature["spot_return"].gt(0),
            f"HIGH_EXPECTED_MOVE_DOWN_Q{int(quantile * 100)}": feature["expected_move_proxy"].ge(threshold)
            & feature["spot_return"].lt(0),
        }
        ledger[f"threshold_q{int(quantile * 100)}"] = threshold
        for name, raw in paths.items():
            accepted = _cooldown(raw, int(density["cooldown_sessions"]))
            first_ready = threshold.loc[threshold.notna()].index.min()
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
    selected_paths: dict[str, str] = {}
    for direction, group in summary.loc[summary["density_pass"]].groupby("direction", observed=True):
        selected_paths[str(direction)] = str(
            group.sort_values("quantile", ascending=False).iloc[0]["mechanism"]
        )
    decision = "PROCEED_TO_FIXED_OPTION_EXPECTED_MOVE_RETURN_TEST" if selected_paths else "STOP_OPTION_EXPECTED_MOVE_ON_DENSITY"
    valid_feature = feature["expected_move_proxy"].replace([np.inf, -np.inf], np.nan).dropna()
    coverage = {
        "feature_sessions": int(len(valid_feature)),
        "total_sessions": int(len(feature)),
        "coverage": float(len(valid_feature) / len(feature)),
        "proxy_median": float(valid_feature.median()),
        "proxy_p10": float(valid_feature.quantile(0.1)),
        "proxy_p90": float(valid_feature.quantile(0.9)),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    ledger.reset_index(names="trade_date").to_csv(
        artifacts / "expected_move_ledger.csv.gz", index=False, compression=compression, lineterminator="\n"
    )
    summary.to_csv(artifacts / "density_summary.csv", index=False, lineterminator="\n")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "feature_coverage": coverage,
        "selected_mechanisms": selected_paths,
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
        "# S005 EX16 结论",
        "",
        "|路径|独立事件|60日中位数|P10|密度门|",
        "|---|---:|---:|---:|---|",
    ]
    lines += [
        f"|{item['mechanism']}|{item['independent_events']}|{item['rolling_60_median']:.1f}|"
        f"{item['rolling_60_p10']:.1f}|{'PASS' if item['density_pass'] else 'FAIL'}|"
        for item in summaries
    ]
    lines += ["", f"冻结路径：`{selected_paths or 'NONE'}`；裁决：`{decision}`。本轮未读取事件后收益。", ""]
    (experiment / "03_execution.md").write_text(
        "# S005 EX16 执行\n\n状态：COMPLETE。已完成期权预期波动代理与无收益密度筛选。\n",
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
            "selected_mechanisms": selected_paths,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
