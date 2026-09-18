from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.baselines import resolve_strategy_payload
from czsc_trader.data import MarketData
from czsc_trader.strategy_runtime import apply_resolved_strategy
from dataflows import DataRequest, Dataflows, Dataset
from strategy_runtime import (
    AccountSnapshot,
    ChannelCapabilities,
    DeploymentSpec,
    ExecutionReceipt,
    RuntimeCompatibilityError,
    RuntimeRunStatus,
    RuntimeContractError,
    StrategyLoader,
    StrategyRelease,
    StrategyRunner,
    StrategyStateSnapshot,
)
from strategy_runtime.strategies.s002_v1 import _calculate_target_history


ROOT = Path(__file__).resolve().parents[4]
NOW = datetime.fromisoformat("2026-09-08T20:30:00+08:00")


def _release() -> tuple[StrategyRelease, dict[str, object]]:
    raw = json.loads((ROOT / "strategies/S002/versions/v1.json").read_text(encoding="utf-8"))
    release = StrategyRelease.from_mapping(raw)
    return release, raw


def _load_years(prefix: str, request: DataRequest) -> pd.DataFrame:
    pieces = [
        pd.read_csv(path) for path in sorted((ROOT / "data/backtest").glob(f"{prefix}_*.csv"))
    ]
    frame = pd.concat(pieces, ignore_index=True)
    dates = pd.to_datetime(frame["date"])
    selected = frame.loc[
        dates.between(pd.Timestamp(request.start), pd.Timestamp(request.end)),
        ["date", "open", "high", "low", "close", "volume", "amount"],
    ].copy()
    return selected.rename(
        columns={
            "date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "amount": "Amount",
        }
    )


def _adjusted_provider(request: DataRequest):
    return _load_years("510500_daily", request), {
        "vendor": "test-backtest-data",
        "frequency": "daily",
        "primary_key": ["Date"],
    }


def _execution_provider(request: DataRequest):
    return _load_years("510500_execution_daily", request), {
        "vendor": "test-backtest-data",
        "frequency": "daily",
        "primary_key": ["Date"],
    }


def _calendar_provider(request: DataRequest):
    dates = pd.date_range(request.start, request.end, freq="D")
    frame = pd.DataFrame(
        {
            "Date": dates,
            "IsOpen": (dates.dayofweek < 5).astype(int),
            "PreviousTradingDate": pd.NaT,
        }
    )
    return frame, {
        "vendor": "test-calendar",
        "frequency": "daily",
        "primary_key": ["Date"],
    }


class _Account:
    def snapshot(self, deployment: DeploymentSpec) -> AccountSnapshot:
        return AccountSnapshot(deployment.account_id, 100_000.0, 100_000.0, 0, 1, NOW)


class _Execution:
    def execute(self, request, idempotency_key: str) -> ExecutionReceipt:
        return ExecutionReceipt(idempotency_key, True, None, "NO_ACTION", "recorded")


class _Channel:
    channel_id = "backtest"
    capabilities = ChannelCapabilities(("LIMIT", "MARKET"))

    def __init__(self) -> None:
        self.execution = _Execution()

    def submit(self, request, idempotency_key: str) -> ExecutionReceipt:
        return self.execution.execute(request, idempotency_key)


def test_loader_discovers_s002_without_a_central_release_switch() -> None:
    release, _ = _release()

    strategy = StrategyLoader().load(release)

    assert strategy.definition.release_id == "S002-v1"
    assert strategy.definition.implementation.module.endswith("strategies.s002_v1")
    assert strategy.definition.runtime_sha256


def test_loader_fails_clearly_when_implementation_is_missing() -> None:
    raw = {
        "strategy_id": "S999",
        "version": "v1",
        "release_id": "S999-v1",
        "strategy_payload": {"kind": "test"},
    }
    from strategy_runtime import canonical_sha256

    raw["release_hash"] = canonical_sha256(raw)
    release = StrategyRelease.from_mapping(raw)

    with pytest.raises(RuntimeCompatibilityError, match="implementation is unavailable"):
        StrategyLoader().load(release)


def test_strategy_release_rejects_payload_with_a_borrowed_hash() -> None:
    _, raw = _release()
    raw["strategy_payload"]["rule"]["portfolio_rule"]["holding_sessions"] = 6

    with pytest.raises(RuntimeContractError, match="complete frozen record"):
        StrategyRelease.from_mapping(raw)


def test_s002_runner_matches_the_frozen_legacy_signal_at_cutoff() -> None:
    release, raw = _release()
    strategy = StrategyLoader().load(release)
    dataflows = Dataflows(
        {
            Dataset.ETF_OHLCV.value: _adjusted_provider,
            Dataset.ETF_UNADJUSTED_DAILY.value: _execution_provider,
            Dataset.TRADING_CALENDAR.value: _calendar_provider,
        }
    )
    deployment = DeploymentSpec(
        "dep-s002-backtest",
        release.release_id,
        release.release_hash,
        "510500.SH",
        "s002-backtest",
        "backtest",
    )
    state = StrategyStateSnapshot(deployment.deployment_id, release.release_hash, 0, NOW, {})

    result = StrategyRunner().run(
        strategy=strategy,
        deployment=deployment,
        state=state,
        dataflows=dataflows,
        account=_Account(),
        channel=_Channel(),
        through=NOW,
        calculation_time=NOW,
    )

    assert result.status is RuntimeRunStatus.ACCEPTED
    assert result.decision is not None
    assert result.decision.valid_at.isoformat() == "2026-09-09T09:30:00+08:00"
    assert result.decision.runtime_sha256 == strategy.definition.runtime_sha256
    assert set(result.decision.input_identity_hashes) == {
        "adjusted_daily",
        "execution_daily",
        "trading_calendar",
    }

    adjusted = _load_years(
        "510500_daily",
        DataRequest(
            Dataset.ETF_OHLCV,
            "510500.SH",
            "2024-11-27",
            "2026-09-08",
            "2026-09-08",
        ),
    ).rename(
        columns={
            "Date": "dt",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "vol",
            "Amount": "amount",
        }
    )
    adjusted["dt"] = pd.to_datetime(adjusted["dt"])
    adjusted.insert(1, "symbol", "510500.SH")
    legacy = resolve_strategy_payload(
        ROOT / "strategies/dependencies/legacy_rule_baselines",
        raw["strategy_payload"],
        release_id=release.release_id,
        release_hash=release.release_hash,
        symbol="510500.SH",
        repository_root=ROOT,
    )
    applied = apply_resolved_strategy(
        MarketData(pd.DataFrame(), adjusted, pd.DataFrame(), {}, "510500.SH"), legacy
    )
    expected = float(applied.target_position.loc[pd.Timestamp("2026-09-08")])
    assert result.decision.target_position == expected


def test_s002_complete_target_history_matches_the_frozen_legacy_path() -> None:
    release, raw = _release()
    strategy = StrategyLoader().load(release)
    request = DataRequest(
        Dataset.ETF_OHLCV,
        "510500.SH",
        "2019-10-08",
        "2026-09-15",
        "2026-09-15",
    )
    daily = _load_years("510500_daily", request)

    payload = raw["strategy_payload"]
    rule = payload["rule"]
    actual = _calculate_target_history(
        daily,
        symbol="510500.SH",
        signal=rule["signal"],
        portfolio=rule["portfolio_rule"],
    )

    adjusted = daily.rename(
        columns={
            "Date": "dt",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "vol",
            "Amount": "amount",
        }
    )
    adjusted["dt"] = pd.to_datetime(adjusted["dt"])
    adjusted.insert(1, "symbol", "510500.SH")
    legacy = resolve_strategy_payload(
        ROOT / "strategies/dependencies/legacy_rule_baselines",
        payload,
        release_id=release.release_id,
        release_hash=release.release_hash,
        symbol="510500.SH",
        repository_root=ROOT,
    )
    expected = apply_resolved_strategy(
        MarketData(pd.DataFrame(), adjusted, pd.DataFrame(), {}, "510500.SH"), legacy
    ).target_position.astype(float)
    expected = expected.reindex(actual.index)

    pd.testing.assert_index_equal(actual.index, expected.index, check_names=False)
    pd.testing.assert_series_equal(
        actual["target_position"].astype(float),
        expected,
        check_names=False,
    )
    assert actual.index[0] == pd.Timestamp("2020-10-19")
    assert actual.index[-1] == pd.Timestamp("2026-09-15")
    assert len(actual) == 1437
    changes = actual["target_position"] - actual["target_position"].shift(1, fill_value=0.0)
    assert int(changes.eq(1.0).sum()) == 28
    assert int(changes.eq(-1.0).sum()) == 28
    assert strategy.definition.release_id == "S002-v1"
