from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260911_S003_EX31"


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
            "conditional_return_analysis",
            "breadth_feature_generation",
            "signal_generation",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX31 may only resolve frozen lifecycle gaps")

    source_spec = protocol["source"]
    source = repo_root / "experiments" / source_spec["experiment_id"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": source_spec["experiment_manifest_sha256"],
        source / "artifacts" / "constituent_daily_panel.csv.gz": source_spec["panel_sha256"],
        source / "artifacts" / "data_quality.json": source_spec["data_quality_sha256"],
        source / "artifacts" / "unexplained_missing.csv": source_spec[
            "unexplained_missing_sha256"
        ],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")

    unresolved = pd.read_csv(source / "artifacts" / "unexplained_missing.csv")
    unresolved["dt"] = pd.to_datetime(unresolved["dt"])
    codes = sorted(unresolved["con_code"].unique())
    pro = get_tushare_pro(repo_root / ".env")
    frames = []
    for code in codes:
        frame = pro.stock_basic(
            ts_code=code,
            list_status=protocol["lifecycle"]["list_status"],
            fields="ts_code,symbol,name,exchange,list_status,list_date,delist_date",
        )
        if frame is not None and not frame.empty:
            frames.append(frame)
    lifecycle = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if lifecycle.empty:
        raise ValueError("Tushare returned no delisted stock lifecycle metadata")
    lifecycle["delist_date"] = pd.to_datetime(
        lifecycle["delist_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    lifecycle["list_date"] = pd.to_datetime(
        lifecycle["list_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    resolved = unresolved.merge(
        lifecycle[["ts_code", "name", "list_status", "list_date", "delist_date"]],
        left_on="con_code",
        right_on="ts_code",
        how="left",
        validate="many_to_one",
    )
    resolved["resolved_by_delisting"] = (
        resolved["delist_date"].notna() & resolved["dt"].ge(resolved["delist_date"])
    )

    panel = pd.read_csv(source / "artifacts" / "constituent_daily_panel.csv.gz")
    panel["dt"] = pd.to_datetime(panel["dt"])
    delist_dates = lifecycle.set_index("ts_code")["delist_date"].to_dict()
    panel["record_status"] = "OBSERVED"
    panel.loc[~panel["observed"].astype(bool) & panel["suspended"].astype(bool), "record_status"] = (
        "SUSPENDED"
    )
    lifecycle_absent = pd.Series(False, index=panel.index)
    for code, delist_date in delist_dates.items():
        lifecycle_absent |= (
            panel["con_code"].eq(code)
            & ~panel["observed"].astype(bool)
            & panel["dt"].ge(delist_date)
        )
    panel.loc[lifecycle_absent, "record_status"] = "DELISTED_STALE_MEMBERSHIP"
    remaining = panel.loc[
        ~panel["observed"].astype(bool)
        & ~panel["suspended"].astype(bool)
        & ~lifecycle_absent
    ]
    allowed = set(protocol["allowed_statuses"])
    unexpected_statuses = sorted(set(panel["record_status"]) - allowed)
    gate = protocol["quality_gate"]
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "source_unexplained_missing_rows": int(len(unresolved)),
        "resolved_by_delisting_rows": int(resolved["resolved_by_delisting"].sum()),
        "remaining_unexplained_rows": int(len(remaining)),
        "lifecycle_codes": lifecycle[
            ["ts_code", "name", "list_status", "list_date", "delist_date"]
        ].assign(
            list_date=lambda frame: frame["list_date"].dt.strftime("%Y-%m-%d"),
            delist_date=lambda frame: frame["delist_date"].dt.strftime("%Y-%m-%d"),
        ).to_dict(orient="records"),
        "record_status_counts": {
            key: int(value) for key, value in panel["record_status"].value_counts().items()
        },
        "unexpected_statuses": unexpected_statuses,
        "price_or_return_fill_rows": int(
            panel.loc[~panel["observed"].astype(bool), ["pct_chg", "vol", "amount"]]
            .notna()
            .any(axis=1)
            .sum()
        ),
    }
    quality["passed"] = bool(
        quality["source_unexplained_missing_rows"] == gate["source_unexplained_missing_rows"]
        and quality["resolved_by_delisting_rows"] == gate["resolved_by_delisting_rows"]
        and quality["remaining_unexplained_rows"] == gate["remaining_unexplained_rows"]
        and not unexpected_statuses
        and quality["price_or_return_fill_rows"] == 0
    )

    panel_out = panel.copy()
    panel_out["dt"] = panel_out["dt"].dt.strftime("%Y-%m-%d")
    panel_out.to_csv(
        artifacts / "lifecycle_aware_constituent_panel.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    lifecycle_out = lifecycle.copy()
    lifecycle_out["list_date"] = lifecycle_out["list_date"].dt.strftime("%Y-%m-%d")
    lifecycle_out["delist_date"] = lifecycle_out["delist_date"].dt.strftime("%Y-%m-%d")
    lifecycle_out.to_csv(artifacts / "lifecycle_evidence.csv", index=False, lineterminator="\n")
    _write_json(artifacts / "data_quality.json", quality)

    status = "PASS" if quality["passed"] else "FAIL"
    names = ", ".join(
        f"{row['ts_code']} {row['name']}（{row['delist_date']}）"
        for row in quality["lifecycle_codes"]
    )
    (experiment / "03_execution.md").write_text(
        "# S003 EX31 执行\n\n"
        f"生命周期复核结果：`{status}`。涉及{name if (name := names) else '无'}。"
        f"EX30的{quality['source_unexplained_missing_rows']}条缺失中，退市解释"
        f"{quality['resolved_by_delisting_rows']}条，剩余{quality['remaining_unexplained_rows']}条。"
        "所有未观测记录均未填入价格、收益、成交量或成交额。\n",
        encoding="utf-8",
    )
    conclusion = (
        "停牌与退市后月度名单滞留均已明确标注，生命周期感知面板可进入宽度特征定义。"
        if quality["passed"]
        else "仍有无法解释缺失，当前不得计算宽度。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX31 结论\n\n"
        f"结论：`{status}`。{conclusion}本轮没有补造数据、计算收益或创建候选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": "2026-09-08",
            "status": status,
            "breadth_feature_generation": False,
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
