"""Causal new-representation diagnosis for the preregistered 0825_EX06 study."""

from __future__ import annotations

from hashlib import sha256
from itertools import combinations
import json
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from .data import load_market_data
from .experiment_archive import validate_experiment_archive


DAILY_DESCRIPTOR_IDS = (
    "close_to_ma20",
    "ma20_slope5",
    "ma10_ma20_spread",
    "drawdown_from_high20",
    "close_location20",
    "return5",
    "return10",
    "prior_low10_buffer",
    "atr5_to_atr20",
    "tr_to_atr20",
    "realized_vol5_to20",
    "downside_semivol5_to20",
    "negative_day_share5",
    "max_drawdown5_to_atr20",
    "daily_close_location",
    "gap_abs_to_atr20",
    "volume5_to20",
    "volume_t_to20",
    "down_up_volume_ratio10",
    "signed_volume_imbalance5",
    "signed_volume_imbalance10",
    "return_volume_corr10",
    "log_volume_slope5",
)

INTRADAY_DESCRIPTOR_IDS = (
    "intraday_down_volume_share",
    "intraday_realized_vol_to20",
    "intraday_close_location",
    "last4_30m_return",
)

WEEKLY_DESCRIPTOR_IDS = ("weekly_close_to_ma10",)
DESCRIPTOR_IDS = DAILY_DESCRIPTOR_IDS + INTRADAY_DESCRIPTOR_IDS + WEEKLY_DESCRIPTOR_IDS

DESCRIPTOR_FAMILIES = {
    **{name: "trend_geometry" for name in DESCRIPTOR_IDS[:8]},
    **{name: "pullback_energy" for name in DESCRIPTOR_IDS[8:16]},
    **{name: "price_volume_structure" for name in DESCRIPTOR_IDS[16:23]},
    **{name: "intraday_weekly_structure" for name in DESCRIPTOR_IDS[23:]},
}


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    result = numerator.astype(float).div(denominator.astype(float))
    return result.where(np.isfinite(result) & denominator.ne(0))


def validate_protocol(protocol: Mapping[str, object]) -> None:
    """Reject drift from the frozen diagnostic-only protocol."""
    if protocol.get("experiment_type") != "new_exit_representation_diagnosis":
        raise ValueError("unexpected experiment type")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("protocol must retain PRE_REGISTERED status")
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise ValueError("visible sample must stop at 2025-12-31")
    if protocol.get("holdout_access_allowed") is not False:
        raise ValueError("protocol must forbid holdout access")
    if int(protocol.get("expected_descriptor_count", -1)) != len(DESCRIPTOR_IDS):
        raise ValueError("descriptor identity count drifted")
    if int(protocol.get("expected_event_count", -1)) != 20:
        raise ValueError("event count drifted")
    if int(protocol.get("primary_material_event_count", -1)) != 14:
        raise ValueError("primary material event count drifted")
    if protocol.get("quantile_boundaries") != [0.2, 0.4, 0.6, 0.8]:
        raise ValueError("quantile boundaries drifted")
    if int(protocol.get("exact_permutation_combinations", -1)) != 364:
        raise ValueError("permutation count drifted")
    if len(protocol.get("discovery_event_ids", [])) != 2:
        raise ValueError("discovery event identities drifted")
    if not protocol.get("confirmation_event_id"):
        raise ValueError("confirmation event identity is missing")
    promotion = protocol.get("promotion")
    if not isinstance(promotion, Mapping) or any(bool(value) for value in promotion.values()):
        raise ValueError("diagnostic protocol must disable promotion")


def _require_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    if "volume" not in frame.columns and "vol" in frame.columns:
        frame = frame.rename(columns={"vol": "volume"})
    required = ["dt", "open", "high", "low", "close", "volume"]
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError(f"OHLCV columns missing: {missing}")
    result = frame.loc[:, required].copy()
    result["dt"] = pd.to_datetime(result["dt"])
    result = result.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
    return result


def _rolling_downside_rms(returns: pd.Series, window: int) -> pd.Series:
    downside_squared = returns.clip(upper=0).pow(2)
    return downside_squared.rolling(window, min_periods=window).mean().pow(0.5)


def _max_drawdown(values: np.ndarray) -> float:
    if len(values) == 0 or not np.isfinite(values).all():
        return np.nan
    peaks = np.maximum.accumulate(values)
    return float(np.max(1.0 - values / peaks))


def _rolling_slope(values: np.ndarray) -> float:
    if not np.isfinite(values).all():
        return np.nan
    x = np.arange(len(values), dtype=float)
    centered = x - x.mean()
    return float(np.dot(centered, values - values.mean()) / np.dot(centered, centered))


def compute_daily_descriptors(daily: pd.DataFrame) -> pd.DataFrame:
    """Compute the frozen 23 daily descriptors without event outcomes."""
    prices = _require_ohlcv(daily)
    close = prices["close"].astype(float)
    opened = prices["open"].astype(float)
    high = prices["high"].astype(float)
    low = prices["low"].astype(float)
    volume = prices["volume"].astype(float)
    returns = close.pct_change()
    ma10 = close.rolling(10, min_periods=10).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    previous_close = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr5 = true_range.rolling(5, min_periods=5).mean()
    atr20 = true_range.rolling(20, min_periods=20).mean()
    realized5 = returns.rolling(5, min_periods=5).std(ddof=1)
    realized20 = returns.rolling(20, min_periods=20).std(ddof=1)
    downside5 = _rolling_downside_rms(returns, 5)
    downside20 = _rolling_downside_rms(returns, 20)
    negative_volume = volume.where(returns.lt(0))
    positive_volume = volume.where(returns.gt(0))
    negative_volume_mean = _safe_divide(
        negative_volume.fillna(0).rolling(10, min_periods=10).sum(),
        returns.lt(0).astype(float).rolling(10, min_periods=10).sum(),
    )
    positive_volume_mean = _safe_divide(
        positive_volume.fillna(0).rolling(10, min_periods=10).sum(),
        returns.gt(0).astype(float).rolling(10, min_periods=10).sum(),
    )
    signed_volume = np.sign(returns.fillna(0)) * volume
    volume_sum5 = volume.rolling(5, min_periods=5).sum()
    volume_sum10 = volume.rolling(10, min_periods=10).sum()
    atr20_fraction = _safe_divide(atr20, close)
    max_drawdown5 = close.rolling(5, min_periods=5).apply(_max_drawdown, raw=True)
    day_range = high - low
    range20 = high.rolling(20, min_periods=20).max() - low.rolling(20, min_periods=20).min()
    log_volume = np.log1p(volume)

    output = pd.DataFrame(
        {
            "dt": prices["dt"],
            "close_to_ma20": _safe_divide(close, ma20) - 1,
            "ma20_slope5": _safe_divide(ma20, ma20.shift(5)) - 1,
            "ma10_ma20_spread": _safe_divide(ma10, ma20) - 1,
            "drawdown_from_high20": _safe_divide(
                close, high.rolling(20, min_periods=20).max()
            )
            - 1,
            "close_location20": _safe_divide(
                close - low.rolling(20, min_periods=20).min(), range20
            ),
            "return5": close.pct_change(5),
            "return10": close.pct_change(10),
            "prior_low10_buffer": _safe_divide(
                close, low.shift(1).rolling(10, min_periods=10).min()
            )
            - 1,
            "atr5_to_atr20": _safe_divide(atr5, atr20),
            "tr_to_atr20": _safe_divide(true_range, atr20),
            "realized_vol5_to20": _safe_divide(realized5, realized20),
            "downside_semivol5_to20": _safe_divide(downside5, downside20),
            "negative_day_share5": returns.lt(0)
            .astype(float)
            .where(returns.notna())
            .rolling(5, min_periods=5)
            .mean(),
            "max_drawdown5_to_atr20": _safe_divide(max_drawdown5, atr20_fraction),
            "daily_close_location": _safe_divide(close - low, day_range),
            "gap_abs_to_atr20": _safe_divide(
                (opened / previous_close - 1).abs(), _safe_divide(atr20, previous_close)
            ),
            "volume5_to20": _safe_divide(
                volume.rolling(5, min_periods=5).mean(),
                volume.rolling(20, min_periods=20).mean(),
            ),
            "volume_t_to20": _safe_divide(volume, volume.rolling(20, min_periods=20).mean()),
            "down_up_volume_ratio10": _safe_divide(
                negative_volume_mean, positive_volume_mean
            ),
            "signed_volume_imbalance5": _safe_divide(
                signed_volume.rolling(5, min_periods=5).sum(), volume_sum5
            ),
            "signed_volume_imbalance10": _safe_divide(
                signed_volume.rolling(10, min_periods=10).sum(), volume_sum10
            ),
            "return_volume_corr10": returns.rolling(10, min_periods=10).corr(log_volume),
            "log_volume_slope5": log_volume.rolling(5, min_periods=5).apply(
                _rolling_slope, raw=True
            ),
        }
    )
    return output.loc[:, ["dt", *DAILY_DESCRIPTOR_IDS]]


def compute_intraday_descriptors(intraday: pd.DataFrame) -> pd.DataFrame:
    """Aggregate four frozen 30-minute descriptors to one causal row per day."""
    bars = _require_ohlcv(intraday)
    bars["trade_date"] = bars["dt"].dt.normalize()
    rows: list[dict[str, object]] = []
    for trade_date, group in bars.groupby("trade_date", sort=True):
        group = group.sort_values("dt").reset_index(drop=True)
        if len(group) < 4:
            raise ValueError(f"intraday day has fewer than four bars: {trade_date}")
        returns = np.empty(len(group), dtype=float)
        returns[0] = float(group.loc[0, "close"] / group.loc[0, "open"] - 1)
        returns[1:] = (
            group["close"].iloc[1:].to_numpy(dtype=float)
            / group["close"].iloc[:-1].to_numpy(dtype=float)
            - 1
        )
        volume = group["volume"].to_numpy(dtype=float)
        total_volume = float(volume.sum())
        down_share = float(volume[returns < 0].sum() / total_volume) if total_volume else np.nan
        day_low = float(group["low"].min())
        day_high = float(group["high"].max())
        day_range = day_high - day_low
        rows.append(
            {
                "dt": pd.Timestamp(trade_date),
                "intraday_down_volume_share": down_share,
                "_intraday_realized_vol": float(np.sqrt(np.square(returns).sum())),
                "intraday_close_location": (
                    float((group.iloc[-1]["close"] - day_low) / day_range)
                    if day_range
                    else np.nan
                ),
                "last4_30m_return": float(
                    group.iloc[-1]["close"] / group.iloc[-4]["open"] - 1
                ),
            }
        )
    result = pd.DataFrame(rows)
    history_median = (
        result["_intraday_realized_vol"]
        .shift(1)
        .rolling(20, min_periods=20)
        .median()
    )
    result["intraday_realized_vol_to20"] = _safe_divide(
        result["_intraday_realized_vol"], history_median
    )
    return result.loc[:, ["dt", *INTRADAY_DESCRIPTOR_IDS]]


def compute_weekly_descriptor(
    daily_dates: pd.Series, weekly: pd.DataFrame
) -> pd.Series:
    """Align the most recent completed weekly close/MA10 value to daily dates."""
    if not {"dt", "close"}.issubset(weekly.columns):
        raise ValueError("weekly frame must contain dt and close")
    weeks = weekly.loc[:, ["dt", "close"]].copy()
    weeks["dt"] = pd.to_datetime(weeks["dt"])
    weeks = weeks.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
    weeks["weekly_close_to_ma10"] = _safe_divide(
        weeks["close"], weeks["close"].rolling(10, min_periods=10).mean()
    ) - 1
    requested = pd.DataFrame(
        {"_order": np.arange(len(daily_dates)), "dt": pd.to_datetime(daily_dates).to_numpy()}
    ).sort_values("dt")
    aligned = pd.merge_asof(
        requested,
        weeks.loc[:, ["dt", "weekly_close_to_ma10"]],
        on="dt",
        direction="backward",
        allow_exact_matches=True,
    ).sort_values("_order")
    return aligned["weekly_close_to_ma10"].reset_index(drop=True)


def _quantile_bin(value: float, quantiles: Sequence[float]) -> str | None:
    if not np.isfinite(value) or len(quantiles) != 4 or not np.isfinite(quantiles).all():
        return None
    for index, boundary in enumerate(quantiles, start=1):
        if value <= boundary:
            return f"Q{index}"
    return "Q5"


def assign_causal_quantile_bins(
    frame: pd.DataFrame,
    protocol: Mapping[str, object],
    *,
    descriptor_ids: Sequence[str] | None = None,
    history_window: int | None = None,
    minimum_history: int | None = None,
) -> pd.DataFrame:
    """Assign current rows to bins computed strictly from earlier rows."""
    ids = tuple(descriptor_ids or [column for column in frame if column != "dt"])
    window = int(history_window or protocol["daily_history_window"])
    minimum = int(minimum_history or protocol["daily_minimum_history"])
    values = frame.loc[:, ["dt", *ids]].copy().sort_values("dt").reset_index(drop=True)
    output = pd.DataFrame({"dt": pd.to_datetime(values["dt"])})
    for descriptor in ids:
        series = pd.to_numeric(values[descriptor], errors="coerce")
        assigned: list[str | None] = []
        for index, current in enumerate(series):
            history = series.iloc[max(0, index - window) : index].dropna()
            if len(history) < minimum or not np.isfinite(current):
                assigned.append(None)
                continue
            quantiles = history.quantile(
                protocol["quantile_boundaries"], interpolation="linear"
            ).to_numpy(dtype=float)
            assigned.append(_quantile_bin(float(current), quantiles))
        output[descriptor] = pd.Series(assigned, dtype=object)
    return output


def discover_and_confirm_signatures(
    events: pd.DataFrame,
    matrix: pd.DataFrame,
    protocol: Mapping[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply the fixed two-event discovery and one-event time confirmation."""
    discovery_ids = [str(value) for value in protocol["discovery_event_ids"]]
    confirmation_id = str(protocol["confirmation_event_id"])
    if not events["event_id"].is_unique:
        raise ValueError("event identities must be unique")
    value_lookup = matrix.pivot(index="event_id", columns="descriptor", values="quantile_bin")
    discovered_rows: list[dict[str, object]] = []
    confirmed_rows: list[dict[str, object]] = []
    for descriptor in sorted(value_lookup.columns.astype(str)):
        values = [value_lookup.at[event_id, descriptor] for event_id in discovery_ids]
        if any(pd.isna(value) for value in values) or len(set(values)) != 1:
            continue
        quantile_bin = str(values[0])
        discovered_rows.append({"descriptor": descriptor, "quantile_bin": quantile_bin})
        if pd.isna(value_lookup.at[confirmation_id, descriptor]):
            continue
        if str(value_lookup.at[confirmation_id, descriptor]) != quantile_bin:
            continue
        support_ids = set(
            value_lookup.index[value_lookup[descriptor].astype(str).eq(quantile_bin)].astype(str)
        )
        protective = events.loc[events["outcome_label"].eq("protective_exit")]
        protective_ids = set(protective["event_id"].astype(str))
        protected_support_ids = support_ids & protective_ids
        downtrend_ids = set(
            protective.loc[protective["regime"].eq("downtrend"), "event_id"].astype(str)
        )
        joint_ids = set(
            protective.loc[
                protective["block_label"].eq("joint_margin_block"), "event_id"
            ].astype(str)
        )
        protective_support = len(protected_support_ids)
        downtrend_support = len(support_ids & downtrend_ids)
        joint_support = len(support_ids & joint_ids)
        low_contamination = (
            protective_support <= int(protocol["maximum_protective_support"])
            and (
                not bool(protocol["require_zero_downtrend_protective_support"])
                or downtrend_support == 0
            )
            and (
                not bool(protocol["require_zero_joint_margin_protective_support"])
                or joint_support == 0
            )
        )
        confirmed_rows.append(
            {
                "descriptor": descriptor,
                "quantile_bin": quantile_bin,
                "protective_support": protective_support,
                "downtrend_protective_support": downtrend_support,
                "joint_margin_protective_support": joint_support,
                "other_false_support": len(
                    support_ids
                    & set(
                        events.loc[
                            events["outcome_label"].eq("false_exit")
                            & ~events["event_id"].isin([*discovery_ids, confirmation_id]),
                            "event_id",
                        ].astype(str)
                    )
                ),
                "neutral_support": len(
                    support_ids
                    & set(
                        events.loc[events["outcome_label"].eq("neutral_exit"), "event_id"].astype(str)
                    )
                ),
                "low_contamination": low_contamination,
            }
        )
    discovered = pd.DataFrame(discovered_rows, columns=["descriptor", "quantile_bin"])
    confirmed = pd.DataFrame(
        confirmed_rows,
        columns=[
            "descriptor",
            "quantile_bin",
            "protective_support",
            "downtrend_protective_support",
            "joint_margin_protective_support",
            "other_false_support",
            "neutral_support",
            "low_contamination",
        ],
    )
    return discovered, confirmed


def exact_permutation_audit(
    events: pd.DataFrame,
    matrix: pd.DataFrame,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Enumerate all primary-label allocations and control family-wise selection."""
    dominant_ids = [
        *[str(value) for value in protocol["discovery_event_ids"]],
        str(protocol["confirmation_event_id"]),
    ]
    protective_ids = events.loc[
        events["outcome_label"].eq("protective_exit"), "event_id"
    ].astype(str).tolist()
    material_ids = dominant_ids + protective_ids
    if len(material_ids) != int(protocol["primary_material_event_count"]):
        raise ValueError("primary material event cohort differs from protocol")
    if not events["event_id"].is_unique:
        raise ValueError("event identities must be unique")
    context = events.set_index("event_id")
    signatures: dict[tuple[str, str], set[str]] = {}
    material = matrix.loc[matrix["event_id"].astype(str).isin(material_ids)].copy()
    material = material.loc[material["quantile_bin"].notna()]
    for (descriptor, quantile_bin), group in material.groupby(
        ["descriptor", "quantile_bin"], sort=True
    ):
        signatures[(str(descriptor), str(quantile_bin))] = set(
            group["event_id"].astype(str)
        )

    def qualifies(pseudo_positive: set[str]) -> bool:
        pseudo_protective = set(material_ids) - pseudo_positive
        for support in signatures.values():
            if not pseudo_positive.issubset(support):
                continue
            contamination = support & pseudo_protective
            if len(contamination) > int(protocol["maximum_protective_support"]):
                continue
            contaminated_rows = context.loc[list(contamination)] if contamination else None
            if (
                contaminated_rows is not None
                and bool(protocol["require_zero_downtrend_protective_support"])
                and contaminated_rows["regime"].eq("downtrend").any()
            ):
                continue
            if (
                contaminated_rows is not None
                and bool(protocol["require_zero_joint_margin_protective_support"])
                and contaminated_rows["block_label"].eq("joint_margin_block").any()
            ):
                continue
            return True
        return False

    qualifying_count = sum(
        qualifies(set(candidate)) for candidate in combinations(material_ids, 3)
    )
    combination_count = sum(1 for _ in combinations(material_ids, 3))
    if combination_count != int(protocol["exact_permutation_combinations"]):
        raise ValueError("exact permutation combination count drifted")
    return {
        "combination_count": combination_count,
        "qualifying_combination_count": qualifying_count,
        "exact_p_value": qualifying_count / combination_count,
        "observed_label_set_qualifies": qualifies(set(dominant_ids)),
    }


def classify_representation(
    *,
    event_count: int,
    descriptor_count: int,
    discovered_count: int,
    confirmed: pd.DataFrame,
    permutation_audit: Mapping[str, object],
    evidence_ok: bool,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Apply the preregistered five-way machine classification order."""
    if (
        not evidence_ok
        or event_count != int(protocol["expected_event_count"])
        or descriptor_count != int(protocol["expected_descriptor_count"])
    ):
        classification = "insufficient_new_representation_evidence"
    elif confirmed.empty:
        classification = "no_shared_new_representation"
    elif bool(confirmed["low_contamination"].astype(bool).any()):
        if float(permutation_audit["exact_p_value"]) <= float(
            protocol["maximum_exact_p_value"]
        ):
            classification = "new_representation_confirmed"
        else:
            classification = "suggestive_but_multiplicity_unconfirmed"
    else:
        classification = "shared_but_contaminated_new_representation"
    return {
        "classification": classification,
        "event_count": int(event_count),
        "descriptor_count": int(descriptor_count),
        "discovered_signature_count": int(discovered_count),
        "confirmed_signature_count": int(len(confirmed)),
        "low_contamination_signature_count": int(
            confirmed.get("low_contamination", pd.Series(dtype=bool)).astype(bool).sum()
        ),
        "exact_p_value": float(permutation_audit.get("exact_p_value", 1.0)),
    }


def validate_visible_hashes(hashes: Mapping[str, str]) -> None:
    """Prove the loaded market bundle contains tracked pre-2026 inputs only."""
    if not hashes:
        raise ValueError("visible market hash set is empty")
    if any("2026" in str(name) for name in hashes):
        raise ValueError("visible market hashes contain 2026")


def validate_event_cohort(
    events: pd.DataFrame, protocol: Mapping[str, object]
) -> None:
    """Validate the fixed twenty-event cohort inherited from 0825_EX04."""
    required = {
        "event_id",
        "signal_date",
        "outcome_label",
        "regime",
        "block_label",
    }
    if not required <= set(events.columns):
        raise ValueError("event cohort is missing required columns")
    if len(events) != int(protocol["expected_event_count"]):
        raise ValueError("event cohort count differs from preregistration")
    if not events["event_id"].is_unique:
        raise ValueError("event identities are duplicated")
    required_ids = {
        *[str(value) for value in protocol["discovery_event_ids"]],
        str(protocol["confirmation_event_id"]),
    }
    if not required_ids <= set(events["event_id"].astype(str)):
        raise ValueError("dominant event identities differ from preregistration")
    counts = events["outcome_label"].value_counts().to_dict()
    if counts != {"protective_exit": 11, "false_exit": 5, "neutral_exit": 4}:
        raise ValueError("event outcome counts differ from the frozen source")


def validate_archive_reference(
    repository_root: Path,
    specification: Mapping[str, object],
    *,
    label: str,
) -> dict[str, object]:
    """Validate a portable tracked archive and the preregistered file records."""
    archive = Path(repository_root) / str(specification.get("path", ""))
    manifest = validate_experiment_archive(archive)
    for key in ("experiment_id", "status", "holdout_accessed"):
        if key in specification and manifest.get(key) != specification.get(key):
            raise ValueError(f"{label} {key} differs from preregistration")
    expected_files = specification.get("files")
    if not isinstance(expected_files, Mapping):
        raise ValueError(f"{label} file identities are missing")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, Mapping):
        raise ValueError(f"{label} manifest files are missing")
    for relative, expected in expected_files.items():
        record = manifest_files.get(str(relative))
        if not isinstance(record, Mapping) or record.get("sha256") != str(expected):
            raise ValueError(f"{label} file hash differs: {relative}")
    return manifest


def validate_artifact_reference(
    repository_root: Path,
    specification: Mapping[str, object],
    *,
    label: str,
) -> str:
    """Validate one byte-exact tracked artifact."""
    path = Path(repository_root) / str(specification.get("path", ""))
    if not path.is_file():
        raise FileNotFoundError(f"{label} artifact is missing: {path}")
    digest = sha256(path.read_bytes()).hexdigest()
    if digest != str(specification.get("sha256", "")):
        raise ValueError(f"{label} hash differs from preregistration")
    return digest


def validate_novelty_exclusion(
    repository_root: Path, specification: Mapping[str, object]
) -> str:
    """Validate the portable EX06 candidate list used to reject renamed factors."""
    relative_path = Path(str(specification.get("path", "")))
    parts = relative_path.parts
    if len(parts) < 4 or parts[0] != "experiments":
        raise ValueError("novelty exclusion path is invalid")
    archive = Path(repository_root) / Path(*parts[:2])
    manifest = validate_experiment_archive(archive)
    name = Path(*parts[2:]).as_posix()
    record = manifest.get("files", {}).get(name)
    expected = str(specification.get("portable_manifest_sha256", ""))
    if not isinstance(record, Mapping) or record.get("sha256") != expected:
        raise ValueError("novelty exclusion hash differs from preregistration")
    return expected


def load_source_events(
    repository_root: Path, specification: Mapping[str, object]
) -> pd.DataFrame:
    """Load the fixed event cohort after its archive identity is validated."""
    path = (
        Path(repository_root)
        / str(specification["path"])
        / "artifacts"
        / "exit_event_counterfactuals.csv"
    )
    return pd.read_csv(path)


def _descriptor_definitions() -> pd.DataFrame:
    formulas = {
        "close_to_ma20": "C/MA20-1",
        "ma20_slope5": "MA20_t/MA20_t-5-1",
        "ma10_ma20_spread": "MA10/MA20-1",
        "drawdown_from_high20": "C/max(H,20)-1",
        "close_location20": "(C-min(L,20))/(max(H,20)-min(L,20))",
        "return5": "C/C_t-5-1",
        "return10": "C/C_t-10-1",
        "prior_low10_buffer": "C/min(L_t-10:t-1)-1",
        "atr5_to_atr20": "ATR5/ATR20",
        "tr_to_atr20": "TR/ATR20",
        "realized_vol5_to20": "sample_std(ret,5)/sample_std(ret,20)",
        "downside_semivol5_to20": "rms(min(ret,0),5)/rms(min(ret,0),20)",
        "negative_day_share5": "count(ret<0,5)/5",
        "max_drawdown5_to_atr20": "max_close_drawdown(5)/(ATR20/C)",
        "daily_close_location": "(C-L)/(H-L)",
        "gap_abs_to_atr20": "abs(O/C_t-1-1)/(ATR20/C_t-1)",
        "volume5_to20": "mean(V,5)/mean(V,20)",
        "volume_t_to20": "V/mean(V,20)",
        "down_up_volume_ratio10": "mean(V|ret<0,10)/mean(V|ret>0,10)",
        "signed_volume_imbalance5": "sum(sign(ret)*V,5)/sum(V,5)",
        "signed_volume_imbalance10": "sum(sign(ret)*V,10)/sum(V,10)",
        "return_volume_corr10": "corr(ret,log1p(V),10)",
        "log_volume_slope5": "ols_slope(log1p(V),0:4)",
        "intraday_down_volume_share": "sum(30m_V|30m_ret<0)/sum(30m_V)",
        "intraday_realized_vol_to20": "sqrt(sum(30m_ret^2))/median(prior20)",
        "intraday_close_location": "(day_C-day_L)/(day_H-day_L)",
        "last4_30m_return": "last_C/first_O_of_last4-1",
        "weekly_close_to_ma10": "completed_week_C/completed_week_MA10-1",
    }
    return pd.DataFrame(
        [
            {
                "descriptor": descriptor,
                "family": DESCRIPTOR_FAMILIES[descriptor],
                "formula": formulas[descriptor],
                "history_basis": "weekly" if descriptor in WEEKLY_DESCRIPTOR_IDS else "daily",
            }
            for descriptor in DESCRIPTOR_IDS
        ]
    )


def _history_counts(
    frame: pd.DataFrame, descriptor_ids: Sequence[str], window: int
) -> pd.DataFrame:
    result = pd.DataFrame({"dt": pd.to_datetime(frame["dt"])})
    for descriptor in descriptor_ids:
        result[descriptor] = (
            pd.to_numeric(frame[descriptor], errors="coerce")
            .notna()
            .astype(int)
            .shift(1)
            .rolling(window, min_periods=1)
            .sum()
            .fillna(0)
            .astype(int)
        )
    return result


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def run_new_exit_representation(
    raw_dir: Path,
    experiment_dir: Path,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Execute the frozen pre-2026 representation diagnosis exactly once."""
    validate_protocol(protocol)
    experiment_dir = Path(experiment_dir).resolve()
    repository_root = experiment_dir.parent.parent
    artifacts = experiment_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    forbidden_names = {"orders.csv", "candidate_results.csv", "frozen_challenger.json"}
    present_forbidden = forbidden_names & {path.name for path in artifacts.iterdir()}
    if present_forbidden:
        raise ValueError(f"diagnostic archive contains forbidden outputs: {sorted(present_forbidden)}")

    event_source = protocol.get("event_source")
    prior_negative = protocol.get("prior_negative_result")
    research_baseline = protocol.get("research_baseline")
    novelty = protocol.get("novelty_exclusion")
    if not all(
        isinstance(value, Mapping)
        for value in (event_source, prior_negative, research_baseline, novelty)
    ):
        raise ValueError("protocol source identities are incomplete")
    source_manifest = validate_archive_reference(
        repository_root, event_source, label="0825_EX04"
    )
    prior_manifest = validate_archive_reference(
        repository_root, prior_negative, label="0825_EX05"
    )
    baseline_digest = validate_artifact_reference(
        repository_root, research_baseline, label="0824_EX04"
    )
    novelty_digest = validate_novelty_exclusion(repository_root, novelty)

    cutoff = pd.Timestamp(str(protocol["visible_sample_end"]))
    market = load_market_data(
        Path(raw_dir), str(protocol["symbol"]), str(protocol["asset_type"]), cutoff=cutoff
    )
    validate_visible_hashes(market.hashes)
    market_max_dates = (
        pd.to_datetime(market.intraday["dt"]).dt.normalize().max(),
        pd.to_datetime(market.daily["dt"]).max(),
        pd.to_datetime(market.weekly["dt"]).max(),
    )
    if any(maximum > cutoff for maximum in market_max_dates):
        raise ValueError("market frame exceeds visible sample cutoff")

    events = load_source_events(repository_root, event_source)
    validate_event_cohort(events, protocol)
    events = events.copy()
    events["signal_date"] = pd.to_datetime(events["signal_date"])
    if events["signal_date"].max() > cutoff:
        raise ValueError("event cohort exceeds visible sample cutoff")

    daily_raw = compute_daily_descriptors(market.daily)
    intraday_raw = compute_intraday_descriptors(market.intraday)
    daily_intraday_raw = daily_raw.merge(
        intraday_raw, on="dt", how="left", validate="one_to_one"
    )
    daily_intraday_ids = DAILY_DESCRIPTOR_IDS + INTRADAY_DESCRIPTOR_IDS
    daily_bins = assign_causal_quantile_bins(
        daily_intraday_raw,
        protocol,
        descriptor_ids=daily_intraday_ids,
    )
    daily_counts = _history_counts(
        daily_intraday_raw,
        daily_intraday_ids,
        int(protocol["daily_history_window"]),
    )

    weekly_dates = pd.to_datetime(market.weekly["dt"]).reset_index(drop=True)
    weekly_raw = pd.DataFrame(
        {
            "dt": weekly_dates,
            "weekly_close_to_ma10": compute_weekly_descriptor(
                weekly_dates, market.weekly
            ),
        }
    )
    weekly_bins = assign_causal_quantile_bins(
        weekly_raw,
        protocol,
        descriptor_ids=WEEKLY_DESCRIPTOR_IDS,
        history_window=int(protocol["weekly_history_window"]),
        minimum_history=int(protocol["weekly_minimum_history"]),
    )
    weekly_counts = _history_counts(
        weekly_raw, WEEKLY_DESCRIPTOR_IDS, int(protocol["weekly_history_window"])
    )
    weekly_evidence = weekly_raw.rename(
        columns={"dt": "weekly_feature_date"}
    ).merge(
        weekly_bins.rename(columns={"dt": "weekly_feature_date"}),
        on="weekly_feature_date",
        suffixes=("_raw", "_bin"),
        validate="one_to_one",
    ).merge(
        weekly_counts.rename(columns={"dt": "weekly_feature_date"}),
        on="weekly_feature_date",
        validate="one_to_one",
    )
    daily_dates = pd.DataFrame({"dt": pd.to_datetime(daily_intraday_raw["dt"])})
    weekly_aligned = pd.merge_asof(
        daily_dates.sort_values("dt"),
        weekly_evidence.sort_values("weekly_feature_date"),
        left_on="dt",
        right_on="weekly_feature_date",
        direction="backward",
        allow_exact_matches=True,
    )

    raw_lookup = daily_intraday_raw.set_index("dt")
    bin_lookup = daily_bins.set_index("dt")
    count_lookup = daily_counts.set_index("dt")
    weekly_lookup = weekly_aligned.set_index("dt")
    matrix_rows: list[dict[str, object]] = []
    for event in events.to_dict(orient="records"):
        signal_date = pd.Timestamp(event["signal_date"])
        if signal_date not in raw_lookup.index:
            raise ValueError(f"event signal date is missing from market data: {signal_date.date()}")
        for descriptor in DESCRIPTOR_IDS:
            if descriptor in WEEKLY_DESCRIPTOR_IDS:
                raw_value = weekly_lookup.at[signal_date, f"{descriptor}_raw"]
                quantile_bin = weekly_lookup.at[signal_date, f"{descriptor}_bin"]
                history_count = weekly_lookup.at[signal_date, descriptor]
                feature_date = weekly_lookup.at[signal_date, "weekly_feature_date"]
            else:
                raw_value = raw_lookup.at[signal_date, descriptor]
                quantile_bin = bin_lookup.at[signal_date, descriptor]
                history_count = count_lookup.at[signal_date, descriptor]
                feature_date = signal_date
            matrix_rows.append(
                {
                    "event_id": str(event["event_id"]),
                    "window": str(event.get("window", "")),
                    "signal_date": signal_date,
                    "outcome_label": str(event["outcome_label"]),
                    "regime": str(event["regime"]),
                    "block_label": str(event["block_label"]),
                    "descriptor": descriptor,
                    "family": DESCRIPTOR_FAMILIES[descriptor],
                    "raw_value": float(raw_value) if pd.notna(raw_value) else np.nan,
                    "quantile_bin": quantile_bin if pd.notna(quantile_bin) else None,
                    "history_count": int(history_count) if pd.notna(history_count) else 0,
                    "feature_date": pd.Timestamp(feature_date) if pd.notna(feature_date) else pd.NaT,
                    "max_input_dt": pd.Timestamp(feature_date) if pd.notna(feature_date) else pd.NaT,
                }
            )
    matrix = pd.DataFrame(matrix_rows)

    expected_rows = len(events) * len(DESCRIPTOR_IDS)
    per_event_counts = matrix.groupby("event_id")["descriptor"].nunique()
    raw_finite = bool(np.isfinite(matrix["raw_value"].to_numpy(dtype=float)).all())
    bins_present = bool(matrix["quantile_bin"].isin(["Q1", "Q2", "Q3", "Q4", "Q5"]).all())
    causal_dates = bool(
        pd.to_datetime(matrix["max_input_dt"])
        .le(pd.to_datetime(matrix["signal_date"]))
        .all()
    )
    history_ok = bool(
        (
            matrix.loc[matrix["descriptor"].ne("weekly_close_to_ma10"), "history_count"]
            >= int(protocol["daily_minimum_history"])
        ).all()
        and (
            matrix.loc[matrix["descriptor"].eq("weekly_close_to_ma10"), "history_count"]
            >= int(protocol["weekly_minimum_history"])
        ).all()
    )
    evidence_checks = {
        "expected_matrix_rows": len(matrix) == expected_rows,
        "descriptor_identity_per_event": bool(per_event_counts.eq(len(DESCRIPTOR_IDS)).all()),
        "raw_values_finite": raw_finite,
        "quantile_bins_present": bins_present,
        "causal_dates": causal_dates,
        "minimum_history": history_ok,
    }
    evidence_ok = all(evidence_checks.values())

    discovered, confirmed = discover_and_confirm_signatures(events, matrix, protocol)
    permutation = exact_permutation_audit(events, matrix, protocol)
    classification = classify_representation(
        event_count=len(events),
        descriptor_count=len(DESCRIPTOR_IDS),
        discovered_count=len(discovered),
        confirmed=confirmed,
        permutation_audit=permutation,
        evidence_ok=evidence_ok,
        protocol=protocol,
    )
    identity = {
        "status": "PASS" if evidence_ok else "INSUFFICIENT",
        "research_baseline": {
            "experiment_id": research_baseline["experiment_id"],
            "path": research_baseline["path"],
            "sha256": baseline_digest,
        },
        "event_source": {
            "experiment_id": source_manifest.get("experiment_id"),
            "status": source_manifest.get("status"),
            "holdout_accessed": source_manifest.get("holdout_accessed"),
            "file_sha256": dict(event_source["files"]),
        },
        "prior_negative_result": {
            "experiment_id": prior_manifest.get("experiment_id"),
            "classification": prior_negative["classification"],
            "holdout_accessed": prior_negative["holdout_accessed"],
            "file_sha256": dict(prior_negative["files"]),
        },
        "novelty_exclusion_sha256": novelty_digest,
        "descriptor_ids": list(DESCRIPTOR_IDS),
        "evidence_checks": evidence_checks,
        "visible_data_hashes": market.hashes,
        "holdout_accessed": False,
    }
    metrics = {
        "status": "COMPLETE",
        "experiment_id": str(protocol["experiment_id"]),
        "visible_sample_end": str(cutoff.date()),
        "visible_data_hashes": market.hashes,
        "holdout_accessed": False,
        "frozen_challenger": None,
        "event_count": len(events),
        "descriptor_count": len(DESCRIPTOR_IDS),
        "matrix_rows": len(matrix),
        "discovered_signature_count": len(discovered),
        "confirmed_signature_count": len(confirmed),
        "low_contamination_signature_count": int(
            confirmed.get("low_contamination", pd.Series(dtype=bool)).astype(bool).sum()
        ),
        "exact_p_value": float(permutation["exact_p_value"]),
        "representation_classification": classification,
    }

    _write_csv(artifacts / "descriptor_definitions.csv", _descriptor_definitions())
    _write_csv(artifacts / "event_new_representation.csv", matrix)
    _write_csv(artifacts / "discovered_signatures.csv", discovered)
    _write_csv(artifacts / "confirmed_signatures.csv", confirmed)
    _write_json(artifacts / "permutation_audit.json", permutation)
    _write_json(artifacts / "representation_classification.json", classification)
    _write_json(artifacts / "identity_audit.json", identity)
    _write_json(artifacts / "metrics.json", metrics)
    return metrics
