from __future__ import annotations

from pathlib import Path
from typing import Protocol

from czsc_trader.application.context import RepositoryContext


class ExperimentHandler(Protocol):
    handler_id: str

    def validate_protocol(self, protocol: dict[str, object]) -> None: ...

    def run(
        self,
        context: RepositoryContext,
        experiment_dir: Path,
    ) -> dict[str, object]: ...

    def replay(
        self,
        context: RepositoryContext,
        source_dir: Path,
        output_dir: Path,
    ) -> dict[str, object]: ...
