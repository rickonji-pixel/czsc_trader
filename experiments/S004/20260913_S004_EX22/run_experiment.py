from __future__ import annotations

from itertools import product
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import load_replay_data
from czsc_trader.backtesting.closing_dislocation_replay import _cooldown
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S004_EX22"


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


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0.0].sum())
    losses = float(-values.loc[values < 0.0].sum())
    return gains / losses if losses > 0 else float("inf") if gains > 0 else 0.0


def _symbol_paths(
    context: RepositoryContext,
    protocol: dict[str, object],
    symbol: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    dataset = protocol["dataset"]
    family = protocol["signal_family"]
    data = load_replay_data(
        context,
        str(dataset["name"]),
        symbol,
        str(dataset["asset_type"]),
        pd.Timestamp(dataset["cutoff"]).date(),
        include_one_minute=True,
    )
    adjusted = data.adjusted.daily.copy()
    adjusted["dt"] = pd.to_datetime(adjusted["dt"]).dt.normalize()
    adjusted = adjusted.set_index("dt").sort_index()
    sessions = pd.DatetimeIndex(adjusted.index)
    minute = data.signal_one_minute.copy()
    minute["trade_date"] = pd.to_datetime(minute["dt"]).dt.normalize()
    tail = minute.groupby("trade_date", sort=True, observed=True).tail(30).copy()
    if not tail.groupby("trade_date", observed=True).size().eq(30).all():
        raise ValueError(f"{symbol}: final 30-minute window is incomplete")
    if not pd.DatetimeIndex(sorted(minute["trade_date"].unique())).equals(sessions):
        raise ValueError(f"{symbol}: one-minute sessions differ from daily sessions")

    raw = data.execution_daily.copy()
    raw["dt"] = pd.to_datetime(raw["dt"]).dt.normalize()
    raw = raw.set_index("dt").sort_index()
    volatility = (
        adjusted["close"].astype(float)
        .pct_change(fill_method=None)
        .rolling(20, min_periods=20)
        .std()
        * np.sqrt(252.0)
    )
    volatility_threshold = volatility.shift(1).rolling(120, min_periods=120).median()
    low_volatility = volatility_threshold.notna() & volatility.lt(volatility_threshold)
    evaluation_start = pd.Timestamp(dataset["evaluation_start"])
    evaluation_end = pd.Timestamp(dataset["evaluation_end"])
    evaluation_sessions = sessions[
        (sessions >= evaluation_start) & (sessions <= evaluation_end)
    ]
    fee = float(protocol["stress_one_way_cost"])
    metrics_rows: list[dict[str, object]] = []
    episode_rows: list[dict[str, object]] = []

    for lookback, persistence, holding in product(
        family["support_lookbacks"],
        family["minimum_reclaim_persistence"],
        family["holding_sessions"],
    ):
        lookback = int(lookback)
        persistence = float(persistence)
        holding = int(holding)
        path_id = f"FB-N{lookback:02d}-P{int(persistence * 100):02d}-H{holding}"
        support = adjusted["close"].astype(float).shift(1).rolling(
            lookback, min_periods=lookback
        ).min()
        tail_support = tail["trade_date"].map(support)
        tail_above = tail["close"].astype(float).ge(tail_support)
        reclaim_persistence = tail_above.groupby(tail["trade_date"], observed=True).mean()
        raw_event = (
            adjusted["low"].astype(float).lt(support)
            & adjusted["close"].astype(float).ge(support)
            & reclaim_persistence.reindex(sessions).ge(persistence).fillna(False)
        )
        raw_dates = pd.DatetimeIndex(raw_event.index[raw_event])
        events = _cooldown(raw_dates, sessions, holding)
        positions = {value: index for index, value in enumerate(sessions)}
        trades: list[dict[str, object]] = []
        for event_date in sorted(events):
            position = positions[event_date]
            exit_position = position + 1 + holding
            if (
                event_date < evaluation_start
                or event_date > evaluation_end
                or exit_position >= len(sessions)
            ):
                continue
            entry_date = sessions[position + 1]
            exit_date = sessions[exit_position]
            entry = float(raw.loc[entry_date, "open"])
            exit_ = float(raw.loc[exit_date, "open"])
            trades.append(
                {
                    "path_id": path_id,
                    "symbol": symbol,
                    "event_date": event_date,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "entry_year": entry_date.year,
                    "support_lookback": lookback,
                    "reclaim_persistence": persistence,
                    "holding_sessions": holding,
                    "low_volatility": bool(low_volatility.loc[event_date]),
                    "stress_return": (exit_ * (1.0 - fee))
                    / (entry * (1.0 + fee))
                    - 1.0,
                }
            )
        frame = pd.DataFrame(trades)
        flags = pd.Series(0, index=evaluation_sessions, dtype=int)
        flags.loc[flags.index.intersection(events)] = 1
        rolling = flags.rolling(60, min_periods=60).sum().dropna()
        recent = frame.loc[
            frame["event_date"] >= evaluation_sessions[-252], "stress_return"
        ]
        low_vol = frame.loc[frame["low_volatility"], "stress_return"]
        annual = frame.groupby("entry_year", observed=True)["stress_return"].mean()
        metrics_rows.append(
            {
                "path_id": path_id,
                "symbol": symbol,
                "support_lookback": lookback,
                "reclaim_persistence": persistence,
                "holding_sessions": holding,
                "closed_trades": int(len(frame)),
                "rolling_60_median": float(rolling.median()),
                "rolling_60_p10": float(rolling.quantile(0.10, interpolation="lower")),
                "stress_mean_return": float(frame["stress_return"].mean()),
                "stress_profit_factor": _profit_factor(frame["stress_return"]),
                "positive_years": int(annual.gt(0.0).sum()),
                "recent_252_trades": int(len(recent)),
                "recent_252_mean_return": float(recent.mean()),
                "low_volatility_trades": int(len(low_vol)),
                "low_volatility_mean_return": float(low_vol.mean()),
                "low_volatility_profit_factor": _profit_factor(low_vol),
            }
        )
        episode_rows.extend(trades)
    return metrics_rows, episode_rows


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(
        bool(protocol.get(key))
        for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("mechanism census cannot create, promote or deploy")
    validate_experiment_archive(repo / "experiments/S004/20260912_S004_EX21")
    accounting = protocol["search_accounting"]
    if int(accounting["cumulative_registered_trials"]) != (
        int(accounting["previous_registered_trials"]) + int(accounting["new_paths"])
    ):
        raise ValueError("search accounting does not reconcile")
    family = protocol["signal_family"]
    path_count = (
        len(family["support_lookbacks"])
        * len(family["minimum_reclaim_persistence"])
        * len(family["holding_sessions"])
    )
    if path_count != int(accounting["new_paths"]):
        raise ValueError("registered path count differs from frozen grid")

    context = RepositoryContext.discover(repo)
    dataset = protocol["dataset"]
    metric_rows: list[dict[str, object]] = []
    episodes: list[dict[str, object]] = []
    for symbol in (dataset["primary_symbol"], dataset["external_symbol"]):
        symbol_metrics, symbol_episodes = _symbol_paths(
            context, protocol, str(symbol)
        )
        metric_rows.extend(symbol_metrics)
        episodes.extend(symbol_episodes)
    metrics = pd.DataFrame(metric_rows)
    episode = pd.DataFrame(episodes)
    acceptance = protocol["acceptance"]
    primary = str(dataset["primary_symbol"])
    external = str(dataset["external_symbol"])
    ranking_rows: list[dict[str, object]] = []
    for path_id, group in metrics.groupby("path_id", sort=True, observed=True):
        by_symbol = group.set_index("symbol")
        if set(by_symbol.index) != {primary, external}:
            raise AssertionError(f"{path_id}: incomplete symbol results")
        target = by_symbol.loc[primary]
        cross = by_symbol.loc[external]
        checks = {
            "primary_sample": target["closed_trades"]
            >= int(acceptance["primary_minimum_closed_trades"]),
            "external_sample": cross["closed_trades"]
            >= int(acceptance["external_minimum_closed_trades"]),
            "primary_density_median": int(acceptance["primary_rolling_60_median_min"])
            <= target["rolling_60_median"]
            <= int(acceptance["primary_rolling_60_median_max"]),
            "primary_density_p10": target["rolling_60_p10"]
            >= int(acceptance["primary_rolling_60_p10_min"]),
            "both_mean": by_symbol["stress_mean_return"].min()
            > float(acceptance["both_symbols_mean_return_min_exclusive"]),
            "both_profit_factor": by_symbol["stress_profit_factor"].min()
            > float(acceptance["both_symbols_profit_factor_min_exclusive"]),
            "both_positive_years": by_symbol["positive_years"].min()
            >= int(acceptance["both_symbols_positive_years_min"]),
            "both_recent": by_symbol["recent_252_mean_return"].min()
            > float(acceptance["both_symbols_recent_252_mean_min_exclusive"]),
            "both_low_vol_sample": by_symbol["low_volatility_trades"].min()
            >= int(acceptance["both_symbols_low_volatility_events_min"]),
            "both_low_vol_mean": by_symbol["low_volatility_mean_return"].min()
            > float(acceptance["both_symbols_low_volatility_mean_min_exclusive"]),
            "both_low_vol_profit_factor": by_symbol["low_volatility_profit_factor"].min()
            > float(acceptance["both_symbols_low_volatility_profit_factor_min_exclusive"]),
        }
        ranking_rows.append(
            {
                "path_id": path_id,
                "eligible": all(checks.values()),
                "worst_symbol_low_volatility_mean": float(
                    by_symbol["low_volatility_mean_return"].min()
                ),
                "worst_symbol_overall_mean": float(
                    by_symbol["stress_mean_return"].min()
                ),
                "primary_closed_trades": int(target["closed_trades"]),
                **{f"check_{name}": bool(value) for name, value in checks.items()},
            }
        )
    ranking = pd.DataFrame(ranking_rows).sort_values(
        [
            "eligible",
            "worst_symbol_low_volatility_mean",
            "worst_symbol_overall_mean",
            "primary_closed_trades",
            "path_id",
        ],
        ascending=[False, False, False, False, True],
    )
    eligible = ranking.loc[ranking["eligible"]]
    selected = None if eligible.empty else str(eligible.iloc[0]["path_id"])
    route = (
        str(protocol["selection"]["no_eligible_path"])
        if selected is None
        else "NOMINATE_FAILED_BREAKDOWN_PROTOTYPE"
    )
    metrics.to_csv(
        artifacts / "path_symbol_metrics.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    ranking.to_csv(
        artifacts / "path_ranking.csv",
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
    )
    episode.to_csv(
        artifacts / "path_episodes.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "tested_paths": int(len(ranking)),
        "eligible_paths": int(len(eligible)),
        "selected_path": selected,
        "route_decision": route,
        "cumulative_registered_trials": int(accounting["cumulative_registered_trials"]),
        "candidate_created": False,
    }
    _write(artifacts / "mechanism_census_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# 20260913_S004_EX22 执行\n\n状态：COMPLETE。12条预注册假跌破路径完成双标的密度、全样本和低波动评价。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# 20260913_S004_EX22 结论\n\n"
        f"合格路径：{len(eligible)}/12；结论：`{route}`。\n\n"
        + (
            f"预注册排序胜出路径为`{selected}`，只获得后续原型构造资格，尚未创建候选。\n"
            if selected is not None
            else "没有路径同时满足中频密度、双标的全样本质量和低波动质量；按协议停止该机制族。\n"
        ),
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S004",
            "symbol": f"{primary}+{external}",
            "development_cutoff": dataset["cutoff"],
            "decision": route,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
