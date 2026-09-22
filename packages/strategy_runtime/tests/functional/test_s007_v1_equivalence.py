from __future__ import annotations

from dataclasses import asdict
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from dataflows import Dataflows, Dataset
from dataflows.local_strategy_data import fetch_strategy_feature_evidence

from strategy_runtime import (
    StrategyImplementation,
    StrategyInit,
    StrategyRelease,
    StrategyRuntime,
    TradableWindow,
)
from strategy_runtime.loader import StrategyLoader


ROOT = Path(__file__).resolve().parents[4]
BASELINE_PATH = ROOT / "packages/strategy_runtime/tests/fixtures/s007_v1_equivalence.json"
SEED_PATH = (
    ROOT
    / "strategies/S007/releases/v1/runtime/strategy_runtime/resources/s007_v1_seed.csv.gz"
)
HISTORICAL_WINDOW = TradableWindow(date(2026, 9, 1), date(2026, 9, 2))
FORWARD_WINDOW = TradableWindow(date(2026, 9, 21), date(2026, 9, 21))


def _baseline() -> dict[str, object]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _release() -> tuple[dict[str, object], StrategyRelease]:
    raw = json.loads(
        (ROOT / "strategies/S007/versions/v1.json").read_text(encoding="utf-8")
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


def _features(raw: dict[str, object]) -> list[str]:
    return sorted(raw["strategy_payload"]["rule"]["score"]["orientations"])


def test_frozen_s007_evidence_is_immutable() -> None:
    baseline = _baseline()
    source = baseline["frozen_source_evidence"]
    source_path = ROOT / source["path"]
    panel = pd.read_csv(source_path)
    assert _file_sha256(source_path) == source["sha256"]
    assert panel.shape == (source["rows"], source["columns"])
    assert panel["date"].iloc[[0, -1]].tolist() == [
        source["first_session"],
        source["last_session"],
    ]

    formal = baseline["formal_execution_evidence"]
    evidence_root = ROOT / formal["directory"]
    for name, expected in formal["files"].items():
        path = evidence_root / name
        assert _file_sha256(path) == expected["sha256"]
        assert len(pd.read_csv(path)) == expected["rows"]

    summary = json.loads(
        (evidence_root / "tdr_replay_summary.json").read_text(encoding="utf-8")
    )
    assert summary["audit"]["status"] == "PASS"
    assert summary["behavior_hash"] == formal["behavior_hash"]
    assert summary["orders"] == formal["semantic_counts"]["orders"]
    assert summary["fills"] == formal["semantic_counts"]["fills"]
    assert summary["metrics"] == {
        **formal["metrics"],
        "closed_trades": formal["semantic_counts"]["closed_trades"],
        "win_loss_ratio": summary["metrics"]["win_loss_ratio"],
        "win_loss_ratio_status": summary["metrics"]["win_loss_ratio_status"],
    }


def test_runtime_definition_preserves_frozen_s007_contract() -> None:
    baseline = _baseline()
    _raw, release = _release()
    strategy = StrategyLoader(ROOT / "strategies").load(release)
    definition = StrategyRuntime(ROOT / "strategies").describe(release)

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


def test_s007_derives_historical_and_forward_preparation_scopes() -> None:
    _raw, release = _release()
    strategy = StrategyLoader(ROOT / "strategies").load(release)
    calendar_dates = tuple(pd.bdate_range("2020-12-01", "2026-10-01").date)

    historical = strategy.derive_calculation_scope(HISTORICAL_WINDOW, calendar_dates)
    assert (historical.calculation_dates[0], historical.calculation_dates[-1]) == (
        date(2021, 1, 4),
        date(2026, 9, 1),
    )
    assert (
        historical.inputs["feature_seed"].start,
        historical.inputs["feature_seed"].end,
    ) == (date(2021, 1, 4), date(2026, 9, 1))
    assert historical.inputs["incremental_shibor_daily"] is None
    assert historical.inputs["incremental_chinext_daily_basic"] is None
    assert historical.inputs["incremental_etf_share_size"] is None
    assert historical.inputs["incremental_spx_daily"] is None

    forward = strategy.derive_calculation_scope(FORWARD_WINDOW, calendar_dates)
    assert (forward.calculation_dates[0], forward.calculation_dates[-1]) == (
        date(2021, 1, 4),
        date(2026, 9, 18),
    )
    assert (
        forward.inputs["feature_seed"].start,
        forward.inputs["feature_seed"].end,
    ) == (date(2021, 1, 4), date(2026, 9, 2))
    assert forward.inputs["adjusted_daily"].start == date(2026, 8, 6)
    assert forward.inputs["incremental_shibor_daily"].start == date(2026, 8, 6)
    assert forward.inputs["incremental_chinext_daily_basic"].start == date(2026, 8, 6)
    assert forward.inputs["incremental_etf_share_size"].end == date(2026, 9, 17)
    assert forward.inputs["incremental_spx_daily"].start == date(2026, 7, 30)


def test_s007_seed_matches_source_and_every_frozen_decision() -> None:
    baseline = _baseline()
    raw, release = _release()
    strategy = StrategyLoader(ROOT / "strategies").load(release)
    features = _features(raw)
    seed = pd.read_csv(SEED_PATH)
    seed_identity = baseline["runtime_seed"]
    assert _file_sha256(SEED_PATH) == seed_identity["sha256"]
    assert seed.shape == (seed_identity["rows"], seed_identity["features"] + 1)
    assert seed["Date"].iloc[[0, -1]].tolist() == [
        seed_identity["first_session"],
        seed_identity["last_session"],
    ]

    source = pd.read_csv(ROOT / baseline["frozen_source_evidence"]["path"])
    expected_seed = source.rename(columns={"date": "Date"}).loc[:, seed.columns]
    assert set(seed.columns[1:]) == set(features)
    pd.testing.assert_frame_equal(seed, expected_seed, check_exact=False, rtol=1e-15)

    sessions = pd.DatetimeIndex(pd.to_datetime(seed["Date"]), name="date")
    actual = strategy.calculate_history({"feature_seed": seed}, sessions)
    frozen = pd.read_csv(
        ROOT / baseline["formal_execution_evidence"]["directory"] / "decisions.csv"
    )
    pd.testing.assert_series_equal(
        actual["base_score"].reset_index(drop=True),
        frozen["factor_score"],
        check_names=False,
        check_exact=False,
        rtol=1e-14,
        atol=1e-14,
    )
    pd.testing.assert_series_equal(
        actual["confirmation_score"].reset_index(drop=True),
        frozen["confirmation_score"],
        check_names=False,
        check_exact=False,
        rtol=1e-14,
        atol=1e-14,
    )
    assert actual["target_position"].tolist() == frozen["target_position"].tolist()
    counts = baseline["formal_execution_evidence"]["semantic_counts"]
    assert actual["target_position"].value_counts().to_dict() == {
        0: counts["cash_sessions"],
        1: counts["position_sessions"],
    }
    assert int(actual["target_position"].diff().eq(1).sum()) == counts["entries"]
    assert int(actual["target_position"].diff().eq(-1).sum()) == counts["exits"]


def test_s007_window_calculation_keeps_canonical_state_history() -> None:
    _raw, release = _release()
    strategy = StrategyLoader(ROOT / "strategies").load(release)
    seed = pd.read_csv(SEED_PATH)
    seed["Date"] = pd.to_datetime(seed["Date"])
    requested = pd.DatetimeIndex(seed["Date"].tail(10), name="date")

    full = strategy.calculate_history({"feature_seed": seed}, pd.DatetimeIndex(seed["Date"]))
    window = strategy.calculate_history({"feature_seed": seed}, requested)

    pd.testing.assert_frame_equal(
        window,
        full.reindex(requested),
        check_names=False,
        check_freq=False,
    )


def test_s007_appends_incremental_features_after_frozen_seed() -> None:
    raw, release = _release()
    strategy = StrategyLoader(ROOT / "strategies").load(release)
    implementation = sys.modules[strategy.__class__.__module__]
    features = _features(raw)
    sessions = pd.bdate_range("2026-08-05", "2026-09-03")
    offsets = np.arange(len(sessions), dtype=float)
    close = 10.0 + offsets * 0.01
    volume = 1000.0 + offsets * 10.0
    inputs = {
        "feature_seed": pd.read_csv(SEED_PATH),
        "adjusted_daily": pd.DataFrame(
            {
                "Date": sessions,
                "Open": close,
                "High": close + 0.2,
                "Low": close - 0.1,
                "Close": close,
                "Volume": volume,
                "Amount": volume * (close - 0.02),
            }
        ),
        "incremental_shibor_daily": pd.DataFrame(
            {"Date": sessions, "OvernightRate": 1.0 + offsets * 0.01}
        ),
        "incremental_chinext_daily_basic": pd.DataFrame(
            {"Date": sessions, "TurnoverRateFreeFloat": 2.0 + offsets * 0.02}
        ),
        "incremental_etf_share_size": pd.DataFrame(
            {"Date": sessions[:-1], "TotalShare": 1000.0 + offsets[:-1]}
        ),
        "incremental_spx_daily": pd.DataFrame(
            {
                "Date": pd.date_range("2026-08-01", "2026-09-02"),
                "PercentChange": np.linspace(-0.01, 0.01, 33),
            }
        ),
    }

    panel = implementation.resolve_s007_feature_panel(
        inputs, raw["strategy_payload"]["rule"]["score"]
    )
    incremental = panel.loc[pd.Timestamp("2026-09-03"), features]

    assert incremental.notna().all()
    assert panel.index.max() == pd.Timestamp("2026-09-03")


def test_historical_prepare_uses_seed_without_incremental_sources(
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
            "vendor": "s007-equivalence",
            "adjustment": "none" if "unadjusted" in request.dataset else "hfq",
            "primary_key": ["Date"],
        }

    def calendar(request):
        requests.append(request)
        dates = pd.date_range(request.start, request.end)
        return pd.DataFrame(
            {"Date": dates, "IsOpen": (dates.dayofweek < 5).astype(int)}
        ), {"vendor": "s007-equivalence", "primary_key": ["Date"]}

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
    instance = StrategyRuntime(ROOT / "strategies").create(
        StrategyInit(release, HISTORICAL_WINDOW, tmp_path)
    )

    prepared = instance.prepare_data()

    assert prepared.available_through == date(2026, 9, 1)
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
    assert len(instance.inspect_signals()) == 2
