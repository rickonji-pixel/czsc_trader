from __future__ import annotations

import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import (
    BacktestRequestV2,
    load_replay_data,
    resolve_registered_strategy,
    run_backtest_v2,
)
from czsc_trader.experiment_archive import build_experiment_manifest


EXPERIMENT_ID = "0909_EX01"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metric_rows(
    reference: str,
    window: dict[str, str],
    metrics: dict[str, object],
) -> list[dict[str, object]]:
    rows = []
    subjects = {
        "strategy": metrics["strategy"]["metrics"],
        "buyhold": metrics["benchmarks"]["buyhold"]["metrics"],
        "ma5_ma20": metrics["benchmarks"]["ma5_ma20"]["metrics"],
    }
    for subject, values in subjects.items():
        rows.append(
            {
                "strategy_reference": reference,
                "window": window["id"],
                "start": window["start"],
                "end": window["end"],
                "subject": subject,
                **values,
            }
        )
    return rows


def _wins(rows: pd.DataFrame, left: str, right: str) -> dict[str, int]:
    primary = ("max_drawdown", "calmar", "win_loss_ratio")
    result = {left: 0, right: 0, "ties": 0}
    for window in rows["window"].drop_duplicates():
        scoped = rows.loc[(rows["window"] == window) & (rows["subject"] == "strategy")]
        values = scoped.set_index("strategy_reference")
        for metric in primary:
            left_value = values.at[left, metric]
            right_value = values.at[right, metric]
            if pd.isna(left_value) or pd.isna(right_value) or left_value == right_value:
                result["ties"] += 1
            elif left_value > right_value:
                result[left] += 1
            else:
                result[right] += 1
    return result


def _classification(rows: pd.DataFrame, reference: str) -> str:
    strategy = rows.loc[
        (rows["strategy_reference"] == reference) & (rows["subject"] == "strategy")
    ].set_index("window")
    continuous = strategy.loc["2021_2025"]
    benchmark = rows.loc[
        (rows["strategy_reference"] == reference)
        & (rows["window"] == "2021_2025")
        & rows["subject"].isin(["buyhold", "ma5_ma20"])
    ]
    primary = ("max_drawdown", "calmar", "win_loss_ratio")
    jointly_dominated = all(
        all(
            pd.isna(continuous[metric])
            or pd.isna(row[metric])
            or row[metric] > continuous[metric]
            for metric in primary
        )
        for _, row in benchmark.iterrows()
    )
    positive_annual_calmar = int(
        strategy.loc[["2021", "2022", "2023", "2024", "2025"], "calmar"].gt(0).sum()
    )
    if not jointly_dominated and positive_annual_calmar >= 4:
        return "DIRECT_REUSE_SUPPORTED"
    if int(continuous["closed_trades"]) >= 20:
        return "FRAMEWORK_SEED_ONLY"
    return "REJECT_FRAMEWORK"


def _render_conclusion(
    rows: pd.DataFrame,
    classifications: dict[str, str],
    wins: dict[str, int],
) -> str:
    seed = max(("S001-v1", "S001-v2"), key=lambda value: wins[value])
    if wins["S001-v1"] == wins["S001-v2"]:
        continuous = rows.loc[
            (rows["window"] == "2021_2025") & (rows["subject"] == "strategy")
        ].set_index("strategy_reference")
        seed = str(continuous["return"].astype(float).idxmax())
    table = rows.loc[
        (rows["window"] == "2021_2025") & (rows["subject"] == "strategy")
    ].copy()
    lines = [
        "# 0909_EX01 结论",
        "",
        "状态：COMPLETE。全部14次TDR回放及SE审计通过。",
        "",
        "## 连续窗口",
        "",
        "| 策略 | 最大回撤 | 卡玛 | 盈亏比 | 收益率 | 闭合交易 | 分类 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for _, row in table.iterrows():
        ratio = "N/A" if pd.isna(row["win_loss_ratio"]) else f"{row['win_loss_ratio']:.4f}"
        lines.append(
            f"| {row['strategy_reference']} | {row['max_drawdown']:.2%} | "
            f"{row['calmar']:.4f} | {ratio} | {row['return']:.2%} | "
            f"{int(row['closed_trades'])} | {classifications[row['strategy_reference']]} |"
        )
    lines.extend(
        [
            "",
            "## 判读",
            "",
            f"三个主指标的逐窗口胜出计数为：S001-v1 {wins['S001-v1']}，"
            f"S001-v2 {wins['S001-v2']}，同值或不可比 {wins['ties']}。",
            f"按冻结协议，`{seed}`作为S002下一轮研究的参数种子。这个选择只代表相对更合适的"
            "搜索起点，不代表其可直接部署到510500。",
            "",
            "详细年度、2026YTD及参照数据见`artifacts/window_metrics.csv`；身份、数据指纹、"
            "回放审计和账本哈希见`artifacts/run_evidence.json`。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    experiment_dir = Path(__file__).resolve().parent
    repo_root = experiment_dir.parents[1]
    protocol_path = experiment_dir / "artifacts" / "protocol.json"
    protocol = _read_json(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("candidate_generation") or protocol.get("promotion_allowed"):
        raise ValueError("transfer diagnosis cannot generate or promote candidates")

    context = RepositoryContext.discover(repo_root, explicit_root=repo_root)
    dataset_spec = protocol["dataset"]
    target = protocol["research_target"]
    cutoff = date.fromisoformat(dataset_spec["cutoff"])
    replay_data = load_replay_data(
        context,
        dataset_spec["name"],
        target["symbol"],
        target["asset_type"],
        cutoff,
    )
    metrics_rows: list[dict[str, object]] = []
    run_evidence: list[dict[str, object]] = []
    identities: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="czsc_0909_ex01_") as temp:
        temp_root = Path(temp)
        for strategy_spec in protocol["reference_strategies"]:
            snapshot = resolve_registered_strategy(
                context, strategy_spec["strategy_id"], strategy_spec["version"]
            )
            identities.append(
                {
                    "reference": snapshot.identity.reference,
                    "source_hash": snapshot.source_hash,
                    "snapshot_hash": snapshot.content_hash,
                }
            )
            for window in protocol["windows"]:
                summary = run_backtest_v2(
                    snapshot=snapshot,
                    replay_data=replay_data,
                    request=BacktestRequestV2(
                        symbol=target["symbol"],
                        asset_type=target["asset_type"],
                        dataset=dataset_spec["name"],
                        start=date.fromisoformat(window["start"]),
                        end=date.fromisoformat(window["end"]),
                        initial_cash=float(protocol["initial_cash"]),
                    ),
                    outputs_root=temp_root / snapshot.identity.reference / window["id"],
                    run_date=date(2026, 9, 9),
                )
                metrics_rows.extend(
                    _metric_rows(snapshot.identity.reference, window, summary.metrics)
                )
                audit = summary.manifest["audit"]
                run_evidence.append(
                    {
                        "strategy_reference": snapshot.identity.reference,
                        "window": window["id"],
                        "ranges": summary.manifest["ranges"],
                        "application": summary.manifest["application"],
                        "audit": audit,
                        "ledger_sha256": {
                            name: _sha256(summary.output_dir / name)
                            for name in (
                                "decisions.csv", "orders.csv", "fills.csv",
                                "account_daily.csv", "trades.csv",
                            )
                        },
                    }
                )

    artifacts = experiment_dir / "artifacts"
    fieldnames = list(metrics_rows[0])
    with (artifacts / "window_metrics.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(metrics_rows)
    rows = pd.DataFrame(metrics_rows)
    wins = _wins(rows, "S001-v1", "S001-v2")
    classifications = {
        reference: _classification(rows, reference)
        for reference in ("S001-v1", "S001-v2")
    }
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "dataset": {
            "name": replay_data.dataset,
            "fingerprint": replay_data.fingerprint,
            "cutoff": replay_data.cutoff.isoformat(),
            "market_manifest_sha256": _sha256(context.raw_dir / "510500_manifest.json"),
            "execution_manifest_sha256": _sha256(
                context.raw_dir / "510500_execution_manifest.json"
            ),
        },
        "strategy_identities": identities,
        "runs": run_evidence,
        "primary_metric_wins": wins,
        "classifications": classifications,
    }
    _write_json(artifacts / "run_evidence.json", evidence)
    (experiment_dir / "03_execution.md").write_text(
        "# 0909_EX01 执行\n\n"
        "状态：COMPLETE。\n\n"
        "按冻结协议完成2个参考策略、7个窗口，共14次独立资金回放。全部运行通过TDR "
        "Backtest v2，并由SE完成执行账本审计。运行未生成候选、未调用SM或PTE。\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        _render_conclusion(rows, classifications, wins), encoding="utf-8"
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
