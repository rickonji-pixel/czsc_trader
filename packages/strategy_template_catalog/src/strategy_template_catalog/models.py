from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
from typing import Any, TypeAlias


Scalar: TypeAlias = str | int | float | bool
Weight: TypeAlias = float | tuple[tuple[str, float], ...] | None


class TemplateValidationError(ValueError):
    """A template definition or instance violates the STC contract."""


class TemplateStatus(StrEnum):
    READY = "READY"
    DEPRECATED = "DEPRECATED"


class TemplateOperator(StrEnum):
    WEIGHTED_SCORE = "WEIGHTED_SCORE"
    GATED_SCORE = "GATED_SCORE"
    REGIME_WEIGHTED_SCORE = "REGIME_WEIGHTED_SCORE"
    EVENT_HOLD = "EVENT_HOLD"
    CORE_OVERLAY = "CORE_OVERLAY"


class ParameterType(StrEnum):
    FLOAT = "FLOAT"
    INTEGER = "INTEGER"
    BOOLEAN = "BOOLEAN"
    CATEGORICAL = "CATEGORICAL"


class InputKind(StrEnum):
    FACTOR = "FACTOR"
    SIGNAL = "SIGNAL"


class WeightMode(StrEnum):
    NONE = "NONE"
    SCALAR = "SCALAR"
    BY_REGIME = "BY_REGIME"


def _exact_fields(payload: dict[str, Any], fields: set[str], kind: str) -> None:
    missing = sorted(fields.difference(payload))
    if missing:
        raise TemplateValidationError(f"{kind} missing fields: {missing}")
    unknown = sorted(set(payload).difference(fields))
    if unknown:
        raise TemplateValidationError(f"{kind} unknown fields: {unknown}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TemplateValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _text_tuple(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise TemplateValidationError(f"{field} must be an array of non-empty strings")
    result = tuple(item.strip() for item in value)
    if not result and not allow_empty:
        raise TemplateValidationError(f"{field} must not be empty")
    if len(result) != len(set(result)):
        raise TemplateValidationError(f"{field} must contain unique values")
    return result


def _scalar(value: object, field: str) -> Scalar:
    if isinstance(value, bool | str | int | float):
        if isinstance(value, float) and not math.isfinite(value):
            raise TemplateValidationError(f"{field} must be finite")
        return value
    raise TemplateValidationError(f"{field} must be a JSON scalar")


@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    parameter_type: ParameterType
    description: str
    default: Scalar
    minimum: float | int | None
    maximum: float | int | None
    choices: tuple[Scalar, ...]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ParameterDefinition":
        _exact_fields(
            payload,
            {"name", "type", "description", "default", "minimum", "maximum", "choices"},
            "parameter",
        )
        minimum = payload["minimum"]
        maximum = payload["maximum"]
        if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, int | float)):
            raise TemplateValidationError("parameter minimum must be numeric or null")
        if maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, int | float)):
            raise TemplateValidationError("parameter maximum must be numeric or null")
        choices_raw = payload["choices"]
        if not isinstance(choices_raw, list):
            raise TemplateValidationError("parameter choices must be an array")
        try:
            parameter_type = ParameterType(str(payload["type"]))
        except ValueError as exc:
            raise TemplateValidationError(f"unknown parameter type: {payload['type']}") from exc
        item = cls(
            name=_text(payload["name"], "parameter name"),
            parameter_type=parameter_type,
            description=_text(payload["description"], "parameter description"),
            default=_scalar(payload["default"], "parameter default"),
            minimum=minimum,
            maximum=maximum,
            choices=tuple(_scalar(value, "parameter choice") for value in choices_raw),
        )
        item.validate_definition()
        item.validate_value(item.default)
        return item

    def validate_definition(self) -> None:
        if self.parameter_type in {ParameterType.FLOAT, ParameterType.INTEGER}:
            if self.minimum is None or self.maximum is None or self.minimum > self.maximum:
                raise TemplateValidationError(f"numeric parameter requires valid bounds: {self.name}")
            if self.choices:
                raise TemplateValidationError(f"numeric parameter cannot declare choices: {self.name}")
            if self.parameter_type is ParameterType.INTEGER and (
                isinstance(self.minimum, float) or isinstance(self.maximum, float)
            ):
                raise TemplateValidationError(
                    f"integer parameter requires integer bounds: {self.name}"
                )
        elif self.parameter_type is ParameterType.CATEGORICAL:
            if not self.choices or self.minimum is not None or self.maximum is not None:
                raise TemplateValidationError(
                    f"categorical parameter requires choices and no bounds: {self.name}"
                )
            typed = {(type(value).__name__, value) for value in self.choices}
            if len(typed) != len(self.choices):
                raise TemplateValidationError(f"parameter choices must be unique: {self.name}")
        elif self.minimum is not None or self.maximum is not None or self.choices:
            raise TemplateValidationError(
                f"boolean parameter cannot declare bounds or choices: {self.name}"
            )

    def validate_value(self, value: object) -> Scalar:
        if self.parameter_type is ParameterType.FLOAT:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TemplateValidationError(f"parameter {self.name} must be numeric")
            normalized: Scalar = float(value)
        elif self.parameter_type is ParameterType.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TemplateValidationError(f"parameter {self.name} must be an integer")
            normalized = value
        elif self.parameter_type is ParameterType.BOOLEAN:
            if not isinstance(value, bool):
                raise TemplateValidationError(f"parameter {self.name} must be boolean")
            normalized = value
        else:
            normalized = _scalar(value, f"parameter {self.name}")
            if not any(type(normalized) is type(choice) and normalized == choice for choice in self.choices):
                raise TemplateValidationError(
                    f"parameter {self.name} must be one of {list(self.choices)}"
                )
        if isinstance(normalized, float) and not math.isfinite(normalized):
            raise TemplateValidationError(f"parameter {self.name} must be finite")
        if self.parameter_type in {ParameterType.FLOAT, ParameterType.INTEGER}:
            if float(normalized) < float(self.minimum) or float(normalized) > float(self.maximum):
                raise TemplateValidationError(f"parameter {self.name} is outside its bounds")
        return normalized

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type": self.parameter_type.value,
            "description": self.description,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices),
        }


@dataclass(frozen=True)
class InputSlotDefinition:
    name: str
    role: str
    description: str
    accepted_kinds: tuple[InputKind, ...]
    minimum_items: int
    maximum_items: int
    weight_mode: WeightMode

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InputSlotDefinition":
        _exact_fields(
            payload,
            {"name", "role", "description", "accepted_kinds", "minimum_items", "maximum_items", "weight_mode"},
            "input slot",
        )
        raw_kinds = _text_tuple(payload["accepted_kinds"], "accepted kinds")
        minimum = payload["minimum_items"]
        maximum = payload["maximum_items"]
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
            raise TemplateValidationError("minimum_items must be a nonnegative integer")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < max(1, minimum):
            raise TemplateValidationError("maximum_items must be positive and at least minimum_items")
        try:
            accepted_kinds = tuple(InputKind(value) for value in raw_kinds)
            weight_mode = WeightMode(str(payload["weight_mode"]))
        except ValueError as exc:
            raise TemplateValidationError("unknown input kind or weight mode") from exc
        return cls(
            name=_text(payload["name"], "slot name"),
            role=_text(payload["role"], "slot role"),
            description=_text(payload["description"], "slot description"),
            accepted_kinds=accepted_kinds,
            minimum_items=minimum,
            maximum_items=maximum,
            weight_mode=weight_mode,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "accepted_kinds": [value.value for value in self.accepted_kinds],
            "minimum_items": self.minimum_items,
            "maximum_items": self.maximum_items,
            "weight_mode": self.weight_mode.value,
        }


@dataclass(frozen=True)
class TemplateDefinition:
    template_id: str
    name: str
    description: str
    status: TemplateStatus
    version: int
    operator: TemplateOperator
    output: str
    complexity: str
    expression: str
    when_to_use: str
    failure_modes: tuple[str, ...]
    input_slots: tuple[InputSlotDefinition, ...]
    parameters: tuple[ParameterDefinition, ...]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TemplateDefinition":
        _exact_fields(
            payload,
            {
                "template_id", "name", "description", "status", "version", "operator",
                "output", "complexity", "expression", "when_to_use", "failure_modes",
                "input_slots", "parameters",
            },
            "template",
        )
        if not isinstance(payload["input_slots"], list) or not isinstance(payload["parameters"], list):
            raise TemplateValidationError("template input_slots and parameters must be arrays")
        version = payload["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise TemplateValidationError("template version must be a positive integer")
        try:
            status = TemplateStatus(str(payload["status"]))
            operator = TemplateOperator(str(payload["operator"]))
        except ValueError as exc:
            raise TemplateValidationError("unknown template status or operator") from exc
        return cls(
            template_id=_text(payload["template_id"], "template id"),
            name=_text(payload["name"], "template name"),
            description=_text(payload["description"], "template description"),
            status=status,
            version=version,
            operator=operator,
            output=_text(payload["output"], "template output"),
            complexity=_text(payload["complexity"], "template complexity"),
            expression=_text(payload["expression"], "template expression"),
            when_to_use=_text(payload["when_to_use"], "template when_to_use"),
            failure_modes=_text_tuple(payload["failure_modes"], "template failure_modes"),
            input_slots=tuple(InputSlotDefinition.from_dict(item) for item in payload["input_slots"]),
            parameters=tuple(ParameterDefinition.from_dict(item) for item in payload["parameters"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "version": self.version,
            "operator": self.operator.value,
            "output": self.output,
            "complexity": self.complexity,
            "expression": self.expression,
            "when_to_use": self.when_to_use,
            "failure_modes": list(self.failure_modes),
            "input_slots": [item.to_dict() for item in self.input_slots],
            "parameters": [item.to_dict() for item in self.parameters],
        }


@dataclass(frozen=True)
class InputBinding:
    slot: str
    source_id: str
    source_kind: InputKind
    state: str | None
    weight: Weight

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InputBinding":
        _exact_fields(payload, {"slot", "source_id", "source_kind", "state", "weight"}, "binding")
        state = payload["state"]
        if state is not None:
            state = _text(state, "binding state")
        raw_weight = payload["weight"]
        weight: Weight
        if raw_weight is None:
            weight = None
        elif isinstance(raw_weight, dict):
            if not raw_weight:
                raise TemplateValidationError("regime weight map must not be empty")
            rows = []
            for regime, value in raw_weight.items():
                key = _text(regime, "regime name")
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise TemplateValidationError("regime weights must be numeric")
                number = float(value)
                if not math.isfinite(number):
                    raise TemplateValidationError("regime weights must be finite")
                rows.append((key, number))
            weight = tuple(sorted(rows))
        elif isinstance(raw_weight, bool) or not isinstance(raw_weight, int | float):
            raise TemplateValidationError("binding weight must be numeric, object or null")
        else:
            weight = float(raw_weight)
            if not math.isfinite(weight):
                raise TemplateValidationError("binding weight must be finite")
        try:
            source_kind = InputKind(str(payload["source_kind"]))
        except ValueError as exc:
            raise TemplateValidationError(
                f"unknown binding source kind: {payload['source_kind']}"
            ) from exc
        return cls(
            slot=_text(payload["slot"], "binding slot"),
            source_id=_text(payload["source_id"], "binding source_id"),
            source_kind=source_kind,
            state=state,
            weight=weight,
        )

    def to_dict(self) -> dict[str, object]:
        weight: object = self.weight
        if isinstance(weight, tuple):
            weight = dict(weight)
        return {
            "slot": self.slot,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "state": self.state,
            "weight": weight,
        }


@dataclass(frozen=True)
class TemplateInstance:
    template_id: str
    template_version: int
    bindings: tuple[InputBinding, ...]
    parameters: tuple[tuple[str, Scalar], ...]

    @property
    def digest(self) -> str:
        raw = json.dumps(
            self._contract(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def instance_id(self) -> str:
        return f"STI-{self.digest[:16].upper()}"

    def _contract(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "bindings": [item.to_dict() for item in self.bindings],
            "parameters": dict(self.parameters),
        }

    def to_dict(self) -> dict[str, object]:
        return {"instance_id": self.instance_id, "digest": self.digest, **self._contract()}
