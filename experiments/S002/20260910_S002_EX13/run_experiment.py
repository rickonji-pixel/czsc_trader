from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)


EXPERIMENT_ID = "20260910_S002_EX13"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate(repo_root: Path, protocol: dict[str, object]) -> None:
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = ("parameter_selection", "candidate_mutation", "filter_creation", "freeze_allowed", "pte_deployment_allowed")
    if any(protocol.get(name) for name in forbidden):
        raise ValueError("diagnostic experiment may not mutate, freeze, or deploy")
    for source in protocol["source_experiments"].values():
        path = repo_root / "experiments" / str(source["experiment_id"])
        validate_experiment_archive(path)
        if _sha256(path / "experiment_manifest.json") != source["manifest_sha256"]:
            raise ValueError(f"source manifest hash differs: {path.name}")


def _summarize(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    return (
        frame.groupby(keys, dropna=False)
        .agg(
            trades=("net_return_5", "size"),
            mean_return_5=("net_return_5", "mean"),
            win_rate_5=("net_return_5", lambda values: values.gt(0).mean()),
            mean_band_return=("band_return_4_6", "mean"),
            summed_return_5=("net_return_5", "sum"),
        )
        .reset_index()
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo_root = experiment.parents[1]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    _validate(repo_root, protocol)

    frame = pd.read_csv(
        repo_root / "experiments" / "20260910_S002_EX08"
        / "artifacts" / "trade_horizon_matrix.csv"
    )
    trend = pd.read_csv(
        repo_root / "experiments" / "20260910_S002_EX11"
        / "artifacts" / "510500_2023_trade_ledger.csv"
    )[["entry_signal_date", "downtrend_60"]]
    frame = frame.merge(trend, on="entry_signal_date", how="left")
    frame["is_2023"] = frame["year"].eq(int(protocol["analysis"]["weak_year"]))
    frame["is_low_vol"] = frame["volatility_bucket"].eq(
        str(protocol["analysis"]["weak_volatility_bucket"])
    )
    frame["year_group"] = frame["is_2023"].map({True: "2023", False: "OTHER"})
    frame["volatility_group"] = frame["is_low_vol"].map(
        {True: "LOW", False: "NON_LOW"}
    )

    cells = _summarize(frame, ["year_group", "volatility_group"])
    annual_volatility = _summarize(frame, ["year", "volatility_bucket"])
    low = int(frame["is_low_vol"].sum())
    year = int(frame["is_2023"].sum())
    intersection = int((frame["is_low_vol"] & frame["is_2023"]).sum())
    year_non_low = int((frame["is_2023"] & ~frame["is_low_vol"]).sum())
    other_low = int((~frame["is_2023"] & frame["is_low_vol"]).sum())
    overlap_low = intersection / low
    overlap_year = intersection / year
    settings = protocol["analysis"]
    high_overlap = max(overlap_low, overlap_year) >= float(settings["overlap_threshold"])
    sparse_counterfactual = min(year_non_low, other_low) < int(
        settings["minimum_counterfactual_cell_trades"]
    )
    diagnosis = "CONFOUNDED" if high_overlap and sparse_counterfactual else "DISTINGUISHABLE"

    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "candidate_id": "S002-C001",
        "diagnosis": diagnosis,
        "total_trades": int(len(frame)),
        "counts": {
            "year_2023": year,
            "low_volatility": low,
            "intersection_2023_low": intersection,
            "counterfactual_2023_non_low": year_non_low,
            "counterfactual_other_year_low": other_low,
        },
        "overlap": {
            "intersection_share_of_low": overlap_low,
            "intersection_share_of_2023": overlap_year,
        },
        "independent_boundary_supported": diagnosis == "DISTINGUISHABLE",
        "candidate_rule_changed": False,
    }
    cells.to_csv(artifacts / "confounding_cells.csv", index=False, encoding="utf-8-sig")
    annual_volatility.to_csv(
        artifacts / "annual_volatility_cells.csv", index=False, encoding="utf-8-sig"
    )
    _write_json(artifacts / "diagnosis.json", summary)

    cell_index = cells.set_index(["year_group", "volatility_group"])
    intersection_row = cell_index.loc[("2023", "LOW")]
    other_low_row = cell_index.loc[("OTHER", "LOW")]
    year_non_low_row = cell_index.loc[("2023", "NON_LOW")]
    (experiment / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。\n\n"
        "已验证EX08和EX11档案及哈希，完成2023/非2023与LOW/非LOW四格归因。"
        "未修改候选、未生成过滤器、未冻结或部署。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n状态：COMPLETE。诊断为`{diagnosis}`。\n\n"
        f"25笔交易中，2023年有{year}笔，低波动有{low}笔，两者交集{intersection}笔。"
        f"交集占全部低波动交易{overlap_low:.2%}，占2023年交易{overlap_year:.2%}。\n\n"
        f"2023低波动{intersection}笔，5日平均收益{float(intersection_row['mean_return_5']):.2%}；"
        f"非2023低波动只有{other_low}笔，平均收益{float(other_low_row['mean_return_5']):.2%}；"
        f"2023非低波动只有{year_non_low}笔，平均收益{float(year_non_low_row['mean_return_5']):.2%}。"
        "两个反事实格均未达到预注册的5笔下限。\n\n"
        "当前数据无法把年份效应和低波动效应可靠拆开，因此不能把低波动或2023定义为"
        "S002-C001的独立适用边界，也不能据此增加过滤器。该现象继续保留为候选风险，"
        "等待后续交易自然补充反事实样本。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S002",
            "symbol": "510500.SH",
            "candidate_id": "S002-C001",
            "development_cutoff": protocol["development_cutoff"],
            "diagnosis": diagnosis,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
