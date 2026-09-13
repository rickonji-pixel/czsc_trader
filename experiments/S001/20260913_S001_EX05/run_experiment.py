from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import hashlib
import json
from pathlib import Path

import pandas as pd
from strategy_evaluator import AuditStatus, audit_replay

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import BacktestRequestV2, load_replay_data, resolve_registered_strategy
from czsc_trader.backtesting.audit_adapter import build_replay_evidence
from czsc_trader.backtesting.execution_replay import replay_account
from czsc_trader.backtesting.metrics import calculate_metrics
from czsc_trader.backtesting.models import StrategyIdentity
from czsc_trader.backtesting.signal_replay import SignalReplay, replay_signals
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S001_EX05"


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decision_id(reference: str, signal_date: pd.Timestamp, target: int) -> str:
    raw = f"{reference}|{signal_date.date()}|{target}".encode()
    return "DEC-" + sha256(raw).hexdigest()[:20].upper()


def _overlay_signals(
    base: SignalReplay,
    daily_close: pd.Series,
    activation: float,
    giveback: float,
) -> SignalReplay:
    reference = f"S001-v2-PP-A{int(activation * 100):02d}-G{int(giveback * 100):02d}"
    content_hash = sha256(
        f"{base.snapshot.content_hash}|{activation:.6f}|{giveback:.6f}".encode()
    ).hexdigest()
    identity = StrategyIdentity(
        kind="CANDIDATE", reference=reference, source=f"experiment:{EXPERIMENT_ID}"
    )
    snapshot = replace(
        base.snapshot,
        identity=identity,
        source_hash=content_hash,
        content_hash=content_hash,
    )
    decisions = base.decisions.copy().sort_values("signal_date").reset_index(drop=True)
    in_base_cycle = False
    locked_out = False
    entry_close = 0.0
    high_close = 0.0
    armed = False
    targets: list[int] = []
    for row in decisions.itertuples(index=False):
        base_target = int(row.target_position)
        close = float(daily_close.loc[pd.Timestamp(row.signal_date)])
        if base_target == 0:
            in_base_cycle = False
            locked_out = False
            entry_close = 0.0
            high_close = 0.0
            armed = False
            target = 0
        elif locked_out:
            target = 0
        elif not in_base_cycle:
            in_base_cycle = True
            entry_close = close
            high_close = close
            target = 1
        else:
            high_close = max(high_close, close)
            if high_close / entry_close - 1.0 >= activation:
                armed = True
            if armed and close / high_close - 1.0 <= -giveback:
                target = 0
                locked_out = True
            else:
                target = 1
        targets.append(target)
    decisions["target_position"] = targets
    decisions["decision_id"] = [
        _decision_id(reference, pd.Timestamp(day), int(target))
        for day, target in zip(decisions["signal_date"], targets, strict=True)
    ]
    return replace(base, snapshot=snapshot, decisions=decisions)


def _row(
    label: str,
    signals: SignalReplay,
    replay_data: object,
    initial_cash: float,
    activation: float | None,
    giveback: float | None,
    required_cagr: float,
    drawdown_floor: float,
) -> tuple[dict[str, object], object]:
    result = replay_account(signals, replay_data, initial_cash)
    metrics = calculate_metrics(result, initial_cash)
    evidence = build_replay_evidence(signals, replay_data, result, initial_cash, metrics)
    audit = audit_replay(evidence)
    if audit.status is not AuditStatus.PASS:
        raise ValueError(f"SE replay audit failed for {label}: {audit.reason_codes}")
    sessions = len(result.account_daily)
    cagr = float((1.0 + float(metrics["return"])) ** (252.0 / sessions) - 1.0)
    max_drawdown = float(metrics["max_drawdown"])
    return_pass = cagr >= required_cagr
    drawdown_pass = max_drawdown >= drawdown_floor
    return (
        {
            "configuration": label,
            "activation_threshold": activation,
            "giveback_threshold": giveback,
            "cagr": cagr,
            "total_return": float(metrics["return"]),
            "max_drawdown": max_drawdown,
            "calmar": float(metrics["calmar"]),
            "win_loss_ratio": float(metrics["win_loss_ratio"]),
            "closed_trades": int(metrics["closed_trades"]),
            "return_gate": "PASS" if return_pass else "FAIL",
            "drawdown_gate": "PASS" if drawdown_pass else "FAIL",
            "primary_gate": "PASS" if return_pass and drawdown_pass else "FAIL",
            "se_replay_audit": audit.status.value,
        },
        result,
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    diagnosis = repo / str(protocol["source_diagnosis_experiment"])
    gap_source = repo / str(protocol["source_gap_experiment"])
    validate_experiment_archive(diagnosis)
    validate_experiment_archive(gap_source)
    mechanism_path = diagnosis / "artifacts/mechanism_summary.json"
    gap_path = gap_source / "artifacts/gap_summary.json"
    if _sha256_file(mechanism_path) != protocol["source_mechanism_summary_sha256"]:
        raise ValueError("mechanism diagnosis differs from protocol")
    if _sha256_file(gap_path) != protocol["source_gap_summary_sha256"]:
        raise ValueError("mandate gap differs from protocol")
    mechanism = _read(mechanism_path)
    if mechanism.get("route_decision") != "TEST_PROFIT_PROTECTION_POLICY_ONLY":
        raise ValueError("mechanism diagnosis did not authorize profit-protection test")
    gap = _read(gap_path)
    buyhold_cagr = float(gap["full_window"]["buyhold_cagr"])
    required_cagr = buyhold_cagr * float(protocol["positive_buyhold_cagr_multiple"])
    drawdown_floor = float(protocol["maximum_drawdown_floor"])

    context = RepositoryContext.discover(repo)
    snapshot = resolve_registered_strategy(
        context, str(protocol["strategy_id"]), str(protocol["strategy_version"])
    )
    if snapshot.source_hash != protocol["strategy_release_hash"]:
        raise ValueError("strategy release differs from protocol")
    cutoff = pd.Timestamp(str(protocol["development_cutoff"])).date()
    replay_data = load_replay_data(
        context,
        str(protocol["dataset"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        cutoff,
    )
    request = BacktestRequestV2(
        symbol=str(protocol["symbol"]),
        asset_type=str(protocol["asset_type"]),
        dataset=replay_data.dataset,
        start=pd.Timestamp(str(protocol["evaluation_start"])).date(),
        end=pd.Timestamp(str(protocol["evaluation_end"])).date(),
        initial_cash=float(protocol["initial_cash"]),
    )
    base = replay_signals(snapshot, replay_data, request.start, request.end)
    daily_close = replay_data.adjusted.daily.set_index("dt")["close"].astype(float)
    rows: list[dict[str, object]] = []
    baseline_row, _ = _row(
        "BASELINE",
        base,
        replay_data,
        request.initial_cash,
        None,
        None,
        required_cagr,
        drawdown_floor,
    )
    rows.append(baseline_row)
    for activation in [float(value) for value in protocol["activation_thresholds"]]:
        for giveback in [float(value) for value in protocol["giveback_thresholds"]]:
            signals = _overlay_signals(base, daily_close, activation, giveback)
            label = f"A{int(activation * 100):02d}-G{int(giveback * 100):02d}"
            row, _ = _row(
                label,
                signals,
                replay_data,
                request.initial_cash,
                activation,
                giveback,
                required_cagr,
                drawdown_floor,
            )
            rows.append(row)
    landscape = pd.DataFrame(rows)
    feasible = landscape.loc[
        landscape["configuration"].ne("BASELINE") & landscape["primary_gate"].eq("PASS")
    ].copy()
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "strategy_reference": snapshot.identity.reference,
        "data_fingerprint": replay_data.fingerprint,
        "buyhold_cagr": buyhold_cagr,
        "required_strategy_cagr": required_cagr,
        "maximum_drawdown_floor": drawdown_floor,
        "scenario_count": len(landscape) - 1,
        "feasible_scenario_count": len(feasible),
        "feasible_configurations": feasible["configuration"].tolist(),
        "finding": "FEASIBLE_REGION_EXISTS" if len(feasible) else "NO_FEASIBLE_REGION",
        "route_decision": (
            "PROCEED_TO_PROFIT_PROTECTION_POLICY_SELECTION"
            if len(feasible) >= 2
            else "RETURN_TO_SIGNAL_MECHANISM_RESEARCH"
        ),
        "candidate_created": False,
    }
    landscape.to_csv(artifacts / "feasibility_landscape.csv", index=False, lineterminator="\n")
    _write(artifacts / "feasibility_summary.json", result)
    (experiment / "03_execution.md").write_text(
        "# S001 EX05 执行\n\n"
        "状态：`COMPLETE`。完成 9 个固定利润保护情景和原策略对照；"
        "全部情景通过 SE 账本审计。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX05 结论\n\n"
        f"9 个利润保护情景中有 {len(feasible)} 个同时满足完整开发池收益门和 15% 最大回撤门。"
        f"裁决：`{result['route_decision']}`。本轮只证明可行区域是否存在，"
        "不从地形中事后选择规则，也不生成候选。\n",
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
            "decision": result["route_decision"],
            "candidate_generation": False,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
