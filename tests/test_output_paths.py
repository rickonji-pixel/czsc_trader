from datetime import date
from pathlib import Path

from czsc_trader.experiment_archive import validate_experiment_archive
import czsc_trader.research as research


def test_create_output_dir_uses_symbol_date_and_first_revision(tmp_path: Path) -> None:
    """Catch output runs falling back to a shared or overwrite-prone directory."""
    assert hasattr(research, "create_output_dir")
    output_dir = research.create_output_dir(tmp_path, "588080.SH", date(2026, 8, 23))

    assert output_dir == tmp_path / "588080_0823_R01"
    assert output_dir.is_dir()


def test_create_output_dir_increments_past_existing_revisions(tmp_path: Path) -> None:
    """Catch a repeated run overwriting or failing on an existing revision."""
    (tmp_path / "588080_0823_R01").mkdir()
    (tmp_path / "588080_0823_R02").mkdir()

    output_dir = research.create_output_dir(tmp_path, "588080.SH", date(2026, 8, 23))

    assert output_dir == tmp_path / "588080_0823_R03"
    assert output_dir.is_dir()


def test_run_dated_research_routes_artifacts_to_experiment_archive(tmp_path: Path) -> None:
    """Catch candidate search writing into the disposable backtest outputs root."""
    assert hasattr(research, "run_dated_research")

    def lightweight_runner(raw_dir: Path, output_dir: Path) -> dict[str, object]:
        (output_dir / "marker.txt").write_text(str(raw_dir), encoding="utf-8")
        return {"output_dir": str(output_dir)}

    summary = research.run_dated_research(
        Path("data/raw"),
        tmp_path,
        run_date=date(2026, 8, 23),
        runner=lightweight_runner,
    )

    expected = tmp_path / "0823_EX01"
    assert Path(summary["experiment_dir"]) == expected
    assert (expected / "artifacts" / "marker.txt").read_text(encoding="utf-8") == str(
        Path("data/raw")
    )
    validate_experiment_archive(expected)
