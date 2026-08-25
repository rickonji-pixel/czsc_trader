"""Event-level exit-signal diagnosis for the frozen EX04 strategy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
import json

import numpy as np
import pandas as pd

from .baselines import resolve_baseline
from .data import load_market_data
from .experiment_archive import validate_experiment_archive
from .ex04_path_attribution_runner import validate_artifact_identity
from .return_only_runner import half_year_periods


_ENDPOINT_REASONS = {
    "ex04_reentry_execution",
    "baseline_exit_execution",
    "window_end_close",
}


def validate_protocol(protocol: Mapping[str, object]) -> None:
    """Reject drift from the preregistered exit-signal diagnosis."""
    if protocol.get("experiment_type") != "ex04_exit_signal_diagnosis":
        raise ValueError("not an EX04 exit signal diagnosis protocol")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("protocol status must remain PRE_REGISTERED")
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise ValueError("visible sample must stop at 2025-12-31")
    if protocol.get("holdout_access_allowed") is not False:
        raise ValueError("holdout access must remain disabled")
    if int(protocol.get("expected_event_count", -1)) != 20:
        raise ValueError("formal diagnosis must contain exactly 20 exit events")
    promotion = protocol.get("promotion")
    if not isinstance(promotion, Mapping) or any(bool(value) for value in promotion.values()):
        raise ValueError("promotion and optimization must remain disabled")
    windows = tuple(str(value) for value in protocol.get("diagnostic_windows", ()))
    losses = tuple(str(value) for value in protocol.get("underperformance_windows", ()))
    controls = tuple(str(value) for value in protocol.get("control_windows", ()))
    if len(windows) != 10 or len(losses) != 3 or set(losses) | set(controls) != set(windows):
        raise ValueError("diagnostic, loss, and control windows differ from preregistration")
    if set(losses) & set(controls):
        raise ValueError("loss and control windows must be disjoint")


def validate_event_evidence(events: pd.DataFrame, *, expected_count: int) -> None:
    """Validate the frozen event cohort before outcome calculation."""
    required = {"event_id", "signal_date", "event_type"}
    if not required <= set(events.columns):
        raise ValueError("exit event evidence columns are incomplete")
    if len(events) != int(expected_count):
        raise ValueError(f"exit event count must be exactly {expected_count}")
    if events["event_id"].astype(str).duplicated().any():
        raise ValueError("exit event IDs contain duplicates")
    if not events["event_type"].astype(str).eq("early_ex04_exit").all():
        raise ValueError("source event cohort contains a different event type")
    dates = events["signal_date"].astype(str)
    if dates.str.contains("2026", regex=False).any():
        raise ValueError("exit event evidence contains a 2026 date")


def _normalized_prices(prices: pd.DataFrame) -> pd.DataFrame:
    frame = prices.copy()
    if "dt" in frame.columns:
        frame = frame.set_index("dt")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index), name="dt")
    frame = frame.sort_index()
    if not {"open", "close"} <= set(frame.columns):
        raise ValueError("prices require open and close columns")
    if frame[["open", "close"]].isna().any().any() or (
        frame[["open", "close"]] <= 0.0
    ).any().any():
        raise ValueError("prices must be finite positive values")
    return frame


def replay_exit_counterfactual(
    prices: pd.DataFrame,
    execution_date: pd.Timestamp,
    endpoint_date: pd.Timestamp,
    endpoint_reason: str,
    *,
    fee_rate: float,
) -> tuple[dict[str, object], pd.DataFrame]:
    """Replay cash versus uninterrupted holding from one divergent exit."""
    frame = _normalized_prices(prices)
    start = pd.Timestamp(execution_date)
    end = pd.Timestamp(endpoint_date)
    if endpoint_reason not in _ENDPOINT_REASONS:
        raise ValueError("unknown exit counterfactual endpoint")
    if start not in frame.index or end not in frame.index or end < start:
        raise ValueError("counterfactual dates are outside available prices")
    fee = float(fee_rate)
    if not 0.0 <= fee < 1.0:
        raise ValueError("fee rate must be between zero and one")
    start_open = float(frame.loc[start, "open"])
    actual_cash = 1.0 - fee
    rows: list[dict[str, object]] = []

    def add_row(
        date: pd.Timestamp,
        point: str,
        actual: float,
        continued: float,
    ) -> None:
        cumulative = float(np.log(continued / actual))
        previous = float(rows[-1]["cumulative_log_advantage"]) if rows else 0.0
        rows.append(
            {
                "date": pd.Timestamp(date),
                "valuation_point": point,
                "actual_wealth": float(actual),
                "continued_wealth": float(continued),
                "cumulative_log_advantage": cumulative,
                "incremental_log_advantage": cumulative - previous,
            }
        )

    add_row(start, "exit_open", actual_cash, 1.0)
    if endpoint_reason == "window_end_close":
        close_dates = frame.index[(frame.index >= start) & (frame.index <= end)]
    else:
        close_dates = frame.index[(frame.index >= start) & (frame.index < end)]
    path_marks = [1.0]
    for date in close_dates:
        continued = float(frame.loc[date, "close"]) / start_open
        point = "endpoint_close" if date == end else "daily_close"
        add_row(pd.Timestamp(date), point, actual_cash, continued)
        path_marks.append(continued)

    if endpoint_reason == "ex04_reentry_execution":
        actual_terminal = actual_cash / (1.0 + fee)
        continued_terminal = float(frame.loc[end, "open"]) / start_open
        add_row(end, "endpoint_open", actual_terminal, continued_terminal)
        path_marks.append(continued_terminal)
    elif endpoint_reason == "baseline_exit_execution":
        actual_terminal = actual_cash
        continued_mark = float(frame.loc[end, "open"]) / start_open
        continued_terminal = continued_mark * (1.0 - fee)
        add_row(end, "endpoint_open", actual_terminal, continued_terminal)
        path_marks.append(continued_mark)
    else:
        actual_terminal = actual_cash
        continued_terminal = float(frame.loc[end, "close"]) / start_open

    ledger = pd.DataFrame(rows)
    log_advantage = float(np.log(continued_terminal / actual_terminal))
    residual = float(ledger["incremental_log_advantage"].sum() - log_advantage)
    summary = {
        "execution_date": start,
        "endpoint_date": end,
        "endpoint_reason": endpoint_reason,
        "trading_days": int(
            ((frame.index >= start) & (frame.index <= end)).sum()
        ),
        "actual_terminal_wealth": actual_terminal,
        "continued_terminal_wealth": continued_terminal,
        "counterfactual_log_advantage": log_advantage,
        "max_favorable_excursion": float(max(path_marks) - 1.0),
        "max_adverse_excursion": float(min(path_marks) - 1.0),
        "closure_residual": residual,
    }
    return summary, ledger


def label_exit(log_advantage: float, materiality: float) -> str:
    """Assign the preregistered strict materiality label."""
    value = float(log_advantage)
    threshold = float(materiality)
    if threshold <= 0.0:
        raise ValueError("exit label materiality must be positive")
    if value > threshold:
        return "false_exit"
    if value < -threshold:
        return "protective_exit"
    return "neutral_exit"


def cliffs_delta(left: Iterable[float], right: Iterable[float]) -> float:
    """Return pairwise dominance with ties contributing zero."""
    left_values = np.asarray(tuple(float(value) for value in left), dtype=float)
    right_values = np.asarray(tuple(float(value) for value in right), dtype=float)
    if left_values.size == 0 or right_values.size == 0:
        raise ValueError("Cliff's delta requires two non-empty samples")
    differences = left_values[:, None] - right_values[None, :]
    return float((np.sum(differences > 0.0) - np.sum(differences < 0.0)) / differences.size)


def _largest_share(values: pd.Series) -> float:
    positive = values.astype(float).loc[values.astype(float) > 0.0]
    total = float(positive.sum())
    return float(positive.max() / total) if total > 0.0 else 0.0


def summarize_contexts(
    events: pd.DataFrame,
    axes: Sequence[str],
    rules: Mapping[str, object],
) -> pd.DataFrame:
    """Summarize each registered categorical axis without combinations."""
    required = {"window", "outcome_label", "counterfactual_log_advantage"} | set(axes)
    if not required <= set(events.columns):
        raise ValueError("context event columns are incomplete")
    minimum_events = int(rules["minimum_non_neutral_events"])
    minimum_windows = int(rules["minimum_windows"])
    minimum_rate = float(rules["minimum_target_rate"])
    maximum_share = float(rules["maximum_single_event_share"])
    rows: list[dict[str, object]] = []
    for axis in axes:
        for value, group in events.groupby(axis, dropna=False, sort=False):
            material = group.loc[group["outcome_label"].ne("neutral_exit")]
            false = material.loc[material["outcome_label"].eq("false_exit")]
            protective = material.loc[material["outcome_label"].eq("protective_exit")]
            non_neutral = len(material)
            false_rate = len(false) / non_neutral if non_neutral else 0.0
            protective_rate = len(protective) / non_neutral if non_neutral else 0.0
            false_share = _largest_share(false["counterfactual_log_advantage"])
            protective_share = _largest_share(
                -protective["counterfactual_log_advantage"].astype(float)
            )
            net = float(group["counterfactual_log_advantage"].astype(float).sum())
            windows = int(material["window"].astype(str).nunique())
            common = non_neutral >= minimum_events and windows >= minimum_windows
            rows.append(
                {
                    "axis": axis,
                    "axis_value": str(value),
                    "event_count": len(group),
                    "non_neutral_events": non_neutral,
                    "window_count": windows,
                    "false_exit_count": len(false),
                    "protective_exit_count": len(protective),
                    "neutral_exit_count": int(group["outcome_label"].eq("neutral_exit").sum()),
                    "false_exit_rate": false_rate,
                    "protective_exit_rate": protective_rate,
                    "net_counterfactual_log_advantage": net,
                    "false_exit_max_single_share": false_share,
                    "protective_exit_max_single_share": protective_share,
                    "false_exit_context": bool(
                        common
                        and false_rate >= minimum_rate
                        and net > 0.0
                        and false_share <= maximum_share
                    ),
                    "protective_exit_context": bool(
                        common
                        and protective_rate >= minimum_rate
                        and net < 0.0
                        and protective_share <= maximum_share
                    ),
                }
            )
    return pd.DataFrame(rows)


def exit_concentration(
    events: pd.DataFrame, rules: Mapping[str, object]
) -> dict[str, object]:
    """Measure concentration only among material false exits."""
    values = (
        events.loc[
            events["outcome_label"].eq("false_exit"),
            "counterfactual_log_advantage",
        ]
        .astype(float)
        .sort_values(ascending=False)
    )
    total = float(values.sum())
    top1 = float(values.head(1).sum() / total) if total > 0.0 else 0.0
    top3 = float(values.head(3).sum() / total) if total > 0.0 else 0.0
    concentrated = top1 >= float(rules["top1_share"]) or top3 >= float(
        rules["top3_share"]
    )
    return {
        "false_exit_count": len(values),
        "total_false_exit_log_loss": total,
        "top1_share": top1,
        "top3_share": top3,
        "concentrated": bool(concentrated),
    }


def classify_exit_quality(
    events: pd.DataFrame,
    contexts: pd.DataFrame,
    features: pd.DataFrame,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Apply the frozen exit-quality machine classification order."""
    validate_protocol(protocol)
    required = {"event_id", "window", "outcome_label", "counterfactual_log_advantage"}
    if not required <= set(events.columns):
        raise ValueError("exit classification evidence columns are incomplete")
    counts = events["outcome_label"].value_counts()
    false_count = int(counts.get("false_exit", 0))
    protective_count = int(counts.get("protective_exit", 0))
    minimum_class = int(protocol["minimum_material_events_per_class"])
    concentration = exit_concentration(events, protocol["concentration"])
    if (
        len(events) != int(protocol["expected_event_count"])
        or false_count < minimum_class
        or protective_count < minimum_class
    ):
        classification = "insufficient_exit_evidence"
    elif bool(concentration["concentrated"]):
        classification = "path_concentrated_exit_failure"
    else:
        material = events.loc[events["outcome_label"].ne("neutral_exit")]
        systematic = protocol["systematic_failure"]
        false_rate = false_count / len(material) if len(material) else 0.0
        false_windows = int(
            events.loc[events["outcome_label"].eq("false_exit"), "window"]
            .astype(str)
            .nunique()
        )
        window_net = events.groupby("window")["counterfactual_log_advantage"].sum()
        positive_windows = int(window_net.gt(0.0).sum())
        if (
            false_rate >= float(systematic["minimum_false_exit_rate"])
            and false_windows >= int(systematic["minimum_false_exit_windows"])
            and positive_windows >= int(systematic["minimum_positive_net_windows"])
        ):
            classification = "systematically_poor_exit_signal"
        else:
            has_false_context = bool(
                not contexts.empty
                and contexts.get("false_exit_context", pd.Series(dtype=bool)).astype(bool).any()
            )
            has_protective_context = bool(
                not contexts.empty
                and contexts.get("protective_exit_context", pd.Series(dtype=bool))
                .astype(bool)
                .any()
            )
            stable_feature = bool(
                not features.empty
                and features.get("stable_large_effect", pd.Series(dtype=bool))
                .astype(bool)
                .any()
            )
            classification = (
                "context_dependent_exit_quality"
                if (has_false_context and has_protective_context) or stable_feature
                else "mixed_exit_quality"
            )
    return {
        "classification": classification,
        "event_count": len(events),
        "false_exit_count": false_count,
        "protective_exit_count": protective_count,
        "neutral_exit_count": int(counts.get("neutral_exit", 0)),
        "concentration": concentration,
    }


def validate_source_archive(
    repository_root: Path, source: Mapping[str, object]
) -> dict[str, object]:
    """Validate EX03 and its portable manifest hashes."""
    source_dir = Path(repository_root) / str(source.get("path", ""))
    manifest = validate_experiment_archive(source_dir)
    if manifest.get("experiment_id") != source.get("experiment_id"):
        raise ValueError("source archive experiment identity differs from preregistration")
    if manifest.get("status") != source.get("status"):
        raise ValueError("source archive status differs from preregistration")
    if manifest.get("holdout_accessed") is not source.get("holdout_accessed"):
        raise ValueError("source archive holdout status differs from preregistration")
    declared = source.get("files")
    files = manifest.get("files")
    if not isinstance(declared, Mapping) or not isinstance(files, Mapping):
        raise ValueError("source archive file identities are missing")
    for relative, expected in declared.items():
        record = files.get(str(relative))
        if not isinstance(record, Mapping) or record.get("sha256") != str(expected):
            raise ValueError(f"source archive file hash differs: {relative}")
    return manifest


def select_endpoint(
    window_ledger: pd.DataFrame,
    execution_date: pd.Timestamp,
    endpoint_priority: Sequence[str],
) -> tuple[pd.Timestamp, str]:
    """Choose the first state-convergence date with deterministic tie priority."""
    required = {"date", "baseline_execution_position", "ex04_execution_position"}
    if not required <= set(window_ledger.columns):
        raise ValueError("window ledger lacks execution positions")
    ledger = window_ledger.copy()
    ledger["date"] = pd.to_datetime(ledger["date"])
    ledger = ledger.sort_values("date")
    start = pd.Timestamp(execution_date)
    current = ledger.loc[ledger["date"].eq(start)]
    if len(current) != 1:
        raise ValueError("exit execution date is missing or duplicated in ledger")
    if float(current.iloc[0]["baseline_execution_position"]) != 1.0 or float(
        current.iloc[0]["ex04_execution_position"]
    ) != 0.0:
        raise ValueError("source event is not a divergent executed exit")
    future = ledger.loc[ledger["date"] > start]
    candidates: dict[str, pd.Timestamp] = {}
    reentry = future.loc[future["ex04_execution_position"].astype(float).eq(1.0), "date"]
    if not reentry.empty:
        candidates["ex04_reentry_execution"] = pd.Timestamp(reentry.iloc[0])
    baseline_exit = future.loc[
        future["baseline_execution_position"].astype(float).eq(0.0), "date"
    ]
    if not baseline_exit.empty:
        candidates["baseline_exit_execution"] = pd.Timestamp(baseline_exit.iloc[0])
    if candidates:
        first_date = min(candidates.values())
        tied = {reason for reason, date in candidates.items() if date == first_date}
        for reason in endpoint_priority:
            if reason in tied:
                return first_date, str(reason)
        raise ValueError("endpoint priority omits a convergence reason")
    return pd.Timestamp(ledger["date"].iloc[-1]), "window_end_close"


def build_continuous_feature_separation(
    events: pd.DataFrame,
    features: Sequence[str],
    rules: Mapping[str, object],
) -> pd.DataFrame:
    """Report pooled effect and cross-window direction without choosing thresholds."""
    required = {"window", "outcome_label"} | set(features)
    if not required <= set(events.columns):
        raise ValueError("continuous feature evidence columns are incomplete")
    minimum_class = int(rules["minimum_events_per_class"])
    minimum_windows = int(rules["minimum_comparable_windows"])
    large = float(rules["large_cliffs_delta"])
    require_direction = bool(rules["require_consistent_window_direction"])
    material = events.loc[events["outcome_label"].isin(("false_exit", "protective_exit"))]
    rows: list[dict[str, object]] = []
    for feature in features:
        false = material.loc[material["outcome_label"].eq("false_exit"), feature].astype(float)
        protective = material.loc[
            material["outcome_label"].eq("protective_exit"), feature
        ].astype(float)
        delta = cliffs_delta(false, protective) if len(false) and len(protective) else 0.0
        window_differences: list[float] = []
        for _, group in material.groupby("window", sort=False):
            left = group.loc[group["outcome_label"].eq("false_exit"), feature].astype(float)
            right = group.loc[
                group["outcome_label"].eq("protective_exit"), feature
            ].astype(float)
            if len(left) and len(right):
                window_differences.append(float(left.median() - right.median()))
        pooled_sign = int(np.sign(delta))
        consistent = bool(
            window_differences
            and pooled_sign != 0
            and all(int(np.sign(value)) == pooled_sign for value in window_differences)
        )
        stable = (
            len(false) >= minimum_class
            and len(protective) >= minimum_class
            and len(window_differences) >= minimum_windows
            and abs(delta) >= large
            and (consistent if require_direction else True)
        )
        rows.append(
            {
                "feature": feature,
                "false_exit_events": len(false),
                "protective_exit_events": len(protective),
                "false_exit_median": float(false.median()) if len(false) else 0.0,
                "protective_exit_median": float(protective.median()) if len(protective) else 0.0,
                "cliffs_delta": delta,
                "comparable_windows": len(window_differences),
                "consistent_window_direction": consistent,
                "stable_large_effect": bool(stable),
            }
        )
    return pd.DataFrame(rows)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _finite_numeric(frame: pd.DataFrame, label: str) -> None:
    numeric = frame.select_dtypes(include=[np.number])
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError(f"{label} contains non-finite values")


def _holding_days_before_exit(
    window_ledger: pd.DataFrame, execution_date: pd.Timestamp
) -> int:
    ledger = window_ledger.copy()
    ledger["date"] = pd.to_datetime(ledger["date"])
    ledger = ledger.sort_values("date")
    prior = ledger.loc[ledger["date"] < pd.Timestamp(execution_date)]
    count = 0
    for value in reversed(prior["ex04_execution_position"].astype(float).tolist()):
        if value != 1.0:
            break
        count += 1
    return count


def _holding_age_bin(days: int, rules: Mapping[str, object]) -> str:
    if days <= int(rules["short_max_days"]):
        return "short"
    if days <= int(rules["medium_max_days"]):
        return "medium"
    return "long"


def _contribution_features(group_rows: pd.DataFrame) -> pd.DataFrame:
    required = {
        "event_id",
        "group",
        "baseline_weighted_contribution",
        "ex04_weighted_contribution",
    }
    if not required <= set(group_rows.columns):
        raise ValueError("group contribution evidence columns are incomplete")
    if group_rows["event_id"].astype(str).duplicated(keep=False).groupby(
        group_rows["event_id"].astype(str)
    ).sum().ne(3).any():
        raise ValueError("each exit event must have exactly three group rows")
    if set(group_rows["group"].astype(str)) != {"structure", "trend", "volume_position"}:
        raise ValueError("exit events contain different factor groups")
    baseline = group_rows.pivot(
        index="event_id", columns="group", values="baseline_weighted_contribution"
    )
    ex04 = group_rows.pivot(
        index="event_id", columns="group", values="ex04_weighted_contribution"
    )
    output = pd.DataFrame(index=baseline.index)
    for group in ("structure", "trend", "volume_position"):
        output[f"baseline_{group}_contribution"] = baseline[group].astype(float)
        output[f"ex04_{group}_contribution"] = ex04[group].astype(float)
        output[f"{group}_contribution_gap"] = baseline[group].astype(float) - ex04[
            group
        ].astype(float)
    return output.reset_index()


def _window_summary(
    events: pd.DataFrame,
    windows: Sequence[str],
    underperformance: set[str],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for window in windows:
        group = events.loc[events["window"].astype(str).eq(window)]
        false = group.loc[group["outcome_label"].eq("false_exit")]
        protective = group.loc[group["outcome_label"].eq("protective_exit")]
        rows.append(
            {
                "window": window,
                "cohort": "underperformance" if window in underperformance else "control",
                "event_count": len(group),
                "false_exit_count": len(false),
                "protective_exit_count": len(protective),
                "neutral_exit_count": int(group["outcome_label"].eq("neutral_exit").sum()),
                "false_exit_log_loss": float(
                    false["counterfactual_log_advantage"].astype(float).sum()
                ),
                "protective_exit_log_benefit": float(
                    -protective["counterfactual_log_advantage"].astype(float).sum()
                ),
                "net_counterfactual_log_advantage": float(
                    group["counterfactual_log_advantage"].astype(float).sum()
                ),
            }
        )
    return pd.DataFrame(rows)


def run_exit_signal_diagnosis(
    raw_dir: Path,
    experiment_dir: Path,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Run the tracked-event EX04 exit diagnosis without optimization or holdout."""
    validate_protocol(protocol)
    experiment_dir = Path(experiment_dir).resolve()
    repository_root = experiment_dir.parent.parent
    artifacts = experiment_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    source = protocol.get("source_archive")
    if not isinstance(source, Mapping):
        raise ValueError("source archive identity is missing")
    source_manifest = validate_source_archive(repository_root, source)
    source_dir = repository_root / str(source["path"])

    general = protocol.get("general_baseline")
    if not isinstance(general, Mapping):
        raise ValueError("general baseline identity is missing")
    baseline = resolve_baseline(
        repository_root / "configs" / "rule_baselines", str(general["version"])
    )
    if baseline.sha256 != str(general["sha256"]):
        raise ValueError("general baseline hash differs from preregistration")
    research = protocol.get("research_object")
    if not isinstance(research, Mapping):
        raise ValueError("research object identity is missing")
    research_digest = validate_artifact_identity(
        repository_root / str(research["path"]),
        str(research["sha256"]),
        label="EX04",
    )

    cutoff = pd.Timestamp(str(protocol["visible_sample_end"]))
    data = load_market_data(
        Path(raw_dir), str(protocol["symbol"]), str(protocol["asset_type"]), cutoff=cutoff
    )
    if any("2026" in str(name) for name in data.hashes):
        raise AssertionError("2026 file entered exit signal diagnosis inputs")
    prices = _normalized_prices(data.daily)

    events_all = pd.read_csv(source_dir / "artifacts" / "decision_events.csv")
    events = events_all.loc[
        events_all["event_type"].astype(str).eq(str(protocol["event_type"]))
    ].copy()
    validate_event_evidence(events, expected_count=int(protocol["expected_event_count"]))
    events["signal_date"] = pd.to_datetime(events["signal_date"])
    events["execution_date"] = pd.to_datetime(events["execution_date"])
    if events[["signal_date", "execution_date"]].max().max() > cutoff:
        raise ValueError("exit event evidence exceeds visible sample cutoff")

    daily_ledger = pd.read_csv(source_dir / "artifacts" / "daily_path_ledger.csv")
    daily_ledger["date"] = pd.to_datetime(daily_ledger["date"])
    group_rows = pd.read_csv(source_dir / "artifacts" / "group_score_contributions.csv")
    group_rows = group_rows.loc[
        group_rows["event_id"].astype(str).isin(set(events["event_id"].astype(str)))
    ].copy()
    if len(group_rows) != len(events) * 3:
        raise ValueError("exit event group contribution row count differs from protocol")
    contribution_features = _contribution_features(group_rows)
    enriched = events.merge(contribution_features, on="event_id", how="left", validate="one_to_one")
    enriched["score_gap"] = enriched["baseline_score"].astype(float) - enriched[
        "ex04_score"
    ].astype(float)
    enriched["ex04_exit_margin"] = enriched["ex04_margin"].astype(float)

    summaries: list[dict[str, object]] = []
    path_frames: list[pd.DataFrame] = []
    tolerance = float(protocol["path_closure_tolerance"])
    priorities = tuple(str(value) for value in protocol["endpoint_priority"])
    age_rules = protocol["holding_age_bins"]
    if not isinstance(age_rules, Mapping):
        raise ValueError("holding age rules are missing")
    materiality = float(protocol["label_materiality_log_wealth"])
    for row in enriched.to_dict(orient="records"):
        event_id = str(row["event_id"])
        window = str(row["window"])
        execution_date = pd.Timestamp(row["execution_date"])
        window_ledger = daily_ledger.loc[daily_ledger["window"].astype(str).eq(window)].copy()
        endpoint_date, endpoint_reason = select_endpoint(
            window_ledger, execution_date, priorities
        )
        replay, path = replay_exit_counterfactual(
            prices,
            execution_date,
            endpoint_date,
            endpoint_reason,
            fee_rate=float(protocol["fee_rate"]),
        )
        if abs(float(replay["closure_residual"])) > tolerance:
            raise AssertionError(f"{event_id}: counterfactual path does not close")
        holding_days = _holding_days_before_exit(window_ledger, execution_date)
        trend_value = float(row["ex04_trend_contribution"])
        trend_sign = "positive" if trend_value > 0.0 else "negative" if trend_value < 0.0 else "zero"
        summary = {
            **row,
            **replay,
            "holding_days": holding_days,
            "holding_age_bin": _holding_age_bin(holding_days, age_rules),
            "ex04_trend_contribution_sign": trend_sign,
            "outcome_label": label_exit(
                float(replay["counterfactual_log_advantage"]), materiality
            ),
            "cohort": (
                "underperformance"
                if window in set(str(value) for value in protocol["underperformance_windows"])
                else "control"
            ),
        }
        summaries.append(summary)
        path.insert(0, "event_id", event_id)
        path.insert(1, "window", window)
        path_frames.append(path)
    event_frame = pd.DataFrame(summaries)
    path_ledger = pd.concat(path_frames, ignore_index=True)

    contexts = summarize_contexts(
        event_frame,
        tuple(str(value) for value in protocol["categorical_axes"]),
        protocol["context_support"],
    )
    features = build_continuous_feature_separation(
        event_frame,
        tuple(str(value) for value in protocol["continuous_features"]),
        protocol["continuous_separation"],
    )
    windows = tuple(str(value) for value in protocol["diagnostic_windows"])
    if tuple(half_year_periods(2021, 2025)) != windows:
        raise ValueError("diagnostic windows differ from frozen half-year periods")
    window_summary = _window_summary(
        event_frame,
        windows,
        set(str(value) for value in protocol["underperformance_windows"]),
    )
    concentration = exit_concentration(event_frame, protocol["concentration"])
    classification = classify_exit_quality(event_frame, contexts, features, protocol)

    for label, frame in (
        ("exit event counterfactuals", event_frame),
        ("exit event path ledger", path_ledger),
        ("exit quality by window", window_summary),
        ("exit quality by context", contexts),
        ("continuous feature separation", features),
    ):
        _finite_numeric(frame, label)
    max_residual = float(event_frame["closure_residual"].astype(float).abs().max())
    if max_residual > tolerance:
        raise AssertionError("formal event counterfactuals do not close")

    identity = {
        "status": "PASS",
        "source_archive": {
            "experiment_id": source_manifest["experiment_id"],
            "status": source_manifest["status"],
            "holdout_accessed": source_manifest["holdout_accessed"],
            "file_sha256": dict(source["files"]),
        },
        "general_baseline": {"version": baseline.version, "sha256": baseline.sha256},
        "research_object_path": str(research["path"]),
        "research_object_sha256": research_digest,
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
    }
    metrics = {
        "status": "COMPLETE",
        "experiment_id": str(protocol["experiment_id"]),
        "visible_sample_end": str(cutoff.date()),
        "visible_data_hashes": data.hashes,
        "holdout_accessed": False,
        "frozen_challenger": None,
        "event_count": len(event_frame),
        "path_ledger_rows": len(path_ledger),
        "endpoint_counts": event_frame["endpoint_reason"].value_counts().sort_index().to_dict(),
        "outcome_counts": event_frame["outcome_label"].value_counts().sort_index().to_dict(),
        "max_absolute_closure_residual": max_residual,
        "qualifying_false_contexts": int(contexts["false_exit_context"].astype(bool).sum()),
        "qualifying_protective_contexts": int(
            contexts["protective_exit_context"].astype(bool).sum()
        ),
        "stable_large_effect_features": int(features["stable_large_effect"].astype(bool).sum()),
        "concentration": concentration,
        "exit_quality_classification": classification,
    }
    _write_csv(artifacts / "exit_event_counterfactuals.csv", event_frame)
    _write_csv(artifacts / "exit_event_path_ledger.csv", path_ledger)
    _write_csv(artifacts / "exit_quality_by_window.csv", window_summary)
    _write_csv(artifacts / "exit_quality_by_context.csv", contexts)
    _write_csv(artifacts / "continuous_feature_separation.csv", features)
    _write_json(artifacts / "exit_concentration.json", concentration)
    _write_json(artifacts / "exit_quality_classification.json", classification)
    _write_json(artifacts / "identity_audit.json", identity)
    _write_json(artifacts / "metrics.json", metrics)
    return metrics
