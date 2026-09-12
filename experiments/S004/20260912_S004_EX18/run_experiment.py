from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pandas as pd
from strategy_evaluator import AuditStatus, RiskLabel, audit_replay

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import (
    build_closing_dislocation_signals,
    load_replay_data,
    resolve_candidate_snapshot,
)
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260912_S004_EX18"
INITIAL_CASH = 100_000.0


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


def _possible_outcomes(
    calendar: pd.DatetimeIndex,
    raw_open: pd.Series,
    fee_rate: float,
) -> pd.Series:
    rows: dict[pd.Timestamp, float] = {}
    for position, event_date in enumerate(calendar[:-2]):
        entry = float(raw_open.loc[calendar[position + 1]])
        exit_ = float(raw_open.loc[calendar[position + 2]])
        rows[event_date] = (exit_ * (1.0 - fee_rate)) / (entry * (1.0 + fee_rate)) - 1.0
    return pd.Series(rows, dtype=float)


def _random_percentile(
    event_dates: pd.DatetimeIndex,
    outcomes: pd.Series,
    actual: float,
    iterations: int,
    seed: int,
) -> tuple[float, float, float]:
    available = pd.DatetimeIndex(outcomes.index)
    rng = np.random.default_rng(seed)
    controls: list[float] = []
    for _ in range(iterations):
        shifted: list[pd.Timestamp] = []
        for year in sorted(set(event_dates.year)):
            actual_year = event_dates[event_dates.year == year]
            year_calendar = available[available.year == year]
            positions = {value: index for index, value in enumerate(year_calendar)}
            source = [value for value in actual_year if value in positions]
            if not source or len(year_calendar) < 2:
                continue
            offset = int(rng.integers(1, len(year_calendar)))
            shifted.extend(
                year_calendar[(positions[value] + offset) % len(year_calendar)]
                for value in source
            )
        sample = outcomes.reindex(pd.DatetimeIndex(shifted)).dropna()
        if not sample.empty:
            controls.append(float(sample.mean()))
    values = np.asarray(controls, dtype=float)
    if values.size != iterations:
        raise ValueError("random control did not produce every registered iteration")
    return (
        float((values <= actual).mean() * 100.0),
        float(np.median(values)),
        float(np.quantile(values, 0.90)),
    )


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
        raise ValueError("cross-section replication cannot select, promote or deploy")
    validate_experiment_archive(repo / "experiments/S004/20260912_S004_EX17")

    source = protocol["source_candidate"]
    payload_path = repo / str(source["payload"])
    source_payload = _read(payload_path)
    if source_payload.get("candidate_hash") != source["candidate_hash"]:
        raise ValueError("source candidate identity differs from frozen protocol")
    payload = deepcopy(source_payload)
    target = protocol["target"]
    symbol = str(target["symbol"])
    payload["symbol"] = symbol
    rule = payload["rule"]
    rule["symbol"] = symbol
    execution = rule["execution"]
    instrument = execution["instrument"]
    adaptations = protocol["allowed_symbol_adaptations"]
    instrument["symbol"] = symbol
    instrument["price_limit_ratio"] = float(adaptations["price_limit_ratio"])
    execution["entry"]["limit_parameter"] = float(adaptations["entry_limit_parameter"])
    execution["exit"]["limit_ratio"] = float(adaptations["exit_limit_ratio"])
    execution["capital"]["fee_rate"] = float(protocol["stress_one_way_cost"])

    context = RepositoryContext.discover(repo)
    dataset = protocol["dataset"]
    data = load_replay_data(
        context,
        str(dataset["name"]),
        symbol,
        str(target["asset_type"]),
        pd.Timestamp(dataset["cutoff"]).date(),
        include_one_minute=True,
    )
    snapshot = resolve_candidate_snapshot(
        context,
        "S004-C001-X510500",
        payload,
        canonical_json_sha256(payload),
        f"{EXPERIMENT_ID}:fixed-rule-replication",
    )
    signals = build_closing_dislocation_signals(
        snapshot,
        data,
        pd.Timestamp(dataset["evaluation_start"]),
        pd.Timestamp(dataset["evaluation_end"]),
    )
    result = replay_account(signals, data, INITIAL_CASH)
    metrics = calculate_metrics(result, INITIAL_CASH)
    replay_audit = audit_replay(
        build_replay_evidence(signals, data, result, INITIAL_CASH, metrics)
    )
    if replay_audit.status is not AuditStatus.PASS:
        raise AssertionError(f"SE replay audit failed: {replay_audit.reason_codes}")
    trades = result.trades.loc[result.trades["status"].eq("CLOSED")].copy()
    entry_orders = result.orders.loc[
        result.orders["side"].eq("BUY") & result.orders["status"].eq("FILLED")
    ].drop_duplicates("cycle_id")
    event_by_cycle = entry_orders.set_index("cycle_id")["signal_date"]
    trades.insert(0, "event_date", trades["cycle_id"].map(event_by_cycle))
    trades["event_date"] = pd.to_datetime(trades["event_date"]).dt.normalize()
    trades["entry_year"] = pd.to_datetime(trades["entry_date"]).dt.year

    evaluation_sessions = pd.DatetimeIndex(
        pd.to_datetime(result.account_daily["date"]).dt.normalize()
    )
    signal_events = pd.DatetimeIndex(
        pd.to_datetime(
            signals.decisions.loc[signals.decisions["target_position"].eq(1), "signal_date"]
        ).dt.normalize()
    )
    flags = pd.Series(0, index=evaluation_sessions, dtype=int)
    flags.loc[flags.index.intersection(signal_events)] = 1
    rolling = flags.rolling(60, min_periods=60).sum().dropna()
    annual = trades.groupby("entry_year", observed=True)["net_return"].agg(["size", "mean"])
    recent_start = evaluation_sessions[-252]
    recent = trades.loc[trades["event_date"] >= recent_start, "net_return"]
    raw_daily = data.execution_daily.copy()
    raw_daily["dt"] = pd.to_datetime(raw_daily["dt"]).dt.normalize()
    raw_open = raw_daily.set_index("dt")["open"].astype(float)
    complete_calendar = pd.DatetimeIndex(raw_open.index)
    complete_calendar = complete_calendar[
        (complete_calendar >= pd.Timestamp(dataset["evaluation_start"]) - pd.Timedelta(days=7))
        & (complete_calendar <= pd.Timestamp(dataset["evaluation_end"]))
    ]
    outcomes = _possible_outcomes(
        complete_calendar,
        raw_open,
        float(protocol["stress_one_way_cost"]),
    )
    percentile, random_median, random_p90 = _random_percentile(
        pd.DatetimeIndex(trades["event_date"]),
        outcomes,
        float(trades["net_return"].mean()),
        int(protocol["random_control"]["iterations"]),
        int(protocol["random_control"]["seed"]),
    )
    acceptance = protocol["acceptance"]
    rolling_median = float(rolling.median())
    rolling_p10 = float(rolling.quantile(0.10, interpolation="lower"))
    mean_return = float(trades["net_return"].mean())
    profit_factor = _profit_factor(trades["net_return"])
    positive_years = int(annual["mean"].gt(0.0).sum())
    recent_mean = float(recent.mean())
    checks = {
        "sample": len(trades) >= int(acceptance["minimum_closed_trades"]),
        "density_median": int(acceptance["rolling_60_median_min"]) <= rolling_median <= int(acceptance["rolling_60_median_max"]),
        "density_p10": rolling_p10 >= int(acceptance["rolling_60_p10_min"]),
        "mean_return": mean_return > float(acceptance["mean_return_min_exclusive"]),
        "profit_factor": profit_factor > float(acceptance["profit_factor_min_exclusive"]),
        "positive_years": positive_years >= int(acceptance["positive_years_min"]),
        "recent_mean": recent_mean > float(acceptance["recent_252_mean_min_exclusive"]),
        "random_control": percentile >= float(acceptance["random_percentile_min"]),
    }
    passed = all(checks.values())
    evidence_label = RiskLabel.FAVORABLE if passed else RiskLabel.MIXED
    trades.to_csv(
        artifacts / "replication_trades.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
    )
    annual.reset_index().to_csv(
        artifacts / "annual_metrics.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "evidence_label": evidence_label.value,
        "target_symbol": symbol,
        "closed_trades": int(len(trades)),
        "rolling_60_median": rolling_median,
        "rolling_60_p10": rolling_p10,
        "stress_mean_return": mean_return,
        "stress_profit_factor": profit_factor,
        "positive_years": positive_years,
        "recent_252_mean_return": recent_mean,
        "random_percentile": percentile,
        "random_median_return": random_median,
        "random_p90_return": random_p90,
        "formal_metrics": metrics,
        "se_replay_audit": replay_audit.status.value,
        "checks": checks,
        "source_candidate_unchanged": True,
        "route_decision": (
            "CROSS_SECTION_REPLICATED" if passed else "PARTIAL_CROSS_SECTION_REPLICATION"
        ),
    }
    _write(artifacts / "replication_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# 20260912_S004_EX18 执行\n\n状态：COMPLETE。固定规则在510500形成{len(trades)}笔闭合交易，SE正式账本审计通过。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# 20260912_S004_EX18 结论\n\n"
        f"横截面标签：`{evidence_label.value}`；结论：`{summary['route_decision']}`。\n\n"
        f"闭合交易{len(trades)}笔，单边6bp后平均收益{mean_return:.3%}、盈亏比{profit_factor:.2f}；"
        f"滚动60日密度中位数/P10为{rolling_median:.0f}/{rolling_p10:.0f}；"
        f"{positive_years}/{len(annual)}个年度为正，最近252日均值{recent_mean:.3%}，随机对照分位{percentile:.1f}%。\n\n"
        "本轮没有修改S004-C001参数，也没有创建新候选、版本或PTE账户。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S004",
            "symbol": symbol,
            "source_candidate_id": source["candidate_id"],
            "development_cutoff": dataset["cutoff"],
            "evidence_label": evidence_label.value,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
