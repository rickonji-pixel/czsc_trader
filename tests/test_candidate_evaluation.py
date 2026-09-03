from types import SimpleNamespace

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.candidate_evaluation import CandidateEvaluationContext, evaluate_candidate_payloads
from czsc_trader.range_diagnostics import range_cycle_objectives
from strategy_evaluator import EvaluationProtocol


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
    result = SimpleNamespace(equity=equity, orders=orders, factor_events=pd.DataFrame())
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
