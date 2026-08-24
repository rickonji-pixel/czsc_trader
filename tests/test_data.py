from pathlib import Path
from hashlib import sha256
import json
import shutil

import pandas as pd
import pytest

import czsc_trader.data as data_module
from czsc_trader.data import load_market_data


RAW_DIR = Path("data/raw")


def test_loads_verified_market_data() -> None:
    """Catch missing partitions, normalization, or intraday sessions."""
    data = load_market_data(RAW_DIR)

    expected_rows = {
        frequency: sum(
            record["rows"]
            for record in data.manifest["files"].values()
            if record["frequency"] == frequency
        )
        for frequency in ("30m", "daily", "weekly")
    }
    assert len(data.intraday) == expected_rows["30m"]
    assert len(data.daily) == expected_rows["daily"]
    assert len(data.weekly) == expected_rows["weekly"]
    assert data.intraday["symbol"].unique().tolist() == ["588080.SH"]
    assert data.intraday.groupby(data.intraday["dt"].dt.normalize()).size().eq(8).all()
    assert len(data.hashes) == 21


def test_cross_frequency_prices_reconcile() -> None:
    """Catch incorrect year concatenation or OHLC aggregation."""
    data = load_market_data(RAW_DIR)
    intraday = data.intraday.assign(date=data.intraday["dt"].dt.normalize())
    aggregated = intraday.groupby("date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    )
    daily = data.daily.set_index("dt")

    pd.testing.assert_frame_equal(
        aggregated,
        daily[["open", "high", "low", "close"]],
        check_freq=False,
        check_names=False,
        rtol=0.0,
        atol=0.005,
    )


def test_truncate_keeps_only_information_available_by_cutoff() -> None:
    """Catch future rows leaking through a truncated research view."""
    data = load_market_data(RAW_DIR)
    cutoff = pd.Timestamp("2025-06-30")
    truncated = data.truncate(cutoff)

    assert truncated.intraday["dt"].max().normalize() <= cutoff
    assert truncated.daily["dt"].max() <= cutoff
    assert truncated.weekly["dt"].max() <= cutoff
    assert truncated.hashes == data.hashes
    assert truncated.symbol == data.symbol
    assert truncated.asset_type == data.asset_type


def test_cutoff_loader_never_opens_future_year_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch holdout K-lines being opened before the challenger is frozen."""
    opened: list[str] = []
    original = data_module._read_one

    def recording_read(path: Path, freq: str, symbol: str) -> pd.DataFrame:
        opened.append(path.name)
        return original(path, freq, symbol)

    monkeypatch.setattr(data_module, "_read_one", recording_read)

    visible = load_market_data(RAW_DIR, cutoff="2025-12-31")

    assert opened
    assert all("_2026.csv" not in name for name in opened)
    assert visible.daily["dt"].max() == pd.Timestamp("2025-12-31")
    assert set(visible.hashes) == {
        f"588080_{frequency}_{year}.csv"
        for frequency in ("30m", "daily", "weekly")
        for year in range(2020, 2026)
    }


def _write_generic_fixture(tmp_path: Path) -> Path:
    files: dict[str, dict[str, object]] = {}
    for frequency in ("30m", "daily", "weekly"):
        source = RAW_DIR / f"588080_{frequency}_2026.csv"
        filename = f"600519_{frequency}_2026.csv"
        destination = tmp_path / filename
        shutil.copyfile(source, destination)
        files[filename] = {
            "frequency": frequency,
            "year": 2026,
            "sha256": sha256(destination.read_bytes()).hexdigest(),
        }
    manifest = {
        "schema_version": 1,
        "symbol": "600519.SH",
        "code": "600519",
        "asset_type": "stock",
        "vendor": "tushare",
        "files": files,
    }
    (tmp_path / "600519_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (tmp_path / "600519_validation.json").write_text(
        json.dumps({"status": "PASS"}), encoding="utf-8"
    )
    return tmp_path


def test_loads_nonfixed_symbol_and_manifest_years(tmp_path: Path) -> None:
    raw_dir = _write_generic_fixture(tmp_path)

    data = load_market_data(raw_dir, "600519.SH", "stock")

    assert data.symbol == "600519.SH"
    assert data.asset_type == "stock"
    assert data.intraday["symbol"].unique().tolist() == ["600519.SH"]
    assert len(data.hashes) == 3
    assert set(data.manifest["files"]) == {
        "600519_30m_2026.csv",
        "600519_daily_2026.csv",
        "600519_weekly_2026.csv",
    }


def test_loader_rejects_nonpass_validation(tmp_path: Path) -> None:
    raw_dir = _write_generic_fixture(tmp_path)
    (raw_dir / "600519_validation.json").write_text(
        json.dumps({"status": "FAIL"}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="validation status"):
        load_market_data(raw_dir, "600519.SH", "stock")


def test_loader_rejects_hash_mismatch(tmp_path: Path) -> None:
    raw_dir = _write_generic_fixture(tmp_path)
    path = raw_dir / "600519_daily_2026.csv"
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="SHA-256"):
        load_market_data(raw_dir, "600519.SH", "stock")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("symbol", "000001.SZ", "symbol"),
        ("asset_type", "etf", "asset type"),
    ],
)
def test_loader_rejects_manifest_identity_mismatch(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    raw_dir = _write_generic_fixture(tmp_path)
    path = raw_dir / "600519_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_market_data(raw_dir, "600519.SH", "stock")
