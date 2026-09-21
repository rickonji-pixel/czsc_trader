"""SRT contract validation shared by data preparation and planning."""

from __future__ import annotations

import pandas as pd
from dataflows import Dataset

from .errors import RuntimeContractError
from .models import CutoffRule, PublishedStrategyData, RuntimeDefinition


def validate_history_depth(
    input_name: str,
    required_observations: int,
    dataframe: pd.DataFrame,
) -> None:
    if required_observations <= 0:
        return
    date_column = next(
        (
            candidate
            for candidate in (
                "Date",
                "date",
                "datetime",
                "dt",
                "trade_date",
                "cal_date",
            )
            if candidate in dataframe.columns
        ),
        None,
    )
    if date_column is None:
        raise RuntimeContractError(
            f"published input {input_name} has no observation timestamp"
        )
    observed = pd.to_datetime(dataframe[date_column], errors="coerce").dropna().nunique()
    if observed < required_observations:
        raise RuntimeContractError(
            f"published input {input_name} has insufficient history: "
            f"required={required_observations}, observed={observed}"
        )


def validate_publication(
    definition: RuntimeDefinition,
    publication: PublishedStrategyData,
) -> None:
    if publication.release_id != definition.release_id:
        raise RuntimeContractError("strategy and publication release IDs differ")
    if publication.release_hash != definition.release_hash:
        raise RuntimeContractError("strategy and publication release hashes differ")
    expected_inputs = {item.name for item in definition.inputs.requirements}
    actual_inputs = set(publication.input_results)
    if actual_inputs != expected_inputs:
        raise RuntimeContractError(
            "publication inputs differ from the declared contract: "
            f"missing={sorted(expected_inputs - actual_inputs)}, "
            f"unexpected={sorted(actual_inputs - expected_inputs)}"
        )
    requested_cutoff = pd.Timestamp(publication.requested_cutoff)
    requirements = {item.name: item for item in definition.inputs.requirements}
    calendar_names = [
        name
        for name, requirement in requirements.items()
        if requirement.dataset == Dataset.TRADING_CALENDAR.value
    ]
    previous_session: pd.Timestamp | None = None
    if len(calendar_names) == 1:
        calendar_result = publication.input_results[calendar_names[0]]
        if calendar_result.ready:
            calendar = calendar_result.dataframe
            if not {"Date", "IsOpen"} <= set(calendar.columns):
                raise RuntimeContractError(
                    "published trading calendar is structurally incomplete"
                )
            dates = pd.to_datetime(calendar["Date"], errors="coerce").dt.normalize()
            open_mask = pd.to_numeric(calendar["IsOpen"], errors="coerce").eq(1)
            candidates = dates.loc[open_mask & dates.lt(requested_cutoff.normalize())]
            if not candidates.empty:
                previous_session = pd.Timestamp(candidates.max())
    for name, requirement in requirements.items():
        data_request = publication.input_requests[name]
        data_result = publication.input_results[name]
        if str(data_request.dataset) != requirement.dataset:
            raise RuntimeContractError(f"publication dataset differs for input {name}")
        if data_request.symbol != requirement.subject:
            raise RuntimeContractError(f"publication subject differs for input {name}")
        if data_request.frequency != requirement.frequency:
            raise RuntimeContractError(f"publication frequency differs for input {name}")
        if requirement.cutoff_rule is CutoffRule.SIGNAL_SESSION:
            if (
                data_request.required_cutoff is None
                or pd.Timestamp(data_request.required_cutoff) != requested_cutoff
            ):
                raise RuntimeContractError(
                    f"publication cutoff differs from signal session for input {name}"
                )
        elif requirement.cutoff_rule is CutoffRule.PREVIOUS_SESSION:
            if (
                data_request.required_cutoff is None
                or previous_session is None
                or pd.Timestamp(data_request.required_cutoff).normalize() != previous_session
            ):
                raise RuntimeContractError(
                    f"publication cutoff is not the exact previous session for input {name}"
                )
        if data_result.ready:
            identity = data_result.identity
            if identity.dataset != str(data_request.dataset):
                raise RuntimeContractError(
                    f"published identity dataset differs for input {name}"
                )
            if identity.symbol != data_request.symbol:
                raise RuntimeContractError(
                    f"published identity subject differs for input {name}"
                )
            if (
                requirement.cutoff_rule is CutoffRule.LATEST_AVAILABLE
                and requirement.maximum_staleness_days
                and pd.Timestamp(identity.data_cutoff)
                < requested_cutoff - pd.Timedelta(days=requirement.maximum_staleness_days)
            ):
                raise RuntimeContractError(
                    f"published input exceeds maximum staleness for input {name}"
                )
            validate_history_depth(name, requirement.lookback_sessions, data_result.dataframe)
