"""Runtime data contract for point-in-time constituent moneyflow breadth."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pandas as pd

from .baselines import ConstituentMoneyflowIntradaySpec
from .identity import raw_file_sha256


_INDEX_BY_TRADE_SYMBOL = {
    "510500.SH": "000905.SH",
}


def support_panel_path(data_dir: Path, release_id: str) -> Path:
    safe = release_id.lower().replace("-", "_")
    return Path(data_dir) / f"{safe}_constituent_moneyflow_panel.csv.gz"


def support_manifest_path(data_dir: Path, release_id: str) -> Path:
    safe = release_id.lower().replace("-", "_")
    return Path(data_dir) / f"{safe}_constituent_moneyflow_manifest.json"


def seed_support_panel(
    repository_root: Path,
    data_dir: Path,
    release_id: str,
    spec: ConstituentMoneyflowIntradaySpec,
) -> Path:
    """Seed an isolated runtime copy from the immutable accepted research panel."""
    source = (Path(repository_root) / spec.source_path).resolve()
    if raw_file_sha256(source) != spec.source_sha256:
        raise ValueError("constituent moneyflow research source hash differs")
    target = support_panel_path(data_dir, release_id)
    manifest = support_manifest_path(data_dir, release_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        target.write_bytes(source.read_bytes())
    frame = pd.read_csv(target, usecols=["dt"])
    if frame.empty:
        raise ValueError("constituent moneyflow runtime panel is empty")
    last_session = pd.to_datetime(frame["dt"]).max().date().isoformat()
    payload = {
        "schema_version": 1,
        "release_id": release_id,
        "source_path": spec.source_path,
        "source_sha256": spec.source_sha256,
        "panel_sha256": raw_file_sha256(target),
        "last_session": last_session,
    }
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return target


def latest_breadth_signal(
    data_dir: Path,
    release_id: str,
    spec: ConstituentMoneyflowIntradaySpec,
    signal_date: pd.Timestamp,
) -> tuple[bool, float, float, float]:
    """Return a causally formed signal for exactly one published market session."""
    target = support_panel_path(data_dir, release_id)
    manifest = support_manifest_path(data_dir, release_id)
    if not target.is_file() or not manifest.is_file():
        raise ValueError("constituent moneyflow runtime support data is not published")
    identity = json.loads(manifest.read_text(encoding="utf-8"))
    if identity.get("release_id") != release_id:
        raise ValueError("constituent moneyflow runtime release identity differs")
    if identity.get("panel_sha256") != raw_file_sha256(target):
        raise ValueError("constituent moneyflow runtime panel hash differs from manifest")
    panel = pd.read_csv(target)
    panel["dt"] = pd.to_datetime(panel["dt"]).dt.normalize()
    panel["weight"] = pd.to_numeric(panel["weight"], errors="raise")
    panel["net_mf_amount"] = pd.to_numeric(panel["net_mf_amount"], errors="coerce")
    observed = panel["observed_moneyflow"].astype(bool)
    panel["observed_weight"] = panel["weight"].where(observed, 0.0)
    panel["positive_weight"] = panel["weight"].where(panel["net_mf_amount"].gt(0), 0.0)
    daily = panel.groupby("dt", sort=True, observed=True).agg(
        total_weight=("weight", "sum"),
        observed_weight=("observed_weight", "sum"),
        positive_weight=("positive_weight", "sum"),
    )
    daily["observed_weight_ratio"] = daily["observed_weight"] / daily["total_weight"]
    daily["moneyflow_breadth"] = daily["positive_weight"] / daily["observed_weight"]
    daily.loc[
        daily["observed_weight_ratio"].lt(spec.minimum_observed_weight_ratio),
        "moneyflow_breadth",
    ] = pd.NA
    daily["threshold"] = (
        daily["moneyflow_breadth"]
        .shift(1)
        .rolling(
            spec.threshold_lookback_sessions,
            min_periods=spec.threshold_lookback_sessions,
        )
        .quantile(spec.threshold_quantile)
    )
    session = pd.Timestamp(signal_date).normalize()
    if session not in daily.index:
        raise ValueError("published constituent moneyflow panel lacks the signal session")
    row = daily.loc[session]
    if pd.isna(row["moneyflow_breadth"]) or pd.isna(row["threshold"]):
        raise ValueError("constituent moneyflow signal lacks valid breadth history")
    breadth = float(row["moneyflow_breadth"])
    threshold = float(row["threshold"])
    coverage = float(row["observed_weight_ratio"])
    return breadth >= threshold, breadth, threshold, coverage


def publish_support_data(
    repository_root: Path,
    data_dir: Path,
    release_id: str,
    spec: ConstituentMoneyflowIntradaySpec,
    through: str,
    *,
    pro=None,
) -> dict[str, object]:
    """Append point-in-time weights and daily moneyflow to the runtime panel."""
    from dataflows.tushare_common import get_tushare_pro

    from .data import load_market_data

    target = seed_support_panel(repository_root, data_dir, release_id, spec)
    panel = pd.read_csv(target)
    panel["dt"] = pd.to_datetime(panel["dt"]).dt.normalize()
    panel["snapshot_date"] = pd.to_datetime(panel["snapshot_date"]).dt.normalize()
    market = load_market_data(Path(data_dir), spec.symbol).daily
    calendar = pd.DatetimeIndex(pd.to_datetime(market["dt"]).dt.normalize())
    calendar = calendar[calendar <= pd.Timestamp(through)]
    if calendar.empty:
        raise ValueError("runtime market calendar is empty")
    last_published = panel["dt"].max()
    missing_sessions = calendar[calendar > last_published]
    if missing_sessions.empty:
        return {
            "release_id": release_id,
            "data_cutoff": calendar.max().date().isoformat(),
            "support_last_session": last_published.date().isoformat(),
            "appended_sessions": 0,
        }
    if missing_sessions[-1] != calendar[-1]:
        raise ValueError("runtime support calendar does not reach the published market cutoff")
    index_code = _INDEX_BY_TRADE_SYMBOL.get(spec.symbol)
    if index_code is None:
        raise ValueError(f"no point-in-time constituent source is registered for {spec.symbol}")
    client = pro or get_tushare_pro(Path(repository_root) / ".env")
    query_start = min(
        panel["snapshot_date"].max(), missing_sessions[0] - pd.Timedelta(days=370)
    )
    weights = client.index_weight(
        index_code=index_code,
        start_date=query_start.strftime("%Y%m%d"),
        end_date=missing_sessions[-1].strftime("%Y%m%d"),
        fields="index_code,con_code,trade_date,weight",
    )
    weights = pd.DataFrame() if weights is None else pd.DataFrame(weights)
    snapshots = panel[["snapshot_date", "con_code", "weight"]].drop_duplicates()
    if not weights.empty:
        required = {"con_code", "trade_date", "weight"}
        if not required.issubset(weights.columns):
            raise ValueError("index_weight response is incomplete")
        incoming = weights[["trade_date", "con_code", "weight"]].rename(
            columns={"trade_date": "snapshot_date"}
        )
        incoming["snapshot_date"] = pd.to_datetime(
            incoming["snapshot_date"].astype(str), format="%Y%m%d"
        ).dt.normalize()
        incoming["weight"] = pd.to_numeric(incoming["weight"], errors="raise")
        snapshots = pd.concat([snapshots, incoming], ignore_index=True).drop_duplicates(
            ["snapshot_date", "con_code"], keep="last"
        )
    snapshots = snapshots.sort_values(["snapshot_date", "con_code"])
    additions: list[pd.DataFrame] = []
    coverage: dict[str, float] = {}
    for session in missing_sessions:
        eligible_dates = snapshots.loc[snapshots["snapshot_date"] <= session, "snapshot_date"]
        if eligible_dates.empty:
            raise ValueError(f"{session.date()}: no point-in-time constituent snapshot")
        snapshot_date = eligible_dates.max()
        members = snapshots.loc[
            snapshots["snapshot_date"].eq(snapshot_date),
            ["con_code", "weight"],
        ].copy()
        if members.empty or members["con_code"].duplicated().any():
            raise ValueError(f"{session.date()}: invalid constituent snapshot")
        flow = client.moneyflow(
            trade_date=session.strftime("%Y%m%d"),
            fields="ts_code,trade_date,net_mf_amount",
        )
        flow = pd.DataFrame() if flow is None else pd.DataFrame(flow)
        if not flow.empty and not {"ts_code", "net_mf_amount"}.issubset(flow.columns):
            raise ValueError(f"{session.date()}: moneyflow response is incomplete")
        selected = members.merge(
            flow[["ts_code", "net_mf_amount"]] if not flow.empty else pd.DataFrame(
                columns=["ts_code", "net_mf_amount"]
            ),
            left_on="con_code",
            right_on="ts_code",
            how="left",
            validate="one_to_one",
        )
        selected["net_mf_amount"] = pd.to_numeric(
            selected["net_mf_amount"], errors="coerce"
        )
        selected["observed_moneyflow"] = selected["net_mf_amount"].notna()
        observed_weight = selected.loc[selected["observed_moneyflow"], "weight"].sum()
        ratio = float(observed_weight / selected["weight"].sum())
        coverage[session.date().isoformat()] = ratio
        if ratio < spec.minimum_observed_weight_ratio:
            raise ValueError(
                f"{session.date()}: observed constituent weight {ratio:.2%} is below gate"
            )
        selected.insert(0, "dt", session.date().isoformat())
        selected.insert(2, "snapshot_date", snapshot_date.date().isoformat())
        additions.append(selected.drop(columns="ts_code"))
    combined = pd.concat([panel, *additions], ignore_index=True)
    combined["dt"] = pd.to_datetime(combined["dt"]).dt.strftime("%Y-%m-%d")
    combined["snapshot_date"] = pd.to_datetime(combined["snapshot_date"]).dt.strftime(
        "%Y-%m-%d"
    )
    temporary = target.with_suffix(target.suffix + ".tmp")
    combined.to_csv(
        temporary,
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    shutil.move(temporary, target)
    manifest = support_manifest_path(data_dir, release_id)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": release_id,
                "source_path": spec.source_path,
                "source_sha256": spec.source_sha256,
                "panel_sha256": raw_file_sha256(target),
                "last_session": missing_sessions[-1].date().isoformat(),
                "information_symbol": index_code,
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "release_id": release_id,
        "data_cutoff": calendar.max().date().isoformat(),
        "support_last_session": missing_sessions[-1].date().isoformat(),
        "appended_sessions": len(missing_sessions),
        "coverage": coverage,
    }
