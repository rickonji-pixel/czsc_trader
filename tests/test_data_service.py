from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import PrepareDataCommand, prepare_data, validate_data
from czsc_trader.application.errors import ValidationError


def test_validate_data_reports_tracked_manifest_and_validation() -> None:
    result = validate_data(RepositoryContext.discover(Path.cwd()), "588080.SH")

    assert result.status == "PASS"
    assert result.result["symbol"] == "588080.SH"
    assert result.result["requested_end"] == "2026-08-24"
    assert result.result["validation_status"] == "PASS"
    assert result.result["frequencies"] == ["30m", "daily", "weekly"]


def test_prepare_data_rejects_invalid_date_range_before_fetching(tmp_path: Path) -> None:
    context = RepositoryContext(
        root=tmp_path,
        raw_dir=tmp_path / "data" / "raw",
        baseline_root=tmp_path / "configs" / "rule_baselines",
        experiments_root=tmp_path / "experiments",
        outputs_root=tmp_path / "outputs",
    )

    with pytest.raises(ValidationError, match="start must not be after end"):
        prepare_data(
            context,
            PrepareDataCommand(
                symbol="588080.SH",
                asset_type="etf",
                start=date(2026, 2, 1),
                end=date(2026, 1, 1),
            ),
        )
