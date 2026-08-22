import json
from pathlib import Path

from czsc_trader.research import run_research


def test_research_writes_audited_artifacts(tmp_path: Path) -> None:
    """Catch missing evidence files or incomplete target-window reporting."""
    summary = run_research(Path("data/raw"), tmp_path)
    expected = {
        "factors.csv",
        "monthly_parameters.csv",
        "orders.csv",
        "equity.csv",
        "metrics.json",
        "report.md",
        "manifest.json",
    }

    assert expected <= {path.name for path in tmp_path.iterdir()}
    assert set(summary["windows"]) == {"2026Q1", "2026H1", "2026_01_08"}
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["audit_status"] == "PASS"
    assert manifest["versions"]["czsc"] == "1.0.1"
    assert manifest["versions"]["vectorbt"] == "1.1.0"
