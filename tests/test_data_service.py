from __future__ import annotations

from pathlib import Path

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import validate_data


def test_validate_data_reports_tracked_manifest_and_validation() -> None:
    result = validate_data(RepositoryContext.discover(Path.cwd()), "588080.SH")

    assert result.status == "PASS"
    assert result.result["symbol"] == "588080.SH"
    assert result.result["requested_end"] == "2026-08-24"
    assert result.result["validation_status"] == "PASS"
    assert result.result["frequencies"] == ["30m", "daily", "weekly"]
