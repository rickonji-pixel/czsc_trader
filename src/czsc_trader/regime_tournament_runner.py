"""Run the preregistered six-candidate, three-window regime tournament."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from .backtest import run_period_backtests
from .baselines import resolve_baseline
from .data import load_market_data
from .experiment_archive import build_experiment_manifest, validate_experiment_archive
from .factors import signal_groups
from .four_layer import positions_from_scores
from .regime_tournament import metric_score, rank_total_scores
from .regime_weight import classify_regimes, lagged_efficiency_ratio, score_with_regime_weights
from .regime_weight_runner import (
    EXPECTED_BASELINE_SHA,
    _daily_prices,
    _metrics,
    _next_open_audit,
    _strategy_inputs,
    _weights_for_candidate,
)


EXPECTED_CANDIDATES = [125, 143, 275, 293, 400, 418]
EXPECTED_SOURCE_SHA = "bac24d498e76d46112ff1a1bf942472ffa40547b0aa73abd089befe6b78a7512"


def validate_regime_tournament_protocol(protocol: Mapping[str, object]) -> None:
    """Reject any EX21 boundary that differs from preregistration."""
    expected = {
        "schema_version": 1,
        "experiment_id": "0901_EX21",
        "handler": "regime_candidate_score_tournament",
        "experiment_type": "regime_candidate_score_tournament",
        "status": "PRE_REGISTERED",
        "symbol": "588080.SH",
        "asset_type": "etf",
        "source_experiment": "0901_EX20",
        "source_candidate_results_sha256": EXPECTED_SOURCE_SHA,
        "candidate_ids": EXPECTED_CANDIDATES,
        "windows": {
            "2026Q1": ["2026-01-01", "2026-03-31"],
            "2026H1": ["2026-01-01", "2026-06-30"],
            "2026M1_M8": ["2026-01-01", "2026-08-28"],
        },
        "metrics": ["max_drawdown", "calmar", "win_loss_ratio"],
        "scoring": {"better": 1.0, "tie": 0.5, "worse": 0.0},
        "baseline_score": 4.5,
        "ranking": "dense_score_desc",
        "tolerance": 1e-12,
        "fee_rate": 0.0005,
        "init_cash": 1_000_000.0,
        "report_only_metrics": ["strategy_return", "sharpe", "exposure", "trade_count"],
        "evidence_role": "observed_2026_diagnostic_only",
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"regime tournament protocol {key} differs from preregistration")
    baseline = protocol.get("baseline")
    if not isinstance(baseline, Mapping):
        raise ValueError("regime tournament baseline is missing")
    if baseline.get("version") != "baseline_20260826" or baseline.get("sha256") != EXPECTED_BASELINE_SHA:
        raise ValueError("regime tournament baseline identity differs")


def select_source_candidates(
    rows: pd.DataFrame, candidate_ids: Sequence[int]
) -> pd.DataFrame:
    """Return exactly the six EX20 research-pass candidates in ID order."""
    required = {
        "candidate_id",
        "pass",
        "trend_trend_multiplier",
        "trend_volume_multiplier",
        "range_trend_multiplier",
        "range_volume_multiplier",
    }
    if not required <= set(rows.columns):
        raise ValueError(f"source candidates missing columns: {sorted(required - set(rows.columns))}")
    passed_mask = rows["pass"].map(lambda value: str(value).strip().lower() == "true")
    selected = rows.loc[passed_mask].copy()
    selected["candidate_id"] = selected["candidate_id"].astype(int)
    expected = list(map(int, candidate_ids))
    if sorted(selected["candidate_id"].tolist()) != sorted(expected):
        raise ValueError("source research-pass candidate identity differs")
    return selected.set_index("candidate_id").loc[expected].reset_index()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _source_candidates(experiments_root: Path, protocol: Mapping[str, object]) -> tuple[pd.DataFrame, float, dict[str, object]]:
    source_dir = Path(experiments_root) / str(protocol["source_experiment"])
    source_manifest = validate_experiment_archive(source_dir)
    record = source_manifest["files"].get("artifacts/candidate_results.csv")
    if not isinstance(record, Mapping) or record.get("sha256") != str(
        protocol["source_candidate_results_sha256"]
    ):
        raise ValueError("EX20 candidate result identity differs")
    rows = pd.read_csv(source_dir / "artifacts" / "candidate_results.csv")
    selected = select_source_candidates(rows, protocol["candidate_ids"])
    source_metrics = json.loads(
        (source_dir / "artifacts" / "metrics.json").read_text(encoding="utf-8")
    )
    threshold = float(source_metrics["er_threshold"])
    return selected, threshold, source_manifest


def run_regime_tournament(
    raw_dir: Path,
    baseline_root: Path,
    experiments_root: Path,
    experiment_dir: Path,
    *,
    execution_commit: str,
) -> dict[str, object]:
    """Evaluate the six frozen EX20 candidates on three observed 2026 windows."""
    experiment_dir = Path(experiment_dir).resolve()
    artifacts = experiment_dir / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    validate_regime_tournament_protocol(protocol)
    candidates, er_threshold, source_manifest = _source_candidates(
        Path(experiments_root), protocol
    )
    baseline = resolve_baseline(
        Path(baseline_root),
        str(protocol["baseline"]["version"]),
        symbol=str(protocol["symbol"]),
    )
    if baseline.sha256 != str(protocol["baseline"]["sha256"]):
        raise ValueError("regime tournament baseline registry identity differs")

    window_specs = {
        name: (pd.Timestamp(bounds[0]), pd.Timestamp(bounds[1]))
        for name, bounds in protocol["windows"].items()
    }
    test_end = max(end for _, end in window_specs.values())
    data = load_market_data(
        raw_dir,
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        cutoff=test_end,
    )
    factors, base_weights, champion = _strategy_inputs(data, baseline)
    groups = signal_groups(factors.columns)
    daily = _daily_prices(data)
    regimes = classify_regimes(
        lagged_efficiency_ratio(daily["close"], 60), er_threshold
    ).reindex(factors.index)
    if regimes.isna().any():
        raise AssertionError("tournament regimes do not align to factors")

    fee_rate = float(protocol["fee_rate"])
    init_cash = float(protocol["init_cash"])
    baseline_results = run_period_backtests(
        data.daily,
        champion.target_position,
        window_specs,
        fee_rate=fee_rate,
        init_cash=init_cash,
    )
    baseline_metrics = {
        name: _metrics(result, init_cash) for name, result in baseline_results.items()
    }
    tolerance = float(protocol["tolerance"])
    detail_rows: list[dict[str, object]] = []
    period_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    execution_audit = True
    for _, candidate in candidates.iterrows():
        candidate_id = int(candidate["candidate_id"])
        multipliers = (
            float(candidate["trend_trend_multiplier"]),
            float(candidate["trend_volume_multiplier"]),
            float(candidate["range_trend_multiplier"]),
            float(candidate["range_volume_multiplier"]),
        )
        selected_weights = _weights_for_candidate(base_weights, groups, multipliers)
        scores = score_with_regime_weights(
            factors, regimes, selected_weights, base_weights
        )
        target = positions_from_scores(
            scores, baseline.rule.enter, baseline.rule.exit, baseline.rule
        )
        results = run_period_backtests(
            data.daily,
            target,
            window_specs,
            fee_rate=fee_rate,
            init_cash=init_cash,
        )
        metrics_by_window = {
            name: _metrics(result, init_cash) for name, result in results.items()
        }
        execution_audit = execution_audit and all(
            _next_open_audit(result.orders) for result in results.values()
        )
        scores_for_candidate: list[float] = []
        for window in window_specs:
            period_rows.append(
                {
                    "candidate_id": candidate_id,
                    "window": window,
                    **metrics_by_window[window],
                }
            )
            for metric in protocol["metrics"]:
                baseline_value = float(baseline_metrics[window][metric])
                candidate_value = float(metrics_by_window[window][metric])
                score = metric_score(
                    candidate_value, baseline_value, tolerance=tolerance
                )
                scores_for_candidate.append(score)
                detail_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "window": window,
                        "metric": metric,
                        "baseline_value": baseline_value,
                        "candidate_value": candidate_value,
                        "difference": candidate_value - baseline_value,
                        "outcome": {1.0: "better", 0.5: "tie", 0.0: "worse"}[score],
                        "score": score,
                    }
                )
        summary_rows.append(
            {
                "candidate_id": candidate_id,
                "total_score": float(sum(scores_for_candidate)),
                "better_count": scores_for_candidate.count(1.0),
                "tie_count": scores_for_candidate.count(0.5),
                "worse_count": scores_for_candidate.count(0.0),
                "trend_trend_multiplier": multipliers[0],
                "trend_volume_multiplier": multipliers[1],
                "range_trend_multiplier": multipliers[2],
                "range_volume_multiplier": multipliers[3],
            }
        )

    rankings = rank_total_scores(pd.DataFrame(summary_rows))
    baseline_score = sum(
        metric_score(value, value, tolerance=tolerance)
        for window in baseline_metrics.values()
        for metric in protocol["metrics"]
        for value in [float(window[metric])]
    )
    if baseline_score != float(protocol["baseline_score"]):
        raise AssertionError("baseline tournament score differs from preregistration")
    if not execution_audit:
        raise AssertionError("tournament next-open execution audit failed")

    pd.DataFrame(detail_rows).to_csv(
        artifacts / "score_details.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(period_rows).to_csv(
        artifacts / "candidate_period_metrics.csv", index=False, encoding="utf-8-sig"
    )
    rankings.to_csv(artifacts / "rankings.csv", index=False, encoding="utf-8-sig")
    _write_json(artifacts / "baseline_metrics.json", baseline_metrics)
    _write_json(
        artifacts / "identity_audit.json",
        {
            "status": "PASS",
            "source_experiment": str(protocol["source_experiment"]),
            "source_candidate_results_sha256": str(
                protocol["source_candidate_results_sha256"]
            ),
            "source_archive_status": source_manifest.get("status"),
            "baseline_version": baseline.version,
            "baseline_sha256": baseline.sha256,
            "candidate_ids": candidates["candidate_id"].astype(int).tolist(),
            "data_hashes": data.hashes,
            "all_orders_signal_before_execution": True,
        },
    )
    summary = {
        "status": "COMPLETE",
        "baseline_score": baseline_score,
        "candidate_count": len(rankings),
        "ranking": rankings[["rank", "candidate_id", "total_score"]].to_dict("records"),
        "evidence_role": str(protocol["evidence_role"]),
    }
    _write_json(artifacts / "metrics.json", summary)

    execution_lines = [
        "# 0901_EX21 执行过程",
        "",
        f"- 执行提交：`{execution_commit}`。",
        "- 固定读取EX20的6个研究合格候选，未新增、删除或搜索参数。",
        "- 同时执行2026Q1、H1和M1—M8三个已见历史窗口。",
        "- 完成9项逐格评分；基线自比较固定为4.5分。",
        "- 源档案身份、行情身份和次日开盘执行审计均为PASS。",
    ]
    (experiment_dir / "03_execution.md").write_text(
        "\n".join(execution_lines) + "\n", encoding="utf-8"
    )
    conclusion_lines = [
        "# 0901_EX21 结论",
        "",
        "状态：`COMPLETE`。本轮是已见2026诊断排名，不设置PASS或自动晋升。",
        "",
        "| 排名 | 候选 | 累计分 | 优于 | 平手 | 差于 |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    conclusion_lines.extend(
        f"| {int(row['rank'])} | {int(row['candidate_id'])} | {float(row['total_score']):.1f} | {int(row['better_count'])} | {int(row['tie_count'])} | {int(row['worse_count'])} |"
        for _, row in rankings.iterrows()
    )
    conclusion_lines.extend(
        [
            "",
            "活动基线基准分为4.5分。精确到窗口和指标的评分见`artifacts/score_details.csv`。",
        ]
    )
    (experiment_dir / "04_conclusion.md").write_text(
        "\n".join(conclusion_lines) + "\n", encoding="utf-8"
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": "0901_EX21",
            "date": "2026-09-01",
            "status": "COMPLETE",
            "symbol": "588080.SH",
            "asset_type": "etf",
            "baseline": protocol["baseline"],
            "protocol_sha256": sha256((artifacts / "protocol.json").read_bytes()).hexdigest(),
            "source_experiment": "0901_EX20",
            "visible_sample_end": "2026-08-28",
            "evidence_role": str(protocol["evidence_role"]),
        },
    )
    validate_experiment_archive(experiment_dir)
    return {**summary, "experiment_dir": str(experiment_dir)}
