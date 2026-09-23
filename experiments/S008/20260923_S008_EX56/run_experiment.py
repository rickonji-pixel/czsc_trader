from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import numpy as np
import pandas as pd
from dataflows import Dataflows, Dataset
from strategy_runtime import StrategyCandidate, StrategyInit, StrategyRuntime, TradableWindow
from strategy_runtime.implementation_identity import implementation_sha256
from strategy_runtime.loader import StrategyLoader
from trading_execution_engine import HistoricalExecutor

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX56"


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
    etf_close = 3.5 * np.exp(
        0.00015 * ordinal + 0.06 * np.sin(ordinal / 47.0) + 0.02 * np.sin(ordinal / 9.0)
    )
    etf_open = etf_close * (1.0 + 0.0015 * np.sin(ordinal / 5.0))
    volume = 8.0e6 * (1.2 + 0.25 * np.sin(ordinal / 13.0))
    etf = pd.DataFrame(
        {
            "Date": sessions,
            "Open": etf_open,
            "High": np.maximum(etf_open, etf_close) * 1.006,
            "Low": np.minimum(etf_open, etf_close) * 0.994,
            "Close": etf_close,
            "Volume": volume,
            "Amount": volume * etf_close,
        }
    )
    silver = 22.0 * np.exp(0.00005 * ordinal + 0.015 * np.sin(ordinal / 53.0))
    ratio = 80.0 * np.exp(
        0.075 * np.sin(ordinal / 39.0) + 0.035 * np.sin(ordinal / 11.0)
    )
    gold = silver * ratio

    def quotes(mid: np.ndarray) -> pd.DataFrame:
        spread = np.maximum(mid * 0.00005, 0.0001)
        return pd.DataFrame(
            {
                "Date": sessions,
                "BidOpen": mid - spread,
                "BidHigh": mid + spread,
                "BidLow": mid - 2.0 * spread,
                "BidClose": mid - spread,
                "AskOpen": mid + spread,
                "AskHigh": mid + 2.0 * spread,
                "AskLow": mid - spread,
                "AskClose": mid + spread,
                "TickQuantity": 10000.0 + 1000.0 * np.sin(ordinal / 8.0),
            }
        )

    calendar_dates = pd.date_range(start, pd.Timestamp(end) + pd.Timedelta(days=20))
    calendar = pd.DataFrame(
        {"Date": calendar_dates, "IsOpen": (calendar_dates.weekday < 5).astype(int)}
    )
    return {
        "etf": etf,
        "xau": quotes(gold),
        "xag": quotes(silver),
        "calendar": calendar,
    }


def _flows(frames: dict[str, pd.DataFrame]) -> Dataflows:
    def fetch(request):
        dataset = str(request.dataset)
        if dataset in {Dataset.ETF_OHLCV.value, Dataset.ETF_UNADJUSTED_DAILY.value}:
            frame = frames["etf"].copy()
        elif dataset == Dataset.FXCM_DAILY.value:
            key = "xau" if str(request.symbol).upper() == "XAUUSD.FXCM" else "xag"
            frame = frames[key].copy()
        elif dataset == Dataset.TRADING_CALENDAR.value:
            frame = frames["calendar"].copy()
        else:
            raise ValueError(f"unexpected synthetic dataset: {dataset}")
        dates = pd.to_datetime(frame["Date"])
        frame = frame.loc[
            dates.between(pd.Timestamp(request.start), pd.Timestamp(request.end))
        ].reset_index(drop=True)
        metadata: dict[str, object] = {
            "vendor": "S008_EX56_SYNTHETIC",
            "synthetic": True,
            "vendor_symbol": request.symbol,
        }
        if dataset == Dataset.ETF_UNADJUSTED_DAILY.value:
            metadata["adjustment"] = "none"
        return frame, metadata

    return Dataflows(
        {
            Dataset.ETF_OHLCV.value: fetch,
            Dataset.ETF_UNADJUSTED_DAILY.value: fetch,
            Dataset.FXCM_DAILY.value: fetch,
            Dataset.TRADING_CALENDAR.value: fetch,
        }
    )


def _candidate(protocol: dict[str, object], source_root: Path) -> StrategyCandidate:
    runtime = dict(protocol["runtime"])
    runtime["source_sha256"] = protocol["sources"]["runtime_source_sha256"]
    return StrategyCandidate(
        "S008",
        "EX56P04",
        {"runtime": runtime, "parameters": dict(protocol["implementation_anchor"])},
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
        "selects_parameters",
        "candidate_generation",
        "reads_sealed_validation",
        "mutates_catalog",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("implementation gate cannot search, select, promote or mutate")

    predecessor = repo / "experiments/S008/20260923_S008_EX55"
    source_root = experiment / "runtime/strategy_runtime"
    source_files = tuple(protocol["runtime"]["source_files"])
    expected_sources = {
        "ex55_manifest_sha256": predecessor / "experiment_manifest.json",
        "ex55_protocol_sha256": predecessor / "artifacts/protocol.json",
    }
    for key, path in expected_sources.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")
    validate_experiment_archive(predecessor)
    runtime_hash = implementation_sha256(source_files, source_root=source_root)
    if runtime_hash != protocol["sources"]["runtime_source_sha256"]:
        raise ValueError("research runtime differs from frozen protocol")

    frames = _synthetic_frames(
        protocol["synthetic_window"]["source_start"],
        protocol["synthetic_window"]["evaluation_end"],
    )
    flows = _flows(frames)
    candidate = _candidate(protocol, source_root)
    algorithm = StrategyLoader().load_candidate(candidate)
    definition = StrategyRuntime().describe(candidate)
    if algorithm.definition.runtime_sha256 != definition.runtime_sha256:
        raise ValueError("SRT loader and runtime definitions differ")
    actual_datasets = {item.dataset for item in definition.inputs.requirements}
    if actual_datasets != set(protocol["allowed_datasets"]):
        raise ValueError("runtime input datasets differ from preregistration")

    calendar_sessions = tuple(
        pd.bdate_range(protocol["synthetic_window"]["source_start"], "2025-01-20").date
    )
    prior_window = TradableWindow(pd.Timestamp("2024-12-30").date(), pd.Timestamp("2024-12-30").date())
    current_window = TradableWindow(pd.Timestamp("2024-12-31").date(), pd.Timestamp("2024-12-31").date())
    prior_scope = algorithm.derive_calculation_scope(prior_window, calendar_sessions)
    current_scope = algorithm.derive_calculation_scope(current_window, calendar_sessions)
    advanced_inputs = sum(
        1
        for name, current in current_scope.inputs.items()
        if current.end > prior_scope.inputs[name].end
    )
    if advanced_inputs < 4:
        raise ValueError("incremental market and FXCM input ranges did not advance")

    evaluation_start = pd.Timestamp(protocol["synthetic_window"]["evaluation_start"])
    evaluation_end = pd.Timestamp(protocol["synthetic_window"]["evaluation_end"])
    tmp_root = repo / ".tmp"
    tmp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="S008_EX56_", dir=tmp_root) as temp:
        with patch("strategy_runtime.preparation.Dataflows", return_value=flows):
            instance = StrategyRuntime().create(
                StrategyInit(
                    candidate,
                    TradableWindow(evaluation_start.date(), evaluation_end.date()),
                    Path(temp),
                )
            )
            preparation = instance.prepare_data()
            history = instance.inspect_signals()
            targets = set(pd.to_numeric(history["target_position"], errors="raise").dropna())
            transitions = int(history["target_position"].diff().abs().fillna(0).gt(0).sum())
            entries = int(history["action"].eq("ENTER").sum())
            exits = int(history["action"].eq("EXIT").sum())
            cooldown_rows = int(history["action"].eq("COOLDOWN").sum())
            ready = history["gold_silver_ratio_distance_120"].notna() & history[
                "gold_minus_silver_return_20"
            ].notna()
            causal = bool(
                (pd.to_datetime(history.loc[ready, "xau_source_date"]) < history.index[ready]).all()
                and (pd.to_datetime(history.loc[ready, "xag_source_date"]) < history.index[ready]).all()
            )
            state_target_match = bool(
                history["target_position"].eq(history["state"].map({"FLAT": 0.0, "LONG": 1.0})).all()
            )
            if targets != {0.0, 1.0} or transitions < 2 or entries < 1 or exits < 1:
                raise ValueError("synthetic behavior does not exercise both states")
            if cooldown_rows < int(protocol["implementation_anchor"]["cooldown_sessions"]):
                raise ValueError("synthetic behavior does not exercise the frozen cooldown")
            if not causal or not state_target_match:
                raise ValueError("causal source dates or state targets are inconsistent")

            primary = instance.run_window(
                executor=_executor(instance, frames["etf"], evaluation_start, evaluation_end, 0.001)
            )
            stress = instance.run_window(
                executor=_executor(instance, frames["etf"], evaluation_start, evaluation_end, 0.003)
            )
            expected_decisions = len(pd.bdate_range(evaluation_start, evaluation_end))
            if len(primary.decisions) != expected_decisions or len(stress.decisions) != expected_decisions:
                raise ValueError("TXE decision coverage is incomplete")
            if primary.orders.empty or stress.orders.empty:
                raise ValueError("TXE did not express prototype orders")

    row = {
        "prototype_id": protocol["prototype_id"],
        "runtime_sha256": definition.runtime_sha256,
        "runtime_source_sha256": runtime_hash,
        "input_requirement_count": len(definition.inputs.requirements),
        "prepared_dataset_identity": preparation.dataset_identity,
        "advanced_input_ranges": advanced_inputs,
        "history_rows": len(history),
        "target_states": "0|1",
        "target_transitions": transitions,
        "entries": entries,
        "exits": exits,
        "cooldown_rows": cooldown_rows,
        "primary_decisions": len(primary.decisions),
        "primary_orders": len(primary.orders),
        "primary_fills": len(primary.fills),
        "stress_decisions": len(stress.decisions),
        "stress_orders": len(stress.orders),
        "stress_fills": len(stress.fills),
    }
    pd.DataFrame([row]).to_csv(
        artifacts / "implementation_gate.csv", index=False, encoding="utf-8"
    )
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": "PROCEED_TO_PRECIOUS_METAL_PREFERENCE_SEARCH_PREREGISTRATION",
        "prototype_id": protocol["prototype_id"],
        "runtime_source_sha256": runtime_hash,
        "checks": {
            "predecessor_archive_valid": True,
            "runtime_source_frozen": True,
            "candidate_load_pass": True,
            "allowed_datasets_exact": True,
            "incremental_scopes_advance": True,
            "strict_prior_fxcm_dates": causal,
            "binary_state_machine_exercised": True,
            "cooldown_exercised": True,
            "primary_txe_pass": True,
            "stress_txe_pass": True,
        },
        "synthetic_data_only": True,
        "real_returns_read": False,
        "search_started": False,
        "parameters_selected": False,
        "candidate_created": False,
        "sealed_validation_read": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "implementation_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX56 执行记录\n\n"
        "贵金属偏好原型通过研究期SRT候选装载。确定性合成数据覆盖FLAT/LONG、入场、退出、"
        "最短持有和冷却路径，并通过严格前值FXCM因果检查、相邻窗口增量范围以及单边10bp/30bp"
        "的TXE完整窗口执行。未读取真实收益、运行搜索、选择参数或创建候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX56 结论\n\n"
        "机器裁决：`PROCEED_TO_PRECIOUS_METAL_PREFERENCE_SEARCH_PREREGISTRATION`。"
        "EX55冻结的两因子状态机已具备因果、确定、可增量执行的研究期实现。该结论只授予正式"
        "搜索预注册资格；合成结果不构成Alpha证据，也没有产生候选。\n",
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
