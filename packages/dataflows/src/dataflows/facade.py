"""Stable DFLS facade over vendor-specific data adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np

from .contract import DataError, DataIdentity, DataRequest, DataResult, Dataset, DataStatus
from .errors import (
    DataContractError,
    DataflowError,
    EmptyDataError,
    IncompleteDataError,
    SourceNotReadyError,
)
from .history_validation import inspect_ohlcv_frame

Provider = Callable[[DataRequest], tuple[pd.DataFrame, Mapping[str, Any]]]


_OHLCV_DATASETS = {
    Dataset.ETF_OHLCV.value,
    Dataset.ETF_UNADJUSTED_DAILY.value,
    Dataset.STOCK_OHLCV.value,
    Dataset.STOCK_UNADJUSTED_DAILY.value,
}

_DATASET_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    Dataset.SHIBOR_DAILY.value: ({"Date", "OvernightRate"}, {"OvernightRate"}),
    Dataset.INDEX_DAILY_BASIC.value: (
        {"Date", "TurnoverRateFreeFloat"},
        {"TurnoverRateFreeFloat"},
    ),
    Dataset.ETF_SHARE_SIZE.value: ({"Date", "TotalShare"}, {"TotalShare"}),
    Dataset.GLOBAL_INDEX_DAILY.value: ({"Date", "PercentChange"}, {"PercentChange"}),
    Dataset.INDEX_CONSTITUENT_WEIGHT.value: (
        {"Date", "ConstituentSymbol", "Weight"},
        {"Weight"},
    ),
    Dataset.STOCK_MONEYFLOW.value: (
        {"Date", "Symbol", "NetMoneyflowAmount"},
        {"NetMoneyflowAmount"},
    ),
    Dataset.TRADING_CALENDAR.value: ({"Date", "IsOpen"}, {"IsOpen"}),
}


def canonical_frame_sha256(dataframe: pd.DataFrame) -> str:
    """Hash dataframe content, column order, dtypes and index deterministically."""

    digest = hashlib.sha256()
    schema = [(str(column), str(dtype)) for column, dtype in dataframe.dtypes.items()]
    digest.update(json.dumps(schema, separators=(",", ":")).encode("utf-8"))
    digest.update(pd.util.hash_pandas_object(dataframe, index=True).values.tobytes())
    return digest.hexdigest()


def _validate_provider_output(
    dataframe: pd.DataFrame,
    request: DataRequest,
    metadata: Mapping[str, Any],
) -> None:
    """Apply the final DFLS-owned contract before READY can cross the facade."""

    dataset = str(request.dataset)
    vendor_symbol = metadata.get("vendor_symbol")
    if vendor_symbol is not None and request.symbol is not None:
        if str(vendor_symbol).upper() != request.symbol.upper():
            raise DataContractError(
                "provider symbol differs from request",
                requested_symbol=request.symbol,
                provider_symbol=str(vendor_symbol),
            )

    if dataset in _OHLCV_DATASETS:
        frequency = (
            "daily"
            if dataset
            in {Dataset.ETF_UNADJUSTED_DAILY.value, Dataset.STOCK_UNADJUSTED_DAILY.value}
            else request.frequency
        )
        declared_period = metadata.get("period")
        if declared_period is not None and str(declared_period) != frequency:
            raise DataContractError(
                "provider frequency differs from request",
                requested_frequency=frequency,
                provider_frequency=str(declared_period),
            )
        expected_asset = "etf" if dataset.startswith("etf.") else "stock"
        declared_asset = metadata.get("asset_type")
        if declared_asset is not None and str(declared_asset) != expected_asset:
            raise DataContractError(
                "provider asset type differs from dataset",
                expected_asset_type=expected_asset,
                provider_asset_type=str(declared_asset),
            )
        if dataset in {
            Dataset.ETF_UNADJUSTED_DAILY.value,
            Dataset.STOCK_UNADJUSTED_DAILY.value,
        } and metadata.get("adjustment") != "none":
            raise DataContractError(
                "unadjusted dataset provider did not declare adjustment=none",
                dataset=dataset,
            )
        inspect_ohlcv_frame(
            dataframe,
            frequency,
            require_complete_days=frequency in {"1m", "5m", "15m", "30m"},
        ).require_pass()
        return

    specification = _DATASET_FIELDS.get(dataset)
    if specification is None:
        return
    required, numeric = specification
    missing = sorted(required.difference(dataframe.columns))
    if missing:
        raise DataContractError(
            "provider output is missing required dataset fields",
            dataset=dataset,
            missing_fields=missing,
        )
    for column in numeric:
        values = pd.to_numeric(dataframe[column], errors="coerce")
        if values.isna().any() or not np.isfinite(values.to_numpy(dtype=float)).all():
            raise DataContractError(
                "provider output contains invalid numeric values",
                dataset=dataset,
                field=column,
            )
    if dataset == Dataset.TRADING_CALENDAR.value:
        flags = pd.to_numeric(dataframe["IsOpen"], errors="coerce")
        if not flags.isin([0, 1]).all():
            raise DataContractError("trading calendar contains invalid open flags")
        observed_dates = pd.DatetimeIndex(
            pd.to_datetime(dataframe["Date"], errors="coerce").dt.normalize()
        )
        expected_dates = pd.date_range(
            pd.Timestamp(request.start).normalize(),
            pd.Timestamp(request.end).normalize(),
            freq="D",
        )
        if not observed_dates.equals(expected_dates):
            raise DataContractError(
                "trading calendar does not cover every requested calendar date",
                missing_dates=[
                    item.date().isoformat()
                    for item in expected_dates.difference(observed_dates)
                ],
                unexpected_dates=[
                    item.date().isoformat()
                    for item in observed_dates.difference(expected_dates)
                ],
            )


def _date_bounds(
    dataframe: pd.DataFrame,
    request: DataRequest,
    metadata: Mapping[str, Any],
) -> tuple[str, str]:
    if "Date" not in dataframe.columns:
        raise DataContractError("published dataframe is missing Date column")
    timestamps = pd.to_datetime(dataframe["Date"], errors="coerce")
    if timestamps.isna().any():
        raise DataContractError("published dataframe contains invalid Date values")
    primary_key = list(metadata.get("primary_key", ["Date"]))
    missing_key = sorted(set(primary_key).difference(dataframe.columns))
    if missing_key:
        raise DataContractError(
            "published dataframe is missing primary-key columns",
            missing_fields=missing_key,
        )
    if dataframe.duplicated(primary_key).any():
        raise DataContractError(
            "published dataframe contains duplicate primary keys",
            primary_key=primary_key,
        )
    if not timestamps.is_monotonic_increasing:
        raise DataContractError("published dataframe must be ordered by Date")

    requested_start = pd.Timestamp(request.start)
    requested_end = pd.Timestamp(request.end)
    if " " not in request.end and "T" not in request.end:
        requested_end += pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    actual_start = timestamps.iloc[0]
    actual_end = timestamps.iloc[-1]
    if actual_start < requested_start or actual_end > requested_end:
        raise DataContractError(
            "published dataframe exceeds the requested time boundary",
            requested_start=str(requested_start),
            requested_end=str(requested_end),
            actual_start=str(actual_start),
            actual_end=str(actual_end),
        )
    if request.required_cutoff is not None:
        required_cutoff = pd.Timestamp(request.required_cutoff)
        if " " not in request.required_cutoff and "T" not in request.required_cutoff:
            actual_comparable = actual_end.normalize()
            required_comparable = required_cutoff.normalize()
        else:
            actual_comparable = actual_end
            required_comparable = required_cutoff
        if actual_comparable < required_comparable:
            raise IncompleteDataError(
                "published dataframe does not reach the required cutoff",
                required_cutoff=str(required_cutoff),
                actual_cutoff=str(actual_end),
            )
    return actual_start.isoformat(), actual_end.isoformat()


class Dataflows:
    """Dataset registry and explicit publication-result boundary for DFLS."""

    def __init__(self, providers: Mapping[str, Provider] | None = None) -> None:
        self._providers = dict(providers) if providers is not None else _default_providers()

    @property
    def datasets(self) -> tuple[str, ...]:
        """Return the registered dataset names in stable lexical order."""

        return tuple(sorted(self._providers))

    def fetch(self, request: DataRequest) -> DataResult:
        provider = self._providers.get(str(request.dataset))
        if provider is None:
            return self._failure(
                DataStatus.FAILED,
                "UNSUPPORTED_DATASET",
                f"unsupported dataset: {request.dataset}",
                request,
            )
        try:
            dataframe, metadata = provider(request)
            if dataframe is None or dataframe.empty:
                raise EmptyDataError("provider returned no rows")
            frame = dataframe.copy()
            _validate_provider_output(frame, request, metadata)
            data_start, data_cutoff = _date_bounds(frame, request, metadata)
            source = str(metadata.get("vendor", "unknown"))
            identity = DataIdentity(
                dataset=str(request.dataset),
                source=source,
                symbol=request.symbol,
                data_start=data_start,
                data_cutoff=data_cutoff,
                content_sha256=canonical_frame_sha256(frame),
                metadata=metadata,
            )
            return DataResult(DataStatus.READY, frame, identity)
        except SourceNotReadyError as exc:
            return self._expected_failure(DataStatus.WAITING_SOURCE, exc, request)
        except EmptyDataError as exc:
            return self._expected_failure(DataStatus.EMPTY, exc, request)
        except IncompleteDataError as exc:
            return self._expected_failure(DataStatus.INCOMPLETE, exc, request)
        except DataflowError as exc:
            return self._expected_failure(DataStatus.FAILED, exc, request)
        except Exception as exc:  # vendor SDK failures cross this boundary as structured errors
            return self._failure(
                DataStatus.FAILED,
                "PROVIDER_EXCEPTION",
                str(exc),
                request,
                exception_type=type(exc).__name__,
            )

    @staticmethod
    def _expected_failure(
        status: DataStatus, exc: DataflowError, request: DataRequest
    ) -> DataResult:
        return Dataflows._failure(
            status,
            exc.code,
            str(exc),
            request,
            retryable=exc.retryable,
            **exc.context,
        )

    @staticmethod
    def _failure(
        status: DataStatus,
        code: str,
        message: str,
        request: DataRequest,
        *,
        retryable: bool = False,
        **context: Any,
    ) -> DataResult:
        details = {
            "dataset": str(request.dataset),
            "symbol": request.symbol,
            "start": request.start,
            "end": request.end,
            **context,
        }
        return DataResult(
            status=status,
            error=DataError(code, message, retryable=retryable, context=details),
        )


def _default_providers() -> dict[str, Provider]:
    from .local_strategy_data import fetch_strategy_feature_evidence
    from .tushare_etf import fetch_etf_ohlcv, fetch_etf_unadjusted_daily
    from .tushare_strategy_data import (
        fetch_etf_share_size,
        fetch_global_index_daily,
        fetch_index_constituent_weight,
        fetch_index_daily_basic,
        fetch_shibor_daily,
        fetch_stock_moneyflow,
        fetch_stock_moneyflow_sessions,
        fetch_trading_calendar,
    )
    from .tushare_stock import fetch_stock_ohlcv, fetch_stock_unadjusted_daily

    def etf_ohlcv(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return fetch_etf_ohlcv(
            _required_symbol(request),
            request.start,
            request.end,
            request.frequency,
            env_file=_env_file(request),
        )

    def etf_unadjusted(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return fetch_etf_unadjusted_daily(
            _required_symbol(request),
            request.start,
            request.end,
            env_file=_env_file(request),
        )

    def stock_ohlcv(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return fetch_stock_ohlcv(
            _required_symbol(request),
            request.start,
            request.end,
            request.frequency,
            env_file=_env_file(request),
        )

    def stock_unadjusted(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return fetch_stock_unadjusted_daily(
            _required_symbol(request),
            request.start,
            request.end,
            env_file=_env_file(request),
        )

    def shibor(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return fetch_shibor_daily(request.start, request.end, env_file=_env_file(request))

    def index_basic(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return fetch_index_daily_basic(
            _required_symbol(request),
            request.start,
            request.end,
            env_file=_env_file(request),
        )

    def etf_shares(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return fetch_etf_share_size(
            _required_symbol(request),
            request.start,
            request.end,
            env_file=_env_file(request),
        )

    def global_index(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return fetch_global_index_daily(
            _required_symbol(request),
            request.start,
            request.end,
            env_file=_env_file(request),
        )

    def index_weights(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return fetch_index_constituent_weight(
            _required_symbol(request),
            request.start,
            request.end,
            env_file=_env_file(request),
        )

    def stock_moneyflow(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        trading_dates = request.options.get("trading_dates")
        if trading_dates is not None:
            if request.symbol is not None:
                raise DataContractError(
                    "explicit-session stock moneyflow requests must be all-market"
                )
            if not isinstance(trading_dates, list | tuple):
                raise DataContractError("trading_dates must be a list or tuple")
            return fetch_stock_moneyflow_sessions(
                tuple(str(item) for item in trading_dates),
                env_file=_env_file(request),
            )
        return fetch_stock_moneyflow(
            request.start,
            request.end,
            symbol=request.symbol,
            env_file=_env_file(request),
        )

    def trading_calendar(request: DataRequest) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return fetch_trading_calendar(
            _required_symbol(request),
            request.start,
            request.end,
            env_file=_env_file(request),
        )

    return {
        Dataset.ETF_OHLCV.value: etf_ohlcv,
        Dataset.ETF_UNADJUSTED_DAILY.value: etf_unadjusted,
        Dataset.STOCK_OHLCV.value: stock_ohlcv,
        Dataset.STOCK_UNADJUSTED_DAILY.value: stock_unadjusted,
        Dataset.SHIBOR_DAILY.value: shibor,
        Dataset.INDEX_DAILY_BASIC.value: index_basic,
        Dataset.ETF_SHARE_SIZE.value: etf_shares,
        Dataset.GLOBAL_INDEX_DAILY.value: global_index,
        Dataset.INDEX_CONSTITUENT_WEIGHT.value: index_weights,
        Dataset.STOCK_MONEYFLOW.value: stock_moneyflow,
        Dataset.TRADING_CALENDAR.value: trading_calendar,
        Dataset.STRATEGY_FEATURE_EVIDENCE.value: fetch_strategy_feature_evidence,
    }


def _env_file(request: DataRequest) -> str | Path | None:
    value = request.options.get("env_file")
    return None if value is None else Path(value)


def _required_symbol(request: DataRequest) -> str:
    if request.symbol is None:
        raise DataContractError(f"{request.dataset} requires a symbol")
    return request.symbol
