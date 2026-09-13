from __future__ import annotations

from pathlib import Path

from czsc_trader.news_events.service import NewsExtractionCommand, run_news_extraction

from .context import RepositoryContext
from .results import CommandResult


def extract_news(
    context: RepositoryContext,
    *,
    input_path: Path,
    scope_path: Path,
    output_dir: Path,
    limit: int | None,
) -> CommandResult:
    def resolve(path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (context.root / path).resolve()

    return run_news_extraction(
        NewsExtractionCommand(
            input_path=resolve(input_path),
            scope_path=resolve(scope_path),
            output_dir=resolve(output_dir),
            limit=limit,
            env_file=context.root / ".env",
        )
    )
