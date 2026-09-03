import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtest import PeriodBacktestResult
from czsc_trader.candidate_evaluation import (
    CandidateEvaluationContext,
    _behavior_key,
    _contiguous_chunks,
    _evaluate_candidate_payloads_reference,
    evaluate_candidate_payloads,
    prepare_evaluation_workspace,
)
from czsc_trader.range_diagnostics import range_cycle_objectives
from strategy_evaluator import EvaluationProtocol


def test_candidate_context_defaults_to_one_worker():
    dates = pd.date_range("2026-01-01", periods=2, freq="D")
    context = CandidateEvaluationContext(
        SimpleNamespace(raw_dir=Path("raw"), baseline_root=Path("baselines")),
        "588080.SH",
        "etf",
        (("full", (dates[0], dates[1])),),
    )
    assert context.workers == 1


def test_behavior_key_includes_target_execution_tier_scenario_and_fee():
    target = pd.Series([0.0, 1.0])
    base = _behavior_key(target, "execution-a", "SCREENING", "standard", 0.0005)
    assert _behavior_key(pd.Series([0.0, 0.0]), "execution-a", "SCREENING", "standard", 0.0005) != base
    assert _behavior_key(target, "execution-b", "SCREENING", "standard", 0.0005) != base
    assert _behavior_key(target, "execution-a", "FORMAL", "standard", 0.0005) != base
    assert _behavior_key(target, "execution-a", "STRESS", "fee_x2", 0.001) != base


def test_contiguous_chunks_are_balanced_and_ordered():
    assert _contiguous_chunks(tuple(range(10)), 3) == (
        (0, 1, 2, 3),
        (4, 5, 6),
        (7, 8, 9),
    )
    assert _contiguous_chunks((1, 2), 8) == ((1,), (2,))
    assert _contiguous_chunks((), 4) == ()


def test_candidate_runner_loads_market_and_factors_once(monkeypatch, tmp_path):
    calls = {"market": 0, "factors": 0}
    dates = pd.date_range("2020-01-01", periods=6, freq="D")
    daily = pd.DataFrame({"dt": dates, "open": range(10, 16), "close": range(10, 16)})
    market = SimpleNamespace(daily=daily, intraday=pd.DataFrame(), truncate=lambda cutoff: market)
    frame = pd.DataFrame({"factor": [0.0] * 6}, index=dates)
    monkeypatch.setattr("czsc_trader.candidate_evaluation.load_market_data", lambda *a: calls.__setitem__("market", calls["market"] + 1) or market)
    monkeypatch.setattr("czsc_trader.candidate_evaluation.generate_factor_frame", lambda *a: calls.__setitem__("factors", calls["factors"] + 1) or SimpleNamespace(frame=frame))
    monkeypatch.setattr("czsc_trader.candidate_evaluation.resolve_strategy_payload", lambda *a, **k: SimpleNamespace(execution=None, strategy="czsc_fixed_rule"))
    target = pd.Series([0, 0, 1, 1, 0, 0], index=dates, dtype=float)
    monkeypatch.setattr("czsc_trader.candidate_evaluation.apply_resolved_baseline", lambda *a, **k: SimpleNamespace(target_position=target, events=pd.DataFrame(), scores=target))
    equity = pd.Series([100, 100, 102], index=dates[-3:])
    orders = pd.DataFrame(columns=["signal_date", "execution_date", "side", "size", "price", "fees"])
    result = PeriodBacktestResult(None, equity, orders, {}, pd.DataFrame())
    monkeypatch.setattr("czsc_trader.candidate_evaluation.run_period_backtests", lambda *a, **k: {"full": result})
    repo = RepositoryContext.discover(tmp_path, explicit_root=tmp_path) if False else SimpleNamespace(raw_dir=tmp_path, baseline_root=tmp_path)
    context = CandidateEvaluationContext(repo, "588080.SH", "etf", (("full", (dates[-3], dates[-1])),), 0.0005, 100.0)
    protocol = EvaluationProtocol.from_dict({
        "schema_version": 1, "standard_version": "opc-v1", "experiment_id": "EX", "research_objective": "x",
        "development_cutoff": "2020-01-06", "incumbent_id": "a", "incumbent_hash": "h", "decision_windows": ["full"],
        "target_windows": ["full"], "execution_policy_hash": "e", "tightened_margins": {}, "shortlist_limit": 2,
        "target_requirements": [], "candidate_manifest": "candidate_manifest.json",
    })
    payloads = ({"candidate_id": "a", "strategy_hash": "h", "strategy_payload": {"rule": {}}}, {"candidate_id": "b", "strategy_hash": "i", "strategy_payload": {"rule": {}}})
    observations = evaluate_candidate_payloads(context, protocol, payloads, ("a", "b"), "SCREENING")
    assert calls == {"market": 1, "factors": 1}
    assert {item.candidate_id for item in observations} == {"a", "b"}


def test_prepare_workspace_loads_market_and_factors_once(monkeypatch, tmp_path):
    calls = {"market": 0, "factors": 0}
    dates = pd.date_range("2020-01-01", periods=6, freq="D")
    daily = pd.DataFrame({"dt": dates, "open": range(10, 16), "close": range(10, 16)})
    market = SimpleNamespace(daily=daily, intraday=pd.DataFrame(), truncate=lambda cutoff: market)
    frame = pd.DataFrame({"factor": [0.0] * 6}, index=dates)
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.load_market_data",
        lambda *args: calls.__setitem__("market", calls["market"] + 1) or market,
    )
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.generate_factor_frame",
        lambda *args: calls.__setitem__("factors", calls["factors"] + 1) or SimpleNamespace(frame=frame),
    )
    repository = SimpleNamespace(raw_dir=tmp_path, baseline_root=tmp_path)
    context = CandidateEvaluationContext(
        repository, "588080.SH", "etf", (("full", (dates[1], dates[-1])),),
    )
    protocol = EvaluationProtocol.from_dict({
        "schema_version": 1, "standard_version": "opc-v1", "experiment_id": "EX", "research_objective": "x",
        "development_cutoff": "2020-01-06", "incumbent_id": "a", "incumbent_hash": "h", "decision_windows": ["full"],
        "target_windows": ["full"], "execution_policy_hash": "e", "tightened_margins": {}, "shortlist_limit": 2,
        "target_requirements": [], "candidate_manifest": "candidate_manifest.json",
    })
    workspace = prepare_evaluation_workspace(context, protocol)
    assert calls == {"market": 1, "factors": 1}
    assert workspace.periods["full"] == (dates[1], dates[-1])
    assert workspace.factor_frame is frame


def test_public_runner_matches_reference_path(monkeypatch, tmp_path):
    dates = pd.date_range("2020-01-01", periods=6, freq="D")
    daily = pd.DataFrame({"dt": dates, "open": range(10, 16), "close": range(10, 16)})
    market = SimpleNamespace(daily=daily, intraday=pd.DataFrame(), truncate=lambda cutoff: market)
    frame = pd.DataFrame({"factor": [0.0] * 6}, index=dates)
    monkeypatch.setattr("czsc_trader.candidate_evaluation.load_market_data", lambda *args: market)
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.generate_factor_frame",
        lambda *args: SimpleNamespace(frame=frame),
    )
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.resolve_strategy_payload",
        lambda *args, **kwargs: SimpleNamespace(execution=None, strategy="czsc_fixed_rule"),
    )
    target = pd.Series([0, 0, 1, 1, 0, 0], index=dates, dtype=float)
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.apply_resolved_baseline",
        lambda *args, **kwargs: SimpleNamespace(target_position=target, events=pd.DataFrame(), scores=target),
    )
    equity = pd.Series([100, 100, 102, 102, 102], index=dates[1:])
    orders = pd.DataFrame(columns=["signal_date", "execution_date", "side", "size", "price", "fees"])
    result = PeriodBacktestResult(None, equity, orders, {}, pd.DataFrame())
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.run_period_backtests", lambda *args, **kwargs: {"full": result},
    )
    repository = SimpleNamespace(raw_dir=tmp_path, baseline_root=tmp_path)
    context = CandidateEvaluationContext(
        repository, "588080.SH", "etf", (("full", (dates[1], dates[-1])),), 0.0005, 100.0,
    )
    protocol = EvaluationProtocol.from_dict({
        "schema_version": 1, "standard_version": "opc-v1", "experiment_id": "EX", "research_objective": "x",
        "development_cutoff": "2020-01-06", "incumbent_id": "a", "incumbent_hash": "h", "decision_windows": ["full"],
        "target_windows": ["full"], "execution_policy_hash": "e", "tightened_margins": {}, "shortlist_limit": 2,
        "target_requirements": [], "candidate_manifest": "candidate_manifest.json",
    })
    payloads = ({"candidate_id": "a", "strategy_hash": "h", "strategy_payload": {"rule": {}}},)
    optimized = evaluate_candidate_payloads(context, protocol, payloads, ("a",), "SCREENING")
    reference = _evaluate_candidate_payloads_reference(context, protocol, payloads, ("a",), "SCREENING")
    assert [item.to_dict() for item in optimized] == [item.to_dict() for item in reference]


def test_regime_candidates_share_normalization_regime_and_realized_behavior(monkeypatch, tmp_path):
    calls = {"normalize": 0, "regime": 0, "backtest": 0, "audit": 0}
    dates = pd.date_range("2020-01-01", periods=6, freq="D")
    daily = pd.DataFrame({"dt": dates, "open": range(10, 16), "close": range(10, 16)})
    market = SimpleNamespace(daily=daily, intraday=pd.DataFrame(), truncate=lambda cutoff: market)
    frame = pd.DataFrame({"factor": [0.0, 0.2, 0.3, -0.1, -0.2, 0.2]}, index=dates)
    monkeypatch.setattr("czsc_trader.candidate_evaluation.load_market_data", lambda *args: market)
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.generate_factor_frame",
        lambda *args: SimpleNamespace(frame=frame),
    )
    rule = SimpleNamespace(
        enter=0.1, exit=0.0, entry_gate="none", confirm_days=1,
        min_hold_days=1, exit_confirm_days=1,
    )
    baseline = SimpleNamespace(
        strategy="czsc_regime_weight", factor_names=("factor",), factor_weights=(1.0,),
        regime_factor_weights={"trend": (1.0,), "range": (1.0,)}, er_lookback=2,
        er_threshold=0.5, rule=rule, execution=None,
    )
    monkeypatch.setattr("czsc_trader.candidate_evaluation.resolve_strategy_payload", lambda *args, **kwargs: baseline)

    def fallback_apply(*args, **kwargs):
        if kwargs.get("normalized_factors") is None:
            calls["normalize"] += 1
        if kwargs.get("regimes") is None:
            calls["regime"] += 1
        scores = frame["factor"].rename("factor_score")
        target = scores.ge(0.1).astype(float).rename("target_position")
        return SimpleNamespace(target_position=target, scores=scores, events=pd.DataFrame())

    monkeypatch.setattr("czsc_trader.candidate_evaluation.apply_resolved_baseline", fallback_apply)
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.normalized_signal_factors",
        lambda raw: calls.__setitem__("normalize", calls["normalize"] + 1) or raw,
        raising=False,
    )
    regimes = pd.Series(["warmup", "warmup", "trend", "trend", "range", "range"], index=dates)
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.classify_regimes",
        lambda *args: calls.__setitem__("regime", calls["regime"] + 1) or regimes,
    )
    equity = pd.Series([100, 101, 102, 101, 103], index=dates[1:])
    orders = pd.DataFrame(columns=["signal_date", "execution_date", "side", "size", "price", "fees"])
    result = PeriodBacktestResult(None, equity, orders, {}, pd.DataFrame())
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.run_period_backtests",
        lambda *args, **kwargs: calls.__setitem__("backtest", calls["backtest"] + 1) or {"full": result},
    )
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.audit_no_lookahead",
        lambda *args: (_ for _ in ()).throw(AssertionError("slow audit path used")),
    )
    monkeypatch.setattr(
        "czsc_trader.candidate_evaluation.audit_candidate_evaluation",
        lambda *args: calls.__setitem__("audit", calls["audit"] + 1),
        raising=False,
    )
    monkeypatch.setattr("czsc_trader.candidate_evaluation.range_cycle_objectives", lambda *args: ())
    repository = SimpleNamespace(raw_dir=tmp_path, baseline_root=tmp_path)
    context = CandidateEvaluationContext(
        repository, "588080.SH", "etf", (("full", (dates[1], dates[-1])),), 0.0005, 100.0,
    )
    protocol = EvaluationProtocol.from_dict({
        "schema_version": 1, "standard_version": "opc-v1", "experiment_id": "EX", "research_objective": "x",
        "development_cutoff": "2020-01-06", "incumbent_id": "a", "incumbent_hash": "h", "decision_windows": ["full"],
        "target_windows": ["full"], "execution_policy_hash": "e", "tightened_margins": {}, "shortlist_limit": 2,
        "target_requirements": [], "candidate_manifest": "candidate_manifest.json",
    })
    payloads = tuple(
        {"candidate_id": candidate_id, "strategy_hash": candidate_id, "strategy_payload": {"rule": {}}}
        for candidate_id in ("a", "b")
    )
    evaluate_candidate_payloads(context, protocol, payloads, ("a", "b"), "SCREENING")
    assert calls == {"normalize": 1, "regime": 1, "backtest": 1, "audit": 2}


@pytest.mark.archive
def test_real_regime_candidates_match_reference_path():
    root = Path(__file__).resolve().parents[1]
    experiment = root / "experiments" / "0903_EX04"
    manifest = json.loads((experiment / "candidate_manifest.json").read_text(encoding="utf-8"))
    protocol = EvaluationProtocol.from_dict(
        json.loads((experiment / "evaluation_protocol.json").read_text(encoding="utf-8"))
    )
    selected_ids = ("S001-v1", "R0539", "R0495", "R1289")
    selected = tuple(
        item for item in manifest["candidates"] if item["candidate_id"] in selected_ids
    )
    periods = tuple(
        (name, (pd.Timestamp(value["start"]), pd.Timestamp(value["end"])))
        for name, value in manifest["windows"].items()
    )
    context = CandidateEvaluationContext(
        RepositoryContext.discover(root, explicit_root=root),
        manifest["symbol"],
        manifest["asset_type"],
        periods,
        manifest["fee_rate"],
        manifest["init_cash"],
    )
    optimized = evaluate_candidate_payloads(
        context, protocol, selected, selected_ids, "SCREENING",
    )
    reference = _evaluate_candidate_payloads_reference(
        context, protocol, selected, selected_ids, "SCREENING",
    )
    assert [item.to_dict() for item in optimized] == [item.to_dict() for item in reference]


@pytest.mark.archive
def test_parallel_regime_candidates_match_reference_path():
    root = Path(__file__).resolve().parents[1]
    experiment = root / "experiments" / "0903_EX04"
    manifest = json.loads((experiment / "candidate_manifest.json").read_text(encoding="utf-8"))
    protocol = EvaluationProtocol.from_dict(
        json.loads((experiment / "evaluation_protocol.json").read_text(encoding="utf-8"))
    )
    selected = tuple(manifest["candidates"][:32])
    selected_ids = tuple(item["candidate_id"] for item in selected)
    full = manifest["windows"]["full"]
    periods = (("full", (pd.Timestamp(full["start"]), pd.Timestamp(full["end"]))),)
    repository = RepositoryContext.discover(root, explicit_root=root)
    parallel_context = CandidateEvaluationContext(
        repository, manifest["symbol"], manifest["asset_type"], periods,
        manifest["fee_rate"], manifest["init_cash"], 2,
    )
    reference_context = CandidateEvaluationContext(
        repository, manifest["symbol"], manifest["asset_type"], periods,
        manifest["fee_rate"], manifest["init_cash"], 1,
    )
    parallel = evaluate_candidate_payloads(
        parallel_context, protocol, selected, selected_ids, "SCREENING",
    )
    reference = _evaluate_candidate_payloads_reference(
        reference_context, protocol, selected, selected_ids, "SCREENING",
    )
    assert [item.to_dict() for item in parallel] == [item.to_dict() for item in reference]


def test_range_cycle_objectives_use_closed_range_to_range_trades():
    dates = pd.date_range("2026-01-01", periods=6, freq="D")
    orders = pd.DataFrame([
        {"signal_date": dates[0], "execution_date": dates[1], "side": "Buy", "size": 100, "price": 10.0, "fees": 0.0},
        {"signal_date": dates[2], "execution_date": dates[3], "side": "Sell", "size": 100, "price": 9.0, "fees": 0.0},
        {"signal_date": dates[3], "execution_date": dates[4], "side": "Buy", "size": 100, "price": 10.0, "fees": 0.0},
        {"signal_date": dates[4], "execution_date": dates[5], "side": "Sell", "size": 100, "price": 12.0, "fees": 0.0},
    ])
    regimes = pd.Series(["range", "range", "range", "range", "trend", "trend"], index=dates)
    values = dict(range_cycle_objectives(orders, regimes, dates))
    assert round(values["range_return"], 10) == -0.1
    assert values["range_trade_count"] == 1.0
    assert values["range_short_loss_count"] == 1.0
