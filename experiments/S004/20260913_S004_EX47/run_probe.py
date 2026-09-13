from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sys
import time as clock
from zoneinfo import ZoneInfo

import pandas as pd

from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S004_EX47"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_trade_date(value: object) -> date:
    text = str(value).strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"unsupported trade date: {text}")


def _previous_open_session(pro, query_date: date) -> date:
    calendar = pro.trade_cal(
        exchange="SSE",
        start_date=(query_date - timedelta(days=20)).strftime("%Y%m%d"),
        end_date=query_date.strftime("%Y%m%d"),
        fields="exchange,cal_date,is_open",
    )
    if calendar is None or calendar.empty:
        raise ValueError("Tushare returned no SSE calendar rows")
    open_dates = sorted(
        parsed
        for value in calendar.loc[calendar["is_open"].astype(str).eq("1"), "cal_date"]
        if (parsed := _parse_trade_date(value)) < query_date
    )
    if not open_dates:
        raise ValueError("cannot resolve previous SSE open session")
    return open_dates[-1]


def _is_open_session(pro, query_date: date) -> bool:
    calendar = pro.trade_cal(
        exchange="SSE",
        start_date=query_date.strftime("%Y%m%d"),
        end_date=query_date.strftime("%Y%m%d"),
        fields="exchange,cal_date,is_open",
    )
    return bool(
        calendar is not None
        and len(calendar) == 1
        and str(calendar.iloc[0]["is_open"]) == "1"
    )


def main() -> int:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    protocol = _read(experiment / "artifacts/protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    timezone = ZoneInfo(str(protocol["timezone"]))
    started = datetime.now(timezone)
    output_dir = repo / ".tmp/s004-margin-arrival/observations"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{started.strftime('%Y%m%dT%H%M%S%f%z')}.json"
    record: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "endpoint": protocol["endpoint"],
        "symbol": protocol["symbol"],
        "requested_at": started.isoformat(),
        "query_calendar_date": started.date().isoformat(),
        "credential_recorded": False,
    }
    exit_code = 1
    try:
        pro = get_tushare_pro(repo / ".env")
        previous = _previous_open_session(pro, started.date())
        current_is_open = _is_open_session(pro, started.date())
        window_start = time.fromisoformat(str(protocol["observation_window_start"]))
        window_end = time.fromisoformat(str(protocol["observation_window_end"]))
        timing_eligible = current_is_open and window_start <= started.time().replace(tzinfo=None) <= window_end
        before = clock.perf_counter()
        frame = pro.margin_detail(
            ts_code=str(protocol["symbol"]),
            trade_date=previous.strftime("%Y%m%d"),
        )
        latency_ms = (clock.perf_counter() - before) * 1000.0
        received = datetime.now(timezone)
        required = [str(value) for value in protocol["required_fields"]]
        frame = pd.DataFrame() if frame is None else frame.copy()
        missing_columns = sorted(set(required) - set(frame.columns))
        if "trade_date" in frame:
            frame["trade_date"] = frame["trade_date"].astype(str)
        matching = frame.loc[
            frame.get("ts_code", pd.Series(index=frame.index, dtype=str)).astype(str).eq(str(protocol["symbol"]))
            & frame.get("trade_date", pd.Series(index=frame.index, dtype=str)).astype(str).eq(previous.strftime("%Y%m%d"))
        ]
        required_complete = bool(
            not missing_columns
            and len(matching) == 1
            and not matching[required].isna().any(axis=None)
        )
        data_valid = len(matching) == 1 and required_complete
        row_payload = matching.iloc[0].to_dict() if len(matching) == 1 else None
        if row_payload is not None:
            row_payload = {
                key: value.item() if hasattr(value, "item") else value
                for key, value in row_payload.items()
            }
        semantic_status = (
            "PASS_ELIGIBLE" if data_valid and timing_eligible else
            "PASS_PREFLIGHT" if data_valid else
            "FAIL_DATA"
        )
        record.update(
            {
                "received_at": received.isoformat(),
                "latency_ms": latency_ms,
                "current_session_is_open": current_is_open,
                "timing_eligible": timing_eligible,
                "expected_trade_date": previous.isoformat(),
                "returned_rows": int(len(frame)),
                "matching_rows": int(len(matching)),
                "missing_columns": missing_columns,
                "required_fields_complete": required_complete,
                "row_sha256": None if row_payload is None else _canonical_hash(row_payload),
                "semantic_status": semantic_status,
            }
        )
        exit_code = 0 if data_valid else 1
    except Exception as exc:
        record.update(
            {
                "received_at": datetime.now(timezone).isoformat(),
                "timing_eligible": False,
                "semantic_status": "FAIL_CALL",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
        )
    output_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": record["semantic_status"], "artifact": str(output_path), "record": record}, ensure_ascii=False, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
