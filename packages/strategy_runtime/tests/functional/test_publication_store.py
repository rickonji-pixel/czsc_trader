from __future__ import annotations

from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd
import pytest
from dataflows import DataIdentity, DataRequest, DataResult, DataStatus, Dataset
from strategy_runtime import (
    PublicationStatus,
    PublishedStrategyData,
    PublishedDataSource,
    RuntimeContractError,
    read_publication,
    write_publication,
)
from strategy_runtime.models import CutoffRule, InputContract, InputRequirement


def publication() -> PublishedStrategyData:
    frame = pd.DataFrame(
        {
            "Date": ["2026-09-15", "2026-09-16"],
            "Close": [1.0, 1.1],
        }
    )
    adjusted_request = DataRequest(
        Dataset.ETF_OHLCV,
        "588080.SH",
        "2026-09-15",
        "2026-09-16",
        "2026-09-16",
        "daily",
    )
    adjusted_identity = DataIdentity(
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
        {
            "adjusted_daily": adjusted_request,
            "execution_daily": DataRequest(
                Dataset.ETF_UNADJUSTED_DAILY,
                "588080.SH",
                "2026-09-15",
                "2026-09-16",
                "2026-09-16",
                "daily",
            ),
        },
        {
            "adjusted_daily": DataResult(DataStatus.READY, frame, adjusted_identity),
            "execution_daily": DataResult(
                DataStatus.READY,
                frame,
                DataIdentity(
                    str(Dataset.ETF_UNADJUSTED_DAILY),
                    "test",
                    "588080.SH",
                    "2026-09-15T00:00:00",
                    "2026-09-16T00:00:00",
                    sha256(b"execution-frame").hexdigest(),
                    {"primary_key": ["Date"]},
                ),
            ),
        },
    )


def definition(value: PublishedStrategyData):
    requirements = (
        InputRequirement(
            "adjusted_daily",
            str(Dataset.ETF_OHLCV),
            "588080.SH",
            "daily",
            0,
            CutoffRule.SIGNAL_SESSION,
        ),
        InputRequirement(
            "execution_daily",
            str(Dataset.ETF_UNADJUSTED_DAILY),
            "588080.SH",
            "daily",
            0,
            CutoffRule.SIGNAL_SESSION,
        ),
    )
    return SimpleNamespace(
        release_id=value.release_id,
        release_hash=value.release_hash,
        inputs=InputContract(requirements),
    )


def write_generation(tmp_path, value: PublishedStrategyData, *, schema_version: int = 2):
    write_publication(value, tmp_path)
    files = {
        path.name: sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.iterdir()
        if path.is_file()
    }
    (tmp_path / "588080_strategy_generation.json").write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "generation_id": "GEN-TEST",
                "dataset": "runtime" if schema_version == 2 else "backtest",
                "symbol": "588080.SH",
                "asset_type": "etf",
                "data_cutoff": value.requested_cutoff,
                "strategy_releases": [value.release_id],
                "files": files,
            }
        ),
        encoding="utf-8",
    )


def test_publication_store_round_trip_preserves_contract_and_frame(tmp_path):
    expected = publication()
    write_publication(expected, tmp_path)

    actual = read_publication(tmp_path, "S007-v1")

    assert actual.release_hash == expected.release_hash
    assert actual.requested_cutoff == "2026-09-16"
    assert actual.input_requests["adjusted_daily"] == expected.input_requests["adjusted_daily"]
    pd.testing.assert_frame_equal(
        actual.input_results["adjusted_daily"].dataframe,
        expected.input_results["adjusted_daily"].dataframe,
    )


def test_publication_store_rejects_modified_frame(tmp_path):
    write_publication(publication(), tmp_path)
    frame_path = tmp_path / "srt_s007_v1_adjusted_daily.csv.gz"
    frame_path.write_bytes(frame_path.read_bytes() + b"modified")

    with pytest.raises(RuntimeContractError, match="was modified"):
        read_publication(tmp_path, "S007-v1")


def test_publication_store_recomputes_stored_content_identity(tmp_path):
    manifest_path = write_publication(publication(), tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"]["adjusted_daily"]["stored_content_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeContractError, match="content identity differs"):
        read_publication(tmp_path, "S007-v1")


@pytest.mark.parametrize("schema_version", [1, 2])
def test_published_data_source_authenticates_bound_generation(
    tmp_path, schema_version
):
    expected = publication()
    write_generation(tmp_path, expected, schema_version=schema_version)

    actual = PublishedDataSource(tmp_path).verify(definition(expected))

    assert actual.isoformat() == "2026-09-16"


def test_published_data_source_rejects_modified_generation_file(tmp_path):
    expected = publication()
    write_generation(tmp_path, expected)
    (tmp_path / "srt_s007_v1_adjusted_daily.csv.gz").write_bytes(b"modified")

    with pytest.raises(RuntimeContractError, match="generation file was modified"):
        PublishedDataSource(tmp_path).verify(definition(expected))


def test_published_data_source_requires_release_binding(tmp_path):
    expected = publication()
    write_generation(tmp_path, expected)
    marker = tmp_path / "588080_strategy_generation.json"
    generation = json.loads(marker.read_text(encoding="utf-8"))
    generation["strategy_releases"] = ["S001-v1"]
    marker.write_text(json.dumps(generation), encoding="utf-8")

    with pytest.raises(RuntimeContractError, match="does not contain"):
        PublishedDataSource(tmp_path).verify(definition(expected))
