"""Runtime support for frozen causal-feature-gate strategies."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from .baselines import CausalFeatureGateSpec
from .data import load_market_data
from .experiment_archive import resolve_repository_experiment_reference
from .identity import raw_file_sha256


SUPPORTED_FEATURES = {
    "micro_share_change_5d_lag1",
    "tsfresh__log_volume_change__mean__lb20",
    "price_close_vwap_deviation",
    "risk_global_spx_return",
    "risk_chinext_turnover_z20",
    "risk_shibor_on_change_5d",
    "price_intraday_range",
}


def _safe_release_id(release_id: str) -> str:
    return release_id.lower().replace("-", "_")


def support_panel_path(data_dir: Path, release_id: str) -> Path:
    return Path(data_dir) / f"{_safe_release_id(release_id)}_causal_feature_panel.csv.gz"


def support_manifest_path(data_dir: Path, release_id: str) -> Path:
    return Path(data_dir) / f"{_safe_release_id(release_id)}_causal_feature_manifest.json"


def causal_percentile(values: pd.Series, window: int, minimum: int) -> pd.Series:
    """Return a centered percentile using only the current and preceding rows."""

    def rank_last(items: np.ndarray) -> float:
        current = items[-1]
        valid = items[np.isfinite(items)]
        if not np.isfinite(current) or len(valid) < minimum:
            return np.nan
        below = np.count_nonzero(valid < current)
        equal = np.count_nonzero(valid == current)
        return float((below + 0.5 * equal) / len(valid) - 0.5)

    return values.astype(float).rolling(window, min_periods=minimum).apply(
        rank_last, raw=True
    )


def score_feature_panel(
    panel: pd.DataFrame,
    spec: CausalFeatureGateSpec,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Apply the frozen normalization, score and hysteresis state machine."""
    orientations = dict(spec.orientations)
    missing = sorted(set(orientations).difference(panel.columns))
    if missing:
        raise ValueError(f"causal-feature runtime panel is missing features: {missing}")
    scores = pd.DataFrame(index=panel.index)
    for feature, orientation in orientations.items():
        scores[feature] = causal_percentile(
            panel[feature],
            spec.normalization_lookback_sessions,
            spec.normalization_minimum_observations,
        ) * orientation
    base = scores.mul(pd.Series(dict(spec.base_weights)), axis=1).sum(
        axis=1, min_count=len(spec.base_weights)
    )
    confirmation = scores.mul(pd.Series(dict(spec.confirmation_weights)), axis=1).sum(
        axis=1, min_count=len(spec.confirmation_weights)
    )
    current = 0
    targets = np.zeros(len(panel), dtype=np.int8)
    for position, (base_value, confirmation_value) in enumerate(
        zip(base.to_numpy(dtype=float), confirmation.to_numpy(dtype=float), strict=True)
    ):
        if np.isfinite(base_value):
            if (
                current == 0
                and base_value >= spec.entry_threshold
                and confirmation_value >= spec.confirmation_threshold
            ):
                current = 1
            elif current == 1 and base_value <= spec.exit_threshold:
                current = 0
        targets[position] = current
    target = pd.Series(targets, index=panel.index, name="decision_target")
    return base.rename("base_score"), confirmation.rename("confirmation_score"), target


def _read_panel(path: Path) -> pd.DataFrame:
    panel = pd.read_csv(path)
    if "date" not in panel:
        raise ValueError("causal-feature runtime panel has no date column")
    panel["date"] = pd.to_datetime(panel["date"], errors="raise").dt.normalize()
    if panel["date"].duplicated().any():
        raise ValueError("causal-feature runtime panel contains duplicate sessions")
    return panel.set_index("date").sort_index()


def _write_panel(path: Path, panel: pd.DataFrame) -> None:
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        panel.reset_index().to_csv(
            temporary,
            index=False,
            encoding="utf-8-sig",
            compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
            lineterminator="\n",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest_payload(
    target: Path,
    release_id: str,
    spec: CausalFeatureGateSpec,
    last_session: pd.Timestamp,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "release_id": release_id,
        "source_path": spec.source_path,
        "source_sha256": spec.source_sha256,
        "panel_sha256": raw_file_sha256(target),
        "last_session": last_session.date().isoformat(),
        "features": sorted(dict(spec.orientations)),
    }


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def seed_support_panel(
    repository_root: Path,
    data_dir: Path,
    release_id: str,
    spec: CausalFeatureGateSpec,
) -> Path:
    """Seed a release-owned runtime panel from immutable accepted research evidence."""
    features = set(dict(spec.orientations))
    unsupported = sorted(features.difference(SUPPORTED_FEATURES))
    if unsupported:
        raise ValueError(f"causal-feature live publication is unsupported: {unsupported}")
    source = resolve_repository_experiment_reference(repository_root, spec.source_path)
    if raw_file_sha256(source) != spec.source_sha256:
        raise ValueError("causal-feature research source hash differs")
    target = support_panel_path(data_dir, release_id)
    manifest_path = support_manifest_path(data_dir, release_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() or manifest_path.is_file():
        if not target.is_file() or not manifest_path.is_file():
            raise ValueError("causal-feature runtime support publication is incomplete")
        identity = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {
            "release_id": release_id,
            "source_path": spec.source_path,
            "source_sha256": spec.source_sha256,
            "features": sorted(features),
            "panel_sha256": raw_file_sha256(target),
        }
        if any(identity.get(key) != value for key, value in expected.items()):
            raise ValueError("causal-feature runtime support identity differs")
        return target
    source_panel = _read_panel(source)
    missing = sorted(features.difference(source_panel.columns))
    if missing:
        raise ValueError(f"causal-feature research source is missing features: {missing}")
    seeded = source_panel.loc[:, sorted(features)]
    if seeded.empty:
        raise ValueError("causal-feature research source is empty")
    _write_panel(target, seeded)
    _write_manifest(
        manifest_path,
        _manifest_payload(target, release_id, spec, seeded.index[-1]),
    )
    return target


def _as_frame(value: object, required: set[str], endpoint: str) -> pd.DataFrame:
    frame = pd.DataFrame() if value is None else pd.DataFrame(value)
    missing = sorted(required.difference(frame.columns))
    if frame.empty or missing:
        detail = "no rows" if frame.empty else f"missing fields {missing}"
        raise ValueError(f"{endpoint} response is incomplete: {detail}")
    return frame


def _materialize_features(
    market_daily: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    *,
    pro: object,
    symbol: str,
) -> pd.DataFrame:
    query_start = (sessions[0] - pd.Timedelta(days=90)).strftime("%Y%m%d")
    query_end = sessions[-1].strftime("%Y%m%d")

    daily = market_daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"], errors="raise").dt.normalize()
    daily = daily.set_index("dt").sort_index().reindex(sessions)
    if daily[["high", "low", "close", "vol", "amount"]].isna().any().any():
        raise ValueError("runtime market data does not cover the feature calendar")
    close = pd.to_numeric(daily["close"], errors="raise")
    volume = pd.to_numeric(daily["vol"], errors="raise")
    amount = pd.to_numeric(daily["amount"], errors="raise")
    output = pd.DataFrame(index=sessions)
    output["price_close_vwap_deviation"] = close / (amount / volume) - 1.0
    output["price_intraday_range"] = (
        pd.to_numeric(daily["high"], errors="raise")
        - pd.to_numeric(daily["low"], errors="raise")
    ) / close
    output["tsfresh__log_volume_change__mean__lb20"] = (
        np.log(volume).diff().rolling(20, min_periods=20).mean()
    )

    shibor = _as_frame(
        pro.shibor(start_date=query_start, end_date=query_end),
        {"date", "on"},
        "shibor",
    )
    shibor["date"] = pd.to_datetime(
        shibor["date"].astype(str), format="%Y%m%d", errors="raise"
    ).dt.normalize()
    on = (
        shibor.drop_duplicates("date", keep="last")
        .set_index("date")["on"]
        .pipe(pd.to_numeric, errors="raise")
        .sort_index()
        .reindex(sessions)
        .ffill(limit=4)
    )
    output["risk_shibor_on_change_5d"] = on.diff(5)

    chinext = _as_frame(
        pro.index_dailybasic(
            ts_code="399006.SZ",
            start_date=query_start,
            end_date=query_end,
            fields="ts_code,trade_date,turnover_rate_f",
        ),
        {"trade_date", "turnover_rate_f"},
        "index_dailybasic",
    )
    chinext["trade_date"] = pd.to_datetime(
        chinext["trade_date"].astype(str), format="%Y%m%d", errors="raise"
    ).dt.normalize()
    turnover = (
        chinext.drop_duplicates("trade_date", keep="last")
        .set_index("trade_date")["turnover_rate_f"]
        .pipe(pd.to_numeric, errors="raise")
        .sort_index()
        .reindex(sessions)
    )
    output["risk_chinext_turnover_z20"] = (
        turnover - turnover.rolling(20).mean()
    ) / turnover.rolling(20).std(ddof=0)

    shares = _as_frame(
        pro.etf_share_size(
            ts_code=symbol,
            start_date=query_start,
            end_date=query_end,
            fields="trade_date,ts_code,total_share",
        ),
        {"trade_date", "total_share"},
        "etf_share_size",
    )
    shares["trade_date"] = pd.to_datetime(
        shares["trade_date"].astype(str), format="%Y%m%d", errors="raise"
    ).dt.normalize()
    total_share = (
        shares.drop_duplicates("trade_date", keep="last")
        .set_index("trade_date")["total_share"]
        .pipe(pd.to_numeric, errors="raise")
        .sort_index()
        .reindex(sessions)
    )
    output["micro_share_change_5d_lag1"] = total_share.pct_change(
        5, fill_method=None
    ).shift(1)

    spx = _as_frame(
        pro.index_global(
            ts_code="SPX",
            start_date=query_start,
            end_date=query_end,
        ),
        {"trade_date", "pct_chg"},
        "index_global",
    )
    spx["trade_date"] = pd.to_datetime(
        spx["trade_date"].astype(str), format="%Y%m%d", errors="raise"
    ).dt.normalize()
    spx["pct_chg"] = pd.to_numeric(spx["pct_chg"], errors="raise") / 100.0
    mapped = pd.merge_asof(
        pd.DataFrame({"date": sessions}),
        spx[["trade_date", "pct_chg"]]
        .drop_duplicates("trade_date", keep="last")
        .sort_values("trade_date"),
        left_on="date",
        right_on="trade_date",
        direction="backward",
        allow_exact_matches=False,
    ).set_index("date")
    output["risk_global_spx_return"] = mapped["pct_chg"]
    return output


def publish_support_data(
    repository_root: Path,
    data_dir: Path,
    release_id: str,
    spec: CausalFeatureGateSpec,
    through: str,
    *,
    pro: object | None = None,
) -> dict[str, object]:
    """Append all causally available features through one published market session."""
    from dataflows.tushare_common import get_tushare_pro

    target = seed_support_panel(repository_root, data_dir, release_id, spec)
    panel = _read_panel(target)
    market = load_market_data(Path(data_dir), spec.symbol).daily
    calendar = pd.DatetimeIndex(pd.to_datetime(market["dt"]).dt.normalize())
    calendar = calendar[calendar <= pd.Timestamp(through)]
    if calendar.empty:
        raise ValueError("causal-feature runtime market calendar is empty")
    missing = calendar[calendar > panel.index[-1]]
    if missing.empty:
        return {
            "release_id": release_id,
            "data_cutoff": calendar[-1].date().isoformat(),
            "support_last_session": panel.index[-1].date().isoformat(),
            "appended_sessions": 0,
        }
    history_start = max(0, int(calendar.get_indexer([missing[0]])[0]) - 30)
    feature_sessions = calendar[history_start:]
    materialized = _materialize_features(
        market,
        feature_sessions,
        pro=pro or get_tushare_pro(Path(repository_root) / ".env"),
        symbol=spec.symbol,
    )
    required = sorted(dict(spec.orientations))
    additions = materialized.reindex(missing).loc[:, required]
    if additions.isna().any().any() or not np.isfinite(additions.to_numpy(dtype=float)).all():
        invalid = sorted(additions.columns[additions.isna().any()].tolist())
        raise ValueError(f"causal-feature publication has unavailable values: {invalid}")
    combined = pd.concat([panel, additions])
    if combined.index.duplicated().any() or not combined.index.is_monotonic_increasing:
        raise ValueError("causal-feature runtime panel append is not monotonic")
    combined.index.name = "date"
    _write_panel(target, combined)
    _write_manifest(
        support_manifest_path(data_dir, release_id),
        _manifest_payload(target, release_id, spec, combined.index[-1]),
    )
    return {
        "release_id": release_id,
        "data_cutoff": calendar[-1].date().isoformat(),
        "support_last_session": combined.index[-1].date().isoformat(),
        "appended_sessions": len(additions),
    }


def latest_signal(
    data_dir: Path,
    release_id: str,
    spec: CausalFeatureGateSpec,
    signal_date: pd.Timestamp,
) -> tuple[int, float, float, dict[str, float]]:
    """Return one exact-session target and its auditable feature values."""
    target = support_panel_path(data_dir, release_id)
    manifest_path = support_manifest_path(data_dir, release_id)
    if not target.is_file() or not manifest_path.is_file():
        raise ValueError("causal-feature runtime support data is not published")
    identity = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {
        "release_id": release_id,
        "source_path": spec.source_path,
        "source_sha256": spec.source_sha256,
        "features": sorted(dict(spec.orientations)),
        "panel_sha256": raw_file_sha256(target),
    }
    if any(identity.get(key) != value for key, value in expected.items()):
        raise ValueError("causal-feature runtime support identity differs")
    panel = _read_panel(target)
    session = pd.Timestamp(signal_date).normalize()
    if session not in panel.index:
        raise ValueError("causal-feature runtime panel lacks the signal session")
    base, confirmation, positions = score_feature_panel(panel, spec)
    if pd.isna(base.loc[session]) or pd.isna(confirmation.loc[session]):
        raise ValueError("causal-feature signal lacks valid score history")
    evidence = {
        name: float(panel.loc[session, name]) for name in sorted(dict(spec.orientations))
    }
    return (
        int(positions.loc[session]),
        float(base.loc[session]),
        float(confirmation.loc[session]),
        evidence,
    )
