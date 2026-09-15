"""Immutable fixed-rule baseline registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re

import numpy as np

from .experiment_archive import resolve_repository_experiment_reference
from .rules import Rule
from .identity import canonical_json_sha256, raw_file_sha256


_VERSION_PATTERN = re.compile(r"baseline_(\d{8})$")
_ENTRY_GATES = {"none", "structure", "trend", "structure_and_trend"}


@dataclass(frozen=True)
class ResolvedBaseline:
    version: str
    rule: Rule | None
    rule_payload: dict[str, object]
    sha256: str
    strategy: str = "czsc_fixed_rule"
    status: str = "active"
    scope: str = "generic"
    symbol: str | None = None
    factor_names: tuple[str, ...] = ()
    factor_weights: tuple[float, ...] = ()
    regime_factor_weights: dict[str, tuple[float, ...]] = field(default_factory=dict)
    er_lookback: int = 0
    er_threshold: float = 0.0
    source_path: str = ""
    source_sha256: str = ""
    selection_sample_end: str = ""
    forward_validation_start: str = ""
    execution: ExecutionSpec | None = None
    event_hold: EventHoldSpec | None = None
    constituent_moneyflow_intraday: ConstituentMoneyflowIntradaySpec | None = None
    closing_dislocation_overnight: ClosingDislocationOvernightSpec | None = None
    causal_feature_gate: CausalFeatureGateSpec | None = None


@dataclass(frozen=True)
class EventHoldSpec:
    """One fully declared CZSC event and fixed holding-period state machine."""

    symbol: str
    price_adjustment: str
    signal_frequency: str
    signal_name: str
    signal_config: dict[str, object]
    output_key: str
    entry_state: str
    trigger: str
    holding_sessions: int
    target_position: float
    warmup_bars: int = 250


@dataclass(frozen=True)
class ConstituentMoneyflowIntradaySpec:
    """Historical-constituent breadth signal with an intraday T+1 overlay."""

    symbol: str
    source_path: str
    source_sha256: str
    minimum_observed_weight_ratio: float
    threshold_lookback_sessions: int
    threshold_quantile: float
    core_fraction: float
    event_fraction: float
    entry_checkpoint: str
    exit_checkpoint: str
    one_way_cost: float
    lot_size: int
    maximum_events_per_day: int
    t_plus_one_inventory_rotation: bool


@dataclass(frozen=True)
class MarginEntryRiskFilterSpec:
    """Causal margin-behavior veto evaluated before the next-session entry."""

    source_path: str
    source_sha256: str
    rolling_sessions: int
    quantile: float
    same_day_return_maximum: float
    live_preopen_arrival_proven: bool


@dataclass(frozen=True)
class ClosingDislocationOvernightSpec:
    """Intraday closing-dislocation signal held from next open to following open."""

    symbol: str
    price_adjustment: str
    signal_frequency: str
    late_window_minutes: int
    threshold_lookback_sessions: int
    threshold_quantile: float
    threshold_lag_sessions: int
    votes_required: int
    cooldown_sessions: int
    mechanisms: tuple[str, ...]
    entry_risk_filter: MarginEntryRiskFilterSpec | None = None


@dataclass(frozen=True)
class CausalFeatureGateSpec:
    """A frozen causal feature panel evaluated by a weighted score and entry gate."""

    symbol: str
    source_path: str
    source_sha256: str
    orientations: tuple[tuple[str, int], ...]
    base_weights: tuple[tuple[str, float], ...]
    confirmation_weights: tuple[tuple[str, float], ...]
    normalization_lookback_sessions: int
    normalization_minimum_observations: int
    entry_threshold: float
    exit_threshold: float
    confirmation_threshold: float


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    market: str
    asset_type: str
    price_tick: float
    lot_size: int
    maximum_order_quantity: int
    price_limit_ratio: float


@dataclass(frozen=True)
class CapitalSpec:
    mode: str
    fee_rate: float
    target_scope: str
    allocation_fraction: float = 1.0


@dataclass(frozen=True)
class VirtualFillSpec:
    buy_open: str
    buy_intraday: str
    touch_only: str
    sell: str
    liquidity_check: str


@dataclass(frozen=True)
class ExecutionSpec:
    entry_order_type: str
    entry_limit_family: str
    entry_limit_parameter: float
    entry_price_rounding: str
    exit_order_type: str
    exit_limit_ratio: float
    exit_price_rounding: str
    instrument: InstrumentSpec
    capital: CapitalSpec
    virtual_fill: VirtualFillSpec


def _validate_version(version: str) -> None:
    match = _VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError(f"Invalid rule baseline version {version!r}; expected baseline_YYYYMMDD")
    try:
        datetime.strptime(match.group(1), "%Y%m%d")
    except ValueError as exc:
        raise ValueError(
            f"Invalid rule baseline version {version!r}; expected a real date in baseline_YYYYMMDD"
        ) from exc


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON object {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def _repository_root(baseline_root: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return Path(explicit).resolve()
    resolved = Path(baseline_root).resolve()
    return next(
        (parent for parent in (resolved, *resolved.parents) if (parent / "pyproject.toml").is_file()),
        resolved.parent.parent,
    )


def _parse_rule(payload: dict[str, object]) -> Rule:
    required = {
        "weights",
        "enter",
        "exit",
        "confirm_days",
        "min_hold_days",
        "exit_confirm_days",
        "entry_gate",
    }
    missing = sorted(required.difference(payload))
    if missing:
        raise ValueError(f"baseline rule missing fields: {missing}")
    weights_value = payload["weights"]
    if not isinstance(weights_value, list) or len(weights_value) != 3:
        raise ValueError("baseline rule must contain exactly three weights")
    weights = tuple(float(value) for value in weights_value)
    if any(value < 0 for value in weights) or abs(sum(weights) - 1.0) > 1e-9:
        raise ValueError("baseline rule weights must be non-negative and sum to 1")
    enter = float(payload["enter"])
    exit_ = float(payload["exit"])
    if exit_ >= enter:
        raise ValueError("baseline rule exit must be lower than enter")
    confirm_days = int(payload["confirm_days"])
    min_hold_days = int(payload["min_hold_days"])
    exit_confirm_days = int(payload["exit_confirm_days"])
    if min(confirm_days, min_hold_days, exit_confirm_days) < 1:
        raise ValueError("baseline rule day counts must be positive integers")
    gate = str(payload["entry_gate"])
    if gate not in _ENTRY_GATES:
        raise ValueError(f"baseline rule has unknown entry gate: {gate}")
    return Rule(
        weights=weights,
        enter=enter,
        exit=exit_,
        confirm_days=confirm_days,
        min_hold_days=min_hold_days,
        exit_confirm_days=exit_confirm_days,
        entry_gate=gate,
    )


def _parse_four_layer(
    payload: dict[str, object], archived: ResolvedBaseline
) -> tuple[Rule, tuple[str, ...], tuple[float, ...]]:
    names_value = payload.get("factor_names")
    weights_value = payload.get("weights")
    spec = payload.get("spec")
    champion = payload.get("champion")
    if not isinstance(names_value, list) or not isinstance(weights_value, dict):
        raise ValueError("four-layer baseline factors or weights are missing")
    if not isinstance(spec, dict) or not isinstance(champion, dict):
        raise ValueError("four-layer baseline spec or champion is missing")
    factor_names = tuple(map(str, names_value))
    if len(factor_names) != 12 or len(set(factor_names)) != 12:
        raise ValueError("four-layer baseline must freeze 12 unique factors")
    if set(map(str, weights_value)) != set(factor_names):
        raise ValueError("four-layer baseline weight identities differ from factors")
    factor_weights = tuple(float(weights_value[name]) for name in factor_names)
    if any(not np.isfinite(value) for value in factor_weights):
        raise ValueError("four-layer baseline weights must be finite")
    if abs(sum(map(abs, factor_weights)) - 1.0) > 1e-12:
        raise ValueError("four-layer baseline weights must have L1 norm one")
    if str(champion.get("version")) != archived.version:
        raise ValueError("four-layer champion version differs from archived baseline")
    if str(champion.get("sha256")) != archived.sha256:
        raise ValueError("four-layer champion SHA-256 differs from archived baseline")
    enter = float(spec["enter"])
    exit_ = float(spec["exit"])
    if exit_ >= enter:
        raise ValueError("four-layer baseline exit must be lower than enter")
    state = archived.rule
    return (
        Rule(
            weights=state.weights,
            enter=enter,
            exit=exit_,
            confirm_days=state.confirm_days,
            min_hold_days=state.min_hold_days,
            exit_confirm_days=state.exit_confirm_days,
            entry_gate=state.entry_gate,
        ),
        factor_names,
        factor_weights,
    )


def _parse_regime_weight(
    payload: dict[str, object], base: ResolvedBaseline
) -> tuple[Rule, tuple[str, ...], tuple[float, ...], dict[str, tuple[float, ...]], int, float]:
    names_value = payload.get("factor_names")
    weights_value = payload.get("weights")
    parent = payload.get("baseline")
    if not isinstance(names_value, list) or not isinstance(weights_value, dict):
        raise ValueError("regime-weight baseline factors or weights are missing")
    if not isinstance(parent, dict):
        raise ValueError("regime-weight parent baseline is missing")
    factor_names = tuple(map(str, names_value))
    if len(factor_names) != 12 or len(set(factor_names)) != 12:
        raise ValueError("regime-weight baseline must freeze 12 unique factors")
    if str(parent.get("version")) != base.version or str(parent.get("sha256")) != base.sha256:
        raise ValueError("regime-weight parent baseline identity differs")
    if tuple(base.factor_names) != factor_names:
        raise ValueError("regime-weight factor identities differ from parent baseline")
    if set(weights_value) != {"trend", "range"}:
        raise ValueError("regime-weight baseline must contain trend and range weights")
    regime_weights: dict[str, tuple[float, ...]] = {}
    for label in ("trend", "range"):
        selected = weights_value[label]
        if not isinstance(selected, dict) or set(map(str, selected)) != set(factor_names):
            raise ValueError(f"regime-weight {label} identities differ from factors")
        values = tuple(float(selected[name]) for name in factor_names)
        if any(not np.isfinite(value) for value in values):
            raise ValueError(f"regime-weight {label} weights must be finite")
        if abs(sum(map(abs, values)) - 1.0) > 1e-12:
            raise ValueError(f"regime-weight {label} weights must have L1 norm one")
        regime_weights[label] = values
    lookback = int(payload.get("er_lookback", 0))
    threshold = float(payload.get("er_threshold", float("nan")))
    if lookback < 2 or not np.isfinite(threshold):
        raise ValueError("regime-weight ER definition is invalid")
    if payload.get("regime_labels") != ["trend", "range", "warmup"]:
        raise ValueError("regime-weight labels differ from frozen definition")
    execution_value = payload.get("execution")
    if execution_value != "next_session_open" and not isinstance(execution_value, dict):
        raise ValueError("regime-weight execution differs from frozen definition")
    enter = float(payload["entry_threshold"])
    exit_ = float(payload["exit_threshold"])
    if exit_ >= enter:
        raise ValueError("regime-weight exit must be lower than enter")
    return (
        Rule(
            weights=base.rule.weights,
            enter=enter,
            exit=exit_,
            confirm_days=int(payload["confirm_days"]),
            min_hold_days=int(payload["min_hold_days"]),
            exit_confirm_days=int(payload["exit_confirm_days"]),
            entry_gate=base.rule.entry_gate,
        ),
        factor_names,
        tuple(base.factor_weights),
        regime_weights,
        lookback,
        threshold,
    )


def _parse_execution(payload: dict[str, object], symbol: str | None) -> ExecutionSpec:
    value = payload.get("execution")
    if not isinstance(value, dict):
        raise ValueError("active baseline must contain a complete execution object")
    entry = value.get("entry")
    exit_ = value.get("exit")
    instrument = value.get("instrument")
    capital = value.get("capital")
    virtual_fill = value.get("virtual_fill")
    if not all(isinstance(item, dict) for item in (entry, exit_, instrument, capital, virtual_fill)):
        raise ValueError("complete execution sections are missing")
    assert isinstance(entry, dict) and isinstance(exit_, dict)
    assert isinstance(instrument, dict) and isinstance(capital, dict)
    assert isinstance(virtual_fill, dict)
    if entry.get("order_type") != "LIMIT" or entry.get("limit_family") != "previous_close_ratio":
        raise ValueError("unsupported entry execution rule")
    if entry.get("price_rounding") != "floor":
        raise ValueError("unsupported entry price rounding")
    if exit_.get("order_type") != "LIMIT" or exit_.get("price_rounding") != "nearest_half_up":
        raise ValueError("unsupported exit execution rule")
    item = InstrumentSpec(
        symbol=str(instrument.get("symbol", "")).upper(),
        market=str(instrument.get("market", "")),
        asset_type=str(instrument.get("asset_type", "")),
        price_tick=float(instrument.get("price_tick", 0)),
        lot_size=int(instrument.get("lot_size", 0)),
        maximum_order_quantity=int(instrument.get("maximum_order_quantity", 0)),
        price_limit_ratio=float(instrument.get("price_limit_ratio", 0)),
    )
    if symbol is not None and item.symbol != str(symbol).upper():
        raise ValueError("complete execution symbol differs from requested symbol")
    if item.market != "CN" or item.asset_type != "etf":
        raise ValueError("complete execution instrument is unsupported")
    if item.price_tick <= 0 or item.lot_size != 100 or item.maximum_order_quantity <= 0:
        raise ValueError("complete execution instrument limits are invalid")
    cap = CapitalSpec(
        mode=str(capital.get("mode", "")),
        fee_rate=float(capital.get("fee_rate", -1)),
        target_scope=str(capital.get("target_scope", "")),
        allocation_fraction=float(capital.get("allocation_fraction", 1.0)),
    )
    if cap.mode not in {"full_available_cash", "available_cash_fraction"}:
        raise ValueError("complete baseline capital rule is unsupported")
    if cap.target_scope != "entry_cycle":
        raise ValueError("complete baseline capital target scope is unsupported")
    if not 0 < cap.allocation_fraction <= 1:
        raise ValueError("complete baseline capital allocation fraction is invalid")
    if cap.mode == "full_available_cash" and cap.allocation_fraction != 1.0:
        raise ValueError("full available cash mode requires allocation fraction one")
    if not 0 <= cap.fee_rate < 1:
        raise ValueError("complete baseline fee rate is invalid")
    fill = VirtualFillSpec(
        buy_open=str(virtual_fill.get("buy_open", "")),
        buy_intraday=str(virtual_fill.get("buy_intraday", "")),
        touch_only=str(virtual_fill.get("touch_only", "")),
        sell=str(virtual_fill.get("sell", "")),
        liquidity_check=str(virtual_fill.get("liquidity_check", "")),
    )
    if fill != VirtualFillSpec(
        "open_at_or_below_limit", "low_strictly_below_limit", "uncertain_unfilled",
        "marketable_limit_at_open", "diagnostic_only",
    ):
        raise ValueError("complete baseline virtual fill rule is unsupported")
    exit_ratio = float(exit_.get("limit_ratio", 0))
    if exit_ratio != item.price_limit_ratio:
        raise ValueError("baseline exit ratio differs from instrument price limit ratio")
    return ExecutionSpec(
        entry_order_type="LIMIT",
        entry_limit_family="previous_close_ratio",
        entry_limit_parameter=float(entry.get("limit_parameter", 0)),
        entry_price_rounding="floor",
        exit_order_type="LIMIT",
        exit_limit_ratio=exit_ratio,
        exit_price_rounding="nearest_half_up",
        instrument=item,
        capital=cap,
        virtual_fill=fill,
    )


def _parse_event_hold(
    payload: dict[str, object],
    *,
    symbol: str,
) -> EventHoldSpec:
    signal = payload.get("signal")
    portfolio = payload.get("portfolio_rule")
    if not isinstance(signal, dict) or not isinstance(portfolio, dict):
        raise ValueError("event-hold signal or portfolio rule is missing")
    config = signal.get("config")
    if not isinstance(config, dict):
        raise ValueError("event-hold signal config is missing")
    frequency = str(signal.get("frequency", ""))
    name = str(signal.get("name", ""))
    if frequency not in {"daily", "日线"}:
        raise ValueError("event-hold currently supports daily signals only")
    if str(config.get("name", "")) != name or str(config.get("freq", "")) != "日线":
        raise ValueError("event-hold signal identity differs from its config")
    if str(signal.get("trigger", "")) != "fresh_transition":
        raise ValueError("event-hold requires a fresh-transition trigger")
    if float(portfolio.get("flat_position", -1)) != 0.0:
        raise ValueError("event-hold flat position must be zero")
    target = float(portfolio.get("target_position", 0))
    holding = int(portfolio.get("holding_sessions", 0))
    if target != 1.0 or holding < 1:
        raise ValueError("event-hold target or holding period is invalid")
    if portfolio.get("ignore_entries_while_holding") is not True:
        raise ValueError("event-hold must ignore entries while holding")
    if portfolio.get("require_fresh_transition_after_exit") is not True:
        raise ValueError("event-hold must require a fresh transition after exit")
    risks = payload.get("risk_filters", [])
    if risks != []:
        raise ValueError("event-hold risk filters are not supported by this frozen mechanism")
    adjustment = str(payload.get("price_adjustment", ""))
    if adjustment != "hfq":
        raise ValueError("event-hold requires hfq signal prices")
    output_key = str(signal.get("output_key", ""))
    entry_state = str(signal.get("entry_state", ""))
    if not name or not output_key or not entry_state:
        raise ValueError("event-hold signal declaration is incomplete")
    declared_symbol = str(payload.get("symbol", symbol)).upper()
    if declared_symbol != symbol.upper():
        raise ValueError("event-hold symbol differs from requested symbol")
    return EventHoldSpec(
        symbol=declared_symbol,
        price_adjustment=adjustment,
        signal_frequency="日线",
        signal_name=name,
        signal_config=dict(config),
        output_key=output_key,
        entry_state=entry_state,
        trigger="fresh_transition",
        holding_sessions=holding,
        target_position=target,
        warmup_bars=int(signal.get("warmup_bars", 250)),
    )


def _parse_constituent_moneyflow_intraday(
    payload: dict[str, object],
    *,
    symbol: str,
    repository_root: Path | None,
) -> ConstituentMoneyflowIntradaySpec:
    rule = payload.get("rule")
    if not isinstance(rule, dict):
        raise ValueError("constituent-moneyflow strategy must contain a rule object")
    declared_symbol = str(rule.get("symbol", payload.get("symbol", ""))).upper()
    if declared_symbol != symbol.upper():
        raise ValueError("constituent-moneyflow symbol differs from requested symbol")
    source = rule.get("data_source")
    feature = rule.get("feature")
    execution = rule.get("execution")
    if not isinstance(source, dict) or not isinstance(feature, dict) or not isinstance(execution, dict):
        raise ValueError("constituent-moneyflow data source, feature or execution is missing")
    source_path = str(source.get("path", ""))
    source_sha256 = str(source.get("sha256", "")).lower()
    if not source_path or len(source_sha256) != 64:
        raise ValueError("constituent-moneyflow source identity is incomplete")
    if repository_root is None:
        raise ValueError("constituent-moneyflow strategy requires a repository root")
    resolved_source = resolve_repository_experiment_reference(repository_root, source_path)
    if not resolved_source.is_file():
        raise ValueError(f"constituent-moneyflow source is missing: {source_path}")
    if raw_file_sha256(resolved_source) != source_sha256:
        raise ValueError("constituent-moneyflow source SHA-256 differs")

    observed_ratio = float(feature.get("minimum_observed_weight_ratio", 0))
    lookback = int(feature.get("threshold_lookback_sessions", 0))
    quantile = float(feature.get("threshold_quantile", -1))
    if not 0 < observed_ratio <= 1 or lookback < 2 or not 0 < quantile < 1:
        raise ValueError("constituent-moneyflow feature parameters are invalid")
    if feature.get("threshold_excludes_current_session") is not True:
        raise ValueError("constituent-moneyflow threshold must exclude the current session")
    if str(feature.get("comparison", "")) != "GREATER_THAN_OR_EQUAL":
        raise ValueError("constituent-moneyflow comparison is unsupported")

    core_fraction = float(execution.get("core_fraction", 0))
    event_fraction = float(execution.get("event_fraction", 0))
    if abs(core_fraction + event_fraction - 1.0) > 1e-12 or min(core_fraction, event_fraction) <= 0:
        raise ValueError("intraday overlay fractions must be positive and sum to one")
    entry_checkpoint = str(execution.get("entry_checkpoint", ""))
    exit_checkpoint = str(execution.get("exit_checkpoint", ""))
    if entry_checkpoint != "OPEN" or exit_checkpoint != "11:30_CLOSE":
        raise ValueError("intraday overlay execution checkpoints are unsupported")
    one_way_cost = float(execution.get("one_way_cost", -1))
    lot_size = int(execution.get("lot_size", 0))
    maximum_events = int(execution.get("maximum_events_per_day", 0))
    if not 0 <= one_way_cost < 0.01 or lot_size < 1 or maximum_events != 1:
        raise ValueError("intraday overlay execution parameters are invalid")
    if execution.get("t_plus_one_inventory_rotation") is not True:
        raise ValueError("intraday overlay requires T+1 inventory rotation")
    return ConstituentMoneyflowIntradaySpec(
        symbol=declared_symbol,
        source_path=source_path,
        source_sha256=source_sha256,
        minimum_observed_weight_ratio=observed_ratio,
        threshold_lookback_sessions=lookback,
        threshold_quantile=quantile,
        core_fraction=core_fraction,
        event_fraction=event_fraction,
        entry_checkpoint=entry_checkpoint,
        exit_checkpoint=exit_checkpoint,
        one_way_cost=one_way_cost,
        lot_size=lot_size,
        maximum_events_per_day=maximum_events,
        t_plus_one_inventory_rotation=True,
    )


def _parse_causal_feature_gate(
    payload: dict[str, object],
    *,
    symbol: str,
    repository_root: Path | None,
) -> CausalFeatureGateSpec:
    rule = payload.get("rule")
    if not isinstance(rule, dict):
        raise ValueError("causal-feature-gate strategy must contain a rule object")
    declared_symbol = str(rule.get("symbol", payload.get("symbol", ""))).upper()
    if declared_symbol != symbol.upper():
        raise ValueError("causal-feature-gate symbol differs from requested symbol")
    source = rule.get("data_source")
    normalization = rule.get("normalization")
    score = rule.get("score")
    if not all(isinstance(item, dict) for item in (source, normalization, score)):
        raise ValueError("causal-feature-gate source, normalization or score is missing")
    assert isinstance(source, dict) and isinstance(normalization, dict) and isinstance(score, dict)
    source_path = str(source.get("path", ""))
    source_sha256 = str(source.get("sha256", "")).lower()
    if not source_path or len(source_sha256) != 64:
        raise ValueError("causal-feature-gate source identity is incomplete")
    if repository_root is None:
        raise ValueError("causal-feature-gate strategy requires a repository root")
    resolved_source = resolve_repository_experiment_reference(repository_root, source_path)
    if not resolved_source.is_file() or raw_file_sha256(resolved_source) != source_sha256:
        raise ValueError("causal-feature-gate source is missing or differs")
    if normalization.get("method") != "CAUSAL_ROLLING_PERCENTILE_CENTERED":
        raise ValueError("causal-feature-gate normalization is unsupported")
    lookback = int(normalization.get("lookback_sessions", 0))
    minimum = int(normalization.get("minimum_observations", 0))
    if lookback < 2 or minimum < 2 or minimum > lookback:
        raise ValueError("causal-feature-gate normalization window is invalid")

    def pairs(name: str, *, signed: bool) -> tuple[tuple[str, float], ...]:
        value = score.get(name)
        if not isinstance(value, dict) or not value:
            raise ValueError(f"causal-feature-gate {name} is missing")
        result = tuple(sorted((str(key), float(item)) for key, item in value.items()))
        if any(not key or not np.isfinite(item) for key, item in result):
            raise ValueError(f"causal-feature-gate {name} contains invalid values")
        if not signed and (any(item < 0 for _, item in result) or not np.isclose(sum(item for _, item in result), 1.0)):
            raise ValueError(f"causal-feature-gate {name} must be nonnegative and sum to one")
        return result

    orientations_raw = pairs("orientations", signed=True)
    if any(value not in {-1.0, 1.0} for _, value in orientations_raw):
        raise ValueError("causal-feature-gate orientations must be -1 or 1")
    orientations = tuple((key, int(value)) for key, value in orientations_raw)
    base_weights = pairs("base_weights", signed=False)
    confirmation_weights = pairs("confirmation_weights", signed=False)
    scored = {key for key, _ in base_weights} | {key for key, _ in confirmation_weights}
    if {key for key, _ in orientations} != scored:
        raise ValueError("causal-feature-gate orientation identities differ from scored features")
    if {key for key, _ in base_weights} & {key for key, _ in confirmation_weights}:
        raise ValueError("causal-feature-gate base and confirmation features overlap")
    entry = float(score.get("entry_threshold", float("nan")))
    exit_ = float(score.get("exit_threshold", float("nan")))
    confirmation = float(score.get("confirmation_threshold", float("nan")))
    if not all(np.isfinite(value) for value in (entry, exit_, confirmation)) or exit_ >= entry:
        raise ValueError("causal-feature-gate thresholds are invalid")
    return CausalFeatureGateSpec(
        symbol=declared_symbol,
        source_path=source_path,
        source_sha256=source_sha256,
        orientations=orientations,
        base_weights=base_weights,
        confirmation_weights=confirmation_weights,
        normalization_lookback_sessions=lookback,
        normalization_minimum_observations=minimum,
        entry_threshold=entry,
        exit_threshold=exit_,
        confirmation_threshold=confirmation,
    )


def _parse_closing_dislocation_overnight(
    payload: dict[str, object],
    *,
    symbol: str,
    repository_root: Path | None,
) -> ClosingDislocationOvernightSpec:
    rule = payload.get("rule")
    if not isinstance(rule, dict):
        raise ValueError("closing-dislocation strategy must contain a rule object")
    declared_symbol = str(rule.get("symbol", payload.get("symbol", ""))).upper()
    if declared_symbol != symbol.upper():
        raise ValueError("closing-dislocation symbol differs from requested symbol")
    feature = rule.get("feature")
    holding = rule.get("holding")
    if not isinstance(feature, dict) or not isinstance(holding, dict):
        raise ValueError("closing-dislocation feature or holding rule is missing")
    mechanisms_value = feature.get("mechanisms")
    if not isinstance(mechanisms_value, list):
        raise ValueError("closing-dislocation mechanisms are missing")
    mechanisms = tuple(map(str, mechanisms_value))
    expected = (
        "LATE_PATH_SELLING",
        "LATE_VOLUME_PRESSURE",
        "CLOSE_VWAP_DISLOCATION",
    )
    if mechanisms != expected:
        raise ValueError("closing-dislocation mechanism identities or order differ")
    price_adjustment = str(feature.get("price_adjustment", ""))
    signal_frequency = str(feature.get("signal_frequency", ""))
    late_window = int(feature.get("late_window_minutes", 0))
    lookback = int(feature.get("threshold_lookback_sessions", 0))
    quantile = float(feature.get("threshold_quantile", -1))
    lag = int(feature.get("threshold_lag_sessions", 0))
    votes = int(feature.get("votes_required", 0))
    cooldown = int(feature.get("cooldown_sessions", -1))
    if price_adjustment != "hfq" or signal_frequency != "1m":
        raise ValueError("closing-dislocation requires 1m hfq signal prices")
    if late_window != 60 or lookback < 2 or not 0 < quantile < 1:
        raise ValueError("closing-dislocation feature parameters are invalid")
    if lag != 1 or votes < 1 or votes > len(mechanisms) or cooldown < 0:
        raise ValueError("closing-dislocation causal or consensus parameters are invalid")
    if feature.get("threshold_excludes_current_session") is not True:
        raise ValueError("closing-dislocation threshold must exclude the current session")
    if str(feature.get("comparison", "")) != "GREATER_THAN_OR_EQUAL":
        raise ValueError("closing-dislocation comparison is unsupported")
    if holding != {
        "entry": "NEXT_SESSION_OPEN",
        "exit": "SESSION_AFTER_ENTRY_OPEN",
    }:
        raise ValueError("closing-dislocation holding rule is unsupported")
    risk_value = rule.get("entry_risk_filter")
    risk_filter: MarginEntryRiskFilterSpec | None = None
    if risk_value is not None:
        if not isinstance(risk_value, dict):
            raise ValueError("closing-dislocation entry risk filter must be an object")
        source = risk_value.get("source")
        if not isinstance(source, dict):
            raise ValueError("closing-dislocation entry risk source is missing")
        source_path = str(source.get("path", ""))
        source_sha256 = str(source.get("sha256", "")).lower()
        if not source_path or len(source_sha256) != 64:
            raise ValueError("closing-dislocation entry risk source identity is incomplete")
        if repository_root is None:
            raise ValueError("closing-dislocation entry risk filter requires a repository root")
        resolved_source = resolve_repository_experiment_reference(repository_root, source_path)
        if not resolved_source.is_file():
            raise ValueError(f"closing-dislocation entry risk source is missing: {source_path}")
        if raw_file_sha256(resolved_source) != source_sha256:
            raise ValueError("closing-dislocation entry risk source SHA-256 differs")
        if str(source.get("endpoint", "")) != "margin_detail":
            raise ValueError("closing-dislocation entry risk source endpoint is unsupported")
        if str(risk_value.get("action", "")) != "DENY_NEXT_SESSION_ENTRY":
            raise ValueError("closing-dislocation entry risk action is unsupported")
        if str(risk_value.get("net_flow", "")) != "RZMRE_MINUS_RZCHE_DIVIDED_BY_PREVIOUS_RZYE":
            raise ValueError("closing-dislocation entry risk net-flow definition is unsupported")
        risk_lookback = int(risk_value.get("rolling_sessions", 0))
        risk_quantile = float(risk_value.get("quantile", -1))
        same_day_maximum = float(risk_value.get("same_day_return_maximum", 1))
        if risk_lookback < 2 or not 0 < risk_quantile < 1 or same_day_maximum != 0.0:
            raise ValueError("closing-dislocation entry risk parameters are invalid")
        if risk_value.get("requires_positive_net_flow") is not True:
            raise ValueError("closing-dislocation entry risk requires positive net flow")
        if risk_value.get("threshold_excludes_current_session") is not True:
            raise ValueError("closing-dislocation entry risk threshold must exclude current session")
        arrival_proven = risk_value.get("live_preopen_arrival_proven")
        if not isinstance(arrival_proven, bool):
            raise ValueError("closing-dislocation entry risk arrival status must be boolean")
        risk_filter = MarginEntryRiskFilterSpec(
            source_path=source_path,
            source_sha256=source_sha256,
            rolling_sessions=risk_lookback,
            quantile=risk_quantile,
            same_day_return_maximum=same_day_maximum,
            live_preopen_arrival_proven=arrival_proven,
        )
    return ClosingDislocationOvernightSpec(
        symbol=declared_symbol,
        price_adjustment=price_adjustment,
        signal_frequency=signal_frequency,
        late_window_minutes=late_window,
        threshold_lookback_sessions=lookback,
        threshold_quantile=quantile,
        threshold_lag_sessions=lag,
        votes_required=votes,
        cooldown_sessions=cooldown,
        mechanisms=mechanisms,
        entry_risk_filter=risk_filter,
    )


def resolve_baseline(
    root: Path,
    version: str | None = None,
    *,
    symbol: str | None = None,
    repository_root: Path | None = None,
) -> ResolvedBaseline:
    """Resolve and verify one immutable baseline."""
    root = Path(root)
    registry = _load_json_object(root / "registry.json")
    explicit = version is not None
    selected_version = str(version or registry.get("latest", ""))
    _validate_version(selected_version)
    baselines = registry.get("baselines")
    if not isinstance(baselines, dict) or selected_version not in baselines:
        raise ValueError(f"Unknown rule baseline: {selected_version}")
    entry = baselines[selected_version]
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid registry entry for {selected_version}")
    status = str(entry.get("status", "active"))
    scope = str(entry.get("scope", "generic"))
    if status not in {"active", "archived"}:
        raise ValueError(f"{selected_version}: unknown baseline status {status!r}")
    if status == "archived" and not explicit:
        raise ValueError(f"{selected_version} is archived and requires an explicit version")
    scoped_symbol = str(entry.get("symbol", "")) or None
    if scope == "symbol":
        if scoped_symbol is None:
            raise ValueError(f"{selected_version}: symbol scope is missing its symbol")
        if symbol is None:
            raise ValueError(f"{selected_version} requires symbol {scoped_symbol}")
        if str(symbol) != scoped_symbol:
            raise ValueError(f"{selected_version} is restricted to {scoped_symbol}")
    filename = str(entry.get("file", ""))
    expected_filename = f"{selected_version}.json"
    if filename != expected_filename:
        raise ValueError(f"{selected_version} must use file {expected_filename}")
    rule_path = root / filename
    if not rule_path.is_file():
        raise ValueError(f"Missing baseline file: {rule_path}")
    payload = _load_json_object(rule_path)
    digest = canonical_json_sha256(payload)
    expected = str(entry.get("sha256", ""))
    if digest != expected:
        raise ValueError(f"{selected_version}: SHA-256 differs from registry")
    strategy = str(entry.get("strategy", "czsc_fixed_rule"))
    factor_names: tuple[str, ...] = ()
    factor_weights: tuple[float, ...] = ()
    regime_factor_weights: dict[str, tuple[float, ...]] = {}
    er_lookback = 0
    er_threshold = 0.0
    source_path = str(entry.get("source_path", ""))
    source_digest = str(entry.get("source_sha256", ""))
    if strategy == "czsc_fixed_rule":
        rule = _parse_rule(payload)
    elif strategy == "czsc_four_layer":
        if not source_path or not source_digest:
            raise ValueError(f"{selected_version}: four-layer source identity is missing")
        source_root = _repository_root(root, repository_root)
        source = resolve_repository_experiment_reference(source_root, source_path)
        if not source.is_file():
            raise ValueError(f"{selected_version}: missing four-layer source {source}")
        if canonical_json_sha256(source) != source_digest:
            raise ValueError(f"{selected_version}: source SHA-256 differs from registry")
        if _load_json_object(source) != payload:
            raise ValueError(f"{selected_version}: promoted payload differs from source")
        champion = payload.get("champion")
        if not isinstance(champion, dict):
            raise ValueError("four-layer baseline champion is missing")
        archived = resolve_baseline(
            root,
            str(champion.get("version", "")),
            repository_root=source_root,
        )
        rule, factor_names, factor_weights = _parse_four_layer(payload, archived)
    elif strategy == "czsc_regime_weight":
        if not source_path or not source_digest:
            raise ValueError(f"{selected_version}: regime-weight source identity is missing")
        source_root = _repository_root(root, repository_root)
        source = resolve_repository_experiment_reference(source_root, source_path)
        if not source.is_file():
            raise ValueError(f"{selected_version}: missing regime-weight source {source}")
        if canonical_json_sha256(source) != source_digest:
            raise ValueError(f"{selected_version}: source SHA-256 differs from registry")
        source_payload = _load_json_object(source)
        if canonical_json_sha256(source_payload) != digest:
            raise ValueError(f"{selected_version}: promoted payload differs from source")
        parent = payload.get("baseline")
        if not isinstance(parent, dict):
            raise ValueError("regime-weight parent baseline is missing")
        base = resolve_baseline(
            root,
            str(parent.get("version", "")),
            repository_root=source_root,
        )
        (
            rule,
            factor_names,
            factor_weights,
            regime_factor_weights,
            er_lookback,
            er_threshold,
        ) = _parse_regime_weight(payload, base)
    else:
        raise ValueError(f"{selected_version}: unknown baseline strategy {strategy!r}")
    execution = _parse_execution(payload, symbol) if status == "active" else None
    return ResolvedBaseline(
        version=selected_version,
        rule=rule,
        rule_payload=payload,
        sha256=digest,
        strategy=strategy,
        status=status,
        scope=scope,
        symbol=scoped_symbol,
        factor_names=factor_names,
        factor_weights=factor_weights,
        regime_factor_weights=regime_factor_weights,
        er_lookback=er_lookback,
        er_threshold=er_threshold,
        source_path=source_path,
        source_sha256=source_digest,
        selection_sample_end=str(entry.get("selection_sample_end", "")),
        forward_validation_start=str(entry.get("forward_validation_start", "")),
        execution=execution,
    )


def resolve_strategy_payload(
    baseline_root: Path,
    strategy_payload: dict[str, object],
    *,
    release_id: str,
    release_hash: str,
    symbol: str,
    repository_root: Path | None = None,
) -> ResolvedBaseline:
    """Parse a frozen Strategy Manager payload without creating another baseline."""
    strategy_kind = str(strategy_payload.get("strategy_kind", ""))
    if strategy_kind == "causal_feature_gate":
        spec = _parse_causal_feature_gate(
            strategy_payload,
            symbol=symbol,
            repository_root=repository_root,
        )
        rule_payload = strategy_payload.get("rule")
        assert isinstance(rule_payload, dict)
        execution = _parse_execution(rule_payload, symbol)
        return ResolvedBaseline(
            version=release_id,
            rule=None,
            rule_payload=dict(rule_payload),
            sha256=release_hash,
            strategy=strategy_kind,
            status="active",
            scope="symbol",
            symbol=symbol,
            execution=execution,
            causal_feature_gate=spec,
        )
    if strategy_kind == "closing_dislocation_overnight":
        spec = _parse_closing_dislocation_overnight(
            strategy_payload,
            symbol=symbol,
            repository_root=repository_root,
        )
        rule_payload = strategy_payload.get("rule")
        assert isinstance(rule_payload, dict)
        execution = _parse_execution(rule_payload, symbol)
        return ResolvedBaseline(
            version=release_id,
            rule=None,
            rule_payload=dict(rule_payload),
            sha256=release_hash,
            strategy=strategy_kind,
            status="active",
            scope="symbol",
            symbol=symbol,
            execution=execution,
            closing_dislocation_overnight=spec,
        )
    if strategy_kind == "constituent_moneyflow_intraday_overlay":
        spec = _parse_constituent_moneyflow_intraday(
            strategy_payload,
            symbol=symbol,
            repository_root=repository_root,
        )
        rule_payload = strategy_payload.get("rule")
        assert isinstance(rule_payload, dict)
        return ResolvedBaseline(
            version=release_id,
            rule=None,
            rule_payload=dict(rule_payload),
            sha256=release_hash,
            strategy=strategy_kind,
            status="active",
            scope="symbol",
            symbol=symbol,
            constituent_moneyflow_intraday=spec,
        )
    if strategy_kind == "czsc_event_hold":
        nested = strategy_payload.get("rule")
        rule_payload = dict(nested) if isinstance(nested, dict) else dict(strategy_payload)
        if "symbol" not in rule_payload and "symbol" in strategy_payload:
            rule_payload["symbol"] = strategy_payload["symbol"]
        event_hold = _parse_event_hold(rule_payload, symbol=symbol)
        execution_value = rule_payload.get("execution")
        execution = (
            _parse_execution(rule_payload, symbol)
            if isinstance(execution_value, dict) and isinstance(execution_value.get("instrument"), dict)
            else None
        )
        return ResolvedBaseline(
            version=release_id,
            rule=None,
            rule_payload=rule_payload,
            sha256=release_hash,
            strategy="czsc_event_hold",
            status="active",
            scope="symbol",
            symbol=symbol,
            execution=execution,
            event_hold=event_hold,
        )
    rule_payload = strategy_payload.get("rule")
    if not isinstance(rule_payload, dict):
        raise ValueError("strategy payload must contain a complete rule object")
    factor_names: tuple[str, ...] = ()
    factor_weights: tuple[float, ...] = ()
    regime_factor_weights: dict[str, tuple[float, ...]] = {}
    er_lookback = 0
    er_threshold = 0.0
    if "er_lookback" in rule_payload:
        parent = rule_payload.get("baseline")
        if not isinstance(parent, dict):
            raise ValueError("regime strategy parent baseline is missing")
        base = resolve_baseline(
            Path(baseline_root),
            str(parent.get("version", "")),
            repository_root=repository_root,
        )
        (
            rule,
            factor_names,
            factor_weights,
            regime_factor_weights,
            er_lookback,
            er_threshold,
        ) = _parse_regime_weight(rule_payload, base)
        strategy_kind = "czsc_regime_weight"
    elif "factor_names" in rule_payload:
        champion = rule_payload.get("champion")
        if not isinstance(champion, dict):
            raise ValueError("four-layer strategy champion is missing")
        base = resolve_baseline(
            Path(baseline_root),
            str(champion.get("version", "")),
            repository_root=repository_root,
        )
        rule, factor_names, factor_weights = _parse_four_layer(rule_payload, base)
        strategy_kind = "czsc_four_layer"
    else:
        rule = _parse_rule(rule_payload)
        strategy_kind = "czsc_fixed_rule"
    execution = _parse_execution(rule_payload, symbol)
    return ResolvedBaseline(
        version=release_id,
        rule=rule,
        rule_payload=rule_payload,
        sha256=release_hash,
        strategy=strategy_kind,
        status="active",
        scope="symbol",
        symbol=symbol,
        factor_names=factor_names,
        factor_weights=factor_weights,
        regime_factor_weights=regime_factor_weights,
        er_lookback=er_lookback,
        er_threshold=er_threshold,
        selection_sample_end="",
        forward_validation_start="",
        execution=execution,
    )


def promote_baseline(
    root: Path,
    selected_rule: Path,
    now: datetime,
) -> ResolvedBaseline:
    """Explicitly freeze a selected rule as the next baseline version."""
    root = Path(root)
    registry_path = root / "registry.json"
    registry = _load_json_object(registry_path)
    baselines = registry.get("baselines")
    if not isinstance(baselines, dict):
        raise ValueError("registry baselines must be an object")
    payload = _load_json_object(Path(selected_rule))
    _parse_rule(payload)
    version = f"baseline_{now.strftime('%Y%m%d')}"
    rule_path = root / f"{version}.json"
    if version in baselines or rule_path.exists():
        raise FileExistsError(f"Baseline already exists: {rule_path}")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    rule_path.write_text(text, encoding="utf-8")
    digest = canonical_json_sha256(payload)
    baselines[version] = {
        "file": rule_path.name,
        "sha256": digest,
        "frozen_at_utc": now.isoformat(),
        "strategy": "czsc_fixed_rule",
        "required_frequencies": ["30m", "daily", "weekly"],
    }
    registry["latest"] = version
    temporary = registry_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(registry_path)
    return resolve_baseline(root, version)
