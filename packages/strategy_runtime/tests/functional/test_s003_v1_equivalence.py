from __future__ import annotations

from dataclasses import asdict
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest
from dataflows import Dataflows, Dataset
from dataflows.local_strategy_data import fetch_strategy_feature_evidence

from strategy_runtime import (
    StrategyImplementation,
    StrategyInit,
    StrategyRelease,
    StrategyRuntime,
    TradableWindow,
)
from strategy_runtime.errors import RuntimeContractError
from strategy_runtime.loader import StrategyLoader


ROOT = Path(__file__).resolve().parents[4]
BASELINE_PATH = ROOT / "packages/strategy_runtime/tests/fixtures/s003_v1_equivalence.json"
SEED_PATH = (
    ROOT
    / "packages/strategy_runtime/src/strategy_runtime/resources/s003_v1_seed.csv.gz"
)
HISTORICAL_WINDOW = TradableWindow(date(2026, 9, 1), date(2026, 9, 8))
FORWARD_WINDOW = TradableWindow(date(2026, 9, 21), date(2026, 9, 21))


def _baseline() -> dict[str, object]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _release() -> tuple[dict[str, object], StrategyRelease]:
    raw = json.loads(
        (ROOT / "strategies/S003/versions/v1.json").read_text(encoding="utf-8")
    )
    return raw, StrategyRelease.from_mapping(raw)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _input_contract(definition) -> list[dict[str, object]]:
    return [
        {
            "name": item.name,
            "dataset": item.dataset,
            "subject": item.subject,
            "frequency": item.frequency,
            "lookback_sessions": item.lookback_sessions,
            "cutoff_rule": item.cutoff_rule.value,
            "maximum_staleness_days": item.maximum_staleness_days,
        }
        for item in definition.inputs.requirements
    ]


def _frozen_history(panel: pd.DataFrame, feature: dict[str, object]) -> pd.DataFrame:
    observed = panel["observed_moneyflow"].astype(str).str.lower().isin({"true", "1"})
    source = panel.copy()
    source["observed_weight"] = source["weight"].where(observed, 0.0)
    source["positive_weight"] = source["weight"].where(
        source["net_mf_amount"].gt(0), 0.0
    )
    expected = source.groupby("dt", sort=True).agg(
        total_weight=("weight", "sum"),
        observed_weight=("observed_weight", "sum"),
        positive_weight=("positive_weight", "sum"),
    )
    expected["observed_weight_ratio"] = (
        expected["observed_weight"] / expected["total_weight"]
    )
    expected["moneyflow_breadth"] = (
        expected["positive_weight"] / expected["observed_weight"]
    )
    lookback = int(feature["threshold_lookback_sessions"])
    expected["threshold"] = (
        expected["moneyflow_breadth"]
        .shift(1)
        .rolling(lookback, min_periods=lookback)
        .quantile(float(feature["threshold_quantile"]))
    )
    expected["target_position"] = (
        expected["moneyflow_breadth"].ge(expected["threshold"]).astype(float)
    )
    expected.index.name = "date"
    return expected


def test_frozen_s003_evidence_is_immutable() -> None:
    baseline = _baseline()
    source = baseline["frozen_source_evidence"]
    source_path = ROOT / source["path"]
    panel = pd.read_csv(source_path)
    assert _file_sha256(source_path) == source["sha256"]
    assert len(panel) == source["rows"]
    assert panel["dt"].nunique() == source["sessions"]

    formal = baseline["formal_execution_evidence"]
    evidence_root = ROOT / formal["directory"]
    for name, expected in formal["files"].items():
        path = evidence_root / name
        assert _file_sha256(path) == expected["sha256"]
        assert len(pd.read_csv(path)) == expected["rows"]

    summary = json.loads(
        (evidence_root / "formal_replay_summary.json").read_text(encoding="utf-8")
    )
    audit = json.loads((evidence_root / "audit.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert all(summary["checks"].values())
    assert summary["metrics"]["closed_trades"] == formal["semantic_counts"][
        "closed_trades"
    ]
    assert audit["status"] == "PASS"


def test_runtime_definition_preserves_frozen_s003_contract() -> None:
    baseline = _baseline()
    _raw, release = _release()
    strategy = StrategyLoader().load(release)
    definition = StrategyRuntime().describe(release)

    assert isinstance(strategy, StrategyImplementation)
    assert definition.release_id == baseline["strategy_reference"]
    assert definition.release_hash == baseline["release_hash"]
    assert definition.runtime_sha256 == baseline["resigned_runtime_sha256"]
    assert (
        definition.implementation.source_sha256
        == baseline["resigned_implementation_sha256"]
    )
    assert _input_contract(definition) == baseline["calculation_contract"]["inputs"]
    assert asdict(definition.decision) == baseline["calculation_contract"]["decision"]


def test_s003_derives_historical_and_forward_preparation_scopes() -> None:
    _raw, release = _release()
    strategy = StrategyLoader().load(release)
    calendar_dates = tuple(pd.bdate_range("2026-01-01", "2026-09-30").date)

    historical = strategy.derive_calculation_scope(HISTORICAL_WINDOW, calendar_dates)
    assert (historical.calculation_dates[0], historical.calculation_dates[-1]) == (
        date(2026, 6, 8),
        date(2026, 9, 7),
    )
    assert historical.inputs["constituent_moneyflow_seed"] is not None
    assert historical.inputs["incremental_constituent_weights"] is None
    assert historical.inputs["incremental_constituent_moneyflow"] is None

    forward = strategy.derive_calculation_scope(FORWARD_WINDOW, calendar_dates)
    assert (forward.calculation_dates[0], forward.calculation_dates[-1]) == (
        date(2026, 6, 26),
        date(2026, 9, 18),
    )
    assert (
        forward.inputs["constituent_moneyflow_seed"].start,
        forward.inputs["constituent_moneyflow_seed"].end,
    ) == (date(2026, 6, 26), date(2026, 9, 8))
    assert (
        forward.inputs["incremental_constituent_moneyflow"].start,
        forward.inputs["incremental_constituent_moneyflow"].end,
    ) == (date(2026, 9, 9), date(2026, 9, 18))


def test_s003_seed_is_compact_and_matches_every_frozen_decision() -> None:
    baseline = _baseline()
    raw, release = _release()
    strategy = StrategyLoader().load(release)
    seed = pd.read_csv(SEED_PATH)
    seed_identity = baseline["runtime_seed"]
    assert _file_sha256(SEED_PATH) == seed_identity["sha256"]
    assert len(seed) == seed_identity["rows"]
    assert seed["Date"].iloc[[0, -1]].tolist() == [
        seed_identity["first_session"],
        seed_identity["last_session"],
    ]

    source = ROOT / baseline["frozen_source_evidence"]["path"]
    panel = pd.read_csv(source)
    panel["dt"] = pd.to_datetime(panel["dt"]).dt.normalize()
    sessions = pd.DatetimeIndex(sorted(panel["dt"].unique()))
    actual = strategy.calculate_history({"constituent_moneyflow_seed": seed}, sessions)
    feature = raw["strategy_payload"]["rule"]["feature"]
    expected = _frozen_history(panel, feature)

    for column in (
        "observed_weight_ratio",
        "moneyflow_breadth",
        "threshold",
        "target_position",
    ):
        pd.testing.assert_series_equal(
            actual[column], expected[column], check_exact=False, rtol=1e-14, atol=1e-14
        )

    evidence_root = ROOT / baseline["formal_execution_evidence"]["directory"]
    frozen_decisions = pd.read_csv(evidence_root / "decisions.csv")
    actual_events = actual.index[actual["target_position"].eq(1.0)]
    expected_events = pd.DatetimeIndex(pd.to_datetime(frozen_decisions["signal_date"]))
    assert actual_events.tolist() == expected_events.tolist()
    assert len(actual_events) == baseline["formal_execution_evidence"][
        "semantic_counts"
    ]["event_signals"]


def test_s003_appends_one_session_from_seed_snapshot() -> None:
    _raw, release = _release()
    strategy = StrategyLoader().load(release)
    seed = pd.read_csv(SEED_PATH).tail(61).reset_index(drop=True)
    members = json.loads(seed.loc[seed.index[-1], "ConstituentsJson"])
    session = pd.Timestamp("2026-09-09")
    weights = pd.DataFrame(columns=["Date", "ConstituentSymbol", "Weight"])
    moneyflow = pd.DataFrame(
        {
            "Date": session,
            "Symbol": [item["symbol"] for item in members],
            "NetMoneyflowAmount": 1.0,
        }
    )

    actual = strategy.calculate_history(
        {
            "constituent_moneyflow_seed": seed,
            "incremental_constituent_weights": weights,
            "incremental_constituent_moneyflow": moneyflow,
        },
        pd.DatetimeIndex([session]),
    )

    assert actual.loc[session, "observed_weight_ratio"] == pytest.approx(1.0)
    assert actual.loc[session, "moneyflow_breadth"] == pytest.approx(1.0)
    assert bool(actual.loc[session, "signal_active"])


def test_s003_rejects_increment_below_frozen_coverage_gate() -> None:
    _raw, release = _release()
    strategy = StrategyLoader().load(release)
    seed = pd.read_csv(SEED_PATH).tail(61).reset_index(drop=True)
    members = json.loads(seed.loc[seed.index[-1], "ConstituentsJson"])
    session = pd.Timestamp("2026-09-09")
    moneyflow = pd.DataFrame(
        {
            "Date": session,
            "Symbol": [item["symbol"] for item in members[:400]],
            "NetMoneyflowAmount": 1.0,
        }
    )

    with pytest.raises(RuntimeContractError, match="coverage is below"):
        strategy.calculate_history(
            {
                "constituent_moneyflow_seed": seed,
                "incremental_constituent_weights": pd.DataFrame(
                    columns=["Date", "ConstituentSymbol", "Weight"]
                ),
                "incremental_constituent_moneyflow": moneyflow,
            },
            pd.DatetimeIndex([session]),
        )


def test_historical_prepare_uses_seed_without_incremental_publications(
    tmp_path, monkeypatch
) -> None:
    requests = []

    def evidence(request):
        requests.append(request)
        return fetch_strategy_feature_evidence(request)

    def market(request):
        requests.append(request)
        dates = pd.bdate_range(request.start, request.end)
        return pd.DataFrame(
            {
                "Date": dates,
                "Open": 8.0,
                "High": 8.1,
                "Low": 7.9,
                "Close": 8.0,
                "Volume": 1000.0,
                "Amount": 8000.0,
            }
        ), {
            "vendor": "s003-equivalence",
            "adjustment": "none" if "unadjusted" in request.dataset else "hfq",
            "primary_key": ["Date"],
        }

    def calendar(request):
        requests.append(request)
        dates = pd.date_range(request.start, request.end)
        return pd.DataFrame(
            {"Date": dates, "IsOpen": (dates.dayofweek < 5).astype(int)}
        ), {"vendor": "s003-equivalence", "primary_key": ["Date"]}

    flows = Dataflows(
        {
            Dataset.STRATEGY_FEATURE_EVIDENCE.value: evidence,
            Dataset.ETF_OHLCV.value: market,
            Dataset.ETF_UNADJUSTED_DAILY.value: market,
            Dataset.TRADING_CALENDAR.value: calendar,
        }
    )
    monkeypatch.setattr("strategy_runtime.preparation.Dataflows", lambda: flows)
    _raw, release = _release()
    instance = StrategyRuntime().create(
        StrategyInit(release, HISTORICAL_WINDOW, tmp_path)
    )

    prepared = instance.prepare_data()

    assert prepared.available_through == date(2026, 9, 7)
    assert {request.dataset for request in requests} == {
        Dataset.STRATEGY_FEATURE_EVIDENCE.value,
        Dataset.ETF_OHLCV.value,
        Dataset.ETF_UNADJUSTED_DAILY.value,
        Dataset.TRADING_CALENDAR.value,
    }
    assert (
        tmp_path
        / "preparations"
        / f"{HISTORICAL_WINDOW.start:%Y%m%d}_{HISTORICAL_WINDOW.end:%Y%m%d}"
        / "prepared-data.json"
    ).is_file()
    assert len(instance.inspect_signals()) == len(
        pd.bdate_range(HISTORICAL_WINDOW.start, HISTORICAL_WINDOW.end)
    )
