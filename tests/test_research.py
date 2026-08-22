import json
from pathlib import Path

from czsc_trader.research import run_research


def test_research_writes_audited_artifacts(tmp_path: Path) -> None:
    """Catch missing evidence files or incomplete target-window reporting."""
    summary = run_research(Path("data/raw"), tmp_path)
    expected = {
        "factors.csv",
        "factor_events.csv",
        "candidate_results.csv",
        "selected_rule.json",
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
    }

    actual = {path.name for path in tmp_path.iterdir()}
    assert expected <= actual
    assert {"orders.csv", "equity.csv"}.isdisjoint(actual)
    assert set(summary["windows"]) == {"2026Q1", "2026H1", "2026_01_08"}
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
    assert manifest["selection_mode"] == "fixed CZSC-only rule; 2026 sample-optimized"
    assert "alpha_lock_policy" not in manifest
    assert "alpha_locks.csv" not in actual
    assert "monthly_parameters.csv" not in actual
    for name in ("2026Q1", "2026H1", "2026_01_08"):
        orders = __import__("pandas").read_csv(tmp_path / f"orders_{name}.csv")
        assert orders["factor_event_id"].notna().all()
        assert set(orders["event_type"]) <= {"Entry", "Exit", "InitialEntry"}
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## 交互式日线图" in report
    assert "chart_2026Q1.html" in report
    assert "2026样本内优化" in report
