from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260911_S003_EX40"


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
            "signal_generation",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX40 may only review the corporate-action-aware data gate")

    dataset = protocol["dataset"]
    source = repo_root / "experiments" / dataset["source_experiment"]
    validate_experiment_archive(source)
    expected = {
        source / "experiment_manifest.json": dataset["source_manifest_sha256"],
        source / "artifacts" / "etf_share_nav_panel.csv.gz": dataset["source_panel_sha256"],
        source / "artifacts" / "data_quality.json": dataset["source_quality_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path.name}")

    source_quality = _read_json(source / "artifacts" / "data_quality.json")
    panel = pd.read_csv(source / "artifacts" / "etf_share_nav_panel.csv.gz")
    date_columns = [
        "dt",
        "announcement_date",
        "share_available_date",
        "nav_available_date",
        "combined_available_date",
    ]
    for column in date_columns:
        panel[column] = pd.to_datetime(panel[column]).dt.normalize()
    panel = panel.sort_values("dt").reset_index(drop=True)
    panel["corporate_action"] = False
    panel["corporate_action_type"] = ""
    panel["flow_feature_eligible"] = True
    panel["premium_feature_eligible"] = True

    action_rows: list[dict[str, object]] = []
    action_checks = protocol["corporate_action_checks"]
    for action in protocol["corporate_actions"]:
        date = pd.Timestamp(action["date"])
        matches = panel.index[panel["dt"].eq(date)].tolist()
        if len(matches) != 1:
            raise ValueError(f"corporate action date missing or duplicated: {date.date()}")
        index = matches[0]
        if index == 0 or index + 1 >= len(panel):
            raise ValueError("corporate action lacks adjacent sessions")
        previous = panel.iloc[index - 1]
        current = panel.iloc[index]
        following = panel.iloc[index + 1]
        share_ratio = float(current["fd_share"] / previous["fd_share"])
        nav_ratio = float(current["unit_nav"] / previous["unit_nav"])
        reciprocal_error = abs(share_ratio * nav_ratio - 1.0)
        next_deviation = abs(float(following["close_nav_deviation"]))
        passed = bool(
            reciprocal_error
            <= float(action_checks["maximum_share_nav_reciprocal_error"])
            and following["dt"] == pd.Timestamp(action["secondary_market_resume_date"])
            and next_deviation
            <= float(action_checks["maximum_next_session_close_nav_deviation"])
        )
        panel.loc[index, "corporate_action"] = True
        panel.loc[index, "corporate_action_type"] = str(action["type"])
        panel.loc[index, "flow_feature_eligible"] = False
        panel.loc[index, "premium_feature_eligible"] = False
        action_rows.append(
            {
                "date": str(date.date()),
                "type": action["type"],
                "share_ratio": share_ratio,
                "nav_ratio": nav_ratio,
                "share_nav_reciprocal_error": reciprocal_error,
                "next_session": str(following["dt"].date()),
                "next_session_close_nav_deviation": float(following["close_nav_deviation"]),
                "passed": passed,
                "source": action["source"],
            }
        )

    normal = panel.loc[panel["premium_feature_eligible"]]
    max_normal_deviation = float(normal["close_nav_deviation"].abs().max())
    action_frame = pd.DataFrame(action_rows)
    gate = protocol["quality_gate"]
    passed = bool(
        bool(action_frame["passed"].all())
        and float(source_quality["share_calendar_coverage_ratio"])
        >= float(gate["minimum_share_calendar_coverage_ratio"])
        and float(source_quality["nav_calendar_coverage_ratio"])
        >= float(gate["minimum_nav_calendar_coverage_ratio"])
        and int(source_quality["share_duplicate_date_rows"]) == 0
        and int(source_quality["nav_duplicate_date_rows"]) == 0
        and bool(source_quality["positive_finite_share_and_nav"])
        and bool(source_quality["announcement_not_before_nav_date"])
        and float(source_quality["p95_combined_availability_delay_sessions"])
        <= float(gate["maximum_p95_combined_availability_delay_sessions"])
        and max_normal_deviation
        <= float(gate["maximum_normal_day_absolute_close_nav_deviation"])
        and int(source_quality["forward_filled_rows"]) == 0
    )
    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "source_experiment_status": "FAIL",
        "source_failure_explained_by_corporate_action": bool(action_frame["passed"].all()),
        "corporate_action_count": int(len(action_frame)),
        "normal_day_count": int(len(normal)),
        "max_normal_day_absolute_close_nav_deviation": max_normal_deviation,
        "share_calendar_coverage_ratio": source_quality["share_calendar_coverage_ratio"],
        "nav_calendar_coverage_ratio": source_quality["nav_calendar_coverage_ratio"],
        "p95_combined_availability_delay_sessions": source_quality[
            "p95_combined_availability_delay_sessions"
        ],
        "forward_filled_rows": source_quality["forward_filled_rows"],
        "flow_ineligible_rows": int((~panel["flow_feature_eligible"]).sum()),
        "premium_ineligible_rows": int((~panel["premium_feature_eligible"]).sum()),
        "passed": passed,
    }

    output = panel.copy()
    for column in date_columns:
        output[column] = pd.to_datetime(output[column]).dt.strftime("%Y-%m-%d")
    output.to_csv(
        artifacts / "governed_etf_share_nav_panel.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    action_frame.to_csv(
        artifacts / "corporate_action_evidence.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    _write_json(artifacts / "data_quality.json", quality)
    status = "PASS" if passed else "FAIL"
    action_row = action_frame.iloc[0]
    (experiment / "03_execution.md").write_text(
        "# S003 EX40 执行\n\n"
        f"公司行为感知数据门：`{status}`。拆分日份额倍率{action_row['share_ratio']:.5f}，"
        f"净值倍率{action_row['nav_ratio']:.5f}，倒数关系误差"
        f"{action_row['share_nav_reciprocal_error']:.3%}；下一交易日收盘净值偏离"
        f"{action_row['next_session_close_nav_deviation']:.3%}。排除1个拆分日后，普通交易日"
        f"最大绝对折溢价{max_normal_deviation:.2%}。\n\n"
        "本轮没有读取条件收益、生成信号或选择阈值。\n",
        encoding="utf-8",
    )
    conclusion = (
        "5000积分接口组合满足ETF资金流机制研究的数据要求。"
        if passed
        else "公司行为治理后仍未满足冻结的数据门，停止该信息源。"
    )
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX40 结论\n\n"
        f"结论：`{status}`。{conclusion}拆分日永久排除于份额流与折溢价特征。"
        "本轮没有创建候选，也没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S003",
            "symbol": "510500.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": status,
            "conditional_return_analysis": False,
            "candidate_generation": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
