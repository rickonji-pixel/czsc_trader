"""Internal data preparation owned by one StrategyInstance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dataflows import DataRequest, DataResult, DataStatus, Dataflows, Dataset

from .contracts import StrategyIdentity, TradableWindow
from .errors import RuntimeContractError, RuntimeExecutionError
from .models import CutoffRule, RuntimeDefinition, canonical_sha256
from .validation import validate_history_depth


@dataclass(frozen=True, slots=True)
class PreparedInputs:
    strategy: StrategyIdentity
    tradable_window: TradableWindow
    available_through: date
    data_identity: str
    requests: Mapping[str, DataRequest]
    results: Mapping[str, DataResult]
    calendar_dates: tuple[date, ...]
    calculation_dates: tuple[date, ...]

    @property
    def input_identities(self) -> dict[str, str]:
        return {
            name: result.identity.content_sha256
            for name, result in self.results.items()
            if result.identity is not None
        }


def _declared_symbol(definition: RuntimeDefinition) -> str:
    subjects = {
        item.subject.upper()
        for item in definition.inputs.requirements
        if item.subject and item.dataset.startswith("etf.")
    }
    if len(subjects) != 1:
        raise RuntimeContractError(
            "strategy data preparation requires exactly one ETF instrument"
        )
    return next(iter(subjects))


def _request_options(
    definition: RuntimeDefinition,
    requirement,
    base: Mapping[str, object],
) -> dict[str, object]:
    options = dict(base)
    if requirement.dataset != Dataset.STRATEGY_FEATURE_EVIDENCE.value:
        return options
    rule = definition.parameters.values.get("rule")
    data_source = rule.get("data_source") if isinstance(rule, Mapping) else None
    if (
        not isinstance(data_source, Mapping)
        or not isinstance(data_source.get("path"), str)
        or not isinstance(data_source.get("sha256"), str)
    ):
        raise RuntimeContractError("strategy evidence data source is incomplete")
    options.update(
        {
            "repository_root": str(Path.cwd().resolve()),
            "source_path": str(data_source["path"]),
            "source_sha256": str(data_source["sha256"]),
        }
    )
    return options


def _calendar_start(definition: RuntimeDefinition, window: TradableWindow) -> date:
    lookback = max(
        (
            item.lookback_sessions
            for item in definition.inputs.requirements
            if item.dataset != Dataset.TRADING_CALENDAR.value
        ),
        default=1,
    )
    policy_start = definition.history.preparation_start(window.start)
    estimated = window.start - timedelta(days=max(31, lookback * 2))
    return min(policy_start, estimated)


def _open_dates(frame: pd.DataFrame) -> tuple[date, ...]:
    if not {"Date", "IsOpen"} <= set(frame.columns):
        raise RuntimeContractError("trading calendar is structurally incomplete")
    dates = pd.to_datetime(frame["Date"], errors="raise").dt.date
    mask = pd.to_numeric(frame["IsOpen"], errors="raise").eq(1)
    result = tuple(dict.fromkeys(dates.loc[mask]))
    if tuple(sorted(result)) != result:
        raise RuntimeContractError("trading calendar dates are not ordered")
    return result


def _input_start(
    definition: RuntimeDefinition,
    requirement,
    first_signal: date,
    calendar_dates: tuple[date, ...],
) -> date:
    available = [item for item in calendar_dates if item <= first_signal]
    required = max(1, requirement.lookback_sessions)
    if len(available) < required:
        raise RuntimeContractError(
            f"trading calendar cannot satisfy input lookback: {requirement.name}"
        )
    result = min(
        definition.history.preparation_start(first_signal),
        available[-required],
    )
    if requirement.cutoff_rule is CutoffRule.LATEST_AVAILABLE:
        result -= timedelta(days=requirement.maximum_staleness_days)
    return result


def _ready(result: DataResult, name: str) -> DataResult:
    if result.status is DataStatus.READY and result.identity is not None:
        return result
    detail = result.error.message if result.error else result.status.value
    raise RuntimeExecutionError(f"data preparation failed for {name}: {detail}")


def prepare_inputs(
    *,
    strategy: StrategyIdentity,
    definition: RuntimeDefinition,
    tradable_window: TradableWindow,
    data_dir: Path,
    dataflows: Dataflows | None = None,
) -> PreparedInputs:
    """Derive, fetch and validate every dataset required by one instance."""

    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    symbol = _declared_symbol(definition)
    if symbol != strategy.symbol:
        raise RuntimeContractError("prepared-data symbol differs from strategy")
    requirements = {item.name: item for item in definition.inputs.requirements}
    calendars = [
        item
        for item in definition.inputs.requirements
        if item.dataset == Dataset.TRADING_CALENDAR.value
    ]
    if len(calendars) != 1:
        raise RuntimeContractError("strategy requires exactly one trading calendar")
    calendar_requirement = calendars[0]
    flows = dataflows or Dataflows()
    calendar_start = _calendar_start(definition, tradable_window)
    calendar_end = tradable_window.end + timedelta(days=20)
    base_options = _request_options(
        definition, calendar_requirement, {}
    )
    required_sessions = max(
        (item.lookback_sessions for item in definition.inputs.requirements),
        default=1,
    )
    for _ in range(8):
        calendar_request = DataRequest(
            calendar_requirement.dataset,
            calendar_requirement.subject,
            calendar_start.isoformat(),
            calendar_end.isoformat(),
            calendar_end.isoformat(),
            calendar_requirement.frequency,
            base_options,
        )
        calendar_result = _ready(
            flows.fetch(calendar_request), calendar_requirement.name
        )
        calendar_dates = _open_dates(calendar_result.dataframe)
        before = [item for item in calendar_dates if item < tradable_window.start]
        if len(before) >= required_sessions:
            break
        span = max(31, (tradable_window.start - calendar_start).days)
        calendar_start -= timedelta(days=span)
    else:
        raise RuntimeContractError(
            "trading calendar cannot satisfy the declared strategy lookback"
        )

    trading_dates = tuple(
        item for item in calendar_dates if tradable_window.contains(item)
    )
    if (
        not trading_dates
        or trading_dates[0] != tradable_window.start
        or trading_dates[-1] != tradable_window.end
    ):
        raise RuntimeContractError("tradable window endpoints must be open sessions")
    first_signal = max(item for item in calendar_dates if item < trading_dates[0])
    last_signal = max(item for item in calendar_dates if item < trading_dates[-1])
    requests: dict[str, DataRequest] = {
        calendar_requirement.name: calendar_request
    }
    results: dict[str, DataResult] = {
        calendar_requirement.name: calendar_result
    }
    for name, requirement in requirements.items():
        if name == calendar_requirement.name:
            continue
        request_start = _input_start(
            definition, requirement, first_signal, calendar_dates
        )
        request_end = last_signal
        if requirement.cutoff_rule is CutoffRule.SIGNAL_SESSION:
            required_cutoff = last_signal.isoformat()
        elif requirement.cutoff_rule is CutoffRule.PREVIOUS_SESSION:
            previous = [item for item in calendar_dates if item < last_signal]
            if not previous:
                raise RuntimeContractError(
                    f"prepared data has no previous session for input {name}"
                )
            request_end = previous[-1]
            required_cutoff = request_end.isoformat()
        else:
            required_cutoff = None
        options = _request_options(definition, requirement, {})
        if (
            requirement.dataset == Dataset.STOCK_MONEYFLOW.value
            and requirement.subject is None
        ):
            options["trading_dates"] = [
                item.isoformat()
                for item in calendar_dates
                if request_start <= item <= request_end
            ]
        request = DataRequest(
            requirement.dataset,
            requirement.subject,
            request_start.isoformat(),
            request_end.isoformat(),
            required_cutoff,
            requirement.frequency,
            options,
        )
        result = _ready(flows.fetch(request), name)
        validate_history_depth(name, requirement.lookback_sessions, result.dataframe)
        if (
            requirement.cutoff_rule is CutoffRule.LATEST_AVAILABLE
            and requirement.maximum_staleness_days
            and pd.Timestamp(result.identity.data_cutoff)
            < pd.Timestamp(last_signal)
            - pd.Timedelta(days=requirement.maximum_staleness_days)
        ):
            raise RuntimeContractError(
                f"prepared input exceeds maximum staleness: {name}"
            )
        requests[name] = request
        results[name] = result

    input_identities = {
        name: result.identity.content_sha256
        for name, result in sorted(results.items())
    }
    data_identity = canonical_sha256(
        {
            "strategy": strategy.reference_id,
            "release_hash": strategy.release_hash,
            "runtime_sha256": strategy.runtime_sha256,
            "tradable_window": {
                "start": tradable_window.start.isoformat(),
                "end": tradable_window.end.isoformat(),
            },
            "available_through": last_signal.isoformat(),
            "inputs": input_identities,
        }
    )
    calculation_start = (
        date.fromisoformat(definition.history.canonical_start)
        if definition.history.canonical_start is not None
        else first_signal
    )
    calculation_dates = tuple(
        item for item in calendar_dates if calculation_start <= item <= last_signal
    )
    return PreparedInputs(
        strategy,
        tradable_window,
        last_signal,
        data_identity,
        requests,
        results,
        calendar_dates,
        calculation_dates,
    )
