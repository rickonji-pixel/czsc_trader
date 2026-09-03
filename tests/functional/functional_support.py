from __future__ import annotations

import json

import pandas as pd


def vendor_frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Date": timestamp,
                "Open": close,
                "High": close,
                "Low": close,
                "Close": close,
                "Volume": 1000.0,
                "Amount": close * 1000.0,
            }
            for timestamp, close in rows
        ]
    )


def invoke_main(arguments: list[str], capsys) -> dict:
    from czsc_trader.cli.main import main

    exit_code = main(arguments)
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert exit_code == 0, payload
    assert payload["status"] == "PASS", payload
    return payload


def invoke_main_failure(arguments: list[str], capsys) -> dict:
    from czsc_trader.cli.main import main

    exit_code = main(arguments)
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_code != 0
    assert payload["status"] == "FAIL"
    return payload
