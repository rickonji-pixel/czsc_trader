"""Typed provider failures translated by the DFLS public facade."""

from __future__ import annotations

from typing import Any


class DataflowError(RuntimeError):
    """Base class for an expected data-publication failure."""

    code = "DATAFLOW_FAILED"
    retryable = False

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = context


class SourceNotReadyError(DataflowError):
    code = "SOURCE_NOT_READY"
    retryable = True


class EmptyDataError(DataflowError, ValueError):
    """No rows were published; also a ValueError for adapter compatibility."""

    code = "EMPTY_DATA"


class IncompleteDataError(DataflowError):
    code = "INCOMPLETE_DATA"
    retryable = True


class DataContractError(DataflowError):
    code = "DATA_CONTRACT_MISMATCH"
