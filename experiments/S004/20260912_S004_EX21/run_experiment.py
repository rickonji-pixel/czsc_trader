from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data
from czsc_trader.backtesting.closing_dislocation_replay import _cooldown, _daily_features
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260912_S004_EX21"


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


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0.0].sum())
    losses = float(-values.loc[values < 0.0].sum())
    return gains / losses if losses > 0 else float("inf") if gains > 0 else 0.0


def _gate_mask(gate: str, trend_up: pd.Series, vol_high: pd.Series) -> pd.Series:
    if gate == "TREND_UP":
        return trend_up
    if gate == "VOL_HIGH":
        return vol_high
    if gate == "TREND_UP_OR_VOL_HIGH":
        return trend_up | vol_high
    if gate == "TREND_UP_AND_VOL_HIGH":
        return trend_up & vol_high
    raise ValueError(f"unknown state gate: {gate}")


def _evaluate_symbol(
    context: RepositoryContext,
    protocol: dict[str, object],
    payload: dict[str, object],
    symbol: str,
) -> list[dict[str, object]]:
    dataset = protocol["dataset"]
    data = load_replay_data(
        context,
        str(dataset["name"]),
        symbol,
        str(dataset["asset_type"]),
        pd.Timestamp(dataset["cutoff"]).date(),
        include_one_minute=True,
    )
    sessions = pd.DatetimeIndex(pd.to_datetime(data.adjusted.daily["dt"])).normalize()
    feature_spec = payload["rule"]["feature"]
    features = _daily_features(data.signal_one_minute, int(feature_spec["late_window_minutes"]))
    votes = pd.DataFrame(index=features.index)
    for mechanism in feature_spec["mechanisms"]:
        score = features[str(mechanism)]
        threshold = score.shift(int(feature_spec["threshold_lag_sessions"])).rolling(
            int(feature_spec["threshold_lookback_sessions"]),
            min_periods=int(feature_spec["threshold_lookback_sessions"]),
        ).quantile(float(feature_spec["threshold_quantile"]))
        votes[str(mechanism)] = threshold.notna() & score.gt(0.0) & score.ge(threshold)
    base_event = votes.sum(axis=1).ge(int(feature_spec["votes_required"]))

    adjusted = data.adjusted.daily.copy()
    adjusted["dt"] = pd.to_datetime(adjusted["dt"]).dt.normalize()
    adjusted = adjusted.set_index("dt").sort_index()
    close = adjusted["close"].astype(float)
    trend_up = close.ge(close.rolling(60, min_periods=60).mean())
    volatility = close.pct_change(fill_method=None).rolling(20, min_periods=20).std() * np.sqrt(252.0)
    vol_threshold = volatility.shift(1).rolling(120, min_periods=120).median()
    vol_high = vol_threshold.notna() & volatility.ge(vol_threshold)

    raw = data.execution_daily.copy()
    raw["dt"] = pd.to_datetime(raw["dt"]).dt.normalize()
    raw = raw.set_index("dt").sort_index()
    fee = float(protocol["stress_one_way_cost"])
    outcome_rows: list[dict[str, object]] = []
    for position, event_date in enumerate(sessions[:-2]):
        entry_date = sessions[position + 1]
        exit_date = sessions[position + 2]
        entry = float(raw.loc[entry_date, "open"])
        exit_ = float(raw.loc[exit_date, "open"])
        outcome_rows.append(
            {
                "event_date": event_date,
                "entry_date": entry_date,
                "entry_year": entry_date.year,
                "stress_return": (exit_ * (1.0 - fee))
                / (entry * (1.0 + fee))
                - 1.0,
            }
        )
    outcomes = pd.DataFrame(outcome_rows).set_index("event_date")
    evaluation_start = pd.Timestamp(dataset["evaluation_start"])
    evaluation_end = pd.Timestamp(dataset["evaluation_end"])
    evaluation_sessions = sessions[
        (sessions >= evaluation_start) & (sessions <= evaluation_end)
    ]
    rows: list[dict[str, object]] = []
    for gate in protocol["gates"]:
        raw_dates = pd.DatetimeIndex(base_event.index[base_event & _gate_mask(str(gate), trend_up, vol_high)])
        events = _cooldown(raw_dates, sessions, int(feature_spec["cooldown_sessions"]))
        selected = pd.DatetimeIndex(sorted(events.intersection(outcomes.index)))
        selected = selected[(selected >= evaluation_start) & (selected <= evaluation_end)]
        trades = outcomes.loc[selected]
        flags = pd.Series(0, index=evaluation_sessions, dtype=int)
        flags.loc[flags.index.intersection(events)] = 1
        rolling = flags.rolling(60, min_periods=60).sum().dropna()
        recent = trades.loc[trades.index >= evaluation_sessions[-252], "stress_return"]
        annual = trades.groupby("entry_year", observed=True)["stress_return"].mean()
        rows.append(
            {
                "gate": gate,
                "symbol": symbol,
                "closed_trades": int(len(trades)),
                "rolling_60_median": float(rolling.median()),
                "rolling_60_p10": float(rolling.quantile(0.10, interpolation="lower")),
                "stress_mean_return": float(trades["stress_return"].mean()),
                "stress_profit_factor": _profit_factor(trades["stress_return"]),
                "positive_years": int(annual.gt(0.0).sum()),
                "recent_252_trades": int(len(recent)),
                "recent_252_mean_return": float(recent.mean()),
            }
        )
    return rows


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if not protocol.get("parameter_selection") or protocol.get("candidate_generation"):
        raise ValueError("state-gate screen selection contract differs from protocol")
    if any(bool(protocol.get(key)) for key in ("promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("state-gate screen cannot promote or deploy")
    validate_experiment_archive(repo / "experiments/S004/20260912_S004_EX20")
    source = protocol["source_candidate"]
    payload = _read(repo / str(source["payload"]))
    if payload.get("candidate_hash") != source["candidate_hash"]:
        raise ValueError("source candidate identity differs from frozen protocol")
    accounting = protocol["search_accounting"]
    if int(accounting["cumulative_registered_trials"]) != (
        int(accounting["previous_registered_trials"])
        + int(accounting["state_hypothesis_paths_used_for_generation"])
        + int(accounting["new_gate_trials"])
    ):
        raise ValueError("search accounting does not reconcile")
    if len(protocol["gates"]) != int(accounting["new_gate_trials"]):
        raise ValueError("registered gate count differs from search accounting")

    context = RepositoryContext.discover(repo)
    dataset = protocol["dataset"]
    rows = []
    for symbol in (dataset["primary_symbol"], dataset["external_symbol"]):
        rows.extend(_evaluate_symbol(context, protocol, payload, str(symbol)))
    metrics = pd.DataFrame(rows)
    acceptance = protocol["acceptance"]
    primary = str(dataset["primary_symbol"])
    external = str(dataset["external_symbol"])
    gate_rows: list[dict[str, object]] = []
    for gate, group in metrics.groupby("gate", sort=True, observed=True):
        by_symbol = group.set_index("symbol")
        if set(by_symbol.index) != {primary, external}:
            raise AssertionError(f"{gate}: incomplete symbol results")
        target = by_symbol.loc[primary]
        cross = by_symbol.loc[external]
        checks = {
            "primary_sample": target["closed_trades"]
            >= int(acceptance["primary_minimum_closed_trades"]),
            "external_sample": cross["closed_trades"]
            >= int(acceptance["external_minimum_closed_trades"]),
            "primary_density_median": int(acceptance["primary_rolling_60_median_min"])
            <= target["rolling_60_median"]
            <= int(acceptance["primary_rolling_60_median_max"]),
            "primary_density_p10": target["rolling_60_p10"]
            >= int(acceptance["primary_rolling_60_p10_min"]),
            "both_mean": by_symbol["stress_mean_return"].min()
            > float(acceptance["both_symbols_mean_return_min_exclusive"]),
            "both_profit_factor": by_symbol["stress_profit_factor"].min()
            > float(acceptance["both_symbols_profit_factor_min_exclusive"]),
            "both_positive_years": by_symbol["positive_years"].min()
            >= int(acceptance["both_symbols_positive_years_min"]),
            "both_recent": by_symbol["recent_252_mean_return"].min()
            > float(acceptance["both_symbols_recent_252_mean_min_exclusive"]),
        }
        gate_rows.append(
            {
                "gate": gate,
                "eligible": all(checks.values()),
                "worst_symbol_mean_return": float(by_symbol["stress_mean_return"].min()),
                "worst_symbol_profit_factor": float(by_symbol["stress_profit_factor"].min()),
                "primary_closed_trades": int(target["closed_trades"]),
                **{f"check_{name}": bool(value) for name, value in checks.items()},
            }
        )
    ranking = pd.DataFrame(gate_rows).sort_values(
        ["eligible", "worst_symbol_mean_return", "primary_closed_trades", "gate"],
        ascending=[False, False, False, True],
    )
    eligible = ranking.loc[ranking["eligible"]]
    selected_gate = None if eligible.empty else str(eligible.iloc[0]["gate"])
    route = (
        str(protocol["selection"]["no_eligible_gate"])
        if selected_gate is None
        else "NOMINATE_STATE_GATED_CHALLENGER"
    )
    metrics.to_csv(
        artifacts / "gate_symbol_metrics.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    ranking.to_csv(
        artifacts / "gate_ranking.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "tested_gates": int(len(ranking)),
        "eligible_gates": int(len(eligible)),
        "selected_gate": selected_gate,
        "route_decision": route,
        "cumulative_registered_trials": int(accounting["cumulative_registered_trials"]),
        "candidate_created": False,
        "source_candidate_unchanged": True,
    }
    _write(artifacts / "state_gate_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# 20260912_S004_EX21 执行\n\n状态：COMPLETE。四个预注册状态门控完成双标的收益、密度和年度评价。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# 20260912_S004_EX21 结论\n\n"
        f"合格门控：{len(eligible)}/4；结论：`{route}`。\n\n"
        + (
            f"预注册排序胜出门控为`{selected_gate}`，只获得后续正式构造资格，尚未创建S004-C002。\n"
            if selected_gate is not None
            else "没有门控同时满足中频密度和双标的质量要求；按协议停止状态门控路线，不继续搜索阈值。\n"
        ),
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S004",
            "symbol": f"{primary}+{external}",
            "candidate_id": source["candidate_id"],
            "development_cutoff": dataset["cutoff"],
            "decision": route,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
