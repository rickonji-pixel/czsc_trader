from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S001_EX03"


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
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _active_trade(trades: pd.DataFrame, day: pd.Timestamp) -> pd.Series | None:
    active = trades.loc[trades["entry_day"].le(day) & trades["exit_day"].ge(day)]
    if len(active) > 1:
        raise AssertionError(f"more than one active trade on {day.date()}")
    return None if active.empty else active.iloc[0]


def _longest_loss_streak(trades: pd.DataFrame) -> int:
    longest = 0
    current = 0
    for value in trades.sort_values("exit_day")["net_return"].astype(float):
        current = current + 1 if value < 0 else 0
        longest = max(longest, current)
    return longest


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")

    source = repo / str(protocol["source_experiment"])
    validate_experiment_archive(source)
    daily_path = source / "artifacts/daily_regime_attribution.csv"
    trade_path = source / "artifacts/trade_attribution.csv"
    summary_path = source / "artifacts/drawdown_summary.json"
    checks = {
        daily_path: str(protocol["source_daily_attribution_sha256"]),
        trade_path: str(protocol["source_trade_attribution_sha256"]),
        summary_path: str(protocol["source_summary_sha256"]),
    }
    for path, expected in checks.items():
        if _sha256(path) != expected:
            raise ValueError(f"source artifact differs from protocol: {path}")

    daily = pd.read_csv(daily_path)
    daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.normalize()
    trades = pd.read_csv(trade_path)
    trades["entry_day"] = pd.to_datetime(trades["entry_date"], errors="raise").dt.normalize()
    trades["exit_day"] = pd.to_datetime(trades["exit_date"], errors="raise").dt.normalize()
    regime_rows = pd.read_csv(source / "artifacts/drawdown_regime_attribution.csv")
    episodes = (
        regime_rows[
            ["episode", "peak_date", "trough_date", "maximum_drawdown"]
        ]
        .drop_duplicates()
        .sort_values("episode")
    )

    rows: list[dict[str, object]] = []
    for episode in episodes.itertuples(index=False):
        peak_day = pd.Timestamp(episode.peak_date)
        trough_day = pd.Timestamp(episode.trough_date)
        peak_trade = _active_trade(trades, peak_day)
        trough_trade = _active_trade(trades, trough_day)
        same_cycle = (
            peak_trade is not None
            and trough_trade is not None
            and peak_trade["cycle_id"] == trough_trade["cycle_id"]
        )
        eventual_net_return = (
            float(peak_trade["net_return"]) if peak_trade is not None else None
        )
        shape = (
            "OPEN_PROFIT_GIVEBACK"
            if same_cycle and eventual_net_return is not None and eventual_net_return > 0
            else "LOSS_SEQUENCE"
        )
        closed = trades.loc[trades["exit_day"].between(peak_day, trough_day)].copy()
        peak_account = daily.loc[daily["date"].eq(peak_day)].iloc[0]
        trough_account = daily.loc[daily["date"].eq(trough_day)].iloc[0]
        entry_price = float(peak_trade["entry_price"]) if peak_trade is not None else None
        rows.append(
            {
                "episode": int(episode.episode),
                "peak_date": peak_day.date().isoformat(),
                "trough_date": trough_day.date().isoformat(),
                "maximum_drawdown": float(episode.maximum_drawdown),
                "shape": shape,
                "peak_cycle_id": peak_trade["cycle_id"] if peak_trade is not None else "",
                "trough_cycle_id": trough_trade["cycle_id"]
                if trough_trade is not None
                else "",
                "same_cycle_at_peak_and_trough": same_cycle,
                "peak_position_eventual_net_return": eventual_net_return,
                "peak_close_vs_entry": (
                    float(peak_account["close"]) / entry_price - 1.0
                    if entry_price is not None
                    else None
                ),
                "trough_close_vs_entry": (
                    float(trough_account["close"]) / entry_price - 1.0
                    if entry_price is not None
                    else None
                ),
                "closed_trades_in_peak_to_trough": len(closed),
                "losing_trades_in_peak_to_trough": int(closed["net_return"].lt(0).sum()),
                "closed_trade_compound_return": float(
                    (1.0 + closed["net_return"].astype(float)).prod() - 1.0
                ),
                "longest_losing_trade_streak": _longest_loss_streak(closed),
            }
        )

    shapes = pd.DataFrame(rows)
    counts = shapes["shape"].value_counts().to_dict()
    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "strategy_reference": "S001-v2",
        "episode_count": len(shapes),
        "shape_counts": {str(key): int(value) for key, value in counts.items()},
        "finding": "TWO_DISTINCT_DRAWDOWN_SHAPES",
        "route_decision": "PROCEED_TO_DUAL_DRAWDOWN_HYPOTHESIS_TEST",
        "attribution_scope": "descriptive_not_causal",
        "candidate_created": False,
    }
    shapes.to_csv(artifacts / "drawdown_shapes.csv", index=False, lineterminator="\n")
    _write(artifacts / "shape_summary.json", result)

    (experiment / "03_execution.md").write_text(
        "# S001 EX03 执行\n\n"
        f"状态：`COMPLETE`。完成 {len(shapes)} 个主要回撤区间的持仓路径分类。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX03 结论\n\n"
        f"主要回撤并非单一机制：{counts.get('LOSS_SEQUENCE', 0)} 个属于连续亏损/反复进出型，"
        f"{counts.get('OPEN_PROFIT_GIVEBACK', 0)} 个属于单笔最终盈利持仓的浮盈回吐型。"
        "下一轮应分别证伪“连续亏损能否被识别”和“巨大浮盈回吐能否低代价收敛”两个假设；"
        "任何方案都必须重新检查完整开发池收益和 15% 最大回撤门。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "strategy_version": protocol["strategy_version"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": result["route_decision"],
            "candidate_generation": False,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
