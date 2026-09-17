from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)


EXPERIMENT_ID = "20260918_S007_EX38"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _behavior_hash(decisions: pd.DataFrame, sessions: pd.DatetimeIndex) -> str:
    target = pd.Series(0, index=sessions, dtype=np.int8)
    valid = decisions.dropna(subset=["valid_session"]).copy()
    valid["valid_session"] = pd.to_datetime(valid["valid_session"]).dt.normalize()
    target.loc[pd.DatetimeIndex(valid["valid_session"])] = valid[
        "target_position"
    ].to_numpy(dtype=np.int8)
    return hashlib.sha256(target.to_numpy(dtype=np.int8).tobytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    protocol = _read(experiment / "02_protocol.json")
    sources = protocol["sources"]
    for key, hash_key in (
        ("formal_replay_archive", "formal_replay_manifest_sha256"),
        ("parameter_audit_archive", "parameter_audit_manifest_sha256"),
        ("cost_audit_archive", "cost_audit_manifest_sha256"),
        ("freeze_archive", "freeze_manifest_sha256"),
    ):
        archive = repo / str(sources[key])
        validate_experiment_archive(archive)
        if _sha256(archive / "experiment_manifest.json") != str(sources[hash_key]):
            raise ValueError(f"source archive differs: {archive}")

    output = repo / str(sources["corrected_backtest"])
    current = _read(output / "metrics.json")["strategy"]["metrics"]
    original = _read(
        repo / str(sources["formal_replay_archive"]) / "artifacts/tdr_replay_summary.json"
    )
    if current != original["metrics"]:
        raise ValueError("corrected replay metrics differ from EX31")
    audit = _read(output / "audit.json")
    if audit["status"] != "PASS" or audit["reason_codes"]:
        raise ValueError("corrected replay did not pass SE audit")

    decisions = pd.read_csv(output / "decisions.csv")
    orders = pd.read_csv(output / "orders.csv")
    fills = pd.read_csv(output / "fills.csv")
    sessions = []
    for year in range(2021, 2027):
        daily = pd.read_csv(repo / f"data/backtest/588080_daily_{year}.csv")
        sessions.extend(pd.to_datetime(daily["date"]).dt.normalize().tolist())
    calendar = pd.DatetimeIndex(sorted(set(sessions)))
    calendar = calendar[
        (calendar >= pd.Timestamp("2021-01-04"))
        & (calendar <= pd.Timestamp(protocol["development_cutoff"]))
    ]
    behavior_hash = _behavior_hash(decisions, calendar)
    if behavior_hash != protocol["expected_behavior_hash"]:
        raise ValueError("corrected replay behavior differs from frozen evidence")

    buy_limit = orders.query(
        "side == 'BUY' and order_type == 'LIMIT' and status == 'FILLED'"
    )
    sell_market = orders.query(
        "side == 'SELL' and order_type == 'MARKET' and status == 'FILLED'"
    )
    sell_open_market = fills.query("side == 'SELL' and trigger == 'OPEN_MARKET'")
    expected = int(protocol["expected_closed_trades"])
    if not (
        len(orders) == len(fills) == expected * 2
        and len(buy_limit) == len(sell_market) == len(sell_open_market) == expected
    ):
        raise ValueError("corrected replay order semantics differ")

    artifacts = experiment / "artifacts"
    artifacts.mkdir(exist_ok=True)
    summary = {
        "schema_version": 1,
        "status": "PASS",
        "behavior_hash": behavior_hash,
        "behavior_diff_sessions": 0,
        "metrics": current,
        "orders": len(orders),
        "fills": len(fills),
        "buy_limit_filled": len(buy_limit),
        "sell_market_filled": len(sell_market),
        "sell_open_market_fills": len(sell_open_market),
        "se_audit": audit,
        "output_hashes": {
            name: _sha256(output / name)
            for name in (
                "audit.json",
                "manifest.json",
                "metrics.json",
                "orders.csv",
                "fills.csv",
                "trades.csv",
            )
        },
        "source_archives_validated": [
            str(sources[key])
            for key in (
                "formal_replay_archive",
                "parameter_audit_archive",
                "cost_audit_archive",
                "freeze_archive",
            )
        ],
    }
    (artifacts / "execution_contract_audit.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "strategy_version": protocol["strategy_version"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": "EXECUTION_CONTRACT_CONFIRMED",
            "strategy_frozen": True,
            "strategy_parameters_changed": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
