from __future__ import annotations

from collections import Counter

from factor_signal_catalog import CatalogRegistry, CatalogValidationError

from .context import RepositoryContext
from .errors import ValidationError
from .results import CommandResult


def _registry(context: RepositoryContext) -> CatalogRegistry:
    try:
        return CatalogRegistry(context.root / "catalog")
    except CatalogValidationError as exc:
        raise ValidationError(
            "factor_signal_catalog_invalid",
            str(exc),
            context={"path": str(context.root / "catalog")},
        ) from exc


def validate_catalog(context: RepositoryContext) -> CommandResult:
    catalog = _registry(context)
    return CommandResult(
        status="PASS",
        command="catalog.validate",
        result={
            "digest": catalog.digest,
            "families": len(catalog.families),
            "factors": len(catalog.factors),
            "signals": len(catalog.signals),
            "factor_statuses": dict(sorted(Counter(item.status.value for item in catalog.factors).items())),
            "signal_statuses": dict(sorted(Counter(item.status.value for item in catalog.signals).items())),
        },
    )


def list_catalog(
    context: RepositoryContext,
    *,
    kind: str,
    family: str | None,
    status: str | None,
    query: str | None,
) -> CommandResult:
    catalog = _registry(context)
    try:
        rows = catalog.list_definitions(kind=kind, family=family, status=status, query=query)
    except CatalogValidationError as exc:
        raise ValidationError("factor_signal_catalog_query_invalid", str(exc)) from exc
    return CommandResult(
        status="PASS",
        command="catalog.list",
        result={"digest": catalog.digest, "count": len(rows), "definitions": rows},
    )


def show_catalog(context: RepositoryContext, definition_id: str) -> CommandResult:
    catalog = _registry(context)
    try:
        definition = catalog.show(definition_id)
    except CatalogValidationError as exc:
        raise ValidationError("factor_signal_catalog_not_found", str(exc)) from exc
    return CommandResult(
        status="PASS",
        command="catalog.show",
        result={"digest": catalog.digest, "definition": definition},
    )
