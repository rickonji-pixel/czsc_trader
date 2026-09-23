from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import numpy as np
import pandas as pd

from dataflows import Dataflows, Dataset
from strategy_runtime import (
    StrategyCandidate,
    StrategyInit,
    StrategyRuntime,
    TradableWindow,
)
from strategy_runtime.implementation_identity import implementation_sha256
from strategy_runtime.loader import StrategyLoader
from trading_execution_engine import HistoricalExecutor

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX20"


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic_frames(start: str, end: str) -> dict[str, pd.DataFrame]:
    sessions = pd.bdate_range(start, end)
    ordinal = np.arange(len(sessions), dtype=float)
    close = 100.0 * np.exp(
        0.00008 * ordinal
        + 0.09 * np.sin(ordinal / 31.0)
        + 0.035 * np.sin(ordinal / 7.0)
    )
    opening = close * (1.0 + 0.0015 * np.sin(ordinal / 5.0))
    high = np.maximum(opening, close) * 1.006
    low = np.minimum(opening, close) * 0.994
    volume = 1_500_000.0 * (
        1.2 + 0.35 * np.sin(ordinal / 9.0) + 0.15 * np.cos(ordinal / 3.0)
    )
    amount = volume * close
    market = pd.DataFrame(
        {
            "Date": sessions,
            "Open": opening,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
            "Amount": amount,
        }
    )
    shibor = pd.DataFrame(
        {
            "Date": sessions,
            "OvernightRate": 2.0
            + 0.7 * np.sin(ordinal / 47.0)
            + 0.2 * np.cos(ordinal / 13.0),
        }
    )
    fx_mid = 6.6 * np.exp(
        0.00001 * ordinal
        + 0.035 * np.sin(ordinal / 43.0)
        + 0.012 * np.sin(ordinal / 11.0)
    )
    spread = 0.002
    fx = pd.DataFrame(
        {
            "Date": sessions,
            "BidOpen": fx_mid - spread,
            "BidHigh": fx_mid + spread,
            "BidLow": fx_mid - 2 * spread,
            "BidClose": fx_mid - spread,
            "AskOpen": fx_mid + spread,
            "AskHigh": fx_mid + 2 * spread,
            "AskLow": fx_mid - spread,
            "AskClose": fx_mid + spread,
            "TickQuantity": 10000.0 + 1000.0 * np.sin(ordinal / 8.0),
        }
    )
    sse_close = 3000.0 * np.exp(
        0.00006 * ordinal
        + 0.12 * np.sin(ordinal / 39.0 + 1.4)
        + 0.025 * np.sin(ordinal / 6.0)
    )
    sse = pd.DataFrame(
        {
            "Date": sessions,
            "Open": sse_close * 0.999,
            "High": sse_close * 1.008,
            "Low": sse_close * 0.992,
            "Close": sse_close,
            "Volume": 2.0e8 * (1.1 + 0.2 * np.sin(ordinal / 10.0)),
            "Amount": 5.0e11 * (1.1 + 0.2 * np.sin(ordinal / 10.0)),
        }
    )
    sse_basic = pd.DataFrame(
        {
            "Date": sessions,
            "TurnoverRateFreeFloat": 2.0
            + 0.8 * np.sin(ordinal / 17.0)
            + 0.25 * np.cos(ordinal / 4.0),
        }
    )
    months = pd.date_range(pd.Timestamp(start).to_period("M").start_time, end, freq="MS")
    month_ordinal = np.arange(len(months), dtype=float)
    cpi = pd.DataFrame(
        {
            "Date": months,
            "NationalYoYPercent": 2.0
            + 0.9 * np.sin(month_ordinal / 7.0)
            + 0.2 * np.cos(month_ordinal / 3.0),
            "NationalMoMPercent": 0.2 * np.sin(month_ordinal / 2.0),
        }
    )
    calendar_dates = pd.date_range(start, pd.Timestamp(end) + pd.Timedelta(days=20))
    calendar = pd.DataFrame(
        {"Date": calendar_dates, "IsOpen": (calendar_dates.weekday < 5).astype(int)}
    )
    return {
        Dataset.ETF_OHLCV.value: market,
        Dataset.ETF_UNADJUSTED_DAILY.value: market,
        Dataset.SHIBOR_DAILY.value: shibor,
        Dataset.USDCNH_DAILY.value: fx,
        Dataset.DOMESTIC_INDEX_DAILY.value: sse,
        Dataset.INDEX_DAILY_BASIC.value: sse_basic,
        Dataset.CN_CPI_MONTHLY.value: cpi,
        Dataset.TRADING_CALENDAR.value: calendar,
    }


def _flows(frames: dict[str, pd.DataFrame]) -> Dataflows:
    def fetch(request):
        dataset = str(request.dataset)
        frame = frames[dataset].copy()
        dates = pd.to_datetime(frame["Date"])
        frame = frame.loc[
            dates.between(pd.Timestamp(request.start), pd.Timestamp(request.end))
        ].reset_index(drop=True)
        metadata = {"vendor": "S008_EX20_SYNTHETIC", "synthetic": True}
        if dataset == Dataset.ETF_UNADJUSTED_DAILY.value:
            metadata["adjustment"] = "none"
        return frame, metadata

    return Dataflows({dataset: fetch for dataset in frames})


def _candidate(
    protocol: dict[str, object], source_root: Path, prototype_id: str, ordinal: int
) -> StrategyCandidate:
    runtime = dict(protocol["runtime"])
    parameters = {
        "prototype_id": prototype_id,
        "normalization": dict(protocol["normalization"]),
        "rule": dict(protocol["smoke_anchors"][prototype_id]),
    }
    return StrategyCandidate(
        "S008",
        f"EX20P{ordinal:02d}",
        {
            "runtime": {
                **runtime,
                "source_sha256": protocol["sources"]["runtime_source_sha256"],
            },
            "parameters": parameters,
        },
        source_root,
    )


def _executor(instance, daily: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, fee: float):
    execution = daily.rename(
        columns={
            "Date": "dt",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "vol",
            "Amount": "amount",
        }
    ).copy()
    execution["symbol"] = "518880.SH"
    return HistoricalExecutor(
        strategy_reference=instance.definition.release_id,
        symbol=instance.identity.symbol,
        execution_daily=execution,
        execution_intraday=pd.DataFrame(columns=["dt", "open", "high", "low", "close"]),
        evaluation_start=start,
        evaluation_end=end,
        initial_cash=1_000_000.0,
        execution_policy=instance.definition.execution,
        order_types=instance.definition.capabilities.order_types,
        fee_rate_override=fee,
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "reads_real_returns",
        "starts_search",
        "selects_winner",
        "candidate_generation",
        "reads_sealed_validation",
        "mutates_catalog",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("implementation gate cannot read returns, search, select or mutate")

    ex19 = repo / "experiments/S008/20260923_S008_EX19"
    source_root = experiment / "runtime/strategy_runtime"
    source_files = tuple(protocol["runtime"]["source_files"])
    source_paths = {
        "ex19_manifest_sha256": ex19 / "experiment_manifest.json",
        "prototype_registry_sha256": ex19 / "artifacts/prototype_registry.json",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")
    actual_source_hash = implementation_sha256(source_files, source_root=source_root)
    if actual_source_hash != protocol["sources"]["runtime_source_sha256"]:
        raise ValueError("prototype runtime source differs from frozen protocol")
    validate_experiment_archive(ex19)

    registry = _read(source_paths["prototype_registry_sha256"])
    prototype_ids = [item["prototype_id"] for item in registry["prototypes"]]
    if set(prototype_ids) != set(protocol["smoke_anchors"]):
        raise ValueError("smoke anchors do not exactly cover EX19 prototypes")
    frames = _synthetic_frames(
        protocol["synthetic_window"]["source_start"],
        protocol["synthetic_window"]["evaluation_end"],
    )
    flows = _flows(frames)
    evaluation_start = pd.Timestamp(protocol["synthetic_window"]["evaluation_start"])
    evaluation_end = pd.Timestamp(protocol["synthetic_window"]["evaluation_end"])
    calendar_sessions = tuple(
        pd.bdate_range(protocol["synthetic_window"]["source_start"], evaluation_end)
        .date
    )
    rows: list[dict[str, object]] = []
    tmp_root = repo / ".tmp"
    tmp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="S008_EX20_", dir=tmp_root) as temp:
        with patch("strategy_runtime.preparation.Dataflows", return_value=flows):
            for ordinal, prototype_id in enumerate(prototype_ids, start=1):
                candidate = _candidate(protocol, source_root, prototype_id, ordinal)
                algorithm = StrategyLoader().load_candidate(candidate)
                definition = StrategyRuntime().describe(candidate)
                if algorithm.definition.runtime_sha256 != definition.runtime_sha256:
                    raise ValueError("SRT loader and runtime definitions differ")
                prior_window = TradableWindow(
                    pd.Timestamp("2024-12-30").date(), pd.Timestamp("2024-12-30").date()
                )
                current_window = TradableWindow(
                    pd.Timestamp("2024-12-31").date(), pd.Timestamp("2024-12-31").date()
                )
                prior_scope = algorithm.derive_calculation_scope(prior_window, calendar_sessions)
                current_scope = algorithm.derive_calculation_scope(current_window, calendar_sessions)
                advanced_inputs = sum(
                    1
                    for name, current in current_scope.inputs.items()
                    if current.end > prior_scope.inputs[name].end
                )
                if advanced_inputs < 6:
                    raise ValueError(f"incremental input ranges did not advance: {prototype_id}")

                instance = StrategyRuntime().create(
                    StrategyInit(
                        candidate,
                        TradableWindow(evaluation_start.date(), evaluation_end.date()),
                        Path(temp) / prototype_id,
                    )
                )
                prepared = instance.prepare_data()
                history = instance.inspect_signals()
                targets = set(pd.to_numeric(history["target_position"], errors="raise").dropna())
                transitions = int(history["target_position"].diff().abs().fillna(0).gt(0).sum())
                if targets != {0.0, 1.0} or transitions < 2:
                    raise ValueError(
                        f"synthetic behavior does not exercise both states: {prototype_id}"
                    )
                primary = instance.run_window(
                    executor=_executor(
                        instance,
                        frames[Dataset.ETF_UNADJUSTED_DAILY.value],
                        evaluation_start,
                        evaluation_end,
                        0.001,
                    )
                )
                stress = instance.run_window(
                    executor=_executor(
                        instance,
                        frames[Dataset.ETF_UNADJUSTED_DAILY.value],
                        evaluation_start,
                        evaluation_end,
                        0.003,
                    )
                )
                expected_decisions = len(pd.bdate_range(evaluation_start, evaluation_end))
                if len(primary.decisions) != expected_decisions or len(stress.decisions) != expected_decisions:
                    raise ValueError(f"TXE decision coverage is incomplete: {prototype_id}")
                if primary.orders.empty or stress.orders.empty:
                    raise ValueError(f"TXE did not express prototype orders: {prototype_id}")
                if not primary.decisions["target_position"].isin([0.0, 1.0]).all():
                    raise ValueError(f"TXE received a non-binary target: {prototype_id}")
                rows.append(
                    {
                        "prototype_id": prototype_id,
                        "runtime_sha256": definition.runtime_sha256,
                        "input_requirement_count": len(definition.inputs.requirements),
                        "prepared_input_count": len(prepared.inputs),
                        "advanced_input_ranges": advanced_inputs,
                        "history_rows": len(history),
                        "target_states": "0|1",
                        "target_transitions": transitions,
                        "primary_decisions": len(primary.decisions),
                        "primary_orders": len(primary.orders),
                        "primary_fills": len(primary.fills),
                        "stress_decisions": len(stress.decisions),
                        "stress_orders": len(stress.orders),
                        "stress_fills": len(stress.fills),
                    }
                )

    evidence_frame = pd.DataFrame(rows)
    evidence_frame.to_csv(
        artifacts / "prototype_implementation_gate.csv", index=False, encoding="utf-8"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": "PROCEED_TO_PREREGISTERED_JOINT_SEARCH",
        "prototype_count": len(rows),
        "component_count": 13,
        "shared_runtime_source_sha256": actual_source_hash,
        "all_binary_targets": True,
        "all_incremental_scopes_advance": True,
        "all_srt_candidate_loads_pass": True,
        "all_primary_txe_runs_pass": True,
        "all_stress_txe_runs_pass": True,
        "synthetic_data_only": True,
        "real_returns_read": False,
        "search_started": False,
        "winner_selected": False,
        "candidate_created": False,
        "sealed_validation_read": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "implementation_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX20 执行记录\n\n"
        "三套原型均通过同一研究期StrategyImplementation加载；使用合成数据完成13组件因果物化、"
        "相邻窗口增量范围、0/100%目标仓位以及单边10bp/30bp的TXE完整窗口执行检查。"
        "没有读取真实收益、运行搜索、选择原型或创建候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX20 结论\n\n"
        "裁决：`PROCEED_TO_PREREGISTERED_JOINT_SEARCH`。三套原型均可由共享SRT实现确定性表达，"
        "所需DFLS输入、因果时点、增量范围和TXE执行口径闭合。下一步必须为三套原型分别预注册"
        "联合搜索预算和裁决规则，合成数据结果不得作为Alpha证据。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
