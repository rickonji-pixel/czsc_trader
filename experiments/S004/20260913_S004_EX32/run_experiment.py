from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S004_EX32"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        protocol.get(key)
        for key in (
            "conditional_return_analysis",
            "event_generation",
            "parameter_selection",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX32 may only audit source availability")

    dataset = protocol["dataset"]
    start = dataset["start"].replace("-", "")
    cutoff = dataset["development_cutoff"].replace("-", "")
    pro = get_tushare_pro(repo / ".env")
    nav = pro.fund_nav(
        ts_code="588080.SH",
        start_date=start,
        end_date=cutoff,
        fields=",".join(protocol["sources"]["fund_nav"]["fields"]),
    )
    share = pro.fund_share(
        ts_code="588080.SH",
        start_date=start,
        end_date=cutoff,
    )
    market = pro.fund_daily(
        ts_code="588080.SH",
        start_date=start,
        end_date=cutoff,
        fields=",".join(protocol["sources"]["fund_daily"]["fields"]),
    )
    if nav is None or nav.empty or share is None or share.empty or market is None or market.empty:
        raise ValueError("one or more ETF primary-market sources returned no rows")

    nav = nav.copy()
    share = share.copy()
    market = market.copy()
    nav["ann_date"] = pd.to_datetime(nav["ann_date"].astype(str), format="%Y%m%d")
    nav["nav_date"] = pd.to_datetime(nav["nav_date"].astype(str), format="%Y%m%d")
    nav["announcement_lag_days"] = (nav["ann_date"] - nav["nav_date"]).dt.days
    nav["year"] = nav["nav_date"].dt.year
    share["trade_date"] = pd.to_datetime(share["trade_date"].astype(str), format="%Y%m%d")
    market["trade_date"] = pd.to_datetime(market["trade_date"].astype(str), format="%Y%m%d")

    lag_by_year = (
        nav.groupby("year")["announcement_lag_days"]
        .agg(rows="count", minimum="min", median="median", maximum="max")
        .reset_index()
    )
    same_day = nav["announcement_lag_days"].eq(0)
    same_day_by_year = same_day.groupby(nav["year"]).mean().rename("same_day_ratio").reset_index()
    lag_by_year = lag_by_year.merge(same_day_by_year, on="year", validate="one_to_one")
    market_dates = set(market["trade_date"])
    nav_dates = set(nav["nav_date"])
    share_dates = set(share["trade_date"])
    sources = protocol["sources"]
    account_points = int(protocol["account"]["tushare_points"])
    same_day_all = bool(same_day.all())
    nav_clock_provable = bool(sources["fund_nav"]["exact_publication_time_documented"])
    share_clock_provable = bool(sources["fund_share"]["exact_publication_time_documented"])
    share_size_accessible = bool(
        int(sources["etf_share_size"]["minimum_points"]) <= account_points
    )
    reasons = []
    if not same_day_all:
        reasons.append("fund_nav_not_announced_on_nav_date_for_all_sessions")
    if not nav_clock_provable:
        reasons.append("fund_nav_has_no_provable_publication_clock_before_20_30")
    if not share_clock_provable:
        reasons.append("fund_share_has_no_announcement_or_publication_clock")
    if not share_size_accessible:
        reasons.append("etf_share_size_requires_more_than_5000_points")
    if sources["etf_share_size"]["documented_publication"].endswith("08_30"):
        reasons.append("etf_share_size_is_published_next_day")

    quality = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "fund_nav_rows": int(len(nav)),
        "fund_nav_first": nav["nav_date"].min().strftime("%Y-%m-%d"),
        "fund_nav_last": nav["nav_date"].max().strftime("%Y-%m-%d"),
        "fund_nav_duplicate_dates": int(nav.duplicated("nav_date", keep=False).sum()),
        "fund_nav_same_day_announcement_ratio": float(same_day.mean()),
        "fund_nav_same_day_for_all_sessions": same_day_all,
        "fund_nav_market_date_coverage_ratio": len(nav_dates & market_dates) / len(market_dates),
        "fund_share_rows": int(len(share)),
        "fund_share_first": share["trade_date"].min().strftime("%Y-%m-%d"),
        "fund_share_last": share["trade_date"].max().strftime("%Y-%m-%d"),
        "fund_share_duplicate_dates": int(share.duplicated("trade_date", keep=False).sum()),
        "fund_share_market_date_coverage_ratio": len(share_dates & market_dates) / len(market_dates),
        "fund_share_announcement_field_available": bool(
            sources["fund_share"]["announcement_field_available"]
        ),
        "fund_nav_publication_clock_provable": nav_clock_provable,
        "fund_share_publication_clock_provable": share_clock_provable,
        "etf_share_size_accessible_with_current_points": share_size_accessible,
        "etf_share_size_documented_publication": sources["etf_share_size"][
            "documented_publication"
        ],
        "causal_gate_failures": reasons,
        "passed": False,
    }
    nav_out = nav.copy()
    nav_out["ann_date"] = nav_out["ann_date"].dt.strftime("%Y-%m-%d")
    nav_out["nav_date"] = nav_out["nav_date"].dt.strftime("%Y-%m-%d")
    share_out = share.copy()
    share_out["trade_date"] = share_out["trade_date"].dt.strftime("%Y-%m-%d")
    market_out = market.copy()
    market_out["trade_date"] = market_out["trade_date"].dt.strftime("%Y-%m-%d")
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    nav_out.to_csv(artifacts / "fund_nav.csv.gz", index=False, compression=compression)
    share_out.to_csv(artifacts / "fund_share.csv.gz", index=False, compression=compression)
    market_out.to_csv(artifacts / "fund_daily.csv.gz", index=False, compression=compression)
    lag_by_year.to_csv(
        artifacts / "nav_announcement_lag_by_year.csv", index=False, lineterminator="\n"
    )
    _write_json(artifacts / "data_quality.json", quality)

    (experiment / "03_execution.md").write_text(
        "# S004 EX32 执行\n\n"
        f"因果可用性门：`FAIL`。净值{quality['fund_nav_rows']}条、份额"
        f"{quality['fund_share_rows']}条，市场日覆盖均为100%；净值同日公告占比"
        f"{quality['fund_nav_same_day_announcement_ratio']:.2%}，且接口未提供20:30前发布证明。"
        "份额接口没有公告时间字段；明确次日8:30更新的ETF份额规模接口需要8000积分。\n\n"
        "本轮没有生成事件或读取未来收益。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S004 EX32 结论\n\n"
        "结论：`STOP_SAME_DAY_ETF_PRIMARY_MARKET_ROUTE`。数据覆盖充足，但无法证明同日20:30"
        "可得；禁止用事后净值或份额回测同日折溢价。若研究份额申赎，必须另立T+2开盘的"
        "保守时点协议。没有创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S004",
            "symbol": "588080.SH",
            "development_cutoff": dataset["development_cutoff"],
            "status": "FAIL",
            "route_decision": "STOP_SAME_DAY_ETF_PRIMARY_MARKET_ROUTE",
            "conditional_return_analysis": False,
            "candidate_generation": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
