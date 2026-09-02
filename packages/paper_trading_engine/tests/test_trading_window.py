from __future__ import annotations

from datetime import datetime

import pytest


@pytest.mark.parametrize(
    ("clock", "allowed"),
    [
        ("09:29:59", False),
        ("09:30:00", True),
        ("11:30:00", True),
        ("11:30:01", False),
        ("13:00:00", True),
        ("14:56:59", True),
        ("14:57:00", False),
        ("23:59:59", False),
    ],
)
def test_submission_window_uses_conservative_continuous_auction_periods(
    clock: str, allowed: bool
) -> None:
    from paper_trading_engine.trading_window import is_submission_window

    moment = datetime.fromisoformat(f"2026-09-02T{clock}+08:00")
    assert is_submission_window(moment) is allowed
