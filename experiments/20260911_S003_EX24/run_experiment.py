from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.relative_style_evaluation import evaluate_relative_style_events
from czsc_trader.relative_style_events import generate_price_volume_confirmation_events


EXPERIMENT_ID = "20260911_S003_EX24"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "parameter_selection", "candidate_generation", "promotion_allowed",
            "mutates_strategy_manager", "mutates_pte",
        )
    ):
        raise ValueError("EX24 may only evaluate its frozen mechanism")

    dataset = protocol["dataset"]
    feature_experiment = repo_root / "experiments" / dataset["feature_experiment"]
    prior_experiment = repo_root / "experiments" / dataset["prior_evaluation_experiment"]
    validate_experiment_archive(feature_experiment)
    validate_experiment_archive(prior_experiment)
    expected = {
        feature_experiment / "experiment_manifest.json": dataset["feature_manifest_sha256"],
        feature_experiment / "artifacts" / "relative_features.csv.gz": dataset["feature_file_sha256"],
        prior_experiment / "experiment_manifest.json": dataset["prior_evaluation_manifest_sha256"],
        repo_root / "data" / "raw" / "510500_intraday_manifest.json": dataset[
            "intraday_manifest_sha256"
        ],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")

    features = pd.read_csv(feature_experiment / "artifacts" / "relative_features.csv.gz")
    mechanism = protocol["mechanism"]
    events, density, feature_evidence = generate_price_volume_confirmation_events(
        features,
        evaluation_start=dataset["evaluation_start"],
        normalization_lookback=int(mechanism["normalization_lookback"]),
        threshold_lookback=int(mechanism["threshold_lookback"]),
        tail_quantile=float(mechanism["tail_quantile"]),
    )
    events.to_csv(artifacts / "mechanism_events.csv", index=False, lineterminator="\n")
    density.to_csv(artifacts / "density_metrics.csv", index=False, lineterminator="\n")
    feature_evidence.to_csv(
        artifacts / "feature_evidence.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )

    density_pass = bool(density.iloc[0]["density_eligible"])
    qualifiers: list[str] = []
    metric_text = "密度门未通过，因此按协议没有读取事件后的收益。"
    if density_pass:
        intraday = load_intraday_research_data(repo_root / "data" / "raw", "510500.SH")
        result = evaluate_relative_style_events(
            events,
            intraday.frames["5m"],
            {
                mechanism["id"]: {
                    "entry_checkpoint": mechanism["entry_checkpoint"],
                    "exit_checkpoint": mechanism["exit_checkpoint"],
                }
            },
            evaluation_start=dataset["evaluation_start"],
            baseline_one_way_cost=float(protocol["execution"]["baseline_one_way_cost"]),
            stress_one_way_cost=float(protocol["execution"]["stress_one_way_cost"]),
            base_fraction=float(protocol["execution"]["base_fraction"]),
            positive_years_required=int(protocol["acceptance"]["positive_years_required"]),
            recent_sessions=int(protocol["acceptance"]["recent_sessions"]),
        )
        for name, frame in {
            "episodes.csv.gz": result.episodes,
            "mechanism_metrics.csv": result.metrics,
            "annual_metrics.csv": result.annual,
            "account_metrics.csv": result.account,
        }.items():
            compression = {"method": "gzip", "compresslevel": 9, "mtime": 0} if name.endswith(".gz") else None
            frame.to_csv(
                artifacts / name,
                index=False,
                encoding="utf-8-sig",
                compression=compression,
                lineterminator="\n",
            )
        row = result.metrics.iloc[0]
        qualifiers = result.metrics.loc[result.metrics["eligible_for_audit"], "mechanism"].tolist()
        metric_text = (
            f"压力均值{row['stress_mean_return']:.3%}，盈亏比{row['stress_profit_factor']:.2f}，"
            f"正收益年份{int(row['positive_years'])}，近期均值{row['recent_stress_mean_return']:.3%}，"
            f"相对静态终值{row['incremental_terminal_return_vs_static']:.2%}，账户最大回撤"
            f"{row['account_max_drawdown']:.2%}，卡玛{row['account_calmar']:.2f}。"
        )

    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "density_pass": density_pass,
        "eligible_for_audit": qualifiers,
        "route_decision": "CONTINUE_AUDIT" if qualifiers else "STOP_TESTED_MECHANISM",
        "candidate_created": False,
    }
    _write_json(artifacts / "evaluation_summary.json", summary)
    density_row = density.iloc[0]
    (experiment / "03_execution.md").write_text(
        "# S003 EX24 执行\n\n"
        f"价格—成交额确认机制共产生{int(density_row['event_count'])}个事件，滚动60日中位数"
        f"{density_row['rolling_60_median']:.1f}，第10百分位{density_row['rolling_60_p10']:.1f}，"
        f"密度门{'通过' if density_pass else '未通过'}。{metric_text}\n",
        encoding="utf-8",
    )
    decision = (
        "机制获得统计稳健性审计资格，但必须将EX22至EX24全部已测试路径计入搜索偏差。"
        if qualifiers
        else "机制未获得审计资格，停止当前OHLCV相对风格研究线。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX24 结论\n\n" + decision
        + " 本轮没有创建候选，也没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": "COMPLETE",
            "eligible_for_audit": qualifiers,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
