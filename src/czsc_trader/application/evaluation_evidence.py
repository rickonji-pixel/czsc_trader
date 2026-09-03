"""Produce immutable facts for the SE-owned provisional-champion audit."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pandas as pd
from strategy_evaluator import (
    AuditIdentity,
    CandidateDescriptor,
    CandidateProfile,
    ChampionAuditRequest,
    ExecutionEvidence,
    ExecutionOrder,
    FactorEvent,
    ParameterPoint,
    ReturnMatrixEvidence,
    StressScenarioResult,
    TrialRecord,
    hash_audit_data,
    hash_candidate_pool,
    hash_execution_evidence,
    hash_return_matrix,
)

from czsc_trader.backtest import run_period_backtests
from czsc_trader.backtest_runner import _complete_baseline_execution_results
from czsc_trader.baseline_execution import apply_resolved_baseline
from czsc_trader.baselines import resolve_strategy_payload
from czsc_trader.candidate_evaluation import CandidateEvaluationContext, prepare_evaluation_workspace
from czsc_trader.four_layer import normalized_signal_factors
from czsc_trader.regime_weight import classify_regimes, lagged_efficiency_ratio


def _daily_returns(equity: pd.Series, init_cash: float) -> pd.Series:
    values = equity.astype(float)
    previous = values.shift(1)
    previous.iloc[0] = float(init_cash)
    return values.div(previous).sub(1.0).rename("return")


def _resolved_and_applied(
    run_context: CandidateEvaluationContext,
    protocol,
    payloads: tuple[dict[str, object], ...],
    candidate_ids: tuple[str, ...],
):
    workspace = prepare_evaluation_workspace(run_context, protocol)
    by_id = {str(item["candidate_id"]): item for item in payloads}
    factor_cache: dict[tuple[str, ...], pd.DataFrame] = {}
    regime_cache: dict[tuple[int, float], pd.Series] = {}
    output = {}
    for candidate_id in candidate_ids:
        item = by_id[candidate_id]
        strategy_payload = item.get("strategy_payload")
        if not isinstance(strategy_payload, dict):
            raise ValueError(f"candidate {candidate_id} has no complete strategy_payload")
        baseline = resolve_strategy_payload(
            run_context.repository.baseline_root, strategy_payload,
            release_id=candidate_id,
            release_hash=str(item.get("strategy_hash", item.get("candidate_hash", ""))),
            symbol=run_context.symbol,
        )
        normalized = None
        regimes = None
        if baseline.strategy in {"czsc_four_layer", "czsc_regime_weight"}:
            names = tuple(map(str, baseline.factor_names))
            normalized = factor_cache.get(names)
            if normalized is None:
                normalized = normalized_signal_factors(workspace.factor_frame.loc[:, list(names)])
                factor_cache[names] = normalized
        if baseline.strategy == "czsc_regime_weight":
            key = (int(baseline.er_lookback), float(baseline.er_threshold))
            regimes = regime_cache.get(key)
            if regimes is None:
                close = workspace.daily_close.reindex(workspace.factor_frame.index)
                regimes = classify_regimes(
                    lagged_efficiency_ratio(close, baseline.er_lookback), baseline.er_threshold,
                )
                regime_cache[key] = regimes
        applied = apply_resolved_baseline(
            workspace.factor_frame, baseline, daily_close=workspace.daily_close,
            normalized_factors=normalized, regimes=regimes,
        )
        output[candidate_id] = (baseline, applied)
    return workspace, output


def _return_evidence(
    run_context: CandidateEvaluationContext,
    workspace,
    applied_by_id,
    candidate_ids: tuple[str, ...],
    *,
    formal_execution: bool,
) -> ReturnMatrixEvidence:
    full_period = workspace.periods.get("full")
    if full_period is None:
        raise ValueError("OPC-v3 audit requires a full period")
    periods = {"full": full_period}
    columns: list[pd.Series] = []
    for candidate_id in candidate_ids:
        baseline, applied = applied_by_id[candidate_id]
        simple = run_period_backtests(
            workspace.data.daily, applied.target_position, periods,
            fee_rate=run_context.fee_rate, init_cash=run_context.init_cash,
        )["full"]
        effective = simple
        if formal_execution:
            if baseline.execution is None:
                raise ValueError(f"candidate {candidate_id} has no frozen execution policy")
            effective = _complete_baseline_execution_results(
                workspace.data.daily, workspace.data.intraday, applied.target_position, periods,
                baseline.execution, fee_rate=run_context.fee_rate, init_cash=run_context.init_cash,
            )["full"]
        columns.append(_daily_returns(effective.equity, run_context.init_cash).rename(candidate_id))
    frame = pd.concat(columns, axis=1, join="inner")
    if frame.empty or frame.isna().any().any():
        raise ValueError("candidate return matrix is empty or misaligned")
    evidence = ReturnMatrixEvidence(
        tuple(timestamp.date().isoformat() for timestamp in pd.DatetimeIndex(frame.index)),
        candidate_ids,
        tuple(tuple(map(float, row)) for row in frame.to_numpy()),
        "",
    )
    return replace(evidence, content_hash=hash_return_matrix(evidence))


def _execution_evidence(
    run_context: CandidateEvaluationContext,
    workspace,
    baseline,
    applied,
) -> ExecutionEvidence:
    full_period = workspace.periods["full"]
    factor_frame = workspace.factor_frame.copy()
    factor_frame.insert(0, "target_position", applied.target_position)
    factor_frame.insert(1, "factor_score", applied.scores)
    result = run_period_backtests(
        workspace.data.daily, applied.target_position, {"full": full_period},
        fee_rate=run_context.fee_rate, init_cash=run_context.init_cash,
        factor_events=applied.events, factor_frame=factor_frame,
    )["full"]
    events = tuple(
        FactorEvent(
            str(row.get("event_id", "")), pd.Timestamp(row["signal_date"]).date().isoformat(),
            str(row["event_type"]), float(row["before_position"]),
            float(row["after_position"]),
            None if row.get("factor_score") is None else float(row["factor_score"]),
        )
        for row in result.factor_events.to_dict("records")
    )
    event_types = {event.event_id: event.event_type for event in events}
    orders = tuple(
        ExecutionOrder(
            pd.Timestamp(row["signal_date"]).date().isoformat(),
            pd.Timestamp(row["execution_date"]).date().isoformat(), str(row["side"]),
            float(row["price"]), float(row["size"]), float(row["fees"]),
            str(row.get("factor_event_id", "")),
            event_types.get(str(row.get("factor_event_id", ""))) == "InitialEntry",
        )
        for row in result.orders.to_dict("records")
    )
    dates = tuple(
        timestamp.date().isoformat()
        for timestamp in pd.DatetimeIndex(applied.target_position.index)
    )
    evidence = ExecutionEvidence(
        dates, tuple(map(float, applied.target_position)), tuple(map(float, applied.scores)),
        events, orders, "", 1,
    )
    return replace(evidence, content_hash=hash_execution_evidence(evidence))


def _parameter_points(
    payloads: tuple[dict[str, object], ...],
    candidate_ids: tuple[str, ...],
    profiles: tuple[CandidateProfile, ...],
    incumbent_id: str,
) -> tuple[ParameterPoint, ...]:
    by_id = {str(item["candidate_id"]): item for item in payloads}
    profile_by_id = {item.candidate_id: item for item in profiles}
    points: list[ParameterPoint] = []
    for candidate_id in candidate_ids:
        payload = by_id[candidate_id]
        strategy_payload = payload.get("strategy_payload")
        rule = strategy_payload.get("rule") if isinstance(strategy_payload, dict) else None
        weights = rule.get("weights") if isinstance(rule, dict) else None
        range_weights = weights.get("range") if isinstance(weights, dict) else None
        if not isinstance(range_weights, dict) or not range_weights:
            raise ValueError(f"candidate {candidate_id} has no range weight coordinates")
        profile = profile_by_id.get(candidate_id)
        if candidate_id == incumbent_id:
            eligible, layer = True, 0
            worst_scores = tuple((name, 0.0) for name in (
                "net_cagr", "max_drawdown", "calmar", "profit_factor",
            ))
        elif profile is None:
            eligible, layer, worst_scores = False, None, ()
        else:
            eligible, layer, worst_scores = profile.eligible, profile.pareto_layer, profile.worst_scores
        points.append(ParameterPoint(
            candidate_id, str(payload.get("behavior_hash", "")),
            tuple(sorted((str(name), float(value)) for name, value in range_weights.items())),
            eligible, layer, worst_scores, candidate_id == incumbent_id,
        ))
    return tuple(points)


def build_champion_audit_request(
    *,
    run_context: CandidateEvaluationContext,
    protocol,
    manifest: dict[str, Any],
    payloads: tuple[dict[str, object], ...],
    candidates: tuple[CandidateDescriptor, ...],
    trials: tuple[TrialRecord, ...],
    ranking,
    screening_profiles: tuple[CandidateProfile, ...],
    formal,
    repeated,
    stress,
    search_candidate_ids: tuple[str, ...],
) -> ChampionAuditRequest:
    champion_id = ranking.champion_id
    if champion_id is None:
        raise ValueError("champion audit requires a provisional champion")
    peers = tuple(
        item.candidate_id for item in ranking.profiles
        if item.pareto_layer == 1 and item.candidate_id != champion_id
    )
    all_required = tuple(dict.fromkeys((protocol.incumbent_id, *search_candidate_ids)))
    workspace, applied = _resolved_and_applied(run_context, protocol, payloads, all_required)
    search_returns = _return_evidence(
        run_context, workspace, applied, search_candidate_ids, formal_execution=False,
    )
    comparison_ids = tuple(dict.fromkeys((champion_id, protocol.incumbent_id, *peers)))
    comparison_returns = _return_evidence(
        run_context, workspace, applied, comparison_ids, formal_execution=True,
    )
    baseline, champion_applied = applied[champion_id]
    execution = _execution_evidence(
        run_context, workspace, baseline, champion_applied,
    )
    parameters = _parameter_points(
        payloads, all_required, screening_profiles, protocol.incumbent_id,
    )
    stress_results = tuple(
        StressScenarioResult(
            scenario, protocol.execution_policy_hash,
            tuple(item for item in stress if item.scenario_id == scenario),
        )
        for scenario in dict.fromkeys(item.scenario_id for item in stress)
    )
    audit_protocol = manifest.get("audit_protocol", {})
    if not isinstance(audit_protocol, dict):
        raise ValueError("audit_protocol must be an object")
    identity = AuditIdentity(
        protocol.experiment_id, hash_candidate_pool(candidates), protocol.development_cutoff,
        hash_audit_data(search_returns, comparison_returns), protocol.execution_policy_hash,
        protocol.standard_version, str(audit_protocol.get("version", "champion-audit-v1")),
        int(audit_protocol.get("seed", 20260903)),
    )
    return ChampionAuditRequest(
        identity, champion_id, protocol.incumbent_id, peers, search_returns,
        comparison_returns, parameters, execution,
        tuple(item for item in formal if item.candidate_id in {champion_id, protocol.incumbent_id}),
        tuple(repeated), candidates, trials, screening_profiles, stress_results,
        int(audit_protocol.get("bootstrap_repetitions", 10_000)),
        tuple(int(value) for value in audit_protocol.get("mean_block_lengths", (21, 10, 42))),
    )
