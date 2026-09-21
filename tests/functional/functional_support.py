from __future__ import annotations

import json

import pandas as pd

from czsc_trader.backtesting.execution_data import BacktestExecutionData


def execution_data_from_replay(replay, *, start, end) -> BacktestExecutionData:
    daily = replay.execution_daily.copy()
    sessions = pd.DatetimeIndex(pd.to_datetime(daily["dt"]).dt.normalize(), name="dt")
    sessions = sessions[
        (sessions >= pd.Timestamp(start).normalize())
        & (sessions <= pd.Timestamp(end).normalize())
    ]
    return BacktestExecutionData(
        replay.dataset,
        replay.root,
        replay.adjusted.symbol,
        replay.adjusted.asset_type,
        replay.adjusted.daily.copy(),
        daily,
        replay.execution_intraday.copy(),
        replay.fingerprint,
        replay.cutoff,
        sessions,
        replay.execution_five_minute,
    )

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
