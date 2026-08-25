from __future__ import annotations

from pathlib import Path

from czsc_trader.application.archive_service import validate_archives
from czsc_trader.application.context import RepositoryContext


def test_archive_service_validates_every_tracked_experiment() -> None:
    result = validate_archives(RepositoryContext.discover(Path.cwd()), all_archives=True)

    assert result.status == "PASS"
    assert result.result["validated_count"] == 15
    assert result.result["experiments"][0] == "0824_EX01"
    assert result.result["experiments"][-1] == "0826_EX01"
