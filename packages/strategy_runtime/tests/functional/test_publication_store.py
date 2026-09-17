from __future__ import annotations

from hashlib import sha256

import pandas as pd
import pytest
from dataflows import DataIdentity, DataRequest, DataResult, DataStatus, Dataset
from strategy_runtime import (
    PublicationStatus,
    PublishedStrategyData,
    RuntimeContractError,
    read_publication,
    write_publication,
)


def publication() -> PublishedStrategyData:
    frame = pd.DataFrame(
        {
            "Date": ["2026-09-15", "2026-09-16"],
            "Close": [1.0, 1.1],
        }
    )
    request = DataRequest(
        Dataset.ETF_OHLCV,
        "588080.SH",
        "2026-09-15",
        "2026-09-16",
        "2026-09-16",
        "daily",
    )
    identity = DataIdentity(
        str(Dataset.ETF_OHLCV),
        "test",
        "588080.SH",
        "2026-09-15T00:00:00",
        "2026-09-16T00:00:00",
        sha256(b"frame").hexdigest(),
        {"primary_key": ["Date"]},
    )
    return PublishedStrategyData(
        "S007-v1",
        sha256(b"release").hexdigest(),
        PublicationStatus.READY,
        "2026-09-16",
        {"market_daily": request},
        {"market_daily": DataResult(DataStatus.READY, frame, identity)},
    )


def test_publication_store_round_trip_preserves_contract_and_frame(tmp_path):
    expected = publication()
    write_publication(expected, tmp_path)

    actual = read_publication(tmp_path, "S007-v1")

    assert actual.release_hash == expected.release_hash
    assert actual.requested_cutoff == "2026-09-16"
    assert actual.input_requests["market_daily"] == expected.input_requests["market_daily"]
    pd.testing.assert_frame_equal(
        actual.input_results["market_daily"].dataframe,
        expected.input_results["market_daily"].dataframe,
    )


def test_publication_store_rejects_modified_frame(tmp_path):
    write_publication(publication(), tmp_path)
    frame_path = tmp_path / "srt_s007_v1_market_daily.csv.gz"
    frame_path.write_bytes(frame_path.read_bytes() + b"modified")

    with pytest.raises(RuntimeContractError, match="was modified"):
        read_publication(tmp_path, "S007-v1")
