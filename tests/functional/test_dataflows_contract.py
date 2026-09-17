from __future__ import annotations

import pandas as pd

from dataflows import (
    DataRequest,
    DataStatus,
    Dataflows,
    Dataset,
    IncompleteDataError,
    SourceNotReadyError,
)
from dataflows.errors import EmptyDataError


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": ["2026-09-14", "2026-09-15"],
            "Open": [1.0, 1.1],
            "High": [1.2, 1.3],
            "Low": [0.9, 1.0],
            "Close": [1.1, 1.2],
            "Volume": [100, 120],
            "Amount": [105.0, 138.0],
        }
    )


def _request(dataset: str | Dataset = Dataset.ETF_OHLCV) -> DataRequest:
    return DataRequest(
        dataset, "588080.SH", "2026-09-14", "2026-09-15", "2026-09-15"
    )


def test_ready_result_has_stable_identity_and_detached_data() -> None:
    source = _frame()
    dataflows = Dataflows(
        {Dataset.ETF_OHLCV.value: lambda request: (source, {"vendor": "test"})}
    )

    first = dataflows.fetch(_request())
    second = dataflows.fetch(_request())

    assert first.status is DataStatus.READY
    assert first.ready
    assert first.identity is not None
    assert first.identity.source == "test"
    assert first.identity.data_cutoff == "2026-09-15T00:00:00"
    assert first.identity.content_sha256 == second.identity.content_sha256
    first.dataframe.loc[0, "Close"] = 99
    assert source.loc[0, "Close"] == 1.1


def test_unknown_dataset_is_explicit_failure() -> None:
    result = Dataflows({}).fetch(_request("unknown.dataset"))

    assert result.status is DataStatus.FAILED
    assert result.error is not None
    assert result.error.code == "UNSUPPORTED_DATASET"
    assert not result.error.retryable


def test_provider_states_are_not_reported_as_ready() -> None:
    def waiting(request: DataRequest):
        raise SourceNotReadyError("daily source has not been published", available_at="20:30")

    def incomplete(request: DataRequest):
        raise IncompleteDataError("one dependency is missing", dependency="shibor")

    waiting_result = Dataflows({Dataset.ETF_OHLCV.value: waiting}).fetch(_request())
    incomplete_result = Dataflows({Dataset.ETF_OHLCV.value: incomplete}).fetch(_request())

    assert waiting_result.status is DataStatus.WAITING_SOURCE
    assert waiting_result.error is not None and waiting_result.error.retryable
    assert waiting_result.dataframe.empty
    assert incomplete_result.status is DataStatus.INCOMPLETE
    assert incomplete_result.error is not None
    assert incomplete_result.error.context["dependency"] == "shibor"


def test_legacy_empty_error_remains_value_error_and_maps_to_empty() -> None:
    def empty(request: DataRequest):
        raise EmptyDataError("vendor returned no data")

    error = EmptyDataError("vendor returned no data")
    result = Dataflows({Dataset.ETF_OHLCV.value: empty}).fetch(_request())

    assert isinstance(error, ValueError)
    assert result.status is DataStatus.EMPTY
    assert result.error is not None and result.error.code == "EMPTY_DATA"


def test_out_of_boundary_data_fails_contract() -> None:
    frame = _frame()
    frame.loc[1, "Date"] = "2026-09-16"
    result = Dataflows(
        {Dataset.ETF_OHLCV.value: lambda request: (frame, {"vendor": "test"})}
    ).fetch(_request())

    assert result.status is DataStatus.FAILED
    assert result.error is not None
    assert result.error.code == "DATA_CONTRACT_MISMATCH"


def test_date_only_end_includes_intraday_rows() -> None:
    frame = _frame().iloc[[0]].copy()
    frame.loc[0, "Date"] = "2026-09-14 15:00:00"
    request = DataRequest(
        Dataset.ETF_OHLCV,
        "588080.SH",
        "2026-09-14",
        "2026-09-14",
        "2026-09-14",
        "30m",
    )

    result = Dataflows(
        {Dataset.ETF_OHLCV.value: lambda ignored: (frame, {"vendor": "test"})}
    ).fetch(request)

    assert result.status is DataStatus.READY


def test_multi_entity_dataset_uses_declared_primary_key() -> None:
    frame = pd.DataFrame(
        {
            "Date": ["2026-09-15", "2026-09-15"],
            "Symbol": ["000001.SZ", "600000.SH"],
            "NetMoneyflowAmount": [1.0, -2.0],
        }
    )
    result = Dataflows(
        {
            Dataset.STOCK_MONEYFLOW.value: lambda ignored: (
                frame,
                {"vendor": "test", "primary_key": ["Date", "Symbol"]},
            )
        }
    ).fetch(
        DataRequest(
            Dataset.STOCK_MONEYFLOW,
            None,
            "2026-09-15",
            "2026-09-15",
            "2026-09-15",
        )
    )

    assert result.status is DataStatus.READY
    assert len(result.dataframe) == 2


def test_default_registry_covers_all_active_frozen_strategy_inputs() -> None:
    expected = {
        Dataset.ETF_OHLCV.value,
        Dataset.ETF_UNADJUSTED_DAILY.value,
        Dataset.SHIBOR_DAILY.value,
        Dataset.INDEX_DAILY_BASIC.value,
        Dataset.ETF_SHARE_SIZE.value,
        Dataset.GLOBAL_INDEX_DAILY.value,
        Dataset.INDEX_CONSTITUENT_WEIGHT.value,
        Dataset.STOCK_MONEYFLOW.value,
        Dataset.TRADING_CALENDAR.value,
    }

    assert expected.issubset(Dataflows().datasets)


def test_required_cutoff_prevents_stale_data_from_becoming_ready() -> None:
    frame = _frame().iloc[[0]].copy()
    request = DataRequest(
        Dataset.ETF_OHLCV,
        "588080.SH",
        "2026-09-14",
        "2026-09-15",
        "2026-09-15",
    )

    result = Dataflows(
        {Dataset.ETF_OHLCV.value: lambda ignored: (frame, {"vendor": "test"})}
    ).fetch(request)

    assert result.status is DataStatus.INCOMPLETE
    assert result.error is not None
    assert result.error.code == "INCOMPLETE_DATA"
