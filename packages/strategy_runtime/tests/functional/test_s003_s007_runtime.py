from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from strategy_runtime import StrategyRelease
from strategy_runtime.loader import StrategyLoader
from strategy_runtime.strategies.s003_v1 import calculate_s003_history
from strategy_runtime.strategies.s007_v1 import (
    calculate_s007_history,
    materialize_s007_features,
    resolve_s007_feature_panel,
)


ROOT = Path(__file__).resolve().parents[4]


def _release(family: str) -> tuple[dict[str, object], StrategyRelease]:
    raw = json.loads((ROOT / f"strategies/{family}/versions/v1.json").read_text(encoding="utf-8"))
    return raw, StrategyRelease.from_mapping(raw)


def test_s003_complete_decision_history_matches_frozen_evidence() -> None:
    raw, release = _release("S003")
    strategy = StrategyLoader().load(release)
    panel = pd.read_csv(
        ROOT / "experiments/S003/20260911_S003_EX43/artifacts/constituent_moneyflow_panel.csv.gz"
    )
    panel["dt"] = pd.to_datetime(panel["dt"]).dt.normalize()
    panel["snapshot_date"] = pd.to_datetime(panel["snapshot_date"]).dt.normalize()
    weights = (
        panel[["snapshot_date", "con_code", "weight"]]
        .drop_duplicates(["snapshot_date", "con_code"], keep="last")
        .rename(
            columns={
                "snapshot_date": "Date",
                "con_code": "ConstituentSymbol",
                "weight": "Weight",
            }
        )
    )
    observed = panel["observed_moneyflow"].astype(str).str.lower().isin({"true", "1"})
    moneyflow = panel.loc[observed, ["dt", "con_code", "net_mf_amount"]].rename(
        columns={
            "dt": "Date",
            "con_code": "Symbol",
            "net_mf_amount": "NetMoneyflowAmount",
        }
    )
    feature = raw["strategy_payload"]["rule"]["feature"]
    actual = calculate_s003_history(
        weights,
        moneyflow,
        pd.DatetimeIndex(sorted(panel["dt"].unique())),
        feature,
    )

    direct = panel.copy()
    direct["observed_weight"] = direct["weight"].where(observed, 0.0)
    direct["positive_weight"] = direct["weight"].where(direct["net_mf_amount"].gt(0), 0.0)
    expected = direct.groupby("dt", sort=True).agg(
        total_weight=("weight", "sum"),
        observed_weight=("observed_weight", "sum"),
        positive_weight=("positive_weight", "sum"),
    )
    expected["observed_weight_ratio"] = expected["observed_weight"] / expected["total_weight"]
    expected["moneyflow_breadth"] = expected["positive_weight"] / expected["observed_weight"]
    expected.loc[
        expected["observed_weight_ratio"].lt(feature["minimum_observed_weight_ratio"]),
        "moneyflow_breadth",
    ] = pd.NA
    expected["threshold"] = (
        expected["moneyflow_breadth"]
        .shift(1)
        .rolling(
            feature["threshold_lookback_sessions"],
            min_periods=feature["threshold_lookback_sessions"],
        )
        .quantile(feature["threshold_quantile"])
    )
    expected["target_position"] = (
        expected["moneyflow_breadth"].ge(expected["threshold"]).astype(float)
    )
    expected.index.name = "date"

    pd.testing.assert_series_equal(actual["moneyflow_breadth"], expected["moneyflow_breadth"])
    pd.testing.assert_series_equal(actual["threshold"], expected["threshold"])
    pd.testing.assert_series_equal(actual["target_position"], expected["target_position"])
    assert strategy.definition.release_id == "S003-v1"


def test_s007_strategy_and_history_api_produce_the_same_decisions() -> None:
    raw, release = _release("S007")
    strategy = StrategyLoader().load(release)
    panel = pd.read_csv(
        ROOT / "experiments/S007/20260915_S007_EX04/artifacts/causal_feature_panel.csv.gz"
    )
    panel["date"] = pd.to_datetime(panel["date"]).dt.normalize()
    panel = panel.set_index("date").sort_index()
    rule = raw["strategy_payload"]["rule"]
    actual = calculate_s007_history(panel, rule["normalization"], rule["score"])
    evidence = panel.loc[:, sorted(rule["score"]["orientations"])].copy().reset_index()
    runtime_actual = strategy.calculate_history(
        {"strategy_evidence": evidence}, panel.index
    )

    pd.testing.assert_series_equal(
        runtime_actual["base_score"], actual["base_score"], check_names=False
    )
    pd.testing.assert_series_equal(
        runtime_actual["confirmation_score"], actual["confirmation_score"], check_names=False
    )
    pd.testing.assert_series_equal(
        runtime_actual["target_position"], actual["target_position"], check_names=False
    )
    assert strategy.definition.release_id == "S007-v1"


def test_s007_materialized_history_preserves_frozen_start_boundary() -> None:
    sessions = pd.to_datetime(["2020-12-31", "2021-01-04"])
    market = pd.DataFrame(
        {
            "Date": sessions,
            "Close": [1.0, 1.1],
            "High": [1.1, 1.2],
            "Low": [0.9, 1.0],
            "Volume": [100.0, 110.0],
            "Amount": [100.0, 121.0],
        }
    )
    inputs = {
        "adjusted_daily": market,
        "shibor_daily": pd.DataFrame(
            {"Date": sessions, "OvernightRate": [2.0, 2.1]}
        ),
        "chinext_daily_basic": pd.DataFrame(
            {"Date": sessions, "TurnoverRateFreeFloat": [1.0, 1.1]}
        ),
        "etf_share_size": pd.DataFrame(
            {"Date": sessions, "TotalShare": [100.0, 101.0]}
        ),
        "spx_daily": pd.DataFrame(
            {
                "Date": pd.to_datetime(["2020-12-30", "2021-01-01"]),
                "PercentChange": [0.1, 0.2],
            }
        ),
    }

    actual = materialize_s007_features(inputs)

    assert actual.index.tolist() == [pd.Timestamp("2021-01-04")]


def test_s007_feature_panel_appends_dfLS_rows_only_after_frozen_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw, _release_record = _release("S007")
    features = sorted(raw["strategy_payload"]["rule"]["score"]["orientations"])
    dates = pd.date_range("2026-09-01", periods=3, freq="D")
    evidence = pd.DataFrame({"Date": dates[:2], **{name: [1.0, 1.0] for name in features}})
    materialized = pd.DataFrame(
        {name: [9.0, 9.0, 2.0] for name in features}, index=dates
    )
    monkeypatch.setattr(
        "strategy_runtime.strategies.s007_v1.materialize_s007_features",
        lambda _inputs: materialized,
    )

    actual = resolve_s007_feature_panel(
        {"strategy_evidence": evidence},
        raw["strategy_payload"]["rule"]["score"],
        dates,
    )

    assert actual.loc[dates[0], features[0]] == 1.0
    assert actual.loc[dates[1], features[0]] == 1.0
    assert actual.loc[dates[2], features[0]] == 2.0
