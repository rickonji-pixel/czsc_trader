import json
from pathlib import Path

from czsc_trader.research import run_research


def test_research_writes_audited_artifacts(tmp_path: Path) -> None:
    """Catch missing evidence files or incomplete target-window reporting."""
    summary = run_research(Path("data/raw"), tmp_path)
    expected = {
        "factors.csv",
        "monthly_parameters.csv",
        "orders_2026Q1.csv",
        "equity_2026Q1.csv",
        "orders_2026H1.csv",
        "equity_2026H1.csv",
        "orders_2026_01_08.csv",
        "equity_2026_01_08.csv",
        "chart_2026Q1.html",
        "chart_2026H1.html",
        "chart_2026_01_08.html",
        "metrics.json",
        "report.md",
        "manifest.json",
        "alpha_locks.csv",
    }

    actual = {path.name for path in tmp_path.iterdir()}
    assert expected <= actual
    assert {"orders.csv", "equity.csv"}.isdisjoint(actual)
    assert set(summary["windows"]) == {"2026Q1", "2026H1", "2026_01_08"}
    assert all(window["pass"] for window in summary["windows"].values())
    assert {window["start"] for window in summary["windows"].values()} == {"2026-01-05"}
    for name in ("2026Q1", "2026H1", "2026_01_08"):
        html = (tmp_path / f"chart_{name}.html").read_text(encoding="utf-8")
        assert "plotly.js v" in html.lower()
        assert r"CZSC\u7b14" in html
        assert r"\u7b56\u7565\u4e70\u5165" in html
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["audit_status"] == "PASS"
    assert manifest["versions"]["czsc"] == "1.0.1"
    assert manifest["versions"]["vectorbt"] == "1.1.0"
    assert manifest["charts"]["files"] == [
        "chart_2026Q1.html",
        "chart_2026H1.html",
        "chart_2026_01_08.html",
    ]
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## 交互式日线图" in report
    assert "chart_2026Q1.html" in report
