from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from czsc_trader.audit import audit_no_lookahead
from czsc_trader.backtest import run_period_backtests
from czsc_trader.position_risk import (
    RiskOverlaySpec,
    build_family_specs,
    build_position_risk_events,
    build_pressure_state,
    build_risk_features,
    compose_overlay_target,
)
from czsc_trader.position_risk_runner import (
    build_hazard_diagnostics,
    _metric_row,
    frozen_spec_payload,
    risk_spec_from_frozen,
    permitted_position_risk_cutoff,
    rank_eligible_position_risk,
    run_position_risk_program,
    validate_position_risk_protocol,
)
from czsc_trader.research.registry import build_default_registry


REPO_ROOT = Path(__file__).resolve().parents[1]


def _daily_fixture(days: int = 30) -> pd.DataFrame:
    dates = pd.bdate_range("2021-01-04", periods=days)
    close = np.linspace(10.0, 13.0, days)
    return pd.DataFrame(
        {
            "dt": dates,
            "open": close - 0.05,
            "high": close + 0.20,
            "low": close - 0.20,
            "close": close,
            "vol": np.full(days, 1000.0),
            "amount": close * 1000.0,
        }
    )


def _intraday_fixture(daily: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for row in daily.itertuples(index=False):
        day = pd.Timestamp(row.dt)
        rows.extend(
            [
                {
                    "dt": day + pd.Timedelta(hours=10),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close) - 0.05,
                    "vol": 750.0,
                    "amount": float(row.close) * 750.0,
                },
                {
                    "dt": day + pd.Timedelta(hours=14),
                    "open": float(row.close) - 0.05,
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "vol": 250.0,
                    "amount": float(row.close) * 250.0,
                },
            ]
        )
    return pd.DataFrame(rows)


def test_risk_features_exclude_current_bar_from_prior_high() -> None:
    daily = _daily_fixture()
    intraday = _intraday_fixture(daily)
    signal_day = pd.Timestamp(daily.loc[20, "dt"])

    features = build_risk_features(daily, intraday)

    expected = daily.set_index("dt").loc[: daily.loc[19, "dt"], "high"].tail(20).max()
    assert features.loc[signal_day, "prior_high_20"] == pytest.approx(expected)
    assert features.loc[signal_day, "prior_high_20"] < daily.loc[20, "high"]


def test_risk_features_include_only_completed_returns_through_signal_day() -> None:
    daily = _daily_fixture()
    daily["close"] = 10.0 + np.array(
        [0, 1, -1, 1, -1] * 6, dtype=float
    ).cumsum()
    daily["open"] = daily["close"]
    daily["high"] = daily["close"] + 0.2
    daily["low"] = daily["close"] - 0.2
    intraday = _intraday_fixture(daily)
    signal_day = pd.Timestamp(daily.loc[20, "dt"])

    features = build_risk_features(daily, intraday)

    indexed = daily.set_index("dt")
    returns = indexed["close"].pct_change()
    assert features.loc[signal_day, "ret_1d"] == pytest.approx(
        returns.loc[signal_day]
    )
    assert features.loc[signal_day, "sma_5"] == pytest.approx(
        indexed.loc[:signal_day, "close"].tail(5).mean()
    )
    assert features.loc[signal_day, "negative_count_5"] == pytest.approx(
        float((returns.loc[:signal_day].tail(5) < 0).sum())
    )


def test_intraday_pressure_uses_completed_same_day_bars() -> None:
    daily = _daily_fixture()
    signal_day = pd.Timestamp(daily.loc[20, "dt"])
    daily.loc[20, ["open", "high", "low", "close"]] = [9.8, 10.0, 9.0, 9.2]
    intraday = _intraday_fixture(daily)
    day_mask = intraday["dt"].dt.normalize().eq(signal_day)
    intraday.loc[day_mask, ["open", "close", "vol"]] = [
        [10.0, 9.0, 750.0],
        [9.0, 9.2, 250.0],
    ]

    features = build_risk_features(daily, intraday)
    original = features.loc[signal_day, ["close_location", "down_volume_share"]]
    mutated_intraday = intraday.copy()
    future_day = pd.Timestamp(daily.loc[21, "dt"])
    future_mask = mutated_intraday["dt"].dt.normalize().eq(future_day)
    mutated_intraday.loc[future_mask, ["open", "close", "vol"]] = [
        [20.0, 1.0, 1_000_000.0],
        [20.0, 1.0, 1_000_000.0],
    ]
    mutated_intraday.loc[future_mask, "high"] = 20.0
    mutated_intraday.loc[future_mask, "low"] = 1.0
    mutated = build_risk_features(daily, mutated_intraday)

    assert original["close_location"] == pytest.approx(0.20)
    assert original["down_volume_share"] == pytest.approx(0.75)
    pd.testing.assert_series_equal(original, mutated.loc[signal_day, original.index])


def test_risk_features_define_neutral_zero_range_and_zero_volume() -> None:
    daily = _daily_fixture()
    signal_day = pd.Timestamp(daily.loc[20, "dt"])
    daily.loc[20, ["open", "high", "low", "close"]] = [10.0, 10.0, 10.0, 10.0]
    intraday = _intraday_fixture(daily)
    day_mask = intraday["dt"].dt.normalize().eq(signal_day)
    intraday.loc[day_mask, ["open", "high", "low", "close", "vol"]] = [
        [10.0, 10.0, 10.0, 10.0, 0.0],
        [10.0, 10.0, 10.0, 10.0, 0.0],
    ]

    features = build_risk_features(daily, intraday)

    assert features.loc[signal_day, "close_location"] == pytest.approx(0.5)
    assert features.loc[signal_day, "down_volume_share"] == pytest.approx(0.0)


@pytest.mark.parametrize("problem", ["duplicate_dt", "nonpositive_close"])
def test_risk_features_reject_invalid_daily_data(problem: str) -> None:
    daily = _daily_fixture()
    if problem == "duplicate_dt":
        daily.loc[1, "dt"] = daily.loc[0, "dt"]
    else:
        daily.loc[1, "close"] = 0.0

    with pytest.raises(ValueError):
        build_risk_features(daily, _intraday_fixture(_daily_fixture()))


def test_family_specs_build_exact_preregistered_grids() -> None:
    trend = build_family_specs("trend_damage")
    persistence = build_family_specs("negative_persistence")
    intraday = build_family_specs("intraday_pressure")

    assert len(trend) == 12
    assert len(persistence) == 6
    assert len(intraday) == 8
    all_specs = trend + persistence + intraday
    assert len({spec.candidate_id for spec in all_specs}) == 26
    assert {spec.pressure_position for spec in all_specs} == {0.5, 0.75}
    assert trend[0].candidate_id == "trend_L20_D0.06_P0.50"
    assert persistence[0].candidate_id == "persist_K5_N4_P0.50"
    assert intraday[0].candidate_id == "intraday_C0.25_V0.60_P0.50"
    with pytest.raises(ValueError):
        build_family_specs("unknown")


def _state_frame() -> pd.DataFrame:
    index = pd.bdate_range("2021-01-04", periods=8)
    return pd.DataFrame(
        {
            "close": [10.0, 8.9, 8.8, 9.1, 9.3, 9.5, 9.6, 9.7],
            "ret_1d": [np.nan, -0.11, -0.01, 0.03, 0.02, 0.02, 0.01, 0.01],
            "sma_5": [10.0, 9.5, 9.4, 9.3, 9.2, 9.3, 9.4, 9.5],
            "sma_20": [10.0, 9.5, 9.4, 9.0, 9.1, 9.2, 9.3, 9.4],
            "prior_high_20": [10.0] * 8,
            "prior_high_60": [10.0] * 8,
            "prior_high_120": [10.0] * 8,
            "negative_count_5": [0, 4, 4, 3, 2, 1, 0, 0],
            "negative_count_10": [0, 7, 7, 6, 5, 4, 3, 2],
            "negative_count_20": [0, 13, 13, 12, 11, 10, 9, 8],
            "close_location": [0.5, 0.2, 0.2, 0.5, 0.5, 0.5, 0.5, 0.5],
            "down_volume_share": [0.0, 0.75, 0.75, 0.2, 0.2, 0.2, 0.2, 0.2],
        },
        index=index,
    )


def test_trend_pressure_persists_until_twenty_day_mean_recovery() -> None:
    features = _state_frame()
    champion = pd.Series([0, 1, 1, 1, 1, 1, 1, 0], index=features.index)
    spec = RiskOverlaySpec(
        "trend_damage", 0.5, (("lookback", 60.0), ("drawdown", 0.10))
    )

    pressure = build_pressure_state(features, champion, spec)
    target = compose_overlay_target(champion, pressure, spec.pressure_position)

    assert pressure.tolist() == [False, True, True, False, False, False, False, False]
    assert target.tolist() == [0.0, 0.5, 0.5, 1.0, 1.0, 1.0, 1.0, 0.0]


def test_persistence_pressure_requires_two_positive_days_to_recover() -> None:
    features = _state_frame()
    champion = pd.Series([0, 1, 1, 1, 1, 1, 1, 0], index=features.index)
    spec = RiskOverlaySpec(
        "negative_persistence", 0.75, (("window", 10.0), ("negative_count", 7.0))
    )

    pressure = build_pressure_state(features, champion, spec)

    assert pressure.tolist() == [False, True, True, True, False, False, False, False]


def test_intraday_pressure_requires_two_nonpressure_recovery_days() -> None:
    features = _state_frame()
    champion = pd.Series([0, 1, 1, 1, 1, 1, 1, 0], index=features.index)
    spec = RiskOverlaySpec(
        "intraday_pressure",
        0.5,
        (("close_location", 0.25), ("down_volume_share", 0.70)),
    )

    pressure = build_pressure_state(features, champion, spec)

    assert pressure.tolist() == [False, True, True, True, False, False, False, False]


def test_position_risk_events_preserve_transition_provenance() -> None:
    features = _state_frame()
    index = features.index[:5]
    champion = pd.Series([0, 1, 1, 1, 0], index=index, dtype=float)
    pressure = pd.Series([False, False, True, False, False], index=index)
    target = compose_overlay_target(champion, pressure, 0.5)
    scores = pd.Series(np.linspace(0.0, 0.4, len(index)), index=index)
    spec = RiskOverlaySpec(
        "trend_damage", 0.5, (("lookback", 60.0), ("drawdown", 0.10))
    )

    events = build_position_risk_events(
        target, champion, scores, features.loc[index], pressure, spec
    )

    assert events["event_type"].tolist() == ["Entry", "Reduce", "Increase", "Exit"]
    assert events["before_position"].tolist() == [0.0, 1.0, 0.5, 1.0]
    assert events["after_position"].tolist() == [1.0, 0.5, 1.0, 0.0]
    assert events["candidate_id"].eq(spec.candidate_id).all()
    assert events.loc[1, "factor_score"] == pytest.approx(scores.iloc[2])
    assert events.loc[1, "close"] == pytest.approx(features.iloc[2]["close"])


def test_position_risk_events_execute_at_next_open_and_pass_audit() -> None:
    daily = _daily_fixture(8)
    index = pd.DatetimeIndex(daily["dt"])
    features = _state_frame().reindex(index)
    champion = pd.Series([0, 1, 1, 1, 0, 0, 0, 0], index=index, dtype=float)
    pressure = pd.Series(
        [False, False, True, False, False, False, False, False], index=index
    )
    target = compose_overlay_target(champion, pressure, 0.5)
    scores = pd.Series(np.linspace(0.0, 0.7, len(index)), index=index)
    spec = RiskOverlaySpec(
        "trend_damage", 0.5, (("lookback", 60.0), ("drawdown", 0.10))
    )
    events = build_position_risk_events(
        target, champion, scores, features, pressure, spec
    )
    factor_frame = features.assign(factor_score=scores)

    result = run_period_backtests(
        daily,
        target,
        {"TEST": (index[1], index[-1])},
        factor_events=events,
        factor_frame=factor_frame,
    )["TEST"]
    audit = audit_no_lookahead(result.orders, events, target, factor_frame)

    assert result.orders["side"].tolist() == ["Buy", "Sell", "Buy", "Sell"]
    assert result.orders["event_type"].tolist() == [
        "Entry",
        "Reduce",
        "Increase",
        "Exit",
    ]
    assert audit["status"] == "PASS"


@pytest.mark.parametrize("experiment_id", [f"0830_EX0{n}" for n in range(3, 8)])
def test_five_round_protocols_match_exact_preregistration(experiment_id: str) -> None:
    path = REPO_ROOT / "experiments" / experiment_id / "artifacts" / "protocol.json"
    protocol = json.loads(path.read_text(encoding="utf-8"))

    validate_position_risk_protocol(protocol, experiment_id)

    protocol["fee_rate"] = 0.001
    with pytest.raises(ValueError):
        validate_position_risk_protocol(protocol, experiment_id)


def test_position_risk_protocol_rejects_grid_and_identity_mutations() -> None:
    path = REPO_ROOT / "experiments" / "0830_EX04" / "artifacts" / "protocol.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    mutations = (
        ("lookbacks", [20, 40, 120]),
        ("candidate_ranking", list(reversed(original["candidate_ranking"]))),
        ("discovery_end", "2024-12-31"),
        ("program_round", 3),
    )
    for key, value in mutations:
        protocol = json.loads(json.dumps(original))
        protocol[key] = value
        with pytest.raises(ValueError, match=key):
            validate_position_risk_protocol(protocol, "0830_EX04")


def test_rank_eligible_position_risk_applies_hard_gate_and_stable_order() -> None:
    rows = pd.DataFrame(
        [
            {
                "candidate_id": "return_fail",
                "full_champion_return": 0.20,
                "full_challenger_return": 0.19,
                "full_champion_max_drawdown": -0.20,
                "full_challenger_max_drawdown": -0.10,
                "max_drawdown_improvement": 0.10,
                "worst_annual_return_delta": 0.01,
                "full_return_delta": -0.01,
                "transition_count": 1,
            },
            {
                "candidate_id": "drawdown_fail",
                "full_champion_return": 0.20,
                "full_challenger_return": 0.21,
                "full_champion_max_drawdown": -0.20,
                "full_challenger_max_drawdown": -0.20,
                "max_drawdown_improvement": 0.0,
                "worst_annual_return_delta": 0.01,
                "full_return_delta": 0.01,
                "transition_count": 1,
            },
            {
                "candidate_id": "eligible_b",
                "full_champion_return": 0.20,
                "full_challenger_return": 0.22,
                "full_champion_max_drawdown": -0.20,
                "full_challenger_max_drawdown": -0.15,
                "max_drawdown_improvement": 0.05,
                "worst_annual_return_delta": 0.00,
                "full_return_delta": 0.02,
                "transition_count": 4,
            },
            {
                "candidate_id": "eligible_a",
                "full_champion_return": 0.20,
                "full_challenger_return": 0.22,
                "full_champion_max_drawdown": -0.20,
                "full_challenger_max_drawdown": -0.15,
                "max_drawdown_improvement": 0.05,
                "worst_annual_return_delta": 0.00,
                "full_return_delta": 0.02,
                "transition_count": 4,
            },
        ]
    )

    ranked = rank_eligible_position_risk(rows)

    assert ranked["candidate_id"].tolist() == ["eligible_a", "eligible_b"]


def test_position_risk_cutoffs_enforce_program_boundaries() -> None:
    protocols = {
        experiment_id: json.loads(
            (
                REPO_ROOT
                / "experiments"
                / experiment_id
                / "artifacts"
                / "protocol.json"
            ).read_text(encoding="utf-8")
        )
        for experiment_id in ("0830_EX03", "0830_EX04", "0830_EX07")
    }

    assert permitted_position_risk_cutoff(protocols["0830_EX03"], "discovery") == pd.Timestamp("2023-12-31")
    assert permitted_position_risk_cutoff(protocols["0830_EX04"], "discovery") == pd.Timestamp("2023-12-31")
    assert permitted_position_risk_cutoff(protocols["0830_EX07"], "validation") == pd.Timestamp("2025-12-31")
    assert permitted_position_risk_cutoff(protocols["0830_EX07"], "historical") == pd.Timestamp("2026-08-28")
    with pytest.raises(ValueError):
        permitted_position_risk_cutoff(protocols["0830_EX04"], "validation")


def test_risk_spec_frozen_payload_round_trips_exactly() -> None:
    spec = build_family_specs("intraday_pressure")[3]

    payload = frozen_spec_payload(spec)
    restored = risk_spec_from_frozen(payload)

    assert restored == spec
    assert payload["candidate_id"] == spec.candidate_id


def test_hazard_diagnostics_only_use_held_days_with_complete_horizons() -> None:
    features = _state_frame()
    index = features.index
    daily = pd.DataFrame(
        {
            "dt": index,
            "open": np.arange(10.0, 18.0),
            "high": np.arange(10.5, 18.5),
            "low": np.arange(9.5, 17.5),
            "close": np.arange(10.2, 18.2),
            "vol": 1000.0,
            "amount": 10000.0,
        }
    )
    champion = pd.Series([0, 1, 1, 1, 0, 0, 0, 0], index=index, dtype=float)
    protocol = json.loads(
        (
            REPO_ROOT
            / "experiments"
            / "0830_EX03"
            / "artifacts"
            / "protocol.json"
        ).read_text(encoding="utf-8")
    )

    events, summary, overlap = build_hazard_diagnostics(
        features, daily, champion, protocol
    )

    assert set(summary["family"]) == {
        "trend_damage",
        "negative_persistence",
        "intraday_pressure",
    }
    assert events["signal_date"].isin(index[champion.eq(1.0)]).all()
    assert events["horizon"].eq(5).all()
    assert set(overlap.columns) == {"left_family", "right_family", "overlap_count"}


def test_position_risk_program_handler_is_registered() -> None:
    protocol = json.loads(
        (
            REPO_ROOT
            / "experiments"
            / "0830_EX03"
            / "artifacts"
            / "protocol.json"
        ).read_text(encoding="utf-8")
    )

    handler = build_default_registry().resolve(protocol, "0830_EX03")

    assert handler.handler_id == "position_risk_five_rounds"


def test_round_five_without_family_winners_fails_before_loading_data(
    tmp_path: Path,
) -> None:
    experiments_root = tmp_path / "experiments"
    experiment_dir = experiments_root / "0830_EX07"
    source = REPO_ROOT / "experiments" / "0830_EX07"
    (experiment_dir / "artifacts").mkdir(parents=True)
    for name in ("01_goal.md", "02_design.md", "implementation_plan.md"):
        shutil.copy2(source / name, experiment_dir / name)
    shutil.copy2(
        source / "artifacts" / "protocol.json",
        experiment_dir / "artifacts" / "protocol.json",
    )

    result = run_position_risk_program(
        tmp_path / "missing-raw",
        REPO_ROOT / "configs" / "rule_baselines",
        experiments_root,
        experiment_dir,
        execution_commit="deadbeef",
    )

    manifest = json.loads(
        (experiment_dir / "experiment_manifest.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "FAIL"
    assert manifest["validation_accessed"] is False
    assert manifest["historical_check_accessed"] is False
    assert not (experiment_dir / "artifacts" / "validation_metrics.csv").exists()


def test_historical_metric_row_accepts_no_annual_windows() -> None:
    metrics = {
        "strategy_return": 0.10,
        "max_drawdown": -0.20,
        "sharpe": 1.0,
        "exposure": 0.5,
        "trade_count": 2,
    }
    challenger_metrics = {**metrics, "strategy_return": 0.11, "max_drawdown": -0.19}
    champion = {"2026FULL": SimpleNamespace(metrics=metrics)}
    challenger = {"2026FULL": SimpleNamespace(metrics=challenger_metrics)}

    row = _metric_row(
        build_family_specs("trend_damage")[0],
        champion,
        challenger,
        full_name="2026FULL",
        annual_names=(),
        transition_count=2,
    )

    assert np.isnan(row["worst_annual_return_delta"])
    assert row["full_return_delta"] == pytest.approx(0.01)
