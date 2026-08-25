from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ResearchContext(Protocol):
    root: Path
    raw_dir: Path
    baseline_root: Path
    experiments_root: Path
    outputs_root: Path


class ResearchProtocolError(ValueError):
    """The protocol cannot select a unique supported research handler."""


class ExperimentHandler(Protocol):
    handler_id: str

    def validate_protocol(self, protocol: dict[str, object]) -> None: ...

    def run(
        self,
        context: ResearchContext,
        experiment_dir: Path,
    ) -> dict[str, object]: ...

    def replay(
        self,
        context: ResearchContext,
        source_dir: Path,
        output_dir: Path,
    ) -> dict[str, object]: ...
