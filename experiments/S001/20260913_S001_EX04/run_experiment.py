from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S001_EX04"


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


def _compound(values: pd.Series) -> float:
    return float((1.0 + values.astype(float)).prod() - 1.0) if len(values) else 0.0


def _streak_group(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    return "2+"


def _trade_paths(trades: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    ordered = trades.sort_values(["entry_day", "exit_day"]).reset_index(drop=True).copy()
    prior_streak = 0
    rows: list[dict[str, object]] = []
    for trade in ordered.itertuples(index=False):
        path = daily.loc[daily["date"].between(trade.entry_day, trade.exit_day)].copy()
        if path.empty:
            raise AssertionError(f"trade has no daily path: {trade.cycle_id}")
        returns = path["close"].astype(float) / float(trade.entry_price) - 1.0
        running_peak = returns.cummax()
        giveback = running_peak - returns
        rows.append(
            {
                "cycle_id": trade.cycle_id,
                "entry_date": trade.entry_day.date().isoformat(),
                "exit_date": trade.exit_day.date().isoformat(),
                "entry_regime": trade.entry_regime,
                "entry_factor_score": float(trade.entry_factor_score),
                "prior_loss_streak": prior_streak,
                "prior_loss_streak_group": _streak_group(prior_streak),
                "net_return": float(trade.net_return),
                "holding_sessions": len(path),
                "maximum_favorable_excursion_close": float(returns.max()),
                "maximum_adverse_excursion_close": float(returns.min()),
                "maximum_peak_to_later_close_giveback": float(giveback.max()),
                "final_close_return": float(returns.iloc[-1]),
            }
        )
        prior_streak = prior_streak + 1 if float(trade.net_return) < 0 else 0
    return pd.DataFrame(rows)


def _streak_summary(paths: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for group in ["0", "1", "2+"]:
        frame = paths.loc[paths["prior_loss_streak_group"].eq(group)]
        rows.append(
            {
                "prior_loss_streak_group": group,
                "trade_count": len(frame),
                "win_rate": float(frame["net_return"].gt(0).mean()) if len(frame) else 0.0,
                "mean_net_return": float(frame["net_return"].mean()) if len(frame) else 0.0,
                "compound_net_return": _compound(frame["net_return"]),
                "worst_net_return": float(frame["net_return"].min()) if len(frame) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _excursion_summary(paths: pd.DataFrame, thresholds: list[float]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for threshold in thresholds:
        frame = paths.loc[paths["maximum_favorable_excursion_close"].ge(threshold)]
        rows.append(
            {
                "mfe_threshold": threshold,
                "trade_count": len(frame),
                "mean_maximum_favorable_excursion": float(
                    frame["maximum_favorable_excursion_close"].mean()
                )
                if len(frame)
                else 0.0,
                "mean_maximum_giveback": float(
                    frame["maximum_peak_to_later_close_giveback"].mean()
                )
                if len(frame)
                else 0.0,
                "maximum_giveback": float(
                    frame["maximum_peak_to_later_close_giveback"].max()
                )
                if len(frame)
                else 0.0,
                "mean_final_net_return": float(frame["net_return"].mean())
                if len(frame)
                else 0.0,
                "compound_final_net_return": _compound(frame["net_return"]),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")

    attribution = repo / str(protocol["source_attribution_experiment"])
    shapes = repo / str(protocol["source_shape_experiment"])
    validate_experiment_archive(attribution)
    validate_experiment_archive(shapes)
    paths = {
        attribution / "artifacts/daily_regime_attribution.csv": str(
            protocol["source_daily_attribution_sha256"]
        ),
        attribution / "artifacts/trade_attribution.csv": str(
            protocol["source_trade_attribution_sha256"]
        ),
        shapes / "artifacts/drawdown_shapes.csv": str(protocol["source_shapes_sha256"]),
        shapes / "artifacts/shape_summary.json": str(
            protocol["source_shape_summary_sha256"]
        ),
    }
    for path, expected in paths.items():
        if _sha256(path) != expected:
            raise ValueError(f"source artifact differs from protocol: {path}")

    daily = pd.read_csv(attribution / "artifacts/daily_regime_attribution.csv")
    daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.normalize()
    trades = pd.read_csv(attribution / "artifacts/trade_attribution.csv")
    trades["entry_day"] = pd.to_datetime(trades["entry_date"], errors="raise").dt.normalize()
    trades["exit_day"] = pd.to_datetime(trades["exit_date"], errors="raise").dt.normalize()

    trade_paths = _trade_paths(trades, daily)
    streaks = _streak_summary(trade_paths)
    thresholds = [float(value) for value in protocol["mfe_scenario_thresholds"]]
    excursions = _excursion_summary(trade_paths, thresholds)
    prolonged = streaks.loc[streaks["prior_loss_streak_group"].eq("2+")].iloc[0]
    high_mfe = excursions.loc[excursions["mfe_threshold"].eq(0.2)].iloc[0]
    streak_signal = bool(
        int(prolonged["trade_count"]) >= 5 and float(prolonged["mean_net_return"]) < 0
    )
    giveback_repeats = bool(
        int(high_mfe["trade_count"]) >= 2 and float(high_mfe["mean_maximum_giveback"]) >= 0.1
    )
    if streak_signal and giveback_repeats:
        route = "TEST_STREAK_AND_PROFIT_PROTECTION_POLICIES"
    elif streak_signal:
        route = "TEST_STREAK_POLICY_ONLY"
    elif giveback_repeats:
        route = "TEST_PROFIT_PROTECTION_POLICY_ONLY"
    else:
        route = "RETURN_TO_SIGNAL_MECHANISM_RESEARCH"

    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "strategy_reference": "S001-v2",
        "closed_trades": len(trade_paths),
        "loss_streak_persistence_observed": streak_signal,
        "high_mfe_giveback_repeats": giveback_repeats,
        "two_plus_prior_losses": {
            key: (int(value) if key == "trade_count" else float(value))
            for key, value in prolonged.to_dict().items()
            if key != "prior_loss_streak_group"
        },
        "mfe_at_least_20_percent": {
            key: (int(value) if key == "trade_count" else float(value))
            for key, value in high_mfe.to_dict().items()
            if key != "mfe_threshold"
        },
        "route_decision": route,
        "attribution_scope": "descriptive_not_causal",
        "candidate_created": False,
    }
    trade_paths.to_csv(
        artifacts / "trade_path_diagnostics.csv", index=False, lineterminator="\n"
    )
    streaks.to_csv(artifacts / "loss_streak_summary.csv", index=False, lineterminator="\n")
    excursions.to_csv(
        artifacts / "profit_giveback_summary.csv", index=False, lineterminator="\n"
    )
    _write(artifacts / "mechanism_summary.json", result)
    (experiment / "03_execution.md").write_text(
        "# S001 EX04 执行\n\n"
        f"状态：`COMPLETE`。分析 {len(trade_paths)} 笔闭合交易的前序连亏与持仓路径。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX04 结论\n\n"
        f"入场前已连续亏损至少两笔的样本共有 {int(prolonged['trade_count'])} 笔，"
        f"后续平均净收益 {float(prolonged['mean_net_return']):.2%}；"
        f"持仓最大浮盈达到 20% 的交易共有 {int(high_mfe['trade_count'])} 笔，"
        f"平均最大回吐 {float(high_mfe['mean_maximum_giveback']):.2%}。"
        f"裁决：`{route}`。本实验确认研究入口，不直接确认任何风控规则。\n",
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
            "decision": route,
            "candidate_generation": False,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
