from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.data import load_market_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260911_S003_EX25"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _dates(frame: pd.DataFrame, column: str = "trade_date") -> pd.Series:
    return pd.to_datetime(frame[column].astype(str), format="%Y%m%d").dt.normalize()


def _missing(expected: pd.DatetimeIndex, observed: pd.Series) -> list[str]:
    return [value.strftime("%Y-%m-%d") for value in expected.difference(pd.DatetimeIndex(observed))]


def _duplicate_rows(frame: pd.DataFrame, columns: list[str]) -> int:
    return int(frame.duplicated(columns, keep=False).sum())


def _fetch_selected_contract_rows(pro, mapping: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for ts_code in sorted(mapping["mapping_ts_code"].dropna().astype(str).unique()):
        frame = pro.fut_daily(
            ts_code=ts_code,
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            fields="ts_code,trade_date,open,high,low,close,settle,vol,amount,oi",
        )
        if frame is not None and not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "conditional_return_analysis",
        "signal_generation",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX25 may only validate IC basis data feasibility")

    dataset = protocol["dataset"]
    calendar_manifest = repo_root / dataset["calendar_manifest"]
    if _sha256(calendar_manifest) != dataset["calendar_manifest_sha256"]:
        raise ValueError("510500 calendar manifest differs from frozen evidence")
    daily = load_market_data(repo_root / "data" / "raw", "510500.SH").daily
    calendar = pd.DatetimeIndex(pd.to_datetime(daily["dt"]).dt.normalize())
    calendar = calendar[(calendar >= dataset["start"]) & (calendar <= dataset["development_cutoff"])]
    if calendar.has_duplicates or not calendar.is_monotonic_increasing:
        raise ValueError("510500 master calendar must be unique and ascending")

    target = protocol["research_target"]
    start_api = dataset["start"].replace("-", "")
    end_api = dataset["development_cutoff"].replace("-", "")
    pro = get_tushare_pro(repo_root / ".env")
    spot = pro.index_daily(
        ts_code=target["spot_index"],
        start_date=start_api,
        end_date=end_api,
        fields="ts_code,trade_date,open,high,low,close,vol,amount",
    )
    mapping = pro.fut_mapping(
        ts_code=target["futures_continuous_code"],
        start_date=start_api,
        end_date=end_api,
    )
    basic = pro.fut_basic(
        exchange=target["futures_exchange"],
        fut_type="1",
        fut_code="IC",
        fields="ts_code,symbol,exchange,name,fut_code,list_date,delist_date",
    )
    for label, frame in {"spot": spot, "mapping": mapping, "basic": basic}.items():
        if frame is None or frame.empty:
            raise ValueError(f"Tushare returned no {label} data")
    contract_daily = _fetch_selected_contract_rows(
        pro, mapping, dataset["start"], dataset["development_cutoff"]
    )
    if contract_daily.empty:
        raise ValueError("Tushare returned no mapped IC contract daily data")

    spot = spot.copy()
    mapping = mapping.copy()
    basic = basic.copy()
    contract_daily = contract_daily.copy()
    spot["dt"] = _dates(spot)
    mapping["dt"] = _dates(mapping)
    contract_daily["dt"] = _dates(contract_daily)
    for column in ("list_date", "delist_date"):
        basic[column] = pd.to_datetime(basic[column].astype(str), format="%Y%m%d", errors="coerce")

    spot = spot[(spot["dt"] >= calendar.min()) & (spot["dt"] <= calendar.max())]
    mapping = mapping[(mapping["dt"] >= calendar.min()) & (mapping["dt"] <= calendar.max())]
    selected = mapping.merge(
        contract_daily,
        left_on=["dt", "mapping_ts_code"],
        right_on=["dt", "ts_code"],
        how="left",
        validate="one_to_one",
        suffixes=("_mapping", "_future"),
    )
    panel = pd.DataFrame({"dt": calendar}).merge(
        spot[["dt", "close"]].rename(columns={"close": "spot_close"}),
        on="dt",
        how="left",
        validate="one_to_one",
    )
    panel = panel.merge(
        selected[["dt", "mapping_ts_code", "close", "settle", "vol", "oi"]].rename(
            columns={
                "close": "futures_close",
                "settle": "futures_settle",
                "vol": "futures_volume",
                "oi": "futures_open_interest",
            }
        ),
        on="dt",
        how="left",
        validate="one_to_one",
    )
    panel = panel.merge(
        basic[["ts_code", "list_date", "delist_date"]].rename(
            columns={"ts_code": "mapping_ts_code"}
        ),
        on="mapping_ts_code",
        how="left",
        validate="many_to_one",
    )
    panel["days_to_expiry"] = (panel["delist_date"] - panel["dt"]).dt.days
    panel["raw_basis"] = panel["futures_close"] / panel["spot_close"] - 1.0
    panel["annualized_basis"] = np.where(
        panel["days_to_expiry"] > 0,
        panel["raw_basis"] * 365.0 / panel["days_to_expiry"],
        np.nan,
    )
    panel["roll_day"] = panel["mapping_ts_code"].ne(panel["mapping_ts_code"].shift(1))
    if not panel.empty:
        panel.loc[panel.index[0], "roll_day"] = False
    panel["available_for_strategy_from"] = panel["dt"].shift(-1)

    price_columns = ["spot_close", "futures_close", "futures_settle"]
    finite_positive = bool(
        np.isfinite(panel[price_columns].to_numpy(dtype=float)).all()
        and (panel[price_columns] > 0).all().all()
    )
    nonnegative_activity = bool(
        (panel[["futures_volume", "futures_open_interest"]] >= 0).all().all()
    )
    within_life = bool(
        ((panel["dt"] >= panel["list_date"]) & (panel["dt"] <= panel["delist_date"])).all()
    )
    missing_spot = _missing(calendar, spot["dt"])
    missing_mapping = _missing(calendar, mapping["dt"])
    missing_contract = panel.loc[panel["futures_close"].isna(), "dt"].dt.strftime("%Y-%m-%d").tolist()
    duplicate_counts = {
        "spot_date_rows": _duplicate_rows(spot, ["dt"]),
        "mapping_date_rows": _duplicate_rows(mapping, ["dt"]),
        "contract_date_code_rows": _duplicate_rows(contract_daily, ["dt", "ts_code"]),
        "basic_code_rows": _duplicate_rows(basic, ["ts_code"]),
    }
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "master_sessions": int(len(calendar)),
        "first_session": calendar.min().strftime("%Y-%m-%d"),
        "last_session": calendar.max().strftime("%Y-%m-%d"),
        "spot_rows": int(len(spot)),
        "mapping_rows": int(len(mapping)),
        "mapped_contract_count": int(mapping["mapping_ts_code"].nunique()),
        "roll_day_count": int(panel["roll_day"].sum()),
        "expiry_day_rows": int((panel["days_to_expiry"] == 0).sum()),
        "missing_spot_dates": missing_spot,
        "missing_mapping_dates": missing_mapping,
        "missing_contract_dates": missing_contract,
        "duplicate_rows": duplicate_counts,
        "finite_positive_prices": finite_positive,
        "nonnegative_activity": nonnegative_activity,
        "contract_metadata_complete": bool(panel[["list_date", "delist_date"]].notna().all().all()),
        "trade_date_within_contract_life": within_life,
    }
    quality["passed"] = bool(
        not missing_spot
        and not missing_mapping
        and not missing_contract
        and not any(duplicate_counts.values())
        and finite_positive
        and nonnegative_activity
        and quality["contract_metadata_complete"]
        and within_life
    )

    mapping_out = mapping[["dt", "ts_code", "mapping_ts_code"]].sort_values("dt")
    for frame in (mapping_out, panel):
        for column in ("dt", "list_date", "delist_date", "available_for_strategy_from"):
            if column in frame:
                frame[column] = pd.to_datetime(frame[column]).dt.strftime("%Y-%m-%d")
    mapping_out.to_csv(artifacts / "ic_main_mapping.csv", index=False, lineterminator="\n")
    panel.to_csv(
        artifacts / "ic_basis_panel.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    _write_json(artifacts / "data_quality.json", quality)

    status = "PASS" if quality["passed"] else "FAIL"
    (experiment / "03_execution.md").write_text(
        "# S003 EX25 执行\n\n"
        f"数据门结果：`{status}`。510500主日历共{quality['master_sessions']}个交易日，"
        f"IC主力映射涉及{quality['mapped_contract_count']}个月合约和{quality['roll_day_count']}次切换。"
        f"现货缺失{len(missing_spot)}日，映射缺失{len(missing_mapping)}日，映射合约行情缺失"
        f"{len(missing_contract)}日。\n\n"
        "本轮只生成基差数据证据，没有读取510500未来收益、生成信号或选择阈值。\n",
        encoding="utf-8",
    )
    conclusion = (
        "IC基差数据满足下一轮机制定义所需的因果性和连续性要求。"
        if quality["passed"]
        else "IC基差数据未满足冻结的数据质量门，当前不得进入收益研究。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX25 结论\n\n"
        f"结论：`{status}`。{conclusion}该结论不构成策略有效性证据，也没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "master_sessions": quality["master_sessions"],
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
