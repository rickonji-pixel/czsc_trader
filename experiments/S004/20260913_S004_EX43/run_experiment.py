from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

import numpy as np
import pandas as pd
from strategy_evaluator import (
    ReturnMatrixEvidence,
    annualized_sharpe,
    calculate_dsr_bundle,
    cscv_pbo,
    effective_trial_count,
    hash_return_matrix,
    stationary_bootstrap_performance,
)

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data
from czsc_trader.backtesting.closing_dislocation_replay import _cooldown, _daily_features
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX43"
SELECTED_ID = "EX40:S004-C002"


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


def _daily_path(
    frame: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    *,
    date_column: str,
    return_column: str = "stress_return",
) -> pd.Series:
    selected = frame[[date_column, return_column]].copy()
    selected[date_column] = pd.to_datetime(selected[date_column], errors="raise").dt.normalize()
    selected = selected.loc[
        selected[date_column].between(calendar[0], calendar[-1], inclusive="both")
    ]
    grouped = selected.groupby(date_column, observed=True)[return_column].apply(
        lambda values: float(np.prod(1.0 + values.astype(float).to_numpy()) - 1.0)
    )
    missing = grouped.index.difference(calendar)
    if not missing.empty:
        raise ValueError(f"trial return dates fall outside audit calendar: {missing[:3].tolist()}")
    result = pd.Series(0.0, index=calendar, dtype=float)
    result.loc[grouped.index] = grouped.to_numpy()
    return result


def _label(value: float, favorable: float, adverse: float, *, lower_better: bool) -> str:
    if lower_better:
        if value <= favorable:
            return "FAVORABLE"
        if value > adverse:
            return "ADVERSE"
    else:
        if value >= favorable:
            return "FAVORABLE"
        if value < adverse:
            return "ADVERSE"
    return "MIXED"


def _next_session(values: pd.Series, calendar: pd.DatetimeIndex) -> pd.Series:
    mapping = pd.Series(calendar[1:], index=calendar[:-1])
    normalized = pd.to_datetime(values, errors="raise").dt.normalize()
    result = normalized.map(mapping)
    if result.isna().any():
        raise ValueError("cannot map one or more entry dates to an exit session")
    return result


def _state_gate_paths(
    data,
    payload: dict[str, object],
    calendar: pd.DatetimeIndex,
    protocol: dict[str, object],
) -> dict[str, pd.Series]:
    feature = payload["rule"]["feature"]
    sessions = pd.DatetimeIndex(pd.to_datetime(data.adjusted.daily["dt"])).normalize()
    features = _daily_features(data.signal_one_minute, int(feature["late_window_minutes"]))
    votes = pd.DataFrame(index=features.index)
    for mechanism in feature["mechanisms"]:
        score = features[str(mechanism)]
        threshold = score.shift(int(feature["threshold_lag_sessions"])).rolling(
            int(feature["threshold_lookback_sessions"]),
            min_periods=int(feature["threshold_lookback_sessions"]),
        ).quantile(float(feature["threshold_quantile"]))
        votes[str(mechanism)] = threshold.notna() & score.gt(0.0) & score.ge(threshold)
    base_event = votes.sum(axis=1).ge(int(feature["votes_required"]))

    adjusted = data.adjusted.daily.copy()
    adjusted["dt"] = pd.to_datetime(adjusted["dt"]).dt.normalize()
    close = adjusted.set_index("dt")["close"].astype(float)
    trend_up = close.ge(close.rolling(60, min_periods=60).mean())
    volatility = close.pct_change(fill_method=None).rolling(20, min_periods=20).std() * np.sqrt(252.0)
    vol_threshold = volatility.shift(1).rolling(120, min_periods=120).median()
    vol_high = vol_threshold.notna() & volatility.ge(vol_threshold)

    raw = data.execution_daily.copy()
    raw["dt"] = pd.to_datetime(raw["dt"]).dt.normalize()
    raw = raw.set_index("dt").sort_index()
    rows: list[dict[str, object]] = []
    fee = 0.0006
    for position, event_date in enumerate(sessions[:-2]):
        entry_date = sessions[position + 1]
        exit_date = sessions[position + 2]
        entry = float(raw.loc[entry_date, "open"])
        exit_ = float(raw.loc[exit_date, "open"])
        rows.append(
            {
                "event_date": event_date,
                "exit_date": exit_date,
                "stress_return": (exit_ * (1.0 - fee)) / (entry * (1.0 + fee)) - 1.0,
            }
        )
    outcomes = pd.DataFrame(rows).set_index("event_date")
    gates = {
        "TREND_UP": trend_up,
        "VOL_HIGH": vol_high,
        "TREND_UP_OR_VOL_HIGH": trend_up | vol_high,
        "TREND_UP_AND_VOL_HIGH": trend_up & vol_high,
    }
    expected = pd.read_csv(
        Path(protocol["state_gate_metrics"]),
    )
    expected = expected.loc[expected["symbol"].eq("588080.SH")].set_index("gate")
    paths: dict[str, pd.Series] = {}
    start, end = calendar[0], calendar[-1]
    for gate, mask in gates.items():
        raw_dates = pd.DatetimeIndex(base_event.index[base_event & mask])
        events = _cooldown(raw_dates, sessions, int(feature["cooldown_sessions"]))
        selected = pd.DatetimeIndex(sorted(events.intersection(outcomes.index)))
        selected = selected[(selected >= start) & (selected <= end)]
        trades = outcomes.loc[selected].reset_index()
        if len(trades) != int(expected.loc[gate, "closed_trades"]):
            raise AssertionError(f"{gate}: reconstructed trade count differs")
        paths[f"EX21:{gate}"] = _daily_path(trades, calendar, date_column="exit_date")
    return paths


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "automatic_acceptance",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(bool(protocol.get(key)) for key in forbidden):
        raise ValueError("statistical audit cannot select, promote or deploy")

    source_ids = (
        "20260912_S004_EX15",
        "20260912_S004_EX20",
        "20260912_S004_EX21",
        "20260913_S004_EX22",
        "20260913_S004_EX23",
        "20260913_S004_EX27",
        "20260913_S004_EX31",
        "20260913_S004_EX35",
        "20260913_S004_EX38",
        "20260913_S004_EX39",
        "20260913_S004_EX40",
        "20260913_S004_EX41",
        "20260913_S004_EX42",
    )
    sources = {item: repo / "experiments" / "S004" / item for item in source_ids}
    for source in sources.values():
        validate_experiment_archive(source)

    prior = pd.read_csv(
        sources["20260912_S004_EX15"] / "artifacts/daily_return_matrix.csv.gz",
        parse_dates=["date"],
    ).set_index("date")
    prior.index = prior.index.normalize()
    if prior.shape[1] != int(protocol["trial_universe"]["prior_matrix"]):
        raise AssertionError("prior statistical matrix does not contain 36 paths")
    calendar = pd.DatetimeIndex(prior.index)
    paths = {str(column): prior[column].astype(float) for column in prior.columns}
    ledger: list[dict[str, object]] = [
        {
            "trial_id": str(column),
            "source": "experiments/S004/20260912_S004_EX15/artifacts/daily_return_matrix.csv.gz",
            "episodes": int(prior[column].ne(0.0).sum()),
        }
        for column in prior.columns
    ]

    panel = pd.read_csv(
        sources["20260912_S004_EX20"] / "artifacts/event_explanation_panel.csv.gz",
        parse_dates=["exit_date"],
    )
    panel = panel.loc[panel["symbol"].eq("588080.SH")]
    hypotheses = {
        "EX20:HYP_TREND_UP": panel.loc[panel["trend_state"].eq("UP")],
        "EX20:HYP_VOL_HIGH": panel.loc[panel["volatility_state"].eq("HIGH")],
        "EX20:HYP_THREE_VOTES": panel.loc[panel["vote_count"].eq(3)],
    }
    for name, frame in hypotheses.items():
        paths[name] = _daily_path(frame, calendar, date_column="exit_date")
        ledger.append({"trial_id": name, "source": "EX20 event explanation panel", "episodes": len(frame)})

    context = RepositoryContext.discover(repo)
    dataset = protocol["dataset"]
    data = load_replay_data(
        context,
        str(dataset["name"]),
        "588080.SH",
        "etf",
        pd.Timestamp(str(dataset["development_cutoff"])).date(),
        include_one_minute=True,
    )
    c001_payload = _read(repo / "experiments/S004/20260912_S004_EX13/candidate_payload.json")
    gate_protocol = {
        "state_gate_metrics": str(
            sources["20260912_S004_EX21"] / "artifacts/gate_symbol_metrics.csv"
        )
    }
    for name, values in _state_gate_paths(data, c001_payload, calendar, gate_protocol).items():
        paths[name] = values
        ledger.append({"trial_id": name, "source": "EX21 reconstructed fixed gate", "episodes": int(values.ne(0).sum())})

    grouped_sources = (
        ("EX22", sources["20260913_S004_EX22"] / "artifacts/path_episodes.csv.gz", "path_id", "exit_date", "588080.SH"),
        ("EX23", sources["20260913_S004_EX23"] / "artifacts/path_episodes.csv.gz", "path_id", "exit_date", "588080.SH"),
    )
    for prefix, path, identity, date_column, symbol in grouped_sources:
        frame = pd.read_csv(path)
        frame = frame.loc[frame["symbol"].eq(symbol)]
        for trial_id, group in frame.groupby(identity, sort=True, observed=True):
            name = f"{prefix}:{trial_id}"
            paths[name] = _daily_path(group, calendar, date_column=date_column)
            ledger.append({"trial_id": name, "source": str(path.relative_to(repo)).replace("\\", "/"), "episodes": len(group)})

    for prefix, source_id in (("EX27", "20260913_S004_EX27"), ("EX31", "20260913_S004_EX31")):
        path = sources[source_id] / "artifacts/trades.csv.gz"
        frame = pd.read_csv(path)
        frame = frame.loc[frame["symbol"].eq("588080.SH")].copy()
        frame["exit_date"] = _next_session(frame["event_date"], calendar)
        name = f"{prefix}:{frame['path_id'].iloc[0]}"
        paths[name] = _daily_path(frame, calendar, date_column="exit_date")
        ledger.append({"trial_id": name, "source": str(path.relative_to(repo)).replace("\\", "/"), "episodes": len(frame)})

    path = sources["20260913_S004_EX35"] / "artifacts/trades.csv.gz"
    frame = pd.read_csv(path)
    for trial_id, group in frame.groupby("path_id", sort=True, observed=True):
        name = f"EX35:{trial_id}"
        paths[name] = _daily_path(group, calendar, date_column="a_share_date")
        ledger.append({"trial_id": name, "source": str(path.relative_to(repo)).replace("\\", "/"), "episodes": len(group)})

    singles = (
        ("EX38:ACCUM_Q60", sources["20260913_S004_EX38"] / "artifacts/trades.csv.gz", "event_date"),
        ("EX39:ACCUM_Q60_510500", sources["20260913_S004_EX39"] / "artifacts/replication_trades.csv.gz", "event_date"),
        (SELECTED_ID, sources["20260913_S004_EX40"] / "artifacts/overlay_episodes.csv.gz", "exit_date"),
    )
    for name, path, date_column in singles:
        frame = pd.read_csv(path)
        if "risk_denied" in frame.columns:
            frame = frame.loc[~frame["risk_denied"].astype(bool)]
        paths[name] = _daily_path(frame, calendar, date_column=date_column)
        ledger.append({"trial_id": name, "source": str(path.relative_to(repo)).replace("\\", "/"), "episodes": len(frame)})

    matrix = pd.DataFrame(paths, index=calendar)
    expected_count = int(protocol["trial_universe"]["raw_trial_count"])
    if matrix.shape != (len(calendar), expected_count) or len(ledger) != expected_count:
        raise AssertionError(f"trial matrix shape differs: {matrix.shape}; ledger={len(ledger)}")
    if matrix.std().le(0).any() or SELECTED_ID not in matrix:
        raise AssertionError("trial matrix is incomplete")

    evidence = ReturnMatrixEvidence(
        tuple(calendar.strftime("%Y-%m-%d")),
        tuple(matrix.columns),
        tuple(tuple(float(value) for value in row) for row in matrix.to_numpy()),
        "",
    )
    evidence = ReturnMatrixEvidence(
        evidence.dates,
        evidence.candidate_ids,
        evidence.returns,
        hash_return_matrix(evidence),
    )
    audit = protocol["audit"]
    pbo = cscv_pbo(evidence, int(audit["cscv_blocks"]))
    sharpes = np.asarray([annualized_sharpe(matrix[column].to_numpy()) for column in matrix])
    effective_count = effective_trial_count(matrix.to_numpy())
    dsr = calculate_dsr_bundle(
        matrix[SELECTED_ID].to_numpy(),
        sharpes,
        raw_count=expected_count,
        effective_count=effective_count,
    )
    bootstraps = [
        stationary_bootstrap_performance(
            matrix[SELECTED_ID].to_numpy(),
            candidate_id="S004-C002",
            repetitions=int(audit["bootstrap_repetitions"]),
            mean_block_length=int(block_length),
            seed=int(audit["seed"]) + int(block_length),
        )
        for block_length in audit["bootstrap_mean_block_lengths"]
    ]
    primary = next(item for item in bootstraps if item.mean_block_length == 21)
    labels = {
        "pbo": _label(float(pbo.pbo), float(audit["favorable_pbo_max"]), float(audit["adverse_pbo_min_exclusive"]), lower_better=True),
        "dsr": _label(float(dsr.effective.probability), float(audit["favorable_dsr_probability_min"]), float(audit["adverse_dsr_probability_below"]), lower_better=False),
        "absolute_bootstrap": "FAVORABLE" if primary.cagr.lower_90 > 0 else "MIXED" if primary.cagr.upper_90 > 0 else "ADVERSE",
    }
    overall = "ADVERSE" if "ADVERSE" in labels.values() else "FAVORABLE" if set(labels.values()) == {"FAVORABLE"} else "MIXED"

    matrix.reset_index(names="date").to_csv(
        artifacts / "daily_return_matrix.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
    )
    pd.DataFrame(ledger).to_csv(artifacts / "trial_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    pd.DataFrame({"trial_id": matrix.columns, "annualized_sharpe": sharpes}).to_csv(
        artifacts / "trial_sharpes.csv", index=False, encoding="utf-8-sig", lineterminator="\n"
    )
    _write(artifacts / "return_matrix_evidence.json", evidence.to_dict())
    _write(
        artifacts / "statistical_details.json",
        {
            "pbo": pbo.to_dict(),
            "dsr": {"raw": asdict(dsr.raw), "effective": asdict(dsr.effective)},
            "absolute_bootstrap": [item.to_dict() for item in bootstraps],
        },
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "evidence_label": overall,
        "direction_labels": labels,
        "raw_trial_count": expected_count,
        "effective_trial_count": effective_count,
        "pbo": pbo.pbo,
        "dsr_raw_probability": dsr.raw.probability,
        "dsr_effective_probability": dsr.effective.probability,
        "bootstrap_21d_cagr_lower_90": primary.cagr.lower_90,
        "bootstrap_21d_calmar_lower_90": primary.calmar.lower_90,
        "frequency_policy": "OBSERVATION_ONLY",
        "candidate_created": False,
    }
    _write(artifacts / "statistical_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# S004 EX43 执行\n\n状态：COMPLETE。SE 对 {expected_count} 条历史收益路径计算了 PBO、有效试验数、DSR 和三档区块 Bootstrap。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX43 结论\n\n"
        f"总标签：`{overall}`。PBO {pbo.pbo:.2%}；有效试验数 {effective_count:.2f}；"
        f"有效 DSR 概率 {dsr.effective.probability:.2%}；21 日区块 Bootstrap 年化收益 90% 下界 "
        f"{primary.cagr.lower_90:.2%}，卡玛 90% 下界 {primary.calmar.lower_90:.2f}。\n\n"
        "交易频率只作观察；本轮不改变候选、SM 或 PTE 状态。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S004",
            "symbol": "588080.SH",
            "candidate_id": "S004-C002",
            "development_cutoff": dataset["development_cutoff"],
            "evidence_label": overall,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
