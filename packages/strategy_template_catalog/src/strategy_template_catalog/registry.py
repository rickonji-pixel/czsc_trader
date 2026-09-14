from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .models import (
    InputBinding,
    Scalar,
    TemplateDefinition,
    TemplateInstance,
    TemplateOperator,
    TemplateStatus,
    TemplateValidationError,
    WeightMode,
)


_ID = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")


def _document(root: Path) -> list[dict[str, Any]]:
    path = Path(root) / "templates.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateValidationError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise TemplateValidationError(f"{path} must be a schema_version 1 object")
    rows = payload.get("items")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TemplateValidationError(f"{path} items must be an array of objects")
    return rows


def _binding_sort_key(item: InputBinding) -> tuple[str, str, str]:
    return item.slot, item.source_id, item.state or ""


class TemplateRegistry:
    """Read-only template catalog and deterministic instance factory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.templates = tuple(TemplateDefinition.from_dict(row) for row in _document(self.root))
        self.validate()

    def validate(self) -> None:
        ids = [item.template_id for item in self.templates]
        invalid = sorted(value for value in ids if not _ID.fullmatch(value))
        if invalid:
            raise TemplateValidationError(f"invalid template ids: {invalid}")
        duplicates = sorted(name for name, count in Counter(ids).items() if count > 1)
        if duplicates:
            raise TemplateValidationError(f"duplicate template ids: {duplicates}")
        operators = [item.operator for item in self.templates if item.status is TemplateStatus.READY]
        missing = sorted(value.value for value in set(TemplateOperator).difference(operators))
        if missing:
            raise TemplateValidationError(f"READY catalog misses operators: {missing}")
        for template in self.templates:
            slots = [item.name for item in template.input_slots]
            parameters = [item.name for item in template.parameters]
            if not template.input_slots or not template.parameters:
                raise TemplateValidationError(
                    f"template requires inputs and parameters: {template.template_id}"
                )
            if len(slots) != len(set(slots)) or len(parameters) != len(set(parameters)):
                raise TemplateValidationError(
                    f"template slot and parameter names must be unique: {template.template_id}"
                )
            if set(slots).intersection(parameters):
                raise TemplateValidationError(
                    f"slot and parameter names overlap: {template.template_id}"
                )
            if template.output != "TARGET_POSITION":
                raise TemplateValidationError(
                    f"unsupported template output: {template.template_id}"
                )

    @property
    def digest(self) -> str:
        payload = [item.to_dict() for item in self.templates]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def list_templates(
        self,
        *,
        operator: str | None = None,
        status: str | None = None,
        query: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        selected_operator: TemplateOperator | None = None
        selected_status: TemplateStatus | None = None
        if operator:
            try:
                selected_operator = TemplateOperator(operator)
            except ValueError as exc:
                raise TemplateValidationError(f"unknown template operator: {operator}") from exc
        if status:
            try:
                selected_status = TemplateStatus(status)
            except ValueError as exc:
                raise TemplateValidationError(f"unknown template status: {status}") from exc
        needle = query.casefold() if query else None
        rows = []
        for template in self.templates:
            if selected_operator and template.operator is not selected_operator:
                continue
            if selected_status and template.status is not selected_status:
                continue
            if needle and needle not in " ".join(
                (template.template_id, template.name, template.description, template.when_to_use)
            ).casefold():
                continue
            rows.append(
                {
                    "template_id": template.template_id,
                    "name": template.name,
                    "operator": template.operator.value,
                    "status": template.status.value,
                    "version": template.version,
                    "complexity": template.complexity,
                }
            )
        return tuple(sorted(rows, key=lambda row: str(row["template_id"])))

    def show(self, template_id: str) -> dict[str, object]:
        return self.get(template_id).to_dict()

    def get(self, template_id: str) -> TemplateDefinition:
        for template in self.templates:
            if template.template_id == template_id:
                return template
        raise TemplateValidationError(f"template not found: {template_id}")

    def instantiate(
        self,
        template_id: str,
        bindings: Sequence[InputBinding | dict[str, Any]],
        parameters: Mapping[str, object] | None = None,
    ) -> TemplateInstance:
        template = self.get(template_id)
        if template.status is not TemplateStatus.READY:
            raise TemplateValidationError(f"template is not READY: {template_id}")
        normalized_bindings = tuple(
            item if isinstance(item, InputBinding) else InputBinding.from_dict(item)
            for item in bindings
        )
        self._validate_bindings(template, normalized_bindings)
        normalized_parameters = self._parameters(template, parameters or {})
        self._validate_operator(template, normalized_bindings, normalized_parameters)
        return TemplateInstance(
            template_id=template.template_id,
            template_version=template.version,
            bindings=tuple(sorted(normalized_bindings, key=_binding_sort_key)),
            parameters=tuple(sorted(normalized_parameters.items())),
        )

    @staticmethod
    def _validate_bindings(
        template: TemplateDefinition,
        bindings: tuple[InputBinding, ...],
    ) -> None:
        slots = {item.name: item for item in template.input_slots}
        unknown = sorted({item.slot for item in bindings}.difference(slots))
        if unknown:
            raise TemplateValidationError(f"unknown binding slots: {unknown}")
        sources = [item.source_id for item in bindings]
        duplicates = sorted(name for name, count in Counter(sources).items() if count > 1)
        if duplicates:
            raise TemplateValidationError(f"duplicate input sources: {duplicates}")
        grouped = {name: [item for item in bindings if item.slot == name] for name in slots}
        for name, slot in slots.items():
            count = len(grouped[name])
            if count < slot.minimum_items or count > slot.maximum_items:
                raise TemplateValidationError(
                    f"slot {name} requires {slot.minimum_items}..{slot.maximum_items} inputs; got {count}"
                )
            for binding in grouped[name]:
                if binding.source_kind not in slot.accepted_kinds:
                    raise TemplateValidationError(
                        f"slot {name} rejects source kind {binding.source_kind.value}"
                    )
                if slot.weight_mode is WeightMode.NONE and binding.weight is not None:
                    raise TemplateValidationError(f"slot {name} does not accept weights")
                if slot.weight_mode is WeightMode.SCALAR and not isinstance(binding.weight, float):
                    raise TemplateValidationError(f"slot {name} requires scalar weights")
                if slot.weight_mode is WeightMode.BY_REGIME and not isinstance(binding.weight, tuple):
                    raise TemplateValidationError(f"slot {name} requires regime weight maps")

    @staticmethod
    def _parameters(
        template: TemplateDefinition,
        supplied: Mapping[str, object],
    ) -> dict[str, Scalar]:
        definitions = {item.name: item for item in template.parameters}
        unknown = sorted(set(supplied).difference(definitions))
        if unknown:
            raise TemplateValidationError(f"unknown template parameters: {unknown}")
        return {
            name: definition.validate_value(supplied.get(name, definition.default))
            for name, definition in definitions.items()
        }

    @staticmethod
    def _validate_operator(
        template: TemplateDefinition,
        bindings: tuple[InputBinding, ...],
        parameters: dict[str, Scalar],
    ) -> None:
        scalar_weights = [item.weight for item in bindings if isinstance(item.weight, float)]
        if scalar_weights and math.isclose(sum(abs(value) for value in scalar_weights), 0.0):
            raise TemplateValidationError("at least one scalar input weight must be nonzero")
        if template.operator is TemplateOperator.REGIME_WEIGHTED_SCORE:
            regime_maps = [dict(item.weight) for item in bindings if isinstance(item.weight, tuple)]
            regimes = [set(item) for item in regime_maps]
            if not regimes or len(regimes[0]) < 2 or any(item != regimes[0] for item in regimes[1:]):
                raise TemplateValidationError(
                    "regime-weighted inputs require the same set of at least two regimes"
                )
        if "entry_threshold" in parameters and "exit_threshold" in parameters:
            if float(parameters["exit_threshold"]) > float(parameters["entry_threshold"]):
                raise TemplateValidationError("exit_threshold must not exceed entry_threshold")
        if template.operator is TemplateOperator.CORE_OVERLAY:
            total = float(parameters["core_position"]) + float(parameters["overlay_position"])
            if total > 1.0:
                raise TemplateValidationError("core_position plus overlay_position must not exceed 1")
