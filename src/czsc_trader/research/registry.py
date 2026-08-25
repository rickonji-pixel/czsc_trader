from __future__ import annotations

from collections.abc import Mapping

from .contracts import ExperimentHandler, ResearchProtocolError


class ExperimentRegistry:
    """Deterministic mapping from protocol identities to handlers."""

    def __init__(self, *, historical: Mapping[str, str] | None = None) -> None:
        self._handlers: dict[str, ExperimentHandler] = {}
        self._historical = dict(historical or {})

    def register(self, handler: ExperimentHandler) -> None:
        handler_id = str(handler.handler_id)
        if not handler_id:
            raise ResearchProtocolError("handler identity must not be empty")
        if handler_id in self._handlers:
            raise ResearchProtocolError(f"duplicate experiment handler: {handler_id}")
        self._handlers[handler_id] = handler

    def resolve(
        self,
        protocol: Mapping[str, object],
        experiment_id: str,
    ) -> ExperimentHandler:
        handler_id = str(
            protocol.get("handler")
            or protocol.get("experiment_type")
            or self._historical.get(experiment_id, "")
        )
        handler = self._handlers.get(handler_id)
        if handler is None:
            raise ResearchProtocolError(
                f"no registered handler for experiment {experiment_id}: "
                f"{handler_id or 'missing identity'}"
            )
        return handler

    @property
    def handler_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))


def build_default_registry() -> ExperimentRegistry:
    from .handlers import registered_handlers

    registry = ExperimentRegistry(historical={"0824_EX01": "champion_challenge"})
    for handler in registered_handlers():
        registry.register(handler)
    return registry
