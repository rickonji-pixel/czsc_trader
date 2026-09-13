from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import (
    BacktestRequestV2,
    build_closing_dislocation_signals,
    load_replay_data,
    resolve_candidate_snapshot,
    run_backtest_v2,
)
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260913_S004_EX42"


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    payload = _read_json(experiment / "candidate_payload.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("formal replay cannot mutate lifecycle state")
    definition = protocol["candidate_definition"]
    expected_hashes = {
        repo / str(definition["manifest"]): str(definition["manifest_sha256"]),
        repo / str(definition["spec"]): str(definition["spec_sha256"]),
        repo / str(definition["episodes"]): str(definition["episodes_sha256"]),
    }
    for path, digest in expected_hashes.items():
        if _sha256(path) != digest:
            raise ValueError(f"candidate evidence hash differs: {path}")
    validate_experiment_archive((repo / str(definition["manifest"])).parent)

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    context = RepositoryContext.discover(repo)
    payload_hash = canonical_json_sha256(payload)
    snapshot = resolve_candidate_snapshot(
        context,
        str(target["candidate_id"]),
        payload,
        payload_hash,
        str(experiment.relative_to(repo) / "candidate_payload.json"),
    )
    data = load_replay_data(
        context,
        str(dataset["name"]),
        str(target["symbol"]),
        "etf",
        pd.Timestamp(str(dataset["cutoff"])).date(),
        include_one_minute=True,
    )
    signals = build_closing_dislocation_signals(
        snapshot,
        data,
        pd.Timestamp(str(dataset["evaluation_start"])),
        pd.Timestamp(str(dataset["evaluation_end"])),
        repo,
    )
    result = replay_account(signals, data, 100_000.0)
    expected = pd.read_csv(
        repo / str(definition["episodes"]),
        parse_dates=["event_date", "entry_date", "exit_date"],
    ).sort_values("event_date").reset_index(drop=True)
    actual = result.trades.loc[result.trades["status"].eq("CLOSED")].copy()
    entry_orders = result.orders.loc[
        result.orders["side"].eq("BUY") & result.orders["status"].eq("FILLED")
    ].drop_duplicates("cycle_id")
    event_by_cycle = entry_orders.set_index("cycle_id")["signal_date"]
    actual.insert(0, "event_date", actual["cycle_id"].map(event_by_cycle))
    actual = actual.sort_values("event_date").reset_index(drop=True)
    if len(actual) != len(expected):
        raise AssertionError(f"trade count differs: formal={len(actual)}, research={len(expected)}")
    for column in ("event_date", "entry_date", "exit_date"):
        pd.testing.assert_series_equal(
            pd.to_datetime(actual[column]).dt.normalize(),
            pd.to_datetime(expected[column]).dt.normalize(),
            check_names=False,
        )
    raw_open = data.execution_daily.copy()
    raw_open["dt"] = pd.to_datetime(raw_open["dt"], errors="raise").dt.normalize()
    raw_open = raw_open.set_index("dt")["open"].astype(float)
    pd.testing.assert_series_equal(
        actual["entry_price"].astype(float),
        pd.Series(raw_open.reindex(expected["entry_date"]).to_numpy()),
        check_names=False,
        atol=1e-12,
        rtol=1e-12,
    )
    pd.testing.assert_series_equal(
        actual["exit_price"].astype(float),
        pd.Series(raw_open.reindex(expected["exit_date"]).to_numpy()),
        check_names=False,
        atol=1e-12,
        rtol=1e-12,
    )
    pd.testing.assert_series_equal(
        actual["net_return"].astype(float),
        expected["baseline_return"].astype(float),
        check_names=False,
        atol=1e-12,
        rtol=1e-12,
    )

    output_root = repo / ".tmp" / "s004-ex42-backtest"
    if output_root.exists():
        shutil.rmtree(output_root)
    summary = run_backtest_v2(
        snapshot=snapshot,
        replay_data=data,
        request=BacktestRequestV2(
            symbol=str(target["symbol"]),
            asset_type="etf",
            dataset=str(dataset["name"]),
            start=pd.Timestamp(str(dataset["evaluation_start"])).date(),
            end=pd.Timestamp(str(dataset["evaluation_end"])).date(),
            initial_cash=100_000.0,
        ),
        outputs_root=output_root,
        run_date=pd.Timestamp("2026-09-13").date(),
        repository_root=repo,
    )
    risk_denials = int(signals.decisions["entry_risk_denied"].astype(bool).sum())
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "candidate_payload_sha256": payload_hash,
        "data_fingerprint": data.fingerprint,
        "research_events": int(len(expected)),
        "formal_signals": int(signals.decisions["target_position"].eq(1).sum()),
        "formal_closed_trades": int(len(actual)),
        "risk_denials": risk_denials,
        "event_and_execution_date_equivalence": "PASS",
        "raw_execution_price_equivalence": "PASS",
        "net_return_equivalence": "PASS",
        "se_replay_audit": summary.manifest["audit"]["status"],
        "formal_metrics": summary.metrics["strategy"]["metrics"],
        "route_decision": "PROCEED_TO_STATISTICAL_AUDIT",
    }
    if evidence["se_replay_audit"] != "PASS":
        raise AssertionError("formal execution or SE audit differs from contract")
    _write_json(artifacts / "equivalence_summary.json", evidence)
    actual[["event_date", "entry_date", "exit_date", "entry_price", "exit_price", "net_return"]].to_csv(
        artifacts / "formal_trades.csv", index=False, lineterminator="\n"
    )
    (experiment / "03_execution.md").write_text(
        "# S004 EX42 执行\n\n"
        f"研究/正式闭合交易均为{len(actual)}笔；融资风险否决{risk_denials}个基础信号；"
        f"事件日、进出场日、未复权成交价和费后收益逐笔一致；SE审计"
        f"{summary.manifest['audit']['status']}。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX42 结论\n\n"
        "结论：`PROCEED_TO_STATISTICAL_AUDIT`。S004-C002正式实现与研究证据等价，尚未获得冻结或"
        "模拟盘资格。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": str(target["symbol"]),
            "development_cutoff": str(dataset["cutoff"]),
            "status": "COMPLETE",
            "route_decision": "PROCEED_TO_STATISTICAL_AUDIT",
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)
    shutil.rmtree(output_root)


if __name__ == "__main__":
    main()
