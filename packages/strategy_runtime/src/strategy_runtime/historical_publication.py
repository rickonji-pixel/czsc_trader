"""Contract-driven historical input publication for SRT execution hosts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta

import pandas as pd
from dataflows import DataRequest, DataResult, DataStatus, Dataflows, Dataset

from .errors import RuntimeContractError
from .models import (
    CutoffRule,
    DeploymentSpec,
    PublicationStatus,
    PublishedStrategyData,
)
from .protocols import ExecutableStrategy


def _request_options(
    definition,
    deployment: DeploymentSpec,
    requirement,
    base: Mapping[str, object],
) -> dict[str, object]:
    options = dict(base)
    if requirement.dataset != Dataset.STRATEGY_FEATURE_EVIDENCE.value:
        return options
    repository_root = deployment.settings.get("repository_root")
    rule = definition.parameters.values.get("rule")
    data_source = rule.get("data_source") if isinstance(rule, Mapping) else None
    if (
        repository_root is None
        or not isinstance(data_source, Mapping)
        or not isinstance(data_source.get("path"), str)
        or not isinstance(data_source.get("sha256"), str)
    ):
        raise RuntimeContractError("strategy evidence publication settings are incomplete")
    options.update(
        {
            "repository_root": str(repository_root),
            "source_path": str(data_source["path"]),
            "source_sha256": str(data_source["sha256"]),
        }
    )
    return options


def _publication_status(results: Mapping[str, DataResult]) -> PublicationStatus:
    statuses = {item.status for item in results.values()}
    if statuses == {DataStatus.READY}:
        return PublicationStatus.READY
    if DataStatus.FAILED in statuses:
        return PublicationStatus.FAILED
    if DataStatus.WAITING_SOURCE in statuses:
        return PublicationStatus.WAITING_SOURCE
    return PublicationStatus.INCOMPLETE


def _declared_symbol(strategy: ExecutableStrategy) -> str | None:
    """Read the effective deployment symbol from the runtime input contract."""

    input_subjects = {
        item.subject.upper()
        for item in strategy.definition.inputs.requirements
        if item.subject and item.dataset.startswith("etf.")
    }
    if len(input_subjects) == 1:
        return next(iter(input_subjects))

    payload = strategy.definition.parameters.values
    rule = payload.get("rule")
    if isinstance(rule, Mapping):
        execution = rule.get("execution")
        if isinstance(execution, Mapping):
            instrument = execution.get("instrument")
            if isinstance(instrument, Mapping) and isinstance(instrument.get("symbol"), str):
                return str(instrument["symbol"]).upper()
        if isinstance(rule.get("symbol"), str):
            return str(rule["symbol"]).upper()
    if isinstance(payload.get("symbol"), str):
        return str(payload["symbol"]).upper()
    return None


def _open_sessions(calendar: pd.DataFrame, start: date, through: date) -> pd.DatetimeIndex:
    dates = pd.to_datetime(
        calendar.loc[calendar["IsOpen"].astype(int).eq(1), "Date"]
    ).dt.normalize()
    return pd.DatetimeIndex(dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(through))])


def publish_history(
    strategy: ExecutableStrategy,
    dataflows: Dataflows,
    deployment: DeploymentSpec,
    *,
    start: date,
    through: date,
) -> PublishedStrategyData:
    """Publish one bounded history strictly from the SRT input contract."""

    if start > through:
        raise RuntimeContractError("historical publication start follows cutoff")
    definition = strategy.definition
    publication_start = definition.history.publication_start(start)
    if definition.release_id != deployment.release_id:
        raise RuntimeContractError("historical deployment release ID differs from strategy")
    if definition.release_hash != deployment.release_hash:
        raise RuntimeContractError("historical deployment release hash differs from strategy")
    declared_symbol = _declared_symbol(strategy)
    if declared_symbol is not None and deployment.symbol.upper() != declared_symbol:
        raise RuntimeContractError("historical deployment symbol differs from frozen strategy")

    options: dict[str, object] = {}
    env_file = deployment.settings.get("env_file")
    if env_file is not None:
        options["env_file"] = str(env_file)

    requirements = {item.name: item for item in definition.inputs.requirements}
    calendar_items = [
        item
        for item in definition.inputs.requirements
        if item.dataset == Dataset.TRADING_CALENDAR.value
    ]
    if len(calendar_items) != 1:
        raise RuntimeContractError("historical publication requires one trading calendar input")
    calendar_requirement = calendar_items[0]
    calendar_end = through + timedelta(days=20)
    calendar_request = DataRequest(
        calendar_requirement.dataset,
        calendar_requirement.subject,
        publication_start.isoformat(),
        calendar_end.isoformat(),
        calendar_end.isoformat(),
        calendar_requirement.frequency,
        options,
    )
    calendar_result = dataflows.fetch(calendar_request)
    requests: dict[str, DataRequest] = {calendar_requirement.name: calendar_request}
    results: dict[str, DataResult] = {calendar_requirement.name: calendar_result}

    if calendar_result.ready:
        sessions = _open_sessions(calendar_result.dataframe, publication_start, through)
        previous = sessions[sessions < pd.Timestamp(through)]
        for name, requirement in requirements.items():
            if name == calendar_requirement.name:
                continue
            request_start = publication_start
            if requirement.dataset == Dataset.INDEX_CONSTITUENT_WEIGHT.value:
                request_start -= timedelta(days=requirement.maximum_staleness_days)
            request_end = through
            required_cutoff: str | None
            if requirement.cutoff_rule is CutoffRule.SIGNAL_SESSION:
                required_cutoff = through.isoformat()
            elif requirement.cutoff_rule is CutoffRule.PREVIOUS_SESSION:
                if previous.empty:
                    raise RuntimeContractError(
                        f"historical publication has no previous session for input {name}"
                    )
                request_end = previous[-1].date()
                required_cutoff = request_end.isoformat()
            else:
                required_cutoff = None
            request_options = dict(options)
            if requirement.dataset == Dataset.STOCK_MONEYFLOW.value and requirement.subject is None:
                request_options["trading_dates"] = [
                    item.date().isoformat() for item in sessions if item.date() <= request_end
                ]
            request = DataRequest(
                requirement.dataset,
                requirement.subject,
                request_start.isoformat(),
                request_end.isoformat(),
                required_cutoff,
                requirement.frequency,
                _request_options(definition, deployment, requirement, request_options),
            )
            requests[name] = request
            results[name] = dataflows.fetch(request)
    else:
        for name, requirement in requirements.items():
            if name == calendar_requirement.name:
                continue
            request = DataRequest(
                requirement.dataset,
                requirement.subject,
                publication_start.isoformat(),
                through.isoformat(),
                None,
                requirement.frequency,
                _request_options(definition, deployment, requirement, options),
            )
            requests[name] = request
            results[name] = DataResult(DataStatus.INCOMPLETE, error=calendar_result.error)

    status = _publication_status(results)
    error = None
    if status is not PublicationStatus.READY:
        error = "; ".join(
            f"{name}:{result.status.value}:{result.error.code if result.error else 'UNKNOWN'}"
            for name, result in results.items()
            if not result.ready
        )
    return PublishedStrategyData(
        definition.release_id,
        definition.release_hash,
        status,
        through.isoformat(),
        requests,
        results,
        error,
    )
