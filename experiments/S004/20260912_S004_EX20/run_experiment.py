from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import (
    build_closing_dislocation_signals,
    load_replay_data,
    resolve_candidate_snapshot,
)
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260912_S004_EX20"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _adapt_payload(source: dict[str, object], symbol: str) -> dict[str, object]:
    payload = deepcopy(source)
    payload["symbol"] = symbol
    rule = payload["rule"]
    rule["symbol"] = symbol
    execution = rule["execution"]
    execution["instrument"]["symbol"] = symbol
    limit = 0.2 if symbol == "588080.SH" else 0.1
    execution["instrument"]["price_limit_ratio"] = limit
    execution["entry"]["limit_parameter"] = limit
    execution["exit"]["limit_ratio"] = limit
    return payload


def _event_panel(
    context: RepositoryContext,
    source_payload: dict[str, object],
    protocol: dict[str, object],
    symbol: str,
) -> pd.DataFrame:
    dataset = protocol["dataset"]
    definitions = protocol["fixed_definitions"]
    data = load_replay_data(
        context,
        str(dataset["name"]),
        symbol,
        str(dataset["asset_type"]),
        pd.Timestamp(dataset["cutoff"]).date(),
        include_one_minute=True,
    )
    payload = _adapt_payload(source_payload, symbol)
    snapshot = resolve_candidate_snapshot(
        context,
        f"S004-C001-X-{symbol[:6]}",
        payload,
        canonical_json_sha256(payload),
        f"{EXPERIMENT_ID}:mechanism-explanation",
    )
    signals = build_closing_dislocation_signals(
        snapshot,
        data,
        pd.Timestamp(dataset["evaluation_start"]),
        pd.Timestamp(dataset["evaluation_end"]),
    )
    decisions = signals.decisions.loc[signals.decisions["target_position"].eq(1)].copy()
    decisions["signal_date"] = pd.to_datetime(decisions["signal_date"]).dt.normalize()
    decisions = decisions.set_index("signal_date")

    adjusted = data.adjusted.daily.copy()
    adjusted["dt"] = pd.to_datetime(adjusted["dt"]).dt.normalize()
    adjusted = adjusted.set_index("dt").sort_index()
    raw = data.execution_daily.copy()
    raw["dt"] = pd.to_datetime(raw["dt"]).dt.normalize()
    raw = raw.set_index("dt").sort_index()
    sessions = pd.DatetimeIndex(raw.index)
    trend_ratio = adjusted["close"].astype(float).div(
        adjusted["close"].astype(float).rolling(60, min_periods=60).mean()
    ) - 1.0
    daily_return = adjusted["close"].astype(float).pct_change(fill_method=None)
    volatility = daily_return.rolling(20, min_periods=20).std() * np.sqrt(252.0)
    volatility_threshold = volatility.shift(1).rolling(120, min_periods=120).median()
    positions = {value: index for index, value in enumerate(sessions)}
    fee = float(definitions["stress_one_way_cost"])
    rows: list[dict[str, object]] = []
    for event_date, decision in decisions.iterrows():
        position = positions.get(event_date)
        if position is None or position + 2 >= len(sessions):
            continue
        entry_date = sessions[position + 1]
        exit_date = sessions[position + 2]
        entry_open = float(raw.loc[entry_date, "open"])
        entry_close = float(raw.loc[entry_date, "close"])
        exit_open = float(raw.loc[exit_date, "open"])
        signal_close = float(raw.loc[event_date, "close"])
        volatility_value = float(volatility.loc[event_date])
        volatility_threshold_value = float(volatility_threshold.loc[event_date])
        volatility_state = (
            "UNKNOWN"
            if pd.isna(volatility_threshold_value)
            else "HIGH"
            if volatility_value >= volatility_threshold_value
            else "LOW"
        )
        rows.append(
            {
                "symbol": symbol,
                "event_date": event_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "vote_count": int(decision["factor_score"]),
                "trend_ratio_60": float(trend_ratio.loc[event_date]),
                "trend_state": "UP" if trend_ratio.loc[event_date] >= 0 else "DOWN",
                "realized_volatility_20": volatility_value,
                "volatility_threshold": volatility_threshold_value,
                "volatility_state": volatility_state,
                "signal_to_entry_gap": entry_open / signal_close - 1.0,
                "entry_intraday_return": entry_close / entry_open - 1.0,
                "entry_close_to_exit_gap": exit_open / entry_close - 1.0,
                "gross_return": exit_open / entry_open - 1.0,
                "stress_return": (exit_open * (1.0 - fee))
                / (entry_open * (1.0 + fee))
                - 1.0,
                "era": "SINCE_2024" if event_date >= pd.Timestamp("2024-01-01") else "BEFORE_2024",
            }
        )
    panel = pd.DataFrame(rows)
    required = (
        "event_date",
        "entry_date",
        "exit_date",
        "vote_count",
        "trend_ratio_60",
        "realized_volatility_20",
        "signal_to_entry_gap",
        "entry_intraday_return",
        "entry_close_to_exit_gap",
        "gross_return",
        "stress_return",
    )
    if panel.empty or panel[list(required)].isna().any().any():
        raise ValueError(f"{symbol}: event explanation panel is empty or incomplete")
    return panel


def _bucket_summary(
    panel: pd.DataFrame,
    dimension: str,
    order: tuple[str, str],
    minimum: int,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows: list[dict[str, object]] = []
    directions: dict[str, object] = {}
    for symbol, group in panel.groupby("symbol", sort=True, observed=True):
        values: dict[str, float] = {}
        counts: dict[str, int] = {}
        for bucket in order:
            selected = group.loc[group[dimension].eq(bucket), "stress_return"]
            values[bucket] = float(selected.mean())
            counts[bucket] = int(len(selected))
            rows.append(
                {
                    "symbol": symbol,
                    "dimension": dimension,
                    "bucket": bucket,
                    "events": len(selected),
                    "mean_stress_return": values[bucket],
                }
            )
        directions[symbol] = {
            "difference": values[order[0]] - values[order[1]],
            "minimum_bucket_met": min(counts.values()) >= minimum,
            "direction_holds": (
                min(counts.values()) >= minimum and values[order[0]] > values[order[1]]
            ),
        }
    return rows, directions


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(bool(protocol.get(key)) for key in forbidden):
        raise ValueError("explanation audit cannot select, promote or deploy")
    for source_id in ("20260912_S004_EX18", "20260912_S004_EX19"):
        validate_experiment_archive(repo / "experiments" / "S004" / source_id)
    source = protocol["source_candidate"]
    payload = _read(repo / str(source["payload"]))
    if payload.get("candidate_hash") != source["candidate_hash"]:
        raise ValueError("source candidate identity differs from frozen protocol")

    context = RepositoryContext.discover(repo)
    panels = [
        _event_panel(context, payload, protocol, symbol)
        for symbol in protocol["dataset"]["symbols"]
    ]
    panel = pd.concat(panels, ignore_index=True)
    minimum = int(protocol["fixed_definitions"]["minimum_events_per_bucket"])
    summaries: list[dict[str, object]] = []
    hypothesis_details: dict[str, object] = {}
    definitions = (
        ("trend", "trend_state", ("UP", "DOWN")),
        ("volatility", "volatility_state", ("HIGH", "LOW")),
        ("signal_strength", "vote_count_label", ("THREE", "TWO")),
    )
    panel["vote_count_label"] = panel["vote_count"].map({2: "TWO", 3: "THREE"})
    for hypothesis, dimension, order in definitions:
        rows, details = _bucket_summary(panel, dimension, order, minimum)
        summaries.extend(rows)
        hypothesis_details[hypothesis] = {
            "symbols": details,
            "consistent": all(item["direction_holds"] for item in details.values()),
        }

    component_rows: list[dict[str, object]] = []
    repair_by_symbol: dict[str, object] = {}
    for symbol, group in panel.groupby("symbol", sort=True, observed=True):
        components = {
            "signal_to_entry_gap": float(group["signal_to_entry_gap"].mean()),
            "entry_intraday_return": float(group["entry_intraday_return"].mean()),
            "entry_close_to_exit_gap": float(group["entry_close_to_exit_gap"].mean()),
            "gross_return": float(group["gross_return"].mean()),
        }
        repair_by_symbol[symbol] = {
            **components,
            "direction_holds": components["entry_intraday_return"] > 0.0,
        }
        component_rows.extend(
            {"symbol": symbol, "component": name, "mean_return": value}
            for name, value in components.items()
        )
    repair_consistent = all(item["direction_holds"] for item in repair_by_symbol.values())
    consistent_states = sum(
        bool(hypothesis_details[name]["consistent"])
        for name in ("trend", "volatility", "signal_strength")
    )
    acceptance = protocol["acceptance"]
    checks = {
        "state_hypotheses": consistent_states
        >= int(acceptance["minimum_consistent_state_hypotheses"]),
        "repair_timing": repair_consistent
        if bool(acceptance["repair_timing_must_hold"])
        else True,
    }
    if all(checks.values()):
        evidence_label = "FAVORABLE"
        route = "MECHANISM_EXPLANATION_SUPPORTED"
    elif consistent_states >= 1 or repair_consistent:
        evidence_label = "MIXED"
        route = "MECHANISM_EXPLANATION_PARTIAL"
    else:
        evidence_label = "WEAK"
        route = "MECHANISM_EXPLANATION_NOT_SUPPORTED"

    era = (
        panel.groupby(["symbol", "era"], observed=True)["stress_return"]
        .agg(["size", "mean"])
        .reset_index()
    )
    panel.to_csv(
        artifacts / "event_explanation_panel.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
    )
    pd.DataFrame(summaries).to_csv(
        artifacts / "state_summary.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    pd.DataFrame(component_rows).to_csv(
        artifacts / "return_components.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    era.to_csv(
        artifacts / "descriptive_era_summary.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "evidence_label": evidence_label,
        "events_by_symbol": {
            symbol: int(len(group))
            for symbol, group in panel.groupby("symbol", observed=True)
        },
        "consistent_state_hypotheses": consistent_states,
        "hypothesis_details": hypothesis_details,
        "repair_timing": {
            "symbols": repair_by_symbol,
            "consistent": repair_consistent,
        },
        "checks": checks,
        "descriptive_era_split_excluded_from_acceptance": True,
        "source_candidate_unchanged": True,
        "route_decision": route,
    }
    _write(artifacts / "mechanism_explanation_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# 20260912_S004_EX20 执行\n\n"
        f"状态：COMPLETE。重建两标的共{len(panel)}笔固定规则事件，完成三类状态切片和收益时段分解。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# 20260912_S004_EX20 结论\n\n"
        f"机制解释标签：`{evidence_label}`；结论：`{route}`。\n\n"
        f"趋势、波动和信号强度三项预注册解释中，有{consistent_states}项在两个标的上方向一致；"
        f"次日盘中修复在两个标的上{'均为正' if repair_consistent else '未同时为正'}。\n\n"
        "2024年前后分段只作为已知结果的描述，不进入裁决；本轮没有增加过滤器或修改候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S004",
            "symbol": "588080.SH+510500.SH",
            "candidate_id": source["candidate_id"],
            "development_cutoff": protocol["dataset"]["cutoff"],
            "evidence_label": evidence_label,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
