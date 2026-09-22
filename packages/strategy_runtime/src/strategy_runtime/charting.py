"""SRT-owned chart context and strategy chart renderer loading."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from importlib import import_module, invalidate_caches
import json
import math
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Mapping

from .deployment import load_strategy_deployment
from .errors import RuntimeCompatibilityError, RuntimeContractError
from .implementation_identity import implementation_sha256


CHART_CONTEXT_VERSION = "strategy_chart.v1"
CHART_CONTRACT_VERSION = 1
_MODES = {"BACKTEST", "FORWARD_OBSERVATION"}
_PLOTLY_RUNTIMES = {"embedded", "external"}
_TOP_LEVEL = {
    "contract_version",
    "mode",
    "strategy",
    "window",
    "market_data",
    "strategy_output",
    "execution",
    "render",
}


def _date(value: object, field: str) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError(f"{field} must be an ISO date") from exc


def _finite(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise RuntimeContractError(f"{field} must be finite")
    return number


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{field} must be an object")
    return deepcopy(dict(value))


def _required_text(value: Mapping[str, Any], field: str, scope: str) -> str:
    text = value.get(field)
    if not isinstance(text, str) or not text.strip():
        raise RuntimeContractError(f"{scope}.{field} is required")
    return text.strip()


def validate_chart_context(value: object, *, expected_mode: str | None = None) -> dict[str, Any]:
    """Validate and normalize one host-supplied, strategy-neutral chart context."""

    context = _mapping(value, "chart context")
    if set(context) != _TOP_LEVEL:
        raise RuntimeContractError("chart context fields are incomplete or unknown")
    if context.get("contract_version") != CHART_CONTEXT_VERSION:
        raise RuntimeContractError(f"chart context must use {CHART_CONTEXT_VERSION}")
    mode = context.get("mode")
    if mode not in _MODES or (expected_mode is not None and mode != expected_mode):
        raise RuntimeContractError("chart context mode is invalid")

    strategy = _mapping(context["strategy"], "strategy")
    for field in ("strategy_id", "reference_id", "identity_hash", "symbol"):
        strategy[field] = _required_text(strategy, field, "strategy")
    if not strategy["reference_id"].startswith(f'{strategy["strategy_id"]}-'):
        raise RuntimeContractError("strategy reference does not belong to strategy_id")
    context["strategy"] = strategy

    window = _mapping(context["window"], "window")
    if mode == "BACKTEST":
        start = _date(window.get("evaluation_start"), "window.evaluation_start")
        end = _date(window.get("evaluation_end"), "window.evaluation_end")
        if start > end:
            raise RuntimeContractError("backtest chart window is reversed")
        window["evaluation_start"] = start
        window["evaluation_end"] = end
    else:
        window["selection_data_cutoff"] = _date(
            window.get("selection_data_cutoff"), "window.selection_data_cutoff"
        )
        sessions = window.get("context_sessions")
        if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions <= 0:
            raise RuntimeContractError("window.context_sessions must be a positive integer")
        _required_text(strategy, "account_id", "strategy")
    context["window"] = window

    market = _mapping(context["market_data"], "market_data")
    if market.get("adjustment") != "hfq":
        raise RuntimeContractError("market_data.adjustment must be hfq")
    _required_text(market, "identity", "market_data")
    bars = market.get("bars")
    if not isinstance(bars, list) or not bars:
        raise RuntimeContractError("market_data.bars must be a non-empty list")
    prior: str | None = None
    normalized_bars: list[dict[str, Any]] = []
    for index, raw in enumerate(bars):
        bar = _mapping(raw, f"market_data.bars[{index}]")
        session = _date(bar.get("date"), f"market_data.bars[{index}].date")
        if prior is not None and session <= prior:
            raise RuntimeContractError("market_data.bars must be unique and increasing")
        prior = session
        bar["date"] = session
        for field in ("open", "high", "low", "close"):
            bar[field] = _finite(bar.get(field), f"market_data.bars[{index}].{field}")
        if not (
            bar["low"] <= bar["open"] <= bar["high"]
            and bar["low"] <= bar["close"] <= bar["high"]
        ):
            raise RuntimeContractError(f"market_data.bars[{index}] has invalid OHLC")
        normalized_bars.append(bar)
    market["bars"] = normalized_bars
    context["market_data"] = market

    context["strategy_output"] = _mapping(context["strategy_output"], "strategy_output")
    context["execution"] = _mapping(context["execution"], "execution")
    render = _mapping(context["render"], "render")
    if render.get("format") != "html" or render.get("plotly_runtime") not in _PLOTLY_RUNTIMES:
        raise RuntimeContractError("chart render options are invalid")
    context["render"] = render
    try:
        json.dumps(context, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError("chart context must be finite JSON data") from exc
    return context


def _paths(values: object, field: str) -> tuple[str, ...]:
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(item, str) for item in values)
    ):
        raise RuntimeCompatibilityError(f"{field} must be a non-empty path list")
    paths = tuple(values)
    if len(paths) != len(set(paths)):
        raise RuntimeCompatibilityError(f"{field} contains duplicate paths")
    for value in paths:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or str(path) != value
            or "\\" in value
            or ":" in value
        ):
            raise RuntimeCompatibilityError(f"{field} contains an unsafe path")
    return paths


class ChartRuntime:
    """Load and invoke the chart implementation locked by an SRT binding."""

    def __init__(self, strategy_root: Path | None = None) -> None:
        self.strategy_root = None if strategy_root is None else Path(strategy_root).resolve()

    @staticmethod
    def _implementation(
        descriptor: object,
        *,
        source_root: Path | None = None,
        install_files: tuple[str, ...] | None = None,
    ) -> object:
        expected = {
            "module", "qualname", "contract_version", "source_files", "source_sha256",
        }
        if not isinstance(descriptor, Mapping) or set(descriptor) != expected:
            raise RuntimeCompatibilityError("chart implementation descriptor is incomplete")
        module_name = descriptor["module"]
        qualname = descriptor["qualname"]
        if (
            not isinstance(module_name, str)
            or not module_name.startswith("strategy_runtime.charts.")
            or not all(part.isidentifier() for part in module_name.split("."))
            or not isinstance(qualname, str)
            or not qualname.isidentifier()
            or descriptor["contract_version"] != CHART_CONTRACT_VERSION
        ):
            raise RuntimeCompatibilityError("chart implementation identity is invalid")
        source_files = _paths(descriptor["source_files"], "charts.source_files")
        implementation_file = (
            module_name.removeprefix("strategy_runtime.").replace(".", "/") + ".py"
        )
        if implementation_file not in source_files:
            raise RuntimeCompatibilityError("chart source closure omits its implementation")
        if install_files is not None and not set(source_files).issubset(install_files):
            raise RuntimeCompatibilityError("chart source closure is not installable")

        root = None if source_root is None else Path(source_root).resolve()
        if root is not None:
            if root.name != "strategy_runtime" or not (root / "charts").is_dir():
                raise RuntimeCompatibilityError("chart source root is not an SRT package")
            import strategy_runtime

            root_text = str(root)
            if root_text not in strategy_runtime.__path__:
                strategy_runtime.__path__.insert(0, root_text)
            previous_bytecode = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                charts = import_module("strategy_runtime.charts")
            finally:
                sys.dont_write_bytecode = previous_bytecode
            charts_root = str(root / "charts")
            if charts_root not in charts.__path__:
                charts.__path__.insert(0, charts_root)
            invalidate_caches()
        actual = implementation_sha256(source_files, source_root=root)
        if descriptor["source_sha256"] != actual:
            raise RuntimeCompatibilityError("chart source hash differs from its binding")

        module = sys.modules.get(module_name)
        already_loaded = module is not None
        if module is not None and getattr(module, "__srt_chart_source_sha256__", None) != actual:
            raise RuntimeCompatibilityError(
                "chart module was already imported from a different source closure"
            )
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            try:
                module = import_module(module_name)
                factory = getattr(module, qualname)
            except (ImportError, AttributeError) as exc:
                raise RuntimeCompatibilityError(
                    f"chart implementation is unavailable: {module_name}.{qualname}"
                ) from exc
        finally:
            sys.dont_write_bytecode = previous_bytecode
        if not already_loaded and root is not None:
            module_file = getattr(module, "__file__", None)
            expected_file = (root / implementation_file).resolve()
            if module_file is None or Path(module_file).resolve() != expected_file:
                raise RuntimeCompatibilityError("chart implementation loaded from another package")
        if factory.__module__ != module_name or factory.__qualname__ != qualname:
            raise RuntimeCompatibilityError("chart factory is an alias for another implementation")
        try:
            implementation = factory()
        except TypeError as exc:
            raise RuntimeCompatibilityError("chart implementation must have a no-argument factory") from exc
        for method in ("render_backtest", "render_forward_observation"):
            if not callable(getattr(implementation, method, None)):
                raise RuntimeCompatibilityError(f"chart implementation has no {method}")
        module.__srt_chart_source_sha256__ = actual
        return implementation

    def validate_descriptor(
        self,
        descriptor: object,
        *,
        source_root: Path,
        install_files: tuple[str, ...],
    ) -> None:
        self._implementation(
            descriptor, source_root=source_root, install_files=install_files
        )

    def _render(
        self,
        reference_id: str,
        mode: str,
        context: object,
        *,
        descriptor: object | None = None,
        source_root: Path | None = None,
        expected_identity_hash: str | None = None,
    ) -> str:
        if descriptor is None:
            if self.strategy_root is None:
                raise RuntimeCompatibilityError(
                    f"strategy deployment root is required for {reference_id}"
                )
            deployment = load_strategy_deployment(self.strategy_root, reference_id)
            descriptor = deployment.binding.get("charts")
            source_root = deployment.source_root
            expected_identity_hash = expected_identity_hash or deployment.release_hash
        elif source_root is None or expected_identity_hash is None:
            raise RuntimeCompatibilityError(
                "candidate chart rendering requires its source root and identity hash"
            )
        if not isinstance(descriptor, Mapping):
            raise RuntimeCompatibilityError(f"{reference_id} has no strategy chart contract")
        implementation = self._implementation(descriptor, source_root=source_root)
        normalized = validate_chart_context(context, expected_mode=mode)
        if normalized["strategy"]["reference_id"] != reference_id:
            raise RuntimeContractError("chart context belongs to another strategy reference")
        if normalized["strategy"]["identity_hash"] != expected_identity_hash:
            raise RuntimeContractError("chart context identity hash differs from its SRT source")
        method = (
            implementation.render_backtest
            if mode == "BACKTEST"
            else implementation.render_forward_observation
        )
        result = method(normalized)
        if not isinstance(result, str) or not result.lstrip().lower().startswith(
            ("<html", "<!doctype html")
        ):
            raise RuntimeContractError("strategy chart renderer must return a complete HTML document")
        return result

    def render_backtest(
        self,
        reference_id: str,
        context: object,
        *,
        descriptor: object | None = None,
        source_root: Path | None = None,
        expected_identity_hash: str | None = None,
    ) -> str:
        return self._render(
            reference_id,
            "BACKTEST",
            context,
            descriptor=descriptor,
            source_root=source_root,
            expected_identity_hash=expected_identity_hash,
        )

    def render_forward_observation(self, reference_id: str, context: object) -> str:
        return self._render(reference_id, "FORWARD_OBSERVATION", context)
