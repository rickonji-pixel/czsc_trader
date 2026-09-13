from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data, resolve_registered_strategy
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.strategy_runtime import apply_resolved_strategy


EXPERIMENT_ID = "20260913_S001_EX16"


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


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in (
            "reads_forward_returns",
            "assigns_trade_direction",
            "candidate_generation",
            "promotion_allowed",
            "mutates_strategy_manager",
            "mutates_pte",
        )
    ):
        raise ValueError("EX16 is a return-blind coverage audit")

    target = dict(protocol["research_target"])
    dataset = dict(protocol["dataset"])
    screen = dict(protocol["coverage_screen"])
    context = RepositoryContext.discover(repo)
    source = resolve_registered_strategy(context, str(target["strategy_id"]), str(target["source_version"]))
    if source.source_hash != target["source_release_hash"]:
        raise ValueError("source strategy release differs from frozen protocol")
    replay = load_replay_data(
        context,
        str(dataset["name"]),
        str(target["symbol"]),
        str(target["asset_type"]),
        pd.Timestamp(str(dataset["cutoff"])).date(),
    )
    applied = apply_resolved_strategy(replay.adjusted, source.resolved_rule)
    start = pd.Timestamp(str(dataset["evaluation_start"])).normalize()
    sessions = pd.DatetimeIndex(
        replay.execution_daily.set_index("dt").sort_index().loc[start:].index
    ).normalize()
    close_target = applied.target_position.reindex(sessions).astype(float)
    if close_target.isna().any():
        raise ValueError("baseline target does not cover evaluation calendar")
    open_position = close_target.shift(1, fill_value=0.0)
    clean_flat = open_position.eq(0.0) & close_target.eq(0.0)

    census_dir = repo / "experiments" / "S001" / str(dataset["census_experiment"])
    census_manifest = validate_experiment_archive(census_dir)
    states = pd.read_csv(census_dir / "artifacts" / "state_density.csv")
    states = states.loc[states["density_capable"].astype(bool)].copy()
    events = pd.read_csv(
        census_dir / "artifacts" / "signal_events.csv.gz",
        parse_dates=["event_time", "event_date"],
    )
    events["event_date"] = events["event_date"].dt.normalize()
    events = events.loc[events["state_id"].isin(states["state_id"])].copy()
    events["flat_at_open"] = open_position.reindex(events["event_date"]).eq(0.0).to_numpy()
    events["flat_after_close"] = close_target.reindex(events["event_date"]).eq(0.0).to_numpy()
    events["clean_flat"] = events["flat_at_open"] & events["flat_after_close"]
    events["event_clock"] = events["event_time"].dt.strftime("%H:%M")

    rows: list[dict[str, object]] = []
    for state_id, group in events.groupby("state_id", sort=True, observed=True):
        meta = states.loc[states["state_id"].eq(state_id)].iloc[0]
        clean = group.loc[group["clean_flat"]]
        clock_share = clean["event_clock"].value_counts(normalize=True)
        clean_days = int(clean["event_date"].nunique())
        clean_rate = float(clean_days * 60 / len(sessions))
        rows.append(
            {
                "state_id": state_id,
                "signal_id": meta["signal_id"],
                "name": meta["name"],
                "state_primary": meta["state_primary"],
                "behavior_hash": meta["behavior_hash"],
                "all_event_days": int(group["event_date"].nunique()),
                "flat_at_open_event_days": int(group.loc[group["flat_at_open"], "event_date"].nunique()),
                "clean_flat_event_days": clean_days,
                "clean_flat_event_days_per_60_sessions": clean_rate,
                "clean_flat_share": float(clean_days / len(group)) if len(group) else 0.0,
                "clean_flat_distinct_clocks": int(clean["event_clock"].nunique()),
                "clean_flat_max_clock_share": float(clock_share.iloc[0]) if not clock_share.empty else None,
                "clean_flat_regime_counts_json": json.dumps(
                    clean["regime"].astype(str).value_counts().sort_index().to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
    coverage = pd.DataFrame(rows)
    coverage["coverage_pass"] = (
        coverage["clean_flat_event_days"].ge(int(screen["minimum_clean_flat_event_days"]))
        & coverage["clean_flat_event_days_per_60_sessions"].ge(
            float(screen["minimum_clean_flat_event_days_per_60_sessions"])
        )
    )
    coverage = coverage.sort_values(
        ["coverage_pass", "clean_flat_event_days_per_60_sessions", "clean_flat_max_clock_share"],
        ascending=[False, False, True],
    )
    coverage.to_csv(artifacts / "flat_coverage.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    events.loc[events["clean_flat"]].to_csv(
        artifacts / "clean_flat_events.csv.gz",
        index=False,
        encoding="utf-8-sig",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "source_strategy": source.identity.reference,
        "census_manifest_sha256": census_manifest["files"]["artifacts/state_density.csv"]["sha256"],
        "evaluation_sessions": int(len(sessions)),
        "baseline_flat_at_open_sessions": int(open_position.eq(0.0).sum()),
        "baseline_clean_flat_sessions": int(clean_flat.sum()),
        "density_capable_states": int(len(states)),
        "coverage_pass_states": int(coverage["coverage_pass"].sum()),
        "unique_coverage_pass_behaviors": int(
            coverage.loc[coverage["coverage_pass"], "behavior_hash"].nunique()
        ),
        "future_returns_read": False,
        "candidate_created": False,
    }
    _write(artifacts / "coverage_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S001 EX16 执行\n\n"
        f"状态：COMPLETE。EX15的{len(states)}个高密度状态全部完成S001空仓覆盖审计；"
        f"{summary['coverage_pass_states']}个状态、{summary['unique_coverage_pass_behaviors']}种非冗余行为"
        "通过预注册覆盖门槛。未读取事件后价格。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX16 结论\n\n"
        f"S001在{summary['evaluation_sessions']}个观察交易日中有{summary['baseline_clean_flat_sessions']}个"
        "连续空仓日。通过覆盖门槛的盘中状态仍有"
        f"{summary['unique_coverage_pass_behaviors']}种非冗余行为，说明分钟级数据中存在足够的潜在增量事件供给。"
        "下一轮必须先按金融语义预注册少量机制与交易方向，再读取收益；不得按收益从全部状态中反向挑选。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "strategy_version": target["source_version"],
            "symbol": target["symbol"],
            "development_cutoff": dataset["cutoff"],
            "promotion_allowed": False,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
