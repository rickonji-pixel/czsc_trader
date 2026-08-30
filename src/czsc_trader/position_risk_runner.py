"""Formal five-round position-risk research runner for 588080.SH."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import json
import platform
from typing import Any

import numpy as np
import pandas as pd

from .position_risk import RiskOverlaySpec
from .position_risk import (
    build_family_specs,
    build_position_risk_events,
    build_pressure_state,
    build_risk_features,
    compose_overlay_target,
)
from .audit import audit_no_lookahead
from .backtest import PeriodBacktestResult, run_period_backtests
from .baselines import ResolvedBaseline, resolve_baseline
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .four_layer_runner import _target_digest
from .objectives import TARGET_PERIODS
from .position_sizing_runner import EXPECTED_CHAMPION, _apply_champion, _identity_audit


COMMON_EXPECTED: dict[str, object] = {
    "schema_version": 1,
    "handler": "position_risk_five_rounds",
    "experiment_type": "position_risk_five_rounds",
    "program_id": "0830_position_risk_five_rounds",
    "status": "PRE_REGISTERED",
    "symbol": "588080.SH",
    "asset_type": "etf",
    "champion": EXPECTED_CHAMPION,
    "warmup_start": "2020-01-01",
    "discovery_start": "2021-01-01",
    "discovery_end": "2023-12-31",
    "validation_start": "2024-01-01",
    "validation_end": "2025-12-31",
    "historical_check_end": "2026-08-28",
    "fee_rate": 0.0005,
    "init_cash": 1_000_000.0,
    "observed_data_disclosure": (
        "all dates are previously observed project history; segment isolation "
        "only prevents feedback within this program"
    ),
}

ELIGIBILITY = (
    "full_return_greater_than_or_equal_to_champion",
    "full_max_drawdown_strictly_greater_than_champion",
)
RANKING = (
    "max_drawdown_improvement_desc",
    "worst_annual_return_delta_desc",
    "full_return_delta_desc",
    "transition_count_asc",
    "candidate_id_asc",
)

ROUND_EXPECTED: dict[str, dict[str, object]] = {
    "0830_EX03": {
        "experiment_id": "0830_EX03",
        "program_round": 1,
        "round_type": "holding_hazard_attribution",
        "representative_states": {
            "trend_damage": {"lookback": 60, "drawdown": 0.10, "sma": 20},
            "negative_persistence": {"window": 10, "negative_count": 7, "sma": 20},
            "intraday_pressure": {
                "close_location": 0.25,
                "down_volume_share": 0.70,
                "sma": 20,
            },
        },
        "forward_horizons": [5, 10],
        "diagnostic_only": True,
        "later_grid_mutation_allowed": False,
        "validation_accessed": False,
        "historical_check_accessed": False,
    },
    "0830_EX04": {
        "experiment_id": "0830_EX04",
        "program_round": 2,
        "round_type": "family_discovery",
        "family": "trend_damage",
        "lookbacks": [20, 60, 120],
        "drawdowns": [0.06, 0.10],
        "pressure_positions": [0.50, 0.75],
        "entry_sma": 20,
        "recovery_sma": 20,
        "candidate_count": 12,
        "selection_eligibility": list(ELIGIBILITY),
        "candidate_ranking": list(RANKING),
        "validation_accessed": False,
        "historical_check_accessed": False,
    },
    "0830_EX05": {
        "experiment_id": "0830_EX05",
        "program_round": 3,
        "round_type": "family_discovery",
        "family": "negative_persistence",
        "count_pairs": [[5, 4], [10, 7], [20, 13]],
        "pressure_positions": [0.50, 0.75],
        "entry_sma": 20,
        "recovery_sma": 5,
        "recovery_positive_days": 2,
        "candidate_count": 6,
        "selection_eligibility": list(ELIGIBILITY),
        "candidate_ranking": list(RANKING),
        "validation_accessed": False,
        "historical_check_accessed": False,
    },
    "0830_EX06": {
        "experiment_id": "0830_EX06",
        "program_round": 4,
        "round_type": "family_discovery",
        "family": "intraday_pressure",
        "close_locations": [0.25, 0.35],
        "down_volume_shares": [0.60, 0.70],
        "pressure_positions": [0.50, 0.75],
        "entry_sma": 20,
        "recovery_sma": 5,
        "recovery_non_pressure_days": 2,
        "candidate_count": 8,
        "selection_eligibility": list(ELIGIBILITY),
        "candidate_ranking": list(RANKING),
        "validation_accessed": False,
        "historical_check_accessed": False,
    },
    "0830_EX07": {
        "experiment_id": "0830_EX07",
        "program_round": 5,
        "round_type": "locked_validation_and_historical_check",
        "source_experiments": ["0830_EX04", "0830_EX05", "0830_EX06"],
        "source_policy": "only_validated_PASS_archives_with_frozen_challenger",
        "new_candidate_construction_allowed": False,
        "validation_eligibility": list(ELIGIBILITY),
        "candidate_ranking": list(RANKING),
        "no_source_policy": "FAIL_without_validation_or_2026_access",
        "no_validation_winner_policy": "FAIL_without_2026_access",
        "historical_pass_rule": (
            "2026FULL_return_greater_than_or_equal_to_champion_and_"
            "max_drawdown_strictly_greater_than_champion"
        ),
        "historical_diagnostic_windows": ["2026Q1", "2026H1", "2026M1-M8"],
        "validation_access_before_source_freeze": False,
        "historical_check_access_before_validation_freeze": False,
    },
}


def validate_position_risk_protocol(
    protocol: Mapping[str, object], experiment_id: str
) -> None:
    """Reject drift from any of the five committed protocols."""
    expected = ROUND_EXPECTED.get(experiment_id)
    if expected is None:
        raise ValueError("experiment_id differs from five-round program")
    for key, value in {**COMMON_EXPECTED, **expected}.items():
        if protocol.get(key) != value:
            raise ValueError(f"position-risk protocol differs for {key}")


def rank_eligible_position_risk(rows: pd.DataFrame) -> pd.DataFrame:
    """Apply the common hard gate before stable candidate ranking."""
    eligible = rows.loc[
        rows["full_challenger_return"].ge(rows["full_champion_return"])
        & rows["full_challenger_max_drawdown"].gt(
            rows["full_champion_max_drawdown"]
        )
    ].copy()
    return eligible.sort_values(
        [
            "max_drawdown_improvement",
            "worst_annual_return_delta",
            "full_return_delta",
            "transition_count",
            "candidate_id",
        ],
        ascending=[False, False, False, True, True],
        kind="stable",
    ).reset_index(drop=True)


def permitted_position_risk_cutoff(
    protocol: Mapping[str, object], stage: str
) -> pd.Timestamp:
    """Return the only data cutoff permitted for a program round and stage."""
    round_number = int(protocol["program_round"])
    if stage == "discovery" and round_number <= 4:
        return pd.Timestamp(str(protocol["discovery_end"]))
    if stage == "validation" and round_number == 5:
        return pd.Timestamp(str(protocol["validation_end"]))
    if stage == "historical" and round_number == 5:
        return pd.Timestamp(str(protocol["historical_check_end"]))
    raise ValueError(f"stage {stage!r} is not permitted for round {round_number}")


def _read_protocol(experiment_dir: Path) -> dict[str, object]:
    return json.loads(
        (experiment_dir / "artifacts" / "protocol.json").read_text(encoding="utf-8")
    )


def frozen_spec_payload(spec: RiskOverlaySpec) -> dict[str, object]:
    """Serialize one frozen family winner without implicit parameters."""
    return {
        "family": spec.family,
        "pressure_position": spec.pressure_position,
        "parameters": {name: value for name, value in spec.parameters},
        "candidate_id": spec.candidate_id,
    }


def risk_spec_from_frozen(payload: Mapping[str, object]) -> RiskOverlaySpec:
    """Restore and verify an exact frozen family specification."""
    raw_parameters = payload.get("parameters")
    if not isinstance(raw_parameters, Mapping):
        raise ValueError("frozen position-risk parameters must be an object")
    family = str(payload["family"])
    names = {
        "trend_damage": ("lookback", "drawdown"),
        "negative_persistence": ("window", "negative_count"),
        "intraday_pressure": ("close_location", "down_volume_share"),
    }.get(family)
    if names is None or set(raw_parameters) != set(names):
        raise ValueError("frozen position-risk family parameters differ")
    spec = RiskOverlaySpec(
        family,
        float(payload["pressure_position"]),
        tuple((name, float(raw_parameters[name])) for name in names),
    )
    if payload.get("candidate_id") != spec.candidate_id:
        raise ValueError("frozen position-risk candidate ID differs")
    if spec not in build_family_specs(family):
        raise ValueError("frozen position-risk candidate is outside preregistration")
    return spec


def _representative_masks(
    features: pd.DataFrame, protocol: Mapping[str, object]
) -> dict[str, pd.Series]:
    raw = protocol["representative_states"]
    if not isinstance(raw, Mapping):
        raise ValueError("representative states must be an object")
    trend = raw["trend_damage"]
    persistence = raw["negative_persistence"]
    intraday = raw["intraday_pressure"]
    assert isinstance(trend, Mapping)
    assert isinstance(persistence, Mapping)
    assert isinstance(intraday, Mapping)
    below = features["close"] < features[f"sma_{int(trend['sma'])}"]
    trend_mask = below & (
        features["close"] / features[f"prior_high_{int(trend['lookback'])}"] - 1.0
        <= -float(trend["drawdown"])
    )
    persistence_mask = (
        features["close"] < features[f"sma_{int(persistence['sma'])}"]
    ) & (
        features[f"negative_count_{int(persistence['window'])}"]
        >= float(persistence["negative_count"])
    )
    intraday_mask = (
        features["close"] < features[f"sma_{int(intraday['sma'])}"]
    ) & (
        features["close_location"] <= float(intraday["close_location"])
    ) & (
        features["down_volume_share"] >= float(intraday["down_volume_share"])
    )
    return {
        "trend_damage": trend_mask.fillna(False),
        "negative_persistence": persistence_mask.fillna(False),
        "intraday_pressure": intraday_mask.fillna(False),
    }


def build_hazard_diagnostics(
    features: pd.DataFrame,
    daily: pd.DataFrame,
    champion_target: pd.Series,
    protocol: Mapping[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Measure preregistered held-state hazards without constructing a strategy."""
    if not features.index.equals(champion_target.index):
        raise ValueError("hazard features and champion path must share an index")
    prices = daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"])
    prices = prices.set_index("dt").sort_index()
    if not prices.index.equals(features.index):
        raise ValueError("hazard prices and features must share an index")
    masks = _representative_masks(features, protocol)
    horizons = tuple(int(value) for value in protocol["forward_horizons"])  # type: ignore[arg-type]
    start = pd.Timestamp(str(protocol["discovery_start"]))
    end = pd.Timestamp(str(protocol["discovery_end"]))
    discovery = (features.index >= start) & (features.index <= end)
    held = champion_target.astype(float).eq(1.0)
    rows: list[dict[str, object]] = []
    for family, mask in masks.items():
        for location in np.flatnonzero((mask & held & discovery).to_numpy()):
            for horizon in horizons:
                if location + horizon >= len(prices.index):
                    continue
                entry_open = float(prices.iloc[location + 1]["open"])
                exit_open = float(prices.iloc[location + horizon]["open"])
                adverse_low = float(
                    prices.iloc[location + 1 : location + horizon + 1]["low"].min()
                )
                rows.append(
                    {
                        "family": family,
                        "signal_date": features.index[location],
                        "year": int(features.index[location].year),
                        "horizon": horizon,
                        "forward_return": exit_open / entry_open - 1.0,
                        "max_adverse_excursion": adverse_low / entry_open - 1.0,
                    }
                )
    events = pd.DataFrame(
        rows,
        columns=(
            "family",
            "signal_date",
            "year",
            "horizon",
            "forward_return",
            "max_adverse_excursion",
        ),
    )
    summaries: list[dict[str, object]] = []
    for family in masks:
        for horizon in horizons:
            subset = events.loc[
                events["family"].eq(family) & events["horizon"].eq(horizon)
            ]
            adverse_abs = subset["max_adverse_excursion"].abs()
            summaries.append(
                {
                    "family": family,
                    "horizon": horizon,
                    "event_count": int(len(subset)),
                    "mean_forward_return": float(subset["forward_return"].mean()),
                    "median_forward_return": float(subset["forward_return"].median()),
                    "loss_tail_rate": float(subset["forward_return"].lt(-0.05).mean()),
                    "mean_max_adverse_excursion": float(
                        subset["max_adverse_excursion"].mean()
                    ),
                    "worst_event_concentration": (
                        float(adverse_abs.max() / adverse_abs.sum())
                        if not subset.empty and float(adverse_abs.sum()) > 0.0
                        else np.nan
                    ),
                    "positive_year_count": int(
                        subset.groupby("year")["forward_return"].mean().gt(0.0).sum()
                    ),
                }
            )
    summary = pd.DataFrame(summaries)
    families = tuple(masks)
    overlaps: list[dict[str, object]] = []
    for left_index, left in enumerate(families):
        for right in families[left_index + 1 :]:
            overlaps.append(
                {
                    "left_family": left,
                    "right_family": right,
                    "overlap_count": int((masks[left] & masks[right] & held & discovery).sum()),
                }
            )
    return events, summary, pd.DataFrame(overlaps)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _periods(start_year: int, end_year: int, full_name: str) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    periods = {
        full_name: (pd.Timestamp(start_year, 1, 1), pd.Timestamp(end_year, 12, 31))
    }
    periods.update(
        {
            str(year): (pd.Timestamp(year, 1, 1), pd.Timestamp(year, 12, 31))
            for year in range(start_year, end_year + 1)
        }
    )
    return periods


def _resolve_champion(baseline_root: Path, protocol: Mapping[str, object]) -> ResolvedBaseline:
    baseline = resolve_baseline(baseline_root, symbol=str(protocol["symbol"]))
    if {
        "version": baseline.version,
        "sha256": baseline.sha256,
        "source_path": baseline.source_path,
        "source_sha256": baseline.source_sha256,
    } != EXPECTED_CHAMPION:
        raise AssertionError("active champion identity differs from five-round program")
    return baseline


def _metric_row(
    spec: RiskOverlaySpec,
    champion: Mapping[str, PeriodBacktestResult],
    challenger: Mapping[str, PeriodBacktestResult],
    *,
    full_name: str,
    annual_names: tuple[str, ...],
    transition_count: int,
) -> dict[str, object]:
    full_champion = champion[full_name].metrics
    full_challenger = challenger[full_name].metrics
    annual_deltas = [
        float(challenger[name].metrics["strategy_return"])
        - float(champion[name].metrics["strategy_return"])
        for name in annual_names
    ]
    row: dict[str, object] = {
        **frozen_spec_payload(spec),
        "full_champion_return": float(full_champion["strategy_return"]),
        "full_challenger_return": float(full_challenger["strategy_return"]),
        "full_return_delta": float(full_challenger["strategy_return"])
        - float(full_champion["strategy_return"]),
        "full_champion_max_drawdown": float(full_champion["max_drawdown"]),
        "full_challenger_max_drawdown": float(full_challenger["max_drawdown"]),
        "max_drawdown_improvement": float(full_challenger["max_drawdown"])
        - float(full_champion["max_drawdown"]),
        "worst_annual_return_delta": (
            min(annual_deltas) if annual_deltas else np.nan
        ),
        "median_annual_return_delta": (
            float(np.median(annual_deltas)) if annual_deltas else np.nan
        ),
        "full_champion_sharpe": float(full_champion["sharpe"]),
        "full_challenger_sharpe": float(full_challenger["sharpe"]),
        "full_champion_exposure": float(full_champion["exposure"]),
        "full_challenger_exposure": float(full_challenger["exposure"]),
        "full_trade_count": int(full_challenger["trade_count"]),
        "transition_count": transition_count,
    }
    row.pop("parameters")
    for name in annual_names:
        champion_metrics = champion[name].metrics
        challenger_metrics = challenger[name].metrics
        row[f"{name}_champion_return"] = float(champion_metrics["strategy_return"])
        row[f"{name}_challenger_return"] = float(challenger_metrics["strategy_return"])
        row[f"{name}_return_delta"] = float(challenger_metrics["strategy_return"]) - float(
            champion_metrics["strategy_return"]
        )
        row[f"{name}_challenger_max_drawdown"] = float(
            challenger_metrics["max_drawdown"]
        )
    return row


def _evaluate_specs(
    data: Any,
    applied: Any,
    specs: tuple[RiskOverlaySpec, ...],
    periods: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    *,
    full_name: str,
    annual_names: tuple[str, ...],
    fee_rate: float,
    init_cash: float,
) -> tuple[pd.DataFrame, dict[str, dict[str, object]], dict[str, PeriodBacktestResult]]:
    features = build_risk_features(data.daily, data.intraday)
    if not features.index.equals(applied.target_position.index):
        raise AssertionError("risk features differ from champion target dates")
    champion_results = run_period_backtests(
        data.daily,
        applied.target_position,
        periods,
        fee_rate=fee_rate,
        init_cash=init_cash,
    )
    rows: list[dict[str, object]] = []
    paths: dict[str, dict[str, object]] = {}
    factor_frame = features.assign(factor_score=applied.scores)
    for spec in specs:
        pressure = build_pressure_state(features, applied.target_position, spec)
        target = compose_overlay_target(
            applied.target_position, pressure, spec.pressure_position
        )
        events = build_position_risk_events(
            target,
            applied.target_position,
            applied.scores,
            features,
            pressure,
            spec,
        )
        challenger_results = run_period_backtests(
            data.daily,
            target,
            periods,
            fee_rate=fee_rate,
            init_cash=init_cash,
            factor_events=events,
            factor_frame=factor_frame,
        )
        full_result = challenger_results[full_name]
        audit = audit_no_lookahead(
            full_result.orders, events, target, factor_frame
        )
        transition_count = int(target.ne(target.shift(1, fill_value=0.0)).sum())
        row = _metric_row(
            spec,
            champion_results,
            challenger_results,
            full_name=full_name,
            annual_names=annual_names,
            transition_count=transition_count,
        )
        row["audit_status"] = audit["status"]
        rows.append(row)
        paths[spec.candidate_id] = {
            "spec": spec,
            "target": target,
            "pressure": pressure,
            "events": events,
            "factor_frame": factor_frame,
            "results": challenger_results,
            "audit": audit,
        }
    return pd.DataFrame(rows), paths, champion_results


def _freeze_family_winner(
    experiment_dir: Path,
    protocol: Mapping[str, object],
    spec: RiskOverlaySpec,
    target: pd.Series,
    selection: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    frozen = {
        "schema_version": 1,
        "experiment_id": experiment_dir.name,
        "program_id": protocol["program_id"],
        "program_round": protocol["program_round"],
        "champion": EXPECTED_CHAMPION,
        "discovery_end": protocol["discovery_end"],
        "spec": frozen_spec_payload(spec),
        "selection_target_sha256": _target_digest(target),
        "selection": dict(selection),
        "fee_rate": protocol["fee_rate"],
    }
    path = experiment_dir / "artifacts" / "frozen_challenger.json"
    _write_json(path, frozen)
    return frozen, sha256(path.read_bytes()).hexdigest()


def _manifest_metadata(
    experiment_dir: Path,
    protocol: Mapping[str, object],
    status: str,
    *,
    visible_sample_end: str,
    validation_accessed: bool,
    historical_check_accessed: bool,
    frozen_digest: str | None = None,
    failure_stage: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "experiment_id": experiment_dir.name,
        "date": "2026-08-30",
        "status": status,
        "program_id": protocol.get("program_id"),
        "program_round": protocol.get("program_round"),
        "symbol": protocol.get("symbol"),
        "asset_type": protocol.get("asset_type"),
        "champion": protocol.get("champion"),
        "visible_sample_end": visible_sample_end,
        "validation_accessed": validation_accessed,
        "historical_check_accessed": historical_check_accessed,
    }
    if frozen_digest is not None:
        metadata["frozen_challenger_sha256"] = frozen_digest
    if failure_stage is not None:
        metadata["failure_stage"] = failure_stage
    return metadata


def _finalize(
    experiment_dir: Path,
    protocol: Mapping[str, object],
    status: str,
    execution_lines: list[str],
    conclusion: str,
    *,
    visible_sample_end: str,
    validation_accessed: bool,
    historical_check_accessed: bool,
    frozen_digest: str | None = None,
    failure_stage: str | None = None,
) -> dict[str, object]:
    (experiment_dir / "03_execution.md").write_text(
        "# 执行过程\n\n" + "\n".join(f"- {line}" for line in execution_lines) + "\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "# 研究结论\n\n" + conclusion.rstrip() + "\n",
        encoding="utf-8",
    )
    metadata = _manifest_metadata(
        experiment_dir,
        protocol,
        status,
        visible_sample_end=visible_sample_end,
        validation_accessed=validation_accessed,
        historical_check_accessed=historical_check_accessed,
        frozen_digest=frozen_digest,
        failure_stage=failure_stage,
    )
    build_experiment_manifest(experiment_dir, metadata)
    validate_experiment_archive(experiment_dir)
    return {"status": status, "experiment_dir": str(experiment_dir)}


def _run_diagnostic(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    protocol: Mapping[str, object],
    execution_commit: str,
) -> dict[str, object]:
    cutoff = permitted_position_risk_cutoff(protocol, "discovery")
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=cutoff)
    baseline = _resolve_champion(baseline_root, protocol)
    _, applied = _apply_champion(data, baseline)
    artifacts = experiment_dir / "artifacts"
    _write_json(artifacts / "identity_audit.json", _identity_audit(baseline, applied, protocol))
    features = build_risk_features(data.daily, data.intraday)
    events, summary, overlap = build_hazard_diagnostics(
        features, data.daily, applied.target_position, protocol
    )
    events.to_csv(artifacts / "hazard_events.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(artifacts / "hazard_summary.csv", index=False, encoding="utf-8-sig")
    overlap.to_csv(artifacts / "state_overlap.csv", index=False, encoding="utf-8-sig")
    _write_json(
        artifacts / "diagnostic_metrics.json",
        {
            "status": "COMPLETE",
            "event_rows": int(len(events)),
            "state_families": list(summary["family"].drop_duplicates()),
            "visible_data_hashes": data.hashes,
            "validation_accessed": False,
            "historical_check_accessed": False,
        },
    )
    return _finalize(
        experiment_dir,
        protocol,
        "COMPLETE",
        [
            "状态：诊断完成（`COMPLETE`）",
            f"执行提交：`{execution_commit}`",
            f"诊断事件行数：{len(events)}",
            "访问2024—2025：否",
            "访问2026：否",
        ],
        "第1轮持仓风险归因已完成。结果只描述预注册状态与后续风险的历史关联，不产生挑战者，也不改变后三轮冻结网格。",
        visible_sample_end=str(cutoff.date()),
        validation_accessed=False,
        historical_check_accessed=False,
    )


def _run_family_discovery(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    protocol: Mapping[str, object],
    execution_commit: str,
) -> dict[str, object]:
    cutoff = permitted_position_risk_cutoff(protocol, "discovery")
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=cutoff)
    baseline = _resolve_champion(baseline_root, protocol)
    _, applied = _apply_champion(data, baseline)
    artifacts = experiment_dir / "artifacts"
    _write_json(artifacts / "identity_audit.json", _identity_audit(baseline, applied, protocol))
    family = str(protocol["family"])
    specs = build_family_specs(family)
    if len(specs) != int(protocol["candidate_count"]):
        raise AssertionError("family grid count differs from protocol")
    periods = _periods(2021, 2023, "2021-2023FULL")
    rows, paths, _ = _evaluate_specs(
        data,
        applied,
        specs,
        periods,
        full_name="2021-2023FULL",
        annual_names=("2021", "2022", "2023"),
        fee_rate=float(protocol["fee_rate"]),
        init_cash=float(protocol["init_cash"]),
    )
    eligible = rank_eligible_position_risk(rows)
    eligible_ids = set(eligible["candidate_id"])
    rows["eligible"] = rows["candidate_id"].isin(eligible_ids)
    rank_map = {candidate_id: rank for rank, candidate_id in enumerate(eligible["candidate_id"], 1)}
    rows.insert(0, "eligible_rank", rows["candidate_id"].map(rank_map))
    rows.to_csv(artifacts / "candidate_results.csv", index=False, encoding="utf-8-sig")
    selection: dict[str, object] = {
        "status": "PASS" if not eligible.empty else "FAIL",
        "family": family,
        "candidate_count": int(len(rows)),
        "eligible_count": int(len(eligible)),
        "selected_candidate": None if eligible.empty else str(eligible.iloc[0]["candidate_id"]),
        "visible_data_hashes": data.hashes,
        "validation_accessed": False,
        "historical_check_accessed": False,
    }
    _write_json(artifacts / "selection_metrics.json", selection)
    if eligible.empty:
        return _finalize(
            experiment_dir,
            protocol,
            "FAIL",
            [
                "状态：家族发现完成（`FAIL`）",
                f"执行提交：`{execution_commit}`",
                f"候选数：{len(rows)}",
                "合格候选数：0",
                "访问2024—2025：否",
                "访问2026：否",
            ],
            f"{experiment_dir.name}的{family}家族没有候选同时保持2021—2023收益并严格改善最大回撤；没有冻结家族优胜者。",
            visible_sample_end=str(cutoff.date()),
            validation_accessed=False,
            historical_check_accessed=False,
            failure_stage="discovery_no_eligible_candidate",
        )
    best_id = str(eligible.iloc[0]["candidate_id"])
    best_path = paths[best_id]
    best_spec = best_path["spec"]
    assert isinstance(best_spec, RiskOverlaySpec)
    frozen, frozen_digest = _freeze_family_winner(
        experiment_dir,
        protocol,
        best_spec,
        best_path["target"],  # type: ignore[arg-type]
        selection,
    )
    selection["frozen_challenger_sha256"] = frozen_digest
    _write_json(artifacts / "selection_metrics.json", selection)
    events = best_path["events"]
    assert isinstance(events, pd.DataFrame)
    events.to_csv(artifacts / "factor_events.csv", index=False, encoding="utf-8-sig")
    full_result = best_path["results"]["2021-2023FULL"]  # type: ignore[index]
    full_result.orders.to_csv(artifacts / "orders.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        {
            "dt": best_path["target"].index,  # type: ignore[union-attr]
            "champion_target": applied.target_position.to_numpy(),
            "challenger_target": best_path["target"].to_numpy(),  # type: ignore[union-attr]
            "pressure": best_path["pressure"].to_numpy(),  # type: ignore[union-attr]
        }
    ).to_csv(artifacts / "position_path.csv", index=False, encoding="utf-8-sig")
    _write_json(artifacts / "causal_audit.json", best_path["audit"])  # type: ignore[arg-type]
    return _finalize(
        experiment_dir,
        protocol,
        "PASS",
        [
            "状态：家族发现完成（`PASS`）",
            f"执行提交：`{execution_commit}`",
            f"候选数：{len(rows)}",
            f"合格候选数：{len(eligible)}",
            f"冻结候选：`{best_id}`",
            "访问2024—2025：否",
            "访问2026：否",
        ],
        f"{experiment_dir.name}在发现段冻结了{family}家族优胜者`{best_id}`；该PASS只表示有资格进入第5轮，不改变活动基线。",
        visible_sample_end=str(cutoff.date()),
        validation_accessed=False,
        historical_check_accessed=False,
        frozen_digest=frozen_digest,
    )


def _load_source_specs(
    experiments_root: Path, protocol: Mapping[str, object]
) -> tuple[RiskOverlaySpec, ...]:
    specs: list[RiskOverlaySpec] = []
    for experiment_id in protocol["source_experiments"]:  # type: ignore[index]
        source_dir = experiments_root / str(experiment_id)
        manifest_path = source_dir / "experiment_manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = validate_experiment_archive(source_dir)
        if manifest.get("status") != "PASS":
            continue
        frozen_path = source_dir / "artifacts" / "frozen_challenger.json"
        digest = sha256(frozen_path.read_bytes()).hexdigest()
        if manifest.get("frozen_challenger_sha256") != digest:
            raise AssertionError(f"{experiment_id}: frozen family winner hash differs")
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        if frozen.get("champion") != EXPECTED_CHAMPION:
            raise AssertionError(f"{experiment_id}: frozen champion differs")
        specs.append(risk_spec_from_frozen(frozen["spec"]))
    return tuple(specs)


def _historical_periods(cutoff: pd.Timestamp) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    return {
        "2026FULL": (pd.Timestamp("2026-01-01"), cutoff),
        **TARGET_PERIODS,
    }


def _run_round_five(
    raw_dir: Path,
    baseline_root: Path,
    experiments_root: Path,
    experiment_dir: Path,
    protocol: Mapping[str, object],
    execution_commit: str,
) -> dict[str, object]:
    artifacts = experiment_dir / "artifacts"
    specs = _load_source_specs(experiments_root, protocol)
    if not specs:
        _write_json(
            artifacts / "selection_metrics.json",
            {
                "status": "FAIL",
                "source_candidate_count": 0,
                "validation_accessed": False,
                "historical_check_accessed": False,
            },
        )
        return _finalize(
            experiment_dir,
            protocol,
            "FAIL",
            [
                "状态：锁定验证未启动（`FAIL`）",
                f"执行提交：`{execution_commit}`",
                "发现段家族优胜者数：0",
                "访问2024—2025：否",
                "访问2026：否",
            ],
            "第5轮没有可进入锁定验证的家族优胜者，因此按预注册规则停止，未访问2024—2026。",
            visible_sample_end=str(protocol["discovery_end"]),
            validation_accessed=False,
            historical_check_accessed=False,
            failure_stage="no_discovery_family_winner",
        )
    validation_cutoff = permitted_position_risk_cutoff(protocol, "validation")
    data = load_market_data(raw_dir, "588080.SH", "etf", cutoff=validation_cutoff)
    baseline = _resolve_champion(baseline_root, protocol)
    _, applied = _apply_champion(data, baseline)
    _write_json(artifacts / "identity_audit.json", _identity_audit(baseline, applied, protocol))
    rows, paths, _ = _evaluate_specs(
        data,
        applied,
        specs,
        _periods(2024, 2025, "2024-2025FULL"),
        full_name="2024-2025FULL",
        annual_names=("2024", "2025"),
        fee_rate=float(protocol["fee_rate"]),
        init_cash=float(protocol["init_cash"]),
    )
    eligible = rank_eligible_position_risk(rows)
    eligible_ids = set(eligible["candidate_id"])
    rows["eligible"] = rows["candidate_id"].isin(eligible_ids)
    rows.to_csv(artifacts / "validation_metrics.csv", index=False, encoding="utf-8-sig")
    selection: dict[str, object] = {
        "status": "PASS" if not eligible.empty else "FAIL",
        "source_candidate_count": len(specs),
        "eligible_count": int(len(eligible)),
        "selected_candidate": None if eligible.empty else str(eligible.iloc[0]["candidate_id"]),
        "validation_accessed": True,
        "historical_check_accessed": False,
        "visible_data_hashes": data.hashes,
    }
    _write_json(artifacts / "selection_metrics.json", selection)
    if eligible.empty:
        return _finalize(
            experiment_dir,
            protocol,
            "FAIL",
            [
                "状态：锁定验证完成（`FAIL`）",
                f"执行提交：`{execution_commit}`",
                f"来源候选数：{len(specs)}",
                "验证段合格候选数：0",
                "访问2024—2025：是",
                "访问2026：否",
            ],
            "第5轮来源候选均未在2024—2025同时保持收益并严格改善最大回撤；没有冻结最终候选，也未访问2026。",
            visible_sample_end=str(validation_cutoff.date()),
            validation_accessed=True,
            historical_check_accessed=False,
            failure_stage="validation_no_eligible_candidate",
        )
    best_id = str(eligible.iloc[0]["candidate_id"])
    validation_path = paths[best_id]
    best_spec = validation_path["spec"]
    assert isinstance(best_spec, RiskOverlaySpec)
    _, frozen_digest = _freeze_family_winner(
        experiment_dir,
        protocol,
        best_spec,
        validation_path["target"],  # type: ignore[arg-type]
        selection,
    )
    historical_cutoff = permitted_position_risk_cutoff(protocol, "historical")
    historical_data = load_market_data(
        raw_dir, "588080.SH", "etf", cutoff=historical_cutoff
    )
    _, historical_applied = _apply_champion(historical_data, baseline)
    rows_2026, historical_paths, champion_results = _evaluate_specs(
        historical_data,
        historical_applied,
        (best_spec,),
        _historical_periods(historical_cutoff),
        full_name="2026FULL",
        annual_names=(),
        fee_rate=float(protocol["fee_rate"]),
        init_cash=float(protocol["init_cash"]),
    )
    candidate_path = historical_paths[best_id]
    candidate_results = candidate_path["results"]
    windows: dict[str, dict[str, object]] = {}
    for name in _historical_periods(historical_cutoff):
        champion_metrics = champion_results[name].metrics
        challenger_metrics = candidate_results[name].metrics
        windows[name] = {
            "champion_return": float(champion_metrics["strategy_return"]),
            "challenger_return": float(challenger_metrics["strategy_return"]),
            "return_delta": float(challenger_metrics["strategy_return"])
            - float(champion_metrics["strategy_return"]),
            "champion_max_drawdown": float(champion_metrics["max_drawdown"]),
            "challenger_max_drawdown": float(challenger_metrics["max_drawdown"]),
            "max_drawdown_improvement": float(challenger_metrics["max_drawdown"])
            - float(champion_metrics["max_drawdown"]),
            "champion_sharpe": float(champion_metrics["sharpe"]),
            "challenger_sharpe": float(challenger_metrics["sharpe"]),
        }
    main = windows["2026FULL"]
    passed = bool(
        float(main["challenger_return"]) >= float(main["champion_return"])
        and float(main["challenger_max_drawdown"])
        > float(main["champion_max_drawdown"])
    )
    _write_json(
        artifacts / "historical_metrics.json",
        {
            "status": "PASS" if passed else "FAIL",
            "selected_candidate": best_id,
            "frozen_challenger_sha256": frozen_digest,
            "windows": windows,
            "validation_accessed": True,
            "historical_check_accessed": True,
        },
    )
    candidate_path["events"].to_csv(  # type: ignore[union-attr]
        artifacts / "factor_events.csv", index=False, encoding="utf-8-sig"
    )
    candidate_results["2026FULL"].orders.to_csv(
        artifacts / "orders.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(
        {
            "dt": candidate_path["target"].index,  # type: ignore[union-attr]
            "champion_target": historical_applied.target_position.to_numpy(),
            "challenger_target": candidate_path["target"].to_numpy(),  # type: ignore[union-attr]
            "pressure": candidate_path["pressure"].to_numpy(),  # type: ignore[union-attr]
        }
    ).to_csv(artifacts / "position_path.csv", index=False, encoding="utf-8-sig")
    _write_json(artifacts / "causal_audit.json", candidate_path["audit"])  # type: ignore[arg-type]
    rows_2026.to_csv(artifacts / "historical_candidate.csv", index=False, encoding="utf-8-sig")
    status = "PASS" if passed else "FAIL"
    return _finalize(
        experiment_dir,
        protocol,
        status,
        [
            f"状态：最终历史验收完成（`{status}`）",
            f"执行提交：`{execution_commit}`",
            f"来源候选数：{len(specs)}",
            f"验证段合格候选数：{len(eligible)}",
            f"冻结候选：`{best_id}`",
            "访问2024—2025：是",
            "访问2026：是",
        ],
        f"第5轮冻结`{best_id}`后完成2026FULL已观察历史回测，最终状态为 **{status}**。活动基线未自动改变。",
        visible_sample_end=str(historical_cutoff.date()),
        validation_accessed=True,
        historical_check_accessed=True,
        frozen_digest=frozen_digest,
        failure_stage=None if passed else "historical_objective_failed",
    )


def _archive_error(
    experiment_dir: Path,
    protocol: Mapping[str, object],
    exc: Exception,
    execution_commit: str,
    *,
    validation_accessed: bool,
    historical_check_accessed: bool,
) -> None:
    _write_json(
        experiment_dir / "artifacts" / "error.json",
        {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "message": str(exc),
            "validation_accessed": validation_accessed,
            "historical_check_accessed": historical_check_accessed,
        },
    )
    if historical_check_accessed:
        visible = str(protocol.get("historical_check_end", "unknown"))
    elif validation_accessed:
        visible = str(protocol.get("validation_end", "unknown"))
    else:
        visible = str(protocol.get("discovery_end", "unknown"))
    _finalize(
        experiment_dir,
        protocol,
        "ERROR",
        [
            "状态：正式执行异常（`ERROR`）",
            f"执行提交：`{execution_commit}`",
            f"异常类型：`{type(exc).__name__}`",
            f"异常信息：{exc}",
            f"访问2024—2025：{'是' if validation_accessed else '否'}",
            f"访问2026：{'是' if historical_check_accessed else '否'}",
        ],
        "本轮因技术异常未形成有效研究结论；活动基线未改变。",
        visible_sample_end=visible,
        validation_accessed=validation_accessed,
        historical_check_accessed=historical_check_accessed,
        failure_stage="runtime_error",
    )


def run_position_risk_program(
    raw_dir: Path,
    baseline_root: Path,
    experiments_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    """Run one preregistered round with strict segment-access gates."""
    experiment_dir = Path(experiment_dir).resolve()
    protocol = _read_protocol(experiment_dir)
    validation_accessed = False
    historical_check_accessed = False
    try:
        validate_position_risk_protocol(protocol, experiment_dir.name)
        if int(protocol["program_round"]) == 1:
            return _run_diagnostic(
                raw_dir, baseline_root, experiment_dir, protocol, execution_commit
            )
        if int(protocol["program_round"]) <= 4:
            return _run_family_discovery(
                raw_dir, baseline_root, experiment_dir, protocol, execution_commit
            )
        sources = _load_source_specs(Path(experiments_root), protocol)
        if sources:
            validation_accessed = True
        result = _run_round_five(
            raw_dir,
            baseline_root,
            Path(experiments_root),
            experiment_dir,
            protocol,
            execution_commit,
        )
        manifest = json.loads(
            (experiment_dir / "experiment_manifest.json").read_text(encoding="utf-8")
        )
        historical_check_accessed = bool(manifest.get("historical_check_accessed"))
        return result
    except Exception as exc:
        if not (experiment_dir / "experiment_manifest.json").exists():
            _archive_error(
                experiment_dir,
                protocol,
                exc,
                execution_commit,
                validation_accessed=validation_accessed,
                historical_check_accessed=historical_check_accessed,
            )
        raise
