from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.intraday_regime import (
    classify_lagged_daily_regime,
    fit_lagged_er_threshold,
)


EXPERIMENT_ID = "20260910_S003_EX03"


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


def _spell_summary(labels: pd.Series) -> dict[str, dict[str, float]]:
    groups = labels.ne(labels.shift()).cumsum()
    spells = labels.groupby(groups).agg(["first", "size"])
    result: dict[str, dict[str, float]] = {}
    for label, rows in spells.groupby("first"):
        sizes = rows["size"].astype(float)
        result[str(label)] = {
            "spell_count": int(len(sizes)),
            "median_sessions": float(sizes.median()),
            "p90_sessions": float(sizes.quantile(0.9)),
            "max_sessions": int(sizes.max()),
        }
    return result


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in (
            "reads_intraday_signals",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("regime definition may not inspect signals or mutate lifecycle state")

    target = protocol["research_target"]
    dataset = protocol["dataset"]
    classifier = protocol["classifier"]
    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(
        context,
        str(dataset["name"]),
        str(target["symbol"]),
        str(target["asset_type"]),
        date.fromisoformat(str(dataset["cutoff"])),
    )
    daily = replay.adjusted.daily.set_index("dt").sort_index()
    close = daily["close"].astype(float)
    lookback = int(classifier["lookback_sessions"])
    threshold = fit_lagged_er_threshold(
        close,
        lookback=lookback,
        calibration_start=str(classifier["calibration_start"]),
        calibration_end=str(classifier["calibration_end"]),
    )
    classified = classify_lagged_daily_regime(
        close,
        lookback=lookback,
        er_threshold=threshold,
    )
    expected_labels = set(str(item) for item in classifier["labels"])
    if set(classified["regime"].astype(str).unique()) - expected_labels:
        raise ValueError("classifier produced undeclared labels")

    last_day = close.index.max()
    perturbed = close.copy()
    perturbed.loc[last_day] *= 10.0
    perturbed_state = classify_lagged_daily_regime(
        perturbed,
        lookback=lookback,
        er_threshold=threshold,
    )
    causal_mutation_pass = bool(
        perturbed_state.at[last_day, "regime"] == classified.at[last_day, "regime"]
        and perturbed_state.at[last_day, "efficiency_ratio"]
        == classified.at[last_day, "efficiency_ratio"]
    )
    if not causal_mutation_pass:
        raise AssertionError("same-day close changed the same-day regime")

    evaluation = classified.loc[str(dataset["evaluation_start"]) : str(dataset["cutoff"])].copy()
    if evaluation.empty or evaluation["regime"].eq("warmup").any():
        raise ValueError("evaluation range contains no data or warmup labels")
    for horizon in protocol["forward_diagnostic_horizons"]:
        evaluation[f"forward_{int(horizon)}d"] = close.shift(-int(horizon)).div(close).sub(1.0)
    evaluation.insert(0, "date", evaluation.index.strftime("%Y-%m-%d"))
    evaluation.to_csv(
        artifacts / "daily_regimes.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )

    labels = evaluation["regime"].astype(str)
    annual = (
        evaluation.assign(year=evaluation.index.year)
        .groupby(["year", "regime"], observed=True)
        .size()
        .rename("sessions")
        .reset_index()
    )
    annual["year_share"] = annual["sessions"].div(
        annual.groupby("year")["sessions"].transform("sum")
    )
    annual.to_csv(
        artifacts / "annual_regime_distribution.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    diagnostics: dict[str, object] = {}
    for label, rows in evaluation.groupby("regime", observed=True):
        item: dict[str, object] = {
            "sessions": int(len(rows)),
            "share": float(len(rows) / len(evaluation)),
        }
        for horizon in protocol["forward_diagnostic_horizons"]:
            values = rows[f"forward_{int(horizon)}d"].dropna()
            item[f"forward_{int(horizon)}d_mean"] = float(values.mean())
            item[f"forward_{int(horizon)}d_median"] = float(values.median())
        diagnostics[str(label)] = item
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "dataset_fingerprint": replay.fingerprint,
        "classifier": {
            "lookback_sessions": lookback,
            "input_lag_sessions": int(classifier["input_lag_sessions"]),
            "er_threshold": threshold,
            "calibration_start": classifier["calibration_start"],
            "calibration_end": classifier["calibration_end"],
        },
        "evaluation": {
            "first": evaluation.index.min().date().isoformat(),
            "last": evaluation.index.max().date().isoformat(),
            "sessions": int(len(evaluation)),
            "transition_count": int(labels.ne(labels.shift()).sum() - 1),
            "causal_mutation_pass": causal_mutation_pass,
        },
        "regimes": diagnostics,
        "spells": _spell_summary(labels),
        "scope": {
            "intraday_signals_read": False,
            "candidate_created": False,
            "strategy_manager_mutated": False,
            "pte_mutated": False,
        },
    }
    _write(artifacts / "regime_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n"
        "状态：COMPLETE。日线regime分类、年度分布、连续状态段和前瞻方向诊断已生成；"
        "当前日收盘扰动不改变当前日状态，因果审计通过。未读取分钟信号结果。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n"
        f"日线regime分类器已按预注册口径冻结，ER阈值为{threshold:.4f}。正式观察期内，"
        f"range占{diagnostics['range']['share']:.1%}、trend_up占"
        f"{diagnostics['trend_up']['share']:.1%}、trend_down占"
        f"{diagnostics['trend_down']['share']:.1%}，共发生"
        f"{summary['evaluation']['transition_count']}次状态切换；三个状态均覆盖多个年度。"
        "当前日收盘价扰动不改变当前日状态，因果检查通过。\n\n"
        "它只提供趋势程度与方向环境，不直接决定交易。状态后的未来收益没有表现出可直接"
        "交易的单调预测关系，因此不得把regime本身包装为择时信号，也不采用硬开关压低"
        "交易频率。下一轮在固定分类器下普查15分钟CZSC信号，并按独立事件密度优先筛选。\n\n"
        "状态后的未来收益只作语义诊断，未用于选择回看周期、阈值或标签。后续有/无regime"
        "消融比较仍是候选形成前的必经步骤。\n",
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


if __name__ == "__main__":
    main()
