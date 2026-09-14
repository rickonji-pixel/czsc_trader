"""Strategy Template Catalog (STC)."""

from .models import (
    InputBinding,
    InputKind,
    InputSlotDefinition,
    ParameterDefinition,
    ParameterType,
    TemplateDefinition,
    TemplateInstance,
    TemplateOperator,
    TemplateStatus,
    TemplateValidationError,
    WeightMode,
)
from .registry import TemplateRegistry


__version__ = "0.1.0"

__all__ = [
    "InputBinding",
    "InputKind",
    "InputSlotDefinition",
    "ParameterDefinition",
    "ParameterType",
    "TemplateDefinition",
    "TemplateInstance",
    "TemplateOperator",
    "TemplateRegistry",
    "TemplateStatus",
    "TemplateValidationError",
    "WeightMode",
]
