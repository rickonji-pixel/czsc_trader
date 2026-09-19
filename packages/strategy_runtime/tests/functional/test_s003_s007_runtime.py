from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pandas as pd
import pytest
from dataflows import DataIdentity, DataRequest, DataResult, DataStatus, Dataset

from czsc_trader.baselines import resolve_strategy_payload
from czsc_trader.causal_feature_gate_runtime import score_feature_panel
from strategy_runtime import (
    AccountSnapshot,
    CalculationRequest,
    DeploymentSpec,
    PublicationStatus,
    PublishedStrategyData,
    StrategyLoader,
    StrategyRelease,
    StrategyStateSnapshot,
)
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


def test_s007_complete_decision_history_matches_frozen_legacy_path() -> None:
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

    resolved = resolve_strategy_payload(
        ROOT / "strategies/dependencies/legacy_rule_baselines",
        raw["strategy_payload"],
        release_id=release.release_id,
        release_hash=release.release_hash,
        symbol="588080.SH",
        repository_root=ROOT,
    )
    expected_base, expected_confirmation, expected_target = score_feature_panel(
        panel, resolved.causal_feature_gate
    )
    pd.testing.assert_series_equal(actual["base_score"], expected_base)
    pd.testing.assert_series_equal(actual["confirmation_score"], expected_confirmation)
    pd.testing.assert_series_equal(
        actual["target_position"], expected_target.astype(int), check_names=False
    )
    pd.testing.assert_series_equal(
        runtime_actual["target_position"], expected_target.astype(int), check_names=False
    )
    assert strategy.definition.release_id == "S007-v1"


def test_s007_live_calculation_uses_the_same_frozen_evidence_as_history() -> None:
    raw, release = _release("S007")
    strategy = StrategyLoader().load(release)
    evidence = pd.read_csv(
        ROOT / "experiments/S007/20260915_S007_EX04/artifacts/causal_feature_panel.csv.gz"
    )
    evidence["date"] = pd.to_datetime(evidence["date"]).dt.normalize()
    panel = evidence.set_index("date").sort_index()
    cutoff = panel.index.max()
    expected = calculate_s007_history(
        panel,
        raw["strategy_payload"]["rule"]["normalization"],
        raw["strategy_payload"]["rule"]["score"],
    ).loc[cutoff]
    frames = {
        "adjusted_daily": pd.DataFrame({"Date": panel.index}),
        "shibor_daily": pd.DataFrame({"Date": [cutoff]}),
        "chinext_daily_basic": pd.DataFrame({"Date": [cutoff]}),
        "etf_share_size": pd.DataFrame({"Date": [cutoff]}),
        "spx_daily": pd.DataFrame({"Date": [cutoff]}),
        "execution_daily": pd.DataFrame({"Date": [cutoff], "Close": [1.0]}),
        "trading_calendar": pd.DataFrame(
            {"Date": [cutoff, cutoff + pd.Timedelta(days=1)], "IsOpen": [1, 1]}
        ),
        "strategy_evidence": evidence,
    }
    input_results = {
        name: DataResult(
            DataStatus.READY,
            frame,
            DataIdentity(
                "test.dataset",
                "test",
                release.release_id,
                str(cutoff.date()),
                str(cutoff.date()),
                sha256(name.encode()).hexdigest(),
                {},
            ),
        )
        for name, frame in frames.items()
    }
    publication = PublishedStrategyData(
        release.release_id,
        release.release_hash,
        PublicationStatus.READY,
        str(cutoff.date()),
        {
            name: DataRequest(
                "test.dataset",
                release.release_id,
                str(cutoff.date()),
                str(cutoff.date()),
                None,
                "daily",
            )
            for name in frames
        },
        input_results,
    )
    now = datetime(2026, 9, 2, 20, 30, tzinfo=timezone(timedelta(hours=8)))
    deployment = DeploymentSpec(
        "dep-s007-v1",
        release.release_id,
        release.release_hash,
        "588080.SH",
        "s007-v1",
        "futu_simulate_cn",
    )
    decision = strategy.calculate(
        CalculationRequest(
            deployment,
            publication,
            AccountSnapshot("s007-v1", 100_000, 100_000, 0, 0, now),
            StrategyStateSnapshot(
                "dep-s007-v1", release.release_hash, 0, now, {}
            ),
            now,
        )
    )

    assert decision.target_position == float(expected["target_position"])
    assert decision.evidence["base_score"] == float(expected["base_score"])
    assert decision.evidence["confirmation_score"] == float(
        expected["confirmation_score"]
    )


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


def test_s007_publication_resolves_previous_session_by_position() -> None:
    _raw, release = _release("S007")
    strategy = StrategyLoader().load(release)
    requests = []

    class Flows:
        def fetch(self, request):
            requests.append(request)
            if str(request.dataset) == Dataset.TRADING_CALENDAR.value:
                frame = pd.DataFrame(
                    {
                        "Date": ["2026-09-15", "2026-09-16", "2026-09-17"],
                        "IsOpen": [1, 1, 1],
                    }
                )
            else:
                frame = pd.DataFrame({"Date": [request.end], "Value": [1.0]})
            identity = DataIdentity(
                str(request.dataset),
                "test",
                request.symbol,
                str(frame["Date"].min()),
                str(frame["Date"].max()),
                sha256(str(request).encode()).hexdigest(),
                {},
            )
            return DataResult(DataStatus.READY, frame, identity)

    publication = strategy.publish_data(
        Flows(),
        DeploymentSpec(
            "test",
            release.release_id,
            release.release_hash,
            "588080.SH",
            "test",
            "test",
            {},
        ),
        datetime(2026, 9, 16, 20, 30, tzinfo=timezone(timedelta(hours=8))),
    )

    shares = next(
        request
        for request in requests
        if str(request.dataset) == Dataset.ETF_SHARE_SIZE.value
    )
    assert shares.required_cutoff == "2026-09-15"
    assert shares.start == "2021-01-04"
    spx = next(
        request
        for request in requests
        if str(request.dataset) == Dataset.GLOBAL_INDEX_DAILY.value
    )
    assert spx.start == "2020-12-01"
    assert all(
        str(request.dataset) != Dataset.STRATEGY_FEATURE_EVIDENCE.value
        for request in requests
    )
    assert publication.requested_cutoff == "2026-09-16"
