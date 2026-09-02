from __future__ import annotations

import subprocess
import sys


def test_cli_parser_does_not_import_backtest_stack() -> None:
    code = "import sys; import czsc_trader.cli.main; print('vectorbt' in sys.modules)"
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.stdout.strip() == "False"
