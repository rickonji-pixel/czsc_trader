from __future__ import annotations

from pathlib import Path

from czsc_trader.application.baseline_service import (
    list_baselines,
    show_baseline,
    validate_baseline,
)
from czsc_trader.application.context import RepositoryContext


def _context() -> RepositoryContext:
    return RepositoryContext.discover(Path.cwd())


def test_list_baselines_reports_registry_roles() -> None:
    result = list_baselines(_context())

    rows = result.result["baselines"]
    statuses = {row["version"]: row["status"] for row in rows}
    assert result.result["latest"] == "baseline_20260826"
    assert statuses == {
        "baseline_20260823": "archived",
        "baseline_20260826": "active",
    }


def test_show_and_validate_baseline_use_existing_hash_verification() -> None:
    shown = show_baseline(_context(), "baseline_20260826", symbol="588080.SH")
    validated = validate_baseline(
        _context(), "baseline_20260826", symbol="588080.SH"
    )

    assert shown.result["strategy"] == "czsc_four_layer"
    assert shown.result["symbol"] == "588080.SH"
    assert validated.status == "PASS"
    assert validated.result["sha256"] == (
        "fc22ca5a973f77faf528cdb3efba4900163e18fe0c08cf79234d79ef22f5c822"
    )
