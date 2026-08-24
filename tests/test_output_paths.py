from datetime import date
from pathlib import Path

import czsc_trader.output_paths as output_paths


def test_create_output_dir_uses_symbol_date_and_first_revision(tmp_path: Path) -> None:
    """Catch output runs falling back to a shared or overwrite-prone directory."""
    output_dir = output_paths.create_output_dir(
        tmp_path, "588080.SH", date(2026, 8, 23)
    )

    assert output_dir == tmp_path / "588080_0823_R01"
    assert output_dir.is_dir()


def test_create_output_dir_increments_past_existing_revisions(tmp_path: Path) -> None:
    """Catch a repeated run overwriting or failing on an existing revision."""
    (tmp_path / "588080_0823_R01").mkdir()
    (tmp_path / "588080_0823_R02").mkdir()

    output_dir = output_paths.create_output_dir(
        tmp_path, "588080.SH", date(2026, 8, 23)
    )

    assert output_dir == tmp_path / "588080_0823_R03"
    assert output_dir.is_dir()
