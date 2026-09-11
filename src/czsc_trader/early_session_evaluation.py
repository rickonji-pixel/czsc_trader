"""Direction and timing evaluation for pre-registered early-session events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pandas as pd

from .intraday_opening_execution import _account_metrics, _performance, _trade_return
from .intraday_opportunity_map import _price_checkpoints


@dataclass(frozen=True)
class EarlySessionEvaluation:
    """Episode, aggregate, annual and account evidence."""

    episodes: pd.DataFrame
    metrics: pd.DataFrame
    annual: pd.DataFrame
    account: pd.DataFrame


def evaluate_early_session_events(
    events: pd.DataFrame,
    five_minute: pd.DataFrame,
    mechanisms: Sequence[Mapping[str, str]],
    unconditional_metrics: pd.DataFrame,
    *,
    evaluation_start: pd.Timestamp | str,
    baseline_one_way_cost: float,
    stress_one_way_cost: float,
    base_fraction: float,
    positive_years_required: int,
) -> EarlySessionEvaluation:
    """Compare signed, opposite and timing-only variants on identical dates."""

    source = events.copy()
    source["trade_date"] = pd.to_datetime(source["trade_date"]).dt.normalize()
    source["direction"] = pd.to_numeric(source["direction"], errors="raise").astype(int)
    if not source["direction"].isin([-1, 1]).all():
        raise ValueError("event directions must be -1 or 1")
    if source.duplicated(["mechanism_id", "trade_date"]).any():
        raise ValueError("events must be unique by mechanism and trading day")
    mechanism_ids = [str(item["mechanism_id"]) for item in mechanisms]
    if set(source["mechanism_id"].astype(str)) != set(mechanism_ids):
        raise ValueError("events differ from frozen mechanism definitions")
    checkpoints = _price_checkpoints(five_minute)
    start = pd.Timestamp(evaluation_start).normalize()
    checkpoints = checkpoints.loc[checkpoints.index >= start]
    calendar = checkpoints.index
    daily_close = checkpoints["15:00_CLOSE"]
    elapsed_years = max((calendar.max() - calendar.min()).days / 365.25, 1.0)
    unconditional = unconditional_metrics.set_index("segment_id")

    episode_frames: list[pd.DataFrame] = []
    provisional: dict[tuple[str, str], dict[str, object]] = {}
    annual_rows: list[dict[str, object]] = []
    account_rows: list[dict[str, object]] = []
    for definition in mechanisms:
        mechanism_id = str(definition["mechanism_id"])
        entry_name = str(definition["entry"])
        exit_name = str(definition["exit"])
        opponent_id = str(definition["unconditional_opponent"])
        if entry_name not in checkpoints or exit_name not in checkpoints:
            raise ValueError(f"{mechanism_id}: unknown price checkpoint")
        if opponent_id not in unconditional.index:
            raise ValueError(f"{mechanism_id}: unconditional opponent is missing")
        selected = source.loc[source["mechanism_id"].eq(mechanism_id)].copy()
        selected = selected.loc[selected["trade_date"] >= start].sort_values("trade_date")
        if not selected["planned_entry"].eq(entry_name).all():
            raise ValueError(f"{mechanism_id}: event entry differs from protocol")
        if not selected["planned_exit"].eq(exit_name).all():
            raise ValueError(f"{mechanism_id}: event exit differs from protocol")
        for variant in ("PRIMARY", "OPPOSITE", "FIXED_LONG"):
            episodes = selected.copy()
            episodes["entry_price"] = checkpoints[entry_name].reindex(
                episodes["trade_date"]
            ).to_numpy()
            episodes["exit_price"] = checkpoints[exit_name].reindex(
                episodes["trade_date"]
            ).to_numpy()
            if episodes[["entry_price", "exit_price"]].isna().any().any():
                raise ValueError(f"{mechanism_id}: missing execution price")
            if variant == "PRIMARY":
                episodes["executed_direction"] = episodes["direction"]
            elif variant == "OPPOSITE":
                episodes["executed_direction"] = -episodes["direction"]
            else:
                episodes["executed_direction"] = 1
            for column, cost in (
                ("gross_return", 0.0),
                ("baseline_return", baseline_one_way_cost),
                ("stress_return", stress_one_way_cost),
            ):
                episodes[column] = [
                    _trade_return(
                        float(row.entry_price),
                        float(row.exit_price),
                        int(row.executed_direction),
                        cost,
                    )
                    for row in episodes.itertuples(index=False)
                ]
            episodes.insert(1, "variant", variant)
            episode_frames.append(episodes)
            gross = _performance(episodes["gross_return"], elapsed_years)
            baseline = _performance(episodes["baseline_return"], elapsed_years)
            stress = _performance(episodes["stress_return"], elapsed_years)
            positive_years = 0
            for year, rows in episodes.groupby(episodes["trade_date"].dt.year, observed=True):
                item = _performance(rows["stress_return"], 1.0)
                annual_rows.append(
                    {
                        "mechanism_id": mechanism_id,
                        "variant": variant,
                        "year": int(year),
                        **item,
                    }
                )
                positive_years += int(float(item["mean_return"]) > 0)
            account = _account_metrics(
                episodes,
                daily_close,
                base_fraction=base_fraction,
            )
            account_rows.append(
                {"mechanism_id": mechanism_id, "variant": variant, **account}
            )
            provisional[(mechanism_id, variant)] = {
                "mechanism_id": mechanism_id,
                "variant": variant,
                "episodes": int(len(episodes)),
                "gross_mean_return": gross["mean_return"],
                "baseline_mean_return": baseline["mean_return"],
                "baseline_profit_factor": baseline["profit_factor"],
                "stress_mean_return": stress["mean_return"],
                "stress_profit_factor": stress["profit_factor"],
                "stress_win_rate": stress["win_rate"],
                "stress_cumulative_return": stress["cumulative_return"],
                "positive_years": positive_years,
                "incremental_terminal_return_vs_static": account[
                    "incremental_terminal_return_vs_static"
                ],
                "unconditional_opponent": opponent_id,
                "unconditional_baseline_mean_return": float(
                    unconditional.loc[opponent_id, "baseline_mean_return"]
                ),
            }

    metric_rows: list[dict[str, object]] = []
    for mechanism_id in mechanism_ids:
        primary = provisional[(mechanism_id, "PRIMARY")]
        opposite = provisional[(mechanism_id, "OPPOSITE")]
        fixed_long = provisional[(mechanism_id, "FIXED_LONG")]
        def base_pass(row: dict[str, object]) -> bool:
            return bool(
                float(row["stress_mean_return"]) > 0
                and float(row["stress_profit_factor"]) > 1
                and float(row["incremental_terminal_return_vs_static"]) > 0
                and int(row["positive_years"]) >= positive_years_required
            )
        signed_feasible = bool(
            base_pass(primary)
            and float(primary["stress_mean_return"]) > float(opposite["stress_mean_return"])
            and float(primary["stress_mean_return"]) > float(fixed_long["stress_mean_return"])
        )
        timing_feasible = bool(
            base_pass(fixed_long)
            and float(fixed_long["baseline_mean_return"])
            > float(fixed_long["unconditional_baseline_mean_return"])
        )
        for variant, row in (
            ("PRIMARY", primary),
            ("OPPOSITE", opposite),
            ("FIXED_LONG", fixed_long),
        ):
            evidence = "CONTROL"
            if variant == "PRIMARY":
                evidence = "FEASIBLE_SIGNED" if signed_feasible else "DIRECTION_FAIL"
            elif variant == "FIXED_LONG":
                evidence = "FEASIBLE_TIMING" if timing_feasible else "TIMING_FAIL"
            metric_rows.append(
                {
                    **row,
                    "opposite_stress_mean_return": opposite["stress_mean_return"],
                    "fixed_long_stress_mean_return": fixed_long["stress_mean_return"],
                    "evidence": evidence,
                }
            )
    return EarlySessionEvaluation(
        episodes=pd.concat(episode_frames, ignore_index=True),
        metrics=pd.DataFrame(metric_rows),
        annual=pd.DataFrame(annual_rows),
        account=pd.DataFrame(account_rows),
    )
