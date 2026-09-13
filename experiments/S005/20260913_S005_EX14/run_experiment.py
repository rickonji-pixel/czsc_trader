from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256
from czsc_trader.intraday_data import load_intraday_research_data
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S005_EX14"
DAILY_FIELDS = "ts_code,trade_date,exchange,pre_settle,pre_close,open,high,low,close,settle,vol,amount,oi"
CONTRACT_FIELDS = (
    "ts_code,exchange,name,per_unit,opt_code,opt_type,call_put,exercise_type,exercise_price,"
    "s_month,maturity_date,list_date,delist_date,last_edate,last_ddate,quote_unit,min_price_chg"
)


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _fetch_daily(client, trade_date: pd.Timestamp, attempts: int = 3) -> pd.DataFrame:
    stamp = trade_date.strftime("%Y%m%d")
    for attempt in range(attempts):
        try:
            frame = client.opt_daily(exchange="SSE", trade_date=stamp, fields=DAILY_FIELDS)
            if frame is None:
                raise ValueError("Tushare returned None")
            return frame
        except Exception:
            if attempt + 1 == attempts:
                raise
            time.sleep(1.0 + attempt)
    raise AssertionError("unreachable")


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = ("conditional_return_analysis", "event_generation", "parameter_selection")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX14 may only validate option data")
    validate_experiment_archive(repo / "experiments/S005/20260913_S005_EX13")

    source = protocol["source"]
    client = get_tushare_pro(repo / ".env")
    contracts = client.opt_basic(
        exchange=str(source["exchange"]),
        opt_code=str(source["underlying_option_code"]),
        fields=CONTRACT_FIELDS,
    )
    if contracts is None or contracts.empty:
        raise ValueError("Tushare returned no 588080 option contracts")
    if not contracts["opt_code"].eq(source["underlying_option_code"]).all():
        raise ValueError("contract response contains another underlying")
    for column in ("list_date", "delist_date", "maturity_date"):
        contracts[column] = pd.to_datetime(contracts[column].astype(str), format="%Y%m%d").dt.normalize()
    contracts["per_unit"] = pd.to_numeric(contracts["per_unit"], errors="coerce")
    contracts["exercise_price"] = pd.to_numeric(contracts["exercise_price"], errors="coerce")
    contracts = contracts.drop_duplicates("ts_code").sort_values(["list_date", "ts_code"]).reset_index(drop=True)

    start = pd.Timestamp(str(protocol["development_start"]))
    cutoff = pd.Timestamp(str(protocol["development_cutoff"]))
    contracts = contracts.loc[(contracts["list_date"] <= cutoff) & (contracts["delist_date"] >= start)].copy()
    contract_codes = set(contracts["ts_code"].astype(str))
    intraday = load_intraday_research_data(repo / "data/raw", str(protocol["symbol"])).frames["1m"]
    sessions = pd.DatetimeIndex(
        sorted(
            pd.to_datetime(intraday["Date"])
            .dt.normalize()
            .loc[lambda values: values.between(start, cutoff)]
            .drop_duplicates()
        )
    )

    frames: list[pd.DataFrame] = []
    fetch_rows: list[dict[str, object]] = []
    for position, session in enumerate(sessions, start=1):
        daily = _fetch_daily(client, session)
        target = daily.loc[daily["ts_code"].astype(str).isin(contract_codes)].copy()
        frames.append(target)
        fetch_rows.append(
            {
                "trade_date": session,
                "exchange_rows": int(len(daily)),
                "target_rows": int(len(target)),
            }
        )
        if position % 100 == 0 or position == len(sessions):
            print(f"fetched {position}/{len(sessions)} sessions", flush=True)

    quotes = pd.concat(frames, ignore_index=True)
    quotes["trade_date"] = pd.to_datetime(quotes["trade_date"].astype(str), format="%Y%m%d").dt.normalize()
    numeric = ["pre_settle", "pre_close", "open", "high", "low", "close", "settle", "vol", "amount", "oi"]
    quotes[numeric] = quotes[numeric].apply(pd.to_numeric, errors="coerce")
    quotes = quotes.merge(
        contracts[
            [
                "ts_code",
                "per_unit",
                "call_put",
                "exercise_price",
                "s_month",
                "maturity_date",
                "list_date",
                "delist_date",
            ]
        ],
        on="ts_code",
        how="left",
        validate="many_to_one",
    )
    quotes["standard_contract"] = quotes["per_unit"].eq(float(protocol["standard_contract_unit"]))
    quotes["valid_quote"] = quotes[["close", "settle", "vol", "oi"]].notna().all(axis=1) & quotes["close"].ge(0)

    expected_rows = 0
    session_rows: list[dict[str, object]] = []
    for session in sessions:
        active = contracts.loc[(contracts["list_date"] <= session) & (contracts["delist_date"] >= session)]
        expected_rows += len(active)
        day = quotes.loc[quotes["trade_date"].eq(session)]
        standard = day.loc[day["standard_contract"] & day["valid_quote"]]
        pair_counts = standard.groupby(["maturity_date", "exercise_price"])["call_put"].nunique()
        session_rows.append(
            {
                "trade_date": session,
                "expected_active_contracts": int(len(active)),
                "returned_contracts": int(len(day)),
                "valid_standard_contracts": int(len(standard)),
                "paired_strikes": int(pair_counts.eq(2).sum()),
            }
        )
    session_quality = pd.DataFrame(session_rows)
    session_coverage = float(session_quality["returned_contracts"].gt(0).mean())
    valid_standard = quotes.loc[quotes["standard_contract"], "valid_quote"]
    valid_standard_quote_coverage = float(valid_standard.mean())
    paired_session_coverage = float(session_quality["paired_strikes"].gt(0).mean())
    duplicate_rows = int(quotes.duplicated(["trade_date", "ts_code"], keep=False).sum())
    unexpected_rows = int(
        (~quotes["trade_date"].between(quotes["list_date"], quotes["delist_date"], inclusive="both")).sum()
    )
    gates = protocol["quality_gates"]
    checks = {
        "session_coverage": session_coverage >= float(gates["minimum_session_coverage"]),
        "valid_standard_quote_coverage": valid_standard_quote_coverage
        >= float(gates["minimum_valid_standard_quote_coverage"]),
        "paired_session_coverage": paired_session_coverage >= float(gates["minimum_paired_session_coverage"]),
        "unique_contract_dates": duplicate_rows == 0,
        "contract_lifecycle": unexpected_rows == 0,
    }
    decision = "PROCEED_TO_OPTION_MECHANISM_PREREGISTRATION" if all(checks.values()) else "STOP_OPTION_DATA_ROUTE"
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "sessions": int(len(sessions)),
        "contracts": int(len(contracts)),
        "standard_contracts": int(contracts["per_unit"].eq(float(protocol["standard_contract_unit"])).sum()),
        "quote_rows": int(len(quotes)),
        "expected_active_contract_rows": int(expected_rows),
        "session_coverage": session_coverage,
        "valid_standard_quote_coverage": valid_standard_quote_coverage,
        "paired_session_coverage": paired_session_coverage,
        "duplicate_contract_date_rows": duplicate_rows,
        "unexpected_lifecycle_rows": unexpected_rows,
        "checks": checks,
        "documented_daily_update": source["documented_daily_update"],
        "production_arrival_status": "UNVERIFIED",
        "decision": decision,
        "reads_588080_future_returns": False,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": raw_file_sha256(protocol_path),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    contract_out = contracts.copy()
    quote_out = quotes.copy()
    for frame, columns in (
        (contract_out, ("list_date", "delist_date", "maturity_date")),
        (quote_out, ("trade_date", "list_date", "delist_date", "maturity_date")),
    ):
        for column in columns:
            frame[column] = pd.to_datetime(frame[column]).dt.strftime("%Y-%m-%d")
    contract_out.to_csv(artifacts / "contracts.csv.gz", index=False, compression=compression, lineterminator="\n")
    quote_out.to_csv(artifacts / "option_daily.csv.gz", index=False, compression=compression, lineterminator="\n")
    session_quality.to_csv(artifacts / "session_quality.csv", index=False, lineterminator="\n")
    pd.DataFrame(fetch_rows).to_csv(artifacts / "fetch_ledger.csv", index=False, lineterminator="\n")
    (artifacts / "data_quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (experiment / "03_execution.md").write_text(
        f"# S005 EX14 执行\n\n状态：COMPLETE。覆盖{len(sessions)}个交易日、{len(contracts)}个历史合约、"
        f"{len(quotes)}条期权日线；数据门裁决：`{decision}`。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX14 结论\n\n"
        f"历史交易日覆盖{session_coverage:.2%}，标准合约有效报价覆盖{valid_standard_quote_coverage:.2%}，"
        f"认购认沽可配对交易日覆盖{paired_session_coverage:.2%}。裁决：`{decision}`。\n\n"
        "该数据只获得进入期权机制预注册的资格；Tushare实际盘后到达时间尚未完成前瞻验证，"
        "本轮没有读取588080后续收益、生成候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
