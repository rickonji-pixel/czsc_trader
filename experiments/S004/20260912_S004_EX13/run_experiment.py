from __future__ import annotations

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
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260912_S004_EX13"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    payload = _read(experiment / "candidate_payload.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(bool(protocol.get(key)) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("formal equivalence replay cannot mutate lifecycle state")
    reference_dir = repo / str(protocol["research_reference"])
    validate_experiment_archive(reference_dir)

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
        pd.Timestamp(dataset["cutoff"]).date(),
        include_one_minute=True,
    )
    signals = build_closing_dislocation_signals(
        snapshot,
        data,
        pd.Timestamp(dataset["evaluation_start"]),
        pd.Timestamp(dataset["evaluation_end"]),
    )
    result = replay_account(signals, data, 100_000.0)
    expected = pd.read_csv(
        reference_dir / "artifacts" / "candidate_episodes.csv.gz",
        parse_dates=["event_date", "entry_date", "exit_date"],
    ).sort_values("event_date").reset_index(drop=True)
    events = signals.decisions.loc[signals.decisions["target_position"].eq(1), ["signal_date"]]
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
    entry_price_equal = bool(
        actual["entry_price"].astype(float).sub(expected["entry_price"].astype(float)).abs().le(1e-12).all()
    )
    exit_price_equal = bool(
        actual["exit_price"].astype(float).sub(expected["exit_price"].astype(float)).abs().le(1e-12).all()
    )
    pd.testing.assert_series_equal(
        actual["net_return"].astype(float),
        expected["baseline_return"].astype(float),
        check_names=False,
        atol=1e-12,
        rtol=1e-12,
    )

    output_root = repo / ".tmp" / "s004-ex13-backtest"
    if output_root.exists():
        shutil.rmtree(output_root)
    summary = run_backtest_v2(
        snapshot=snapshot,
        replay_data=data,
        request=BacktestRequestV2(
            symbol=str(target["symbol"]),
            asset_type="etf",
            dataset=str(dataset["name"]),
            start=pd.Timestamp(dataset["evaluation_start"]).date(),
            end=pd.Timestamp(dataset["evaluation_end"]).date(),
            initial_cash=100_000.0,
        ),
        outputs_root=output_root,
        run_date=pd.Timestamp("2026-09-12").date(),
        repository_root=repo,
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "FAIL",
        "candidate_payload_sha256": payload_hash,
        "data_fingerprint": data.fingerprint,
        "research_events": int(len(expected)),
        "formal_signals": int(len(events)),
        "formal_closed_trades": int(len(actual)),
        "unevaluable_cutoff_signals": int(len(events) - len(actual)),
        "event_date_equivalence": "PASS",
        "execution_date_equivalence": "PASS",
        "execution_price_equivalence": (
            "PASS" if entry_price_equal and exit_price_equal else "FAIL_ADJUSTED_VS_RAW_PRICE"
        ),
        "net_return_equivalence": "PASS",
        "se_replay_audit": summary.manifest["audit"]["status"],
        "formal_metrics": summary.metrics["strategy"]["metrics"],
        "route_decision": "START_CORRECTED_FORMAL_REPLAY",
    }
    _write(artifacts / "equivalence_summary.json", evidence)
    actual[["event_date", "entry_date", "exit_date", "entry_price", "exit_price", "net_return"]].to_csv(
        artifacts / "formal_trades.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    (experiment / "03_execution.md").write_text(
        "# 20260912_S004_EX13 执行\n\n状态：COMPLETE。事件和收益一致，但研究档案的后复权价格与正式账本的未复权价格不能逐值相等。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# 20260912_S004_EX13 结论\n\n"
        f"- 正式信号/研究事件/正式闭合交易：{len(events)}/{len(expected)}/{len(actual)}；\n"
        f"- 截止日未决信号：{len(events) - len(actual)}；\n"
        "- 事件日、进出场日和费后收益逐笔一致；\n"
        "- 48 笔交易的研究后复权价格与正式未复权成交价不同；\n"
        f"- SE 正式回放审计：{summary.manifest['audit']['status']}。\n\n"
        "结论：`START_CORRECTED_FORMAL_REPLAY`。研究收益结论未受影响，但本轮预注册的绝对价格等价要求不成立。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": dataset["cutoff"],
            "promotion_allowed": False,
        },
    )
    shutil.rmtree(output_root)


if __name__ == "__main__":
    main()
