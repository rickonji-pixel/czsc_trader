from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from czsc_trader.new_exit_representation_runner import (
    DAILY_DESCRIPTOR_IDS,
    DESCRIPTOR_IDS,
    INTRADAY_DESCRIPTOR_IDS,
    assign_causal_quantile_bins,
    classify_representation,
    compute_daily_descriptors,
    compute_intraday_descriptors,
    compute_weekly_descriptor,
    discover_and_confirm_signatures,
    exact_permutation_audit,
    run_new_exit_representation,
    validate_archive_reference,
    validate_artifact_reference,
    validate_event_cohort,
    validate_novelty_exclusion,
    validate_visible_hashes,
    validate_protocol,
)


def _protocol(**overrides: object) -> dict[str, object]:
    protocol = json.loads(
        Path("experiments/0825_EX06/artifacts/protocol.json").read_text(
            encoding="utf-8"
        )
    )
    protocol.update(overrides)
    return protocol


def _daily_fixture(periods: int = 40) -> pd.DataFrame:
    close = np.arange(100.0, 100.0 + periods)
    return pd.DataFrame(
        {
            "dt": pd.bdate_range("2020-01-01", periods=periods),
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.arange(1_000.0, 1_000.0 + 10 * periods, 10.0),
        }
    )


def _intraday_fixture(days: int = 22) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    dates = pd.bdate_range("2020-01-01", periods=days)
    times = ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")
    for day_index, date in enumerate(dates):
        prior = 100.0 + day_index
        for bar_index, time in enumerate(times):
            opened = prior if bar_index else 100.0 + day_index
            closed = opened * (1.001 if bar_index % 2 == 0 else 0.999)
            rows.append(
                {
                    "dt": pd.Timestamp(f"{date.date()} {time}"),
                    "open": opened,
                    "high": max(opened, closed) + 0.1,
                    "low": min(opened, closed) - 0.1,
                    "close": closed,
                    "volume": float(100 + bar_index),
                }
            )
            prior = closed
    return pd.DataFrame(rows)


def _events() -> pd.DataFrame:
    dominant = [
        "2021H1:early_ex04_exit:20210616",
        "2021H2:early_ex04_exit:20210706",
        "2023H1:early_ex04_exit:20230208",
    ]
    rows = [
        {
            "event_id": event_id,
            "signal_date": date,
            "outcome_label": "false_exit",
            "regime": "uptrend" if index != 2 else "sideways",
            "block_label": "joint_margin_block",
        }
        for index, (event_id, date) in enumerate(
            zip(dominant, ("2021-06-16", "2021-07-06", "2023-02-08"), strict=True)
        )
    ]
    for index in range(11):
        rows.append(
            {
                "event_id": f"protective-{index:02d}",
                "signal_date": f"2024-{index + 1:02d}-01",
                "outcome_label": "protective_exit",
                "regime": "downtrend" if index < 4 else "sideways",
                "block_label": "independent_double_block" if index < 9 else "joint_margin_block",
            }
        )
    return pd.DataFrame(rows)


def _matrix_for_bins(protective_bins: list[str]) -> pd.DataFrame:
    events = _events()
    bins = ["Q1", "Q1", "Q1", *protective_bins]
    return pd.DataFrame(
        {
            "event_id": events["event_id"],
            "descriptor": "close_to_ma20",
            "raw_value": np.arange(len(events), dtype=float),
            "quantile_bin": bins,
            "history_count": 252,
            "feature_date": pd.to_datetime(events["signal_date"]),
            "max_input_dt": pd.to_datetime(events["signal_date"]),
        }
    )


def test_protocol_freezes_diagnostic_boundary() -> None:
    validate_protocol(_protocol())
    invalid = _protocol(holdout_access_allowed=True)
    with pytest.raises(ValueError, match="holdout"):
        validate_protocol(invalid)
    invalid = _protocol(expected_descriptor_count=27)
    with pytest.raises(ValueError, match="descriptor"):
        validate_protocol(invalid)


def test_daily_descriptor_identity_and_literal_formulas() -> None:
    daily = _daily_fixture()
    frame = compute_daily_descriptors(daily)

    assert len(DAILY_DESCRIPTOR_IDS) == 23
    assert len(DESCRIPTOR_IDS) == 28
    assert frame.columns.tolist() == ["dt", *DAILY_DESCRIPTOR_IDS]
    last = len(daily) - 1
    ma20 = daily.loc[last - 19 : last, "close"].mean()
    ma10 = daily.loc[last - 9 : last, "close"].mean()
    assert frame.loc[last, "close_to_ma20"] == pytest.approx(
        daily.loc[last, "close"] / ma20 - 1
    )
    assert frame.loc[last, "ma10_ma20_spread"] == pytest.approx(ma10 / ma20 - 1)
    assert frame.loc[last, "drawdown_from_high20"] == pytest.approx(
        daily.loc[last, "close"] / daily.loc[last - 19 : last, "high"].max() - 1
    )
    assert frame.loc[last, "negative_day_share5"] == 0
    assert np.isnan(frame.loc[0, "return_volume_corr10"])


def test_descriptors_accept_normalized_market_data_vol_column() -> None:
    daily = _daily_fixture().rename(columns={"volume": "vol"})
    frame = compute_daily_descriptors(daily)

    assert frame["volume_t_to20"].notna().any()


def test_daily_descriptors_turn_zero_denominators_into_nan() -> None:
    daily = _daily_fixture()
    daily.loc[:, ["open", "high", "low", "close"]] = 100.0
    daily.loc[:, "volume"] = 1_000.0
    frame = compute_daily_descriptors(daily)

    assert np.isnan(frame.iloc[-1]["realized_vol5_to20"])
    assert np.isnan(frame.iloc[-1]["downside_semivol5_to20"])
    assert np.isnan(frame.iloc[-1]["return_volume_corr10"])
    assert np.isfinite(frame.iloc[-1]["signed_volume_imbalance10"])


def test_intraday_descriptors_use_first_open_to_close_and_prior_history() -> None:
    intraday = _intraday_fixture()
    frame = compute_intraday_descriptors(intraday)

    assert frame.columns.tolist() == ["dt", *INTRADAY_DESCRIPTOR_IDS]
    assert len(frame) == 22
    final_day = intraday["dt"].dt.normalize().max()
    bars = intraday.loc[intraday["dt"].dt.normalize() == final_day]
    first_return = bars.iloc[0]["close"] / bars.iloc[0]["open"] - 1
    remaining = bars["close"].iloc[1:].to_numpy() / bars["close"].iloc[:-1].to_numpy() - 1
    returns = np.r_[first_return, remaining]
    expected_down_share = bars.loc[returns < 0, "volume"].sum() / bars["volume"].sum()
    assert frame.iloc[-1]["intraday_down_volume_share"] == pytest.approx(expected_down_share)
    assert np.isfinite(frame.iloc[-1]["intraday_realized_vol_to20"])
    assert np.isnan(frame.iloc[19]["intraday_realized_vol_to20"])


def test_weekly_descriptor_uses_only_last_completed_week() -> None:
    weekly_dates = pd.date_range("2020-01-03", periods=32, freq="W-FRI")
    weekly = pd.DataFrame({"dt": weekly_dates, "close": np.arange(100.0, 132.0)})
    daily_dates = pd.Series(
        [weekly_dates[20], weekly_dates[21] - pd.Timedelta(days=2), weekly_dates[21]]
    )
    result = compute_weekly_descriptor(daily_dates, weekly)

    value20 = weekly.loc[20, "close"] / weekly.loc[11:20, "close"].mean() - 1
    value21 = weekly.loc[21, "close"] / weekly.loc[12:21, "close"].mean() - 1
    assert result.iloc[0] == pytest.approx(value20)
    assert result.iloc[1] == pytest.approx(value20)
    assert result.iloc[2] == pytest.approx(value21)


def test_causal_quantile_bins_exclude_current_and_use_lower_closed_boundary() -> None:
    raw = pd.DataFrame(
        {"dt": pd.bdate_range("2020-01-01", periods=7), "x": [1, 2, 3, 4, 5, 100, 2]}
    )
    protocol = _protocol(daily_history_window=5, daily_minimum_history=5)
    bins = assign_causal_quantile_bins(raw, protocol, descriptor_ids=("x",))

    assert bins.loc[5, "x"] == "Q5"
    assert bins.loc[6, "x"] == "Q1"
    assert bins.loc[4, "x"] is None


def test_quantile_bins_accept_distinct_weekly_history_settings() -> None:
    raw = pd.DataFrame(
        {"dt": pd.date_range("2020-01-03", periods=22, freq="W-FRI"), "x": range(22)}
    )
    bins = assign_causal_quantile_bins(
        raw,
        _protocol(),
        descriptor_ids=("x",),
        history_window=26,
        minimum_history=20,
    )

    assert bins.loc[19, "x"] is None
    assert bins.loc[20, "x"] == "Q5"


def test_discovery_confirmation_and_contamination_are_sequential() -> None:
    events = _events()
    matrix = _matrix_for_bins(["Q2"] * 4 + ["Q3"] * 5 + ["Q2"] * 2)
    discovered, confirmed = discover_and_confirm_signatures(events, matrix, _protocol())

    assert discovered[["descriptor", "quantile_bin"]].to_dict("records") == [
        {"descriptor": "close_to_ma20", "quantile_bin": "Q1"}
    ]
    row = confirmed.iloc[0]
    assert int(row["protective_support"]) == 0
    assert int(row["downtrend_protective_support"]) == 0
    assert int(row["joint_margin_protective_support"]) == 0
    assert bool(row["low_contamination"])


def test_exact_permutation_enumerates_all_364_label_sets() -> None:
    events = _events()
    matrix = _matrix_for_bins(["Q2"] * 4 + ["Q3"] * 5 + ["Q2"] * 2)
    audit = exact_permutation_audit(events, matrix, _protocol())

    assert audit["combination_count"] == 364
    assert audit["qualifying_combination_count"] == 11
    assert audit["exact_p_value"] == pytest.approx(11 / 364)


@pytest.mark.parametrize(
    ("confirmed", "audit", "evidence_ok", "expected"),
    [
        (pd.DataFrame(), {"exact_p_value": 1.0}, False, "insufficient_new_representation_evidence"),
        (
            pd.DataFrame([{"low_contamination": True}]),
            {"exact_p_value": 0.04},
            True,
            "new_representation_confirmed",
        ),
        (
            pd.DataFrame([{"low_contamination": True}]),
            {"exact_p_value": 0.10},
            True,
            "suggestive_but_multiplicity_unconfirmed",
        ),
        (
            pd.DataFrame([{"low_contamination": False}]),
            {"exact_p_value": 1.0},
            True,
            "shared_but_contaminated_new_representation",
        ),
        (pd.DataFrame(), {"exact_p_value": 1.0}, True, "no_shared_new_representation"),
    ],
)
def test_classification_order(
    confirmed: pd.DataFrame,
    audit: dict[str, object],
    evidence_ok: bool,
    expected: str,
) -> None:
    result = classify_representation(
        event_count=20,
        descriptor_count=28,
        discovered_count=0 if confirmed.empty else 1,
        confirmed=confirmed,
        permutation_audit=audit,
        evidence_ok=evidence_ok,
        protocol=_protocol(),
    )

    assert result["classification"] == expected


def test_visible_hashes_and_event_cohort_reject_identity_drift() -> None:
    validate_visible_hashes({"588080_daily_2025.csv": "abc"})
    with pytest.raises(ValueError, match="2026"):
        validate_visible_hashes({"588080_daily_2026.csv": "abc"})

    events = pd.read_csv("experiments/0825_EX04/artifacts/exit_event_counterfactuals.csv")
    validate_event_cohort(events, _protocol())
    with pytest.raises(ValueError, match="event"):
        validate_event_cohort(events.iloc[:-1], _protocol())


def test_tracked_source_identity_chain_matches_preregistration() -> None:
    protocol = _protocol()
    root = Path.cwd()

    source = validate_archive_reference(root, protocol["event_source"], label="0825_EX04")
    prior = validate_archive_reference(
        root, protocol["prior_negative_result"], label="0825_EX05"
    )
    baseline = validate_artifact_reference(
        root, protocol["research_baseline"], label="0824_EX04"
    )
    novelty = validate_novelty_exclusion(root, protocol["novelty_exclusion"])

    assert source["experiment_id"] == "0825_EX04"
    assert prior["experiment_id"] == "0825_EX05"
    assert baseline == protocol["research_baseline"]["sha256"]
    assert novelty == protocol["novelty_exclusion"]["portable_manifest_sha256"]


def test_tracked_source_identity_chain_rejects_hash_drift() -> None:
    protocol = _protocol()
    root = Path.cwd()
    source = json.loads(json.dumps(protocol["event_source"]))
    source["files"]["artifacts/metrics.json"] = "0" * 64
    with pytest.raises(ValueError, match="hash differs"):
        validate_archive_reference(root, source, label="0825_EX04")

    baseline = json.loads(json.dumps(protocol["research_baseline"]))
    baseline["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash differs"):
        validate_artifact_reference(root, baseline, label="0824_EX04")

    novelty = json.loads(json.dumps(protocol["novelty_exclusion"]))
    novelty["portable_manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash differs"):
        validate_novelty_exclusion(root, novelty)


def _formal_market_fixture():
    from czsc_trader.data import MarketData

    dates = pd.bdate_range("2020-01-01", "2025-12-31")
    close = 100 + np.linspace(0, 50, len(dates)) + np.sin(np.arange(len(dates)) / 13)
    daily = pd.DataFrame(
        {
            "dt": dates,
            "open": close * 0.999,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "vol": 10_000 + (np.arange(len(dates)) % 37) * 100,
        }
    )
    times = ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")
    intraday_rows: list[dict[str, object]] = []
    for index, date in enumerate(dates):
        day_close = float(close[index])
        for bar_index, time in enumerate(times):
            bar_close = day_close * (0.998 + 0.0005 * (bar_index + 1))
            bar_open = day_close * (0.998 + 0.0005 * bar_index)
            intraday_rows.append(
                {
                    "dt": pd.Timestamp(f"{date.date()} {time}"),
                    "open": bar_open,
                    "high": max(bar_open, bar_close) * 1.001,
                    "low": min(bar_open, bar_close) * 0.999,
                    "close": bar_close,
                    "vol": float(1_000 + bar_index * 10),
                }
            )
    intraday = pd.DataFrame(intraday_rows)
    weekly = (
        daily.assign(_week=daily["dt"].dt.to_period("W-SUN"))
        .groupby("_week", as_index=False)
        .agg(
            dt=("dt", "max"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            vol=("vol", "sum"),
        )
        .drop(columns="_week", errors="ignore")
    )
    return MarketData(
        intraday=intraday,
        daily=daily,
        weekly=weekly,
        hashes={"588080_daily_2025.csv": "abc", "588080_30m_2025.csv": "def", "588080_weekly_2025.csv": "ghi"},
    )


def test_formal_runner_writes_only_frozen_diagnostic_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    experiment_dir = tmp_path / "experiments" / "0825_EX06"
    artifacts = experiment_dir / "artifacts"
    artifacts.mkdir(parents=True)
    protocol = _protocol()
    (artifacts / "protocol.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    source_events = pd.read_csv(
        "experiments/0825_EX04/artifacts/exit_event_counterfactuals.csv"
    )

    monkeypatch.setattr(
        "czsc_trader.new_exit_representation_runner.validate_archive_reference",
        lambda *args, **kwargs: {"experiment_id": kwargs.get("label", "source")},
    )
    monkeypatch.setattr(
        "czsc_trader.new_exit_representation_runner.validate_artifact_reference",
        lambda *args, **kwargs: "baseline-sha",
    )
    monkeypatch.setattr(
        "czsc_trader.new_exit_representation_runner.validate_novelty_exclusion",
        lambda *args, **kwargs: "novelty-sha",
    )
    monkeypatch.setattr(
        "czsc_trader.new_exit_representation_runner.load_market_data",
        lambda *args, **kwargs: _formal_market_fixture(),
    )
    monkeypatch.setattr(
        "czsc_trader.new_exit_representation_runner.load_source_events",
        lambda *args, **kwargs: source_events.copy(),
    )

    summary = run_new_exit_representation(tmp_path / "data" / "raw", experiment_dir, protocol)

    assert summary["status"] == "COMPLETE"
    assert summary["event_count"] == 20
    assert summary["descriptor_count"] == 28
    assert summary["holdout_accessed"] is False
    assert summary["frozen_challenger"] is None
    expected = {
        "protocol.json",
        "identity_audit.json",
        "descriptor_definitions.csv",
        "event_new_representation.csv",
        "discovered_signatures.csv",
        "confirmed_signatures.csv",
        "permutation_audit.json",
        "representation_classification.json",
        "metrics.json",
    }
    assert {path.name for path in artifacts.iterdir()} == expected
    matrix = pd.read_csv(artifacts / "event_new_representation.csv")
    assert len(matrix) == 20 * 28
    assert pd.to_datetime(matrix["max_input_dt"]).le(
        pd.to_datetime(matrix["signal_date"])
    ).all()
    for forbidden in ("orders.csv", "candidate_results.csv", "frozen_challenger.json"):
        assert not (artifacts / forbidden).exists()
