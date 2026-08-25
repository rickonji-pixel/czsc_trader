"""Trading-path attribution for EX04 relative to the registered champion."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
import json

import numpy as np
import pandas as pd

from .backtest import PeriodBacktestResult, run_period_backtests
from .baselines import resolve_baseline
from .data import load_market_data
from .ex04_attribution_runner import market_regimes
from .factors import generate_factor_frame, signal_groups
from .four_layer import (
    positions_from_scores,
    score_four_layer,
    validate_fixed_factor_weights,
)
from .four_layer_runner import _equivalence
from .return_only_runner import half_year_periods
from .rules import Rule


def validate_artifact_identity(path: Path, expected_sha256: str, *, label: str) -> str:
    """Return the byte hash only when it matches the preregistered identity."""
    digest = sha256(Path(path).read_bytes()).hexdigest()
    if digest != str(expected_sha256):
        raise ValueError(f"{label} artifact hash differs from preregistration")
    return digest


def validate_source_evidence(
    repository_root: Path, source: Mapping[str, object]
) -> dict[str, str]:
    """Validate every declared source-evidence path/hash pair."""
    paths = {key.removesuffix("_path"): value for key, value in source.items() if key.endswith("_path")}
    hashes = {
        key.removesuffix("_sha256"): value
        for key, value in source.items()
        if key.endswith("_sha256")
    }
    if not paths or set(paths) != set(hashes):
        raise ValueError("source evidence path/hash declarations are incomplete")
    validated: dict[str, str] = {}
    for name, relative in paths.items():
        try:
            validated[name] = validate_artifact_identity(
                Path(repository_root) / str(relative),
                str(hashes[name]),
                label=f"source evidence {name}",
            )
        except ValueError as exc:
            raise ValueError(f"source evidence {name} hash differs from preregistration") from exc
    return validated


def reject_holdout_hashes(hashes: Mapping[str, str]) -> None:
    """Reject even metadata-level evidence that a 2026 file was loaded."""
    if any("2026" in str(name) for name in hashes):
        raise AssertionError("2026 file entered EX04 path attribution inputs")


def validate_path_ledger(
    ledger: pd.DataFrame,
    variant_metrics: pd.DataFrame,
    windows: tuple[str, ...],
    *,
    tolerance: float,
) -> dict[str, float]:
    """Validate finite, complete, exactly closing per-window path evidence."""
    required_ledger = {"window", "log_wealth_delta", "path_mechanism"}
    required_metrics = {"window", "variant", "strategy_return"}
    if not required_ledger <= set(ledger.columns) or not required_metrics <= set(
        variant_metrics.columns
    ):
        raise ValueError("path ledger or variant metrics columns are incomplete")
    values = ledger["log_wealth_delta"].astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("path ledger contains non-finite values")
    if ledger["path_mechanism"].astype(str).eq("unclassified_baseline_only").any():
        raise ValueError("path ledger contains unclassified baseline-only days")
    residuals: dict[str, float] = {}
    for window in windows:
        rows = variant_metrics.loc[variant_metrics["window"].astype(str).eq(window)]
        indexed = rows.set_index(rows["variant"].astype(str))
        if set(indexed.index) < {"baseline", "ex04"}:
            raise ValueError(f"{window}: baseline or EX04 metrics are missing")
        terminal = np.log1p(float(indexed.loc["ex04", "strategy_return"])) - np.log1p(
            float(indexed.loc["baseline", "strategy_return"])
        )
        daily = float(
            ledger.loc[ledger["window"].astype(str).eq(window), "log_wealth_delta"]
            .astype(float)
            .sum()
        )
        residual = daily - terminal
        if abs(residual) > float(tolerance):
            raise AssertionError(f"{window}: path ledger does not close; residual={residual}")
        residuals[window] = residual
    return residuals


def validate_protocol(protocol: Mapping[str, object]) -> None:
    """Reject drift from the frozen EX04 path-diagnosis protocol."""
    if protocol.get("experiment_type") != "ex04_path_attribution":
        raise ValueError("not an EX04 path attribution protocol")
    if protocol.get("status") != "PRE_REGISTERED":
        raise ValueError("protocol status must remain PRE_REGISTERED")
    if protocol.get("visible_sample_end") != "2025-12-31":
        raise ValueError("visible sample must stop at 2025-12-31")
    if protocol.get("holdout_access_allowed") is not False:
        raise ValueError("holdout access must remain disabled")
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


def positions_from_dual_scores(
    entry_scores: pd.Series,
    enter: float,
    exit_scores: pd.Series,
    exit: float,
    state_rule: Rule,
) -> pd.Series:
    """Evolve one causal state using separate frozen entry and exit scores."""
    if not entry_scores.index.equals(exit_scores.index):
        raise ValueError("entry and exit score indices must match exactly")
    if float(exit) >= float(enter):
        raise ValueError("exit threshold must be below entry threshold")
    if state_rule.entry_gate != "none":
        raise ValueError("dual-score attribution requires entry_gate none")
    position = 0.0
    confirmations = 0
    exit_confirmations = 0
    holding_days = 0
    output: list[float] = []
    for entry_score, exit_score in zip(
        entry_scores.astype(float), exit_scores.astype(float), strict=True
    ):
        if position == 0.0:
            confirmations = confirmations + 1 if entry_score >= enter else 0
            if confirmations >= state_rule.confirm_days:
                position = 1.0
                holding_days = 1
                confirmations = 0
                exit_confirmations = 0
        else:
            eligible = holding_days >= state_rule.min_hold_days
            exit_confirmations = exit_confirmations + 1 if eligible and exit_score <= exit else 0
            if exit_confirmations >= state_rule.exit_confirm_days:
                position = 0.0
                holding_days = 0
                confirmations = 0
                exit_confirmations = 0
            else:
                holding_days += 1
        output.append(position)
    return pd.Series(output, index=entry_scores.index, name="target_position", dtype=float)


def _true_segments(mask: pd.Series) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    if not mask.any():
        return []
    groups = mask.ne(mask.shift(fill_value=False)).cumsum()
    return [
        (pd.Timestamp(values.index[0]), pd.Timestamp(values.index[-1]))
        for _, values in mask.loc[mask].groupby(groups.loc[mask])
    ]


def label_baseline_only_days(
    baseline_execution: pd.Series, ex04_execution: pd.Series
) -> tuple[pd.DataFrame, pd.Series]:
    """Label every day and summarize each contiguous baseline holding episode."""
    if not baseline_execution.index.equals(ex04_execution.index):
        raise ValueError("execution position indices must match exactly")
    baseline = baseline_execution.astype(float)
    ex04 = ex04_execution.astype(float)
    if not baseline.isin((0.0, 1.0)).all() or not ex04.isin((0.0, 1.0)).all():
        raise ValueError("execution positions must be long/cash values")
    labels = pd.Series(index=baseline.index, dtype="string", name="path_mechanism")
    labels.loc[baseline.eq(0.0) & ex04.eq(0.0)] = "both_cash"
    labels.loc[baseline.eq(1.0) & ex04.eq(1.0)] = "both_long"
    labels.loc[baseline.eq(0.0) & ex04.eq(1.0)] = "ex04_only"
    baseline_segments = _true_segments(baseline.eq(1.0))
    episode_rows: list[dict[str, object]] = []
    for episode_number, (start, end) in enumerate(baseline_segments, start=1):
        episode_index = baseline.index[(baseline.index >= start) & (baseline.index <= end)]
        overlap = ex04.loc[episode_index].eq(1.0)
        ex04_segments = _true_segments(overlap)
        mechanisms: list[str] = []
        if not ex04_segments:
            labels.loc[episode_index] = "fully_missed_entry"
            mechanisms.append("fully_missed_entry")
        else:
            first = ex04_segments[0][0]
            last = ex04_segments[-1][1]
            missing = overlap.loc[~overlap].index
            for date in missing:
                if date < first:
                    label = "late_entry"
                elif date > last:
                    label = "early_exit"
                else:
                    label = "interrupted_holding"
                labels.loc[date] = label
                if label not in mechanisms:
                    mechanisms.append(label)
        if labels.loc[episode_index].isna().any():
            labels.loc[episode_index[labels.loc[episode_index].isna()]] = (
                "unclassified_baseline_only"
            )
            mechanisms.append("unclassified_baseline_only")
        episode_rows.append(
            {
                "episode_id": episode_number,
                "start": start,
                "end": end,
                "trading_days": len(episode_index),
                "ex04_overlap_days": int(overlap.sum()),
                "ex04_segment_count": len(ex04_segments),
                "episode_class": "+".join(mechanisms) if mechanisms else "fully_covered",
            }
        )
    if labels.isna().any():
        raise AssertionError("path labeling left trading days unclassified")
    return pd.DataFrame(episode_rows), labels


def classify_score_block(
    kind: str,
    baseline_score: float,
    ex04_score: float,
    *,
    baseline_enter: float,
    ex04_enter: float,
    baseline_exit: float,
    ex04_exit: float,
) -> str:
    """Attribute a divergent entry or exit condition to weights and thresholds."""
    if kind == "entry":
        baseline_condition = baseline_score >= baseline_enter
        combined_condition = ex04_score >= ex04_enter
        weight_blocks = ex04_score < baseline_enter
        threshold_blocks = baseline_score < ex04_enter
    elif kind == "exit":
        baseline_condition = baseline_score > baseline_exit
        combined_condition = ex04_score <= ex04_exit
        weight_blocks = ex04_score <= baseline_exit
        threshold_blocks = baseline_score <= ex04_exit
    else:
        raise ValueError("score block kind must be entry or exit")
    if not baseline_condition or (combined_condition if kind == "entry" else not combined_condition):
        return "state_path_not_score"
    if weight_blocks and threshold_blocks:
        return "independent_double_block"
    if weight_blocks:
        return "weight_block"
    if threshold_blocks:
        return "threshold_block"
    return "joint_margin_block"


def validate_log_wealth_closure(
    baseline_daily_returns: pd.Series,
    ex04_daily_returns: pd.Series,
    baseline_total_return: float,
    ex04_total_return: float,
    *,
    tolerance: float,
) -> float:
    """Validate that daily log-return differences reconcile to terminal wealth."""
    if not baseline_daily_returns.index.equals(ex04_daily_returns.index):
        raise ValueError("daily return indices must match exactly")
    if (baseline_daily_returns <= -1.0).any() or (ex04_daily_returns <= -1.0).any():
        raise ValueError("daily returns must be greater than -100%")
    daily_delta = np.log1p(ex04_daily_returns.astype(float)) - np.log1p(
        baseline_daily_returns.astype(float)
    )
    terminal_delta = np.log1p(float(ex04_total_return)) - np.log1p(
        float(baseline_total_return)
    )
    residual = float(daily_delta.sum() - terminal_delta)
    if abs(residual) > float(tolerance):
        raise AssertionError(f"log wealth attribution does not close: residual={residual}")
    return residual


def classify_mechanism(
    mechanism_rows: pd.DataFrame,
    hybrid_rows: pd.DataFrame,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Apply preregistered path-share and causal-hybrid support gates."""
    validate_protocol(protocol)
    required_mechanism = {"window", "mechanism", "negative_log_loss"}
    required_hybrid = {"window", "variant", "return_delta_vs_ex04"}
    if not required_mechanism <= set(mechanism_rows.columns):
        raise ValueError("mechanism contribution rows are incomplete")
    if not required_hybrid <= set(hybrid_rows.columns):
        raise ValueError("hybrid counterfactual rows are incomplete")
    windows = tuple(str(value) for value in protocol["underperformance_windows"])
    family_map = protocol["mechanism_families"]
    rules = protocol["classification"]
    if not isinstance(family_map, Mapping) or not isinstance(rules, Mapping):
        raise ValueError("mechanism family or classification rules are missing")
    opportunity_mechanisms = {
        str(value) for family in ("entry", "exit") for value in family_map[family]
    } | {"unclassified_baseline_only"}
    rows = mechanism_rows.loc[
        mechanism_rows["window"].astype(str).isin(windows)
        & mechanism_rows["mechanism"].astype(str).isin(opportunity_mechanisms)
    ].copy()
    unclassified = float(
        rows.loc[
            rows["mechanism"].eq("unclassified_baseline_only"), "negative_log_loss"
        ].astype(float).sum()
    )
    total = float(rows["negative_log_loss"].astype(float).sum())
    if total <= 0.0 or unclassified > 1e-15:
        return {
            "classification": "insufficient_path_evidence",
            "total_negative_log_loss": total,
            "unclassified_negative_log_loss": unclassified,
        }

    support: dict[str, dict[str, object]] = {}
    variant_for = {
        "entry": "baseline_entry_ex04_exit",
        "exit": "ex04_entry_baseline_exit",
    }
    for family in ("entry", "exit"):
        names = set(str(value) for value in family_map[family])
        family_rows = rows.loc[rows["mechanism"].astype(str).isin(names)]
        family_total = float(family_rows["negative_log_loss"].astype(float).sum())
        pooled_share = family_total / total
        window_shares: dict[str, float] = {}
        for window in windows:
            window_rows = rows.loc[rows["window"].astype(str).eq(window)]
            window_total = float(window_rows["negative_log_loss"].astype(float).sum())
            numerator = float(
                window_rows.loc[
                    window_rows["mechanism"].astype(str).isin(names), "negative_log_loss"
                ].astype(float).sum()
            )
            window_shares[window] = numerator / window_total if window_total > 0.0 else 0.0
        strong_path = (
            pooled_share >= float(rules["pooled_negative_log_share"])
            and sum(
                share >= float(rules["per_window_negative_log_share"])
                for share in window_shares.values()
            )
            >= int(rules["minimum_supported_windows"])
        )
        joint_path = (
            pooled_share >= float(rules["joint_pooled_negative_log_share"])
            and sum(
                share >= float(rules["joint_per_window_negative_log_share"])
                for share in window_shares.values()
            )
            >= int(rules["minimum_supported_windows"])
        )
        variant_rows = hybrid_rows.loc[
            hybrid_rows["window"].astype(str).isin(windows)
            & hybrid_rows["variant"].astype(str).eq(variant_for[family])
        ]
        improvements = variant_rows["return_delta_vs_ex04"].astype(float)
        hybrid_support = (
            len(improvements) == len(windows)
            and int(
                improvements.gt(float(rules["counterfactual_return_improvement"])).sum()
            )
            >= int(rules["minimum_supported_windows"])
            and (
                float(improvements.median()) > 0.0
                if bool(rules["require_positive_median_improvement"])
                else True
            )
        )
        support[family] = {
            "pooled_negative_log_share": pooled_share,
            "window_negative_log_shares": window_shares,
            "strong_path_support": strong_path,
            "joint_path_support": joint_path,
            "hybrid_support": hybrid_support,
            "hybrid_improved_windows": int(
                improvements.gt(float(rules["counterfactual_return_improvement"])).sum()
            ),
            "hybrid_median_return_improvement": (
                float(improvements.median()) if not improvements.empty else 0.0
            ),
        }
    entry = support["entry"]
    exit_ = support["exit"]
    if (
        bool(entry["joint_path_support"])
        and bool(exit_["joint_path_support"])
        and bool(entry["hybrid_support"])
        and bool(exit_["hybrid_support"])
    ):
        classification = "joint_entry_exit_failure"
    elif bool(entry["strong_path_support"]) and bool(entry["hybrid_support"]):
        classification = "entry_failure"
    elif bool(exit_["strong_path_support"]) and bool(exit_["hybrid_support"]):
        classification = "exit_failure"
    else:
        classification = "mixed_path_failure"
    return {
        "classification": classification,
        "total_negative_log_loss": total,
        "unclassified_negative_log_loss": unclassified,
        "entry": entry,
        "exit": exit_,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _normalize_prices(daily: pd.DataFrame) -> pd.DataFrame:
    prices = daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="dt")
    return prices.sort_index()


def _execution_target(
    target: pd.Series, period_index: pd.DatetimeIndex, full_index: pd.DatetimeIndex
) -> pd.Series:
    prior = full_index[full_index < period_index[0]]
    if prior.empty:
        raise ValueError("period has no prior signal date")
    execution = target.reindex(period_index).shift(1)
    execution.iloc[0] = float(target.loc[prior[-1]])
    return execution.astype(float).rename("execution_position")


def _daily_returns(result: PeriodBacktestResult, init_cash: float) -> pd.Series:
    returns = result.equity.astype(float).pct_change(fill_method=None)
    returns.iloc[0] = float(result.equity.iloc[0] / init_cash - 1.0)
    return returns.astype(float)


def _variant_metric_rows(
    results: Mapping[str, Mapping[str, PeriodBacktestResult]],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for variant, windows in results.items():
        for window, result in windows.items():
            metrics = result.metrics
            rows.append(
                {
                    "variant": variant,
                    "window": window,
                    "strategy_return": float(metrics["strategy_return"]),
                    "sharpe": float(metrics["sharpe"]),
                    "max_drawdown": float(metrics["max_drawdown"]),
                    "exposure": float(metrics["exposure"]),
                    "trade_count": int(metrics["trade_count"]),
                    "start": str(metrics["start"]),
                    "end": str(metrics["end"]),
                }
            )
    frame = pd.DataFrame(rows)
    baseline = frame.loc[frame["variant"].eq("baseline")].set_index("window")
    ex04 = frame.loc[frame["variant"].eq("ex04")].set_index("window")
    frame["return_delta_vs_baseline"] = [
        float(value) - float(baseline.loc[window, "strategy_return"])
        for value, window in zip(frame["strategy_return"], frame["window"], strict=True)
    ]
    frame["return_delta_vs_ex04"] = [
        float(value) - float(ex04.loc[window, "strategy_return"])
        for value, window in zip(frame["strategy_return"], frame["window"], strict=True)
    ]
    return frame


def _episode_ids(index: pd.DatetimeIndex, episodes: pd.DataFrame, window: str) -> pd.Series:
    values = pd.Series("", index=index, dtype="string", name="baseline_episode_id")
    for _, episode in episodes.iterrows():
        episode_id = f"{window}:E{int(episode['episode_id']):03d}"
        mask = (index >= pd.Timestamp(episode["start"])) & (
            index <= pd.Timestamp(episode["end"])
        )
        values.loc[mask] = episode_id
    return values


def build_baseline_episode_table(
    episodes: pd.DataFrame, window_ledger: pd.DataFrame, window: str
) -> pd.DataFrame:
    """Attach exact path contributions to window-qualified baseline episodes."""
    formatted = episodes.copy()
    formatted.insert(0, "window", window)
    formatted["baseline_episode_id"] = [
        f"{window}:E{int(value):03d}" for value in formatted.pop("episode_id")
    ]
    contributions = (
        window_ledger.loc[window_ledger["baseline_episode_id"].astype(str).ne("")]
        .groupby("baseline_episode_id", sort=False)
        .agg(
            episode_log_wealth_delta=("log_wealth_delta", "sum"),
            episode_negative_log_loss=(
                "log_wealth_delta",
                lambda values: float((-values.astype(float)).clip(lower=0.0).sum()),
            ),
        )
        .reset_index()
    )
    return formatted.merge(contributions, on="baseline_episode_id", how="left")


def _finite_numeric(frame: pd.DataFrame, label: str) -> None:
    numeric = frame.select_dtypes(include=[np.number])
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError(f"{label} contains non-finite numeric values")


def run_ex04_path_attribution(
    raw_dir: Path,
    baseline_root: Path,
    experiment_dir: Path,
    protocol: Mapping[str, object],
) -> dict[str, object]:
    """Run the preregistered pre-2026 EX04 trading-path diagnosis."""
    validate_protocol(protocol)
    experiment_dir = Path(experiment_dir).resolve()
    repository_root = experiment_dir.parent.parent
    artifacts = experiment_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    source = protocol.get("source_evidence")
    if not isinstance(source, Mapping):
        raise ValueError("source evidence identities are missing")
    source_hashes = validate_source_evidence(repository_root, source)

    general = protocol.get("general_baseline")
    if not isinstance(general, Mapping):
        raise ValueError("general baseline identity is missing")
    baseline = resolve_baseline(Path(baseline_root), str(general["version"]))
    if baseline.sha256 != str(general["sha256"]):
        raise ValueError("general baseline hash differs from preregistration")

    research = protocol.get("research_object")
    if not isinstance(research, Mapping):
        raise ValueError("research object identity is missing")
    frozen_path = repository_root / str(research["path"])
    frozen_digest = validate_artifact_identity(
        frozen_path, str(research["sha256"]), label="EX04"
    )
    frozen = json.loads(frozen_path.read_text(encoding="utf-8-sig"))

    cutoff = pd.Timestamp(str(protocol["visible_sample_end"]))
    data = load_market_data(
        Path(raw_dir), str(protocol["symbol"]), str(protocol["asset_type"]), cutoff=cutoff
    )
    reject_holdout_hashes(data.hashes)
    factor_frame = generate_factor_frame(data).frame
    factors, baseline_weights, equivalence = _equivalence(
        factor_frame, baseline.rule, float(protocol["equivalence_tolerance"])
    )
    factor_names = tuple(str(name) for name in frozen["factor_names"])
    if factor_names != tuple(factors.columns):
        raise ValueError("EX04 factor identities or order differ from preregistration")
    ex04_weights = pd.Series(
        [float(frozen["weights"][name]) for name in factor_names],
        index=factors.columns,
        name="weight",
    )
    validate_fixed_factor_weights(ex04_weights, factors.columns, 0.0)
    ex04_enter = float(frozen["spec"]["enter"])
    ex04_exit = float(frozen["spec"]["exit"])
    baseline_score = score_four_layer(factors, baseline_weights)
    ex04_score = score_four_layer(factors, ex04_weights)

    targets = {
        "baseline": positions_from_scores(
            baseline_score, baseline.rule.enter, baseline.rule.exit, baseline.rule
        ),
        "ex04": positions_from_scores(
            ex04_score, ex04_enter, ex04_exit, baseline.rule
        ),
        "weights_only": positions_from_scores(
            ex04_score, baseline.rule.enter, baseline.rule.exit, baseline.rule
        ),
        "thresholds_only": positions_from_scores(
            baseline_score, ex04_enter, ex04_exit, baseline.rule
        ),
        "baseline_entry_ex04_exit": positions_from_dual_scores(
            baseline_score, baseline.rule.enter, ex04_score, ex04_exit, baseline.rule
        ),
        "ex04_entry_baseline_exit": positions_from_dual_scores(
            ex04_score, ex04_enter, baseline_score, baseline.rule.exit, baseline.rule
        ),
    }
    declared_variants = tuple(str(value) for value in protocol["variants"])
    if tuple(targets) != declared_variants:
        raise ValueError("formal variants differ from preregistration")

    periods = half_year_periods(2021, 2025)
    windows = tuple(str(value) for value in protocol["diagnostic_windows"])
    if tuple(periods) != windows:
        raise ValueError("half-year windows differ from preregistration")
    fee_rate = float(protocol["fee_rate"])
    init_cash = float(protocol["init_cash"])
    results = {
        variant: run_period_backtests(
            data.daily,
            target,
            periods,
            fee_rate=fee_rate,
            init_cash=init_cash,
        )
        for variant, target in targets.items()
    }
    variant_metrics = _variant_metric_rows(results)

    prices = _normalize_prices(data.daily)
    regime_rules = protocol["regime"]
    if not isinstance(regime_rules, Mapping):
        raise ValueError("regime rules are missing")
    regimes = market_regimes(
        prices["close"],
        int(regime_rules["lookback_trading_days"]),
        float(regime_rules["uptrend_threshold"]),
        float(regime_rules["downtrend_threshold"]),
        int(regime_rules["information_lag_days"]),
    )

    ledger_frames: list[pd.DataFrame] = []
    episode_frames: list[pd.DataFrame] = []
    closure: dict[str, float] = {}
    for window in windows:
        baseline_result = results["baseline"][window]
        ex04_result = results["ex04"][window]
        index = pd.DatetimeIndex(baseline_result.equity.index, name="dt")
        baseline_execution = _execution_target(targets["baseline"], index, prices.index)
        ex04_execution = _execution_target(targets["ex04"], index, prices.index)
        episodes, mechanisms = label_baseline_only_days(
            baseline_execution, ex04_execution
        )
        episode_id = _episode_ids(index, episodes, window)
        baseline_returns = _daily_returns(baseline_result, init_cash)
        ex04_returns = _daily_returns(ex04_result, init_cash)
        residual = validate_log_wealth_closure(
            baseline_returns,
            ex04_returns,
            float(baseline_result.metrics["strategy_return"]),
            float(ex04_result.metrics["strategy_return"]),
            tolerance=float(protocol["path_closure_tolerance"]),
        )
        closure[window] = residual
        log_delta = np.log1p(ex04_returns) - np.log1p(baseline_returns)
        position_combination = pd.Series(
            np.select(
                [
                    baseline_execution.eq(0.0) & ex04_execution.eq(0.0),
                    baseline_execution.eq(1.0) & ex04_execution.eq(1.0),
                    baseline_execution.eq(1.0) & ex04_execution.eq(0.0),
                ],
                ["both_cash", "both_long", "baseline_only"],
                default="ex04_only",
            ),
            index=index,
        )
        market_return = prices.loc[index, "close"].pct_change(fill_method=None)
        market_return.iloc[0] = (
            float(prices.loc[index[0], "close"]) / float(prices.loc[index[0], "open"]) - 1.0
        )
        window_ledger = pd.DataFrame(
                {
                    "window": window,
                    "date": index,
                    "regime": regimes.reindex(index).astype(str),
                    "baseline_decision_position": targets["baseline"].reindex(index),
                    "ex04_decision_position": targets["ex04"].reindex(index),
                    "baseline_execution_position": baseline_execution,
                    "ex04_execution_position": ex04_execution,
                    "position_combination": position_combination,
                    "path_mechanism": mechanisms.astype(str),
                    "baseline_episode_id": episode_id,
                    "market_return": market_return,
                    "baseline_daily_return": baseline_returns,
                    "ex04_daily_return": ex04_returns,
                    "daily_return_delta": ex04_returns - baseline_returns,
                    "log_wealth_delta": log_delta,
                }
            )
        ledger_frames.append(window_ledger)
        if not episodes.empty:
            episode_frames.append(
                build_baseline_episode_table(episodes, window_ledger, window)
            )
    ledger = pd.concat(ledger_frames, ignore_index=True)
    baseline_episodes = (
        pd.concat(episode_frames, ignore_index=True)
        if episode_frames
        else pd.DataFrame(
            columns=[
                "window",
                "baseline_episode_id",
                "start",
                "end",
                "trading_days",
                "ex04_overlap_days",
                "ex04_segment_count",
                "episode_class",
            ]
        )
    )
    closure = validate_path_ledger(
        ledger,
        variant_metrics,
        windows,
        tolerance=float(protocol["path_closure_tolerance"]),
    )

    grouped = ledger.groupby(
        ["window", "regime", "position_combination", "path_mechanism"],
        sort=False,
        dropna=False,
    )
    window_regime_path = grouped.agg(
        trading_days=("date", "size"),
        market_return_sum=("market_return", "sum"),
        baseline_exposure=("baseline_execution_position", "mean"),
        ex04_exposure=("ex04_execution_position", "mean"),
        baseline_daily_return_sum=("baseline_daily_return", "sum"),
        ex04_daily_return_sum=("ex04_daily_return", "sum"),
        daily_return_delta_sum=("daily_return_delta", "sum"),
        log_wealth_delta=("log_wealth_delta", "sum"),
    ).reset_index()

    mechanism_contributions = (
        ledger.assign(
            negative_log_loss=(-ledger["log_wealth_delta"]).clip(lower=0.0),
            positive_log_gain=ledger["log_wealth_delta"].clip(lower=0.0),
        )
        .groupby(["window", "path_mechanism"], sort=False)
        .agg(
            trading_days=("date", "size"),
            episode_count=("baseline_episode_id", lambda values: values[values.ne("")].nunique()),
            log_wealth_delta=("log_wealth_delta", "sum"),
            negative_log_loss=("negative_log_loss", "sum"),
            positive_log_gain=("positive_log_gain", "sum"),
        )
        .reset_index()
        .rename(columns={"path_mechanism": "mechanism"})
    )

    hybrid_counterfactuals = variant_metrics.loc[
        variant_metrics["variant"].isin(
            ("baseline_entry_ex04_exit", "ex04_entry_baseline_exit")
        )
    ].copy()

    groups = signal_groups(factors.columns)
    if tuple(groups) != tuple(str(value) for value in protocol["factor_groups"]):
        raise ValueError("factor groups differ from preregistration")
    event_rows: list[dict[str, object]] = []
    contribution_rows: list[dict[str, object]] = []
    full_index = pd.DatetimeIndex(factors.index, name="dt")
    previous_baseline = targets["baseline"].shift(1).fillna(0.0)
    previous_ex04 = targets["ex04"].shift(1).fillna(0.0)
    entry_event = previous_baseline.eq(0.0) & targets["baseline"].eq(1.0) & targets["ex04"].eq(0.0)
    exit_event = previous_ex04.eq(1.0) & targets["ex04"].eq(0.0) & targets["baseline"].eq(1.0)
    ledger_lookup = ledger.set_index(["window", "date"])
    for kind, mask, event_type in (
        ("entry", entry_event, "missed_baseline_entry"),
        ("exit", exit_event, "early_ex04_exit"),
    ):
        for signal_date in full_index[mask.reindex(full_index).fillna(False)]:
            matching_windows = [
                name for name, (start, end) in periods.items() if start <= signal_date <= end
            ]
            if not matching_windows:
                continue
            window = matching_windows[0]
            location = full_index.get_loc(signal_date)
            if not isinstance(location, (int, np.integer)) or location + 1 >= len(full_index):
                continue
            execution_date = full_index[location + 1]
            lookup_key = (window, execution_date)
            if lookup_key in ledger_lookup.index:
                path = ledger_lookup.loc[lookup_key]
                mechanism = str(path["path_mechanism"])
                episode_id = str(path["baseline_episode_id"])
            else:
                mechanism = "outside_window_execution"
                episode_id = ""
            baseline_value = float(baseline_score.loc[signal_date])
            ex04_value = float(ex04_score.loc[signal_date])
            block = classify_score_block(
                kind,
                baseline_value,
                ex04_value,
                baseline_enter=float(baseline.rule.enter),
                ex04_enter=ex04_enter,
                baseline_exit=float(baseline.rule.exit),
                ex04_exit=ex04_exit,
            )
            event_id = f"{window}:{event_type}:{signal_date:%Y%m%d}"
            event_rows.append(
                {
                    "event_id": event_id,
                    "window": window,
                    "signal_date": signal_date,
                    "execution_date": execution_date,
                    "event_type": event_type,
                    "regime": str(regimes.loc[signal_date]),
                    "baseline_episode_id": episode_id,
                    "path_mechanism": mechanism,
                    "block_label": block,
                    "baseline_score": baseline_value,
                    "ex04_score": ex04_value,
                    "baseline_enter_threshold": float(baseline.rule.enter),
                    "ex04_enter_threshold": ex04_enter,
                    "baseline_exit_threshold": float(baseline.rule.exit),
                    "ex04_exit_threshold": ex04_exit,
                    "baseline_margin": baseline_value
                    - (float(baseline.rule.enter) if kind == "entry" else float(baseline.rule.exit)),
                    "ex04_margin": ex04_value - (ex04_enter if kind == "entry" else ex04_exit),
                    "weights_only_condition": bool(
                        ex04_value >= float(baseline.rule.enter)
                        if kind == "entry"
                        else ex04_value <= float(baseline.rule.exit)
                    ),
                    "thresholds_only_condition": bool(
                        baseline_value >= ex04_enter
                        if kind == "entry"
                        else baseline_value <= ex04_exit
                    ),
                }
            )
            factor_values = factors.loc[signal_date]
            for group, names in groups.items():
                contribution_rows.append(
                    {
                        "event_id": event_id,
                        "window": window,
                        "signal_date": signal_date,
                        "event_type": event_type,
                        "group": group,
                        "baseline_weighted_contribution": float(
                            (factor_values.loc[list(names)] * baseline_weights.loc[list(names)]).sum()
                        ),
                        "ex04_weighted_contribution": float(
                            (factor_values.loc[list(names)] * ex04_weights.loc[list(names)]).sum()
                        ),
                    }
                )
    decision_events = pd.DataFrame(event_rows)
    group_contributions = pd.DataFrame(contribution_rows)

    classification = classify_mechanism(
        mechanism_contributions, hybrid_counterfactuals, protocol
    )
    for label, frame in (
        ("variant metrics", variant_metrics),
        ("daily path ledger", ledger),
        ("window regime path", window_regime_path),
        ("baseline episodes", baseline_episodes),
        ("mechanism contributions", mechanism_contributions),
        ("decision events", decision_events),
        ("group score contributions", group_contributions),
        ("hybrid counterfactuals", hybrid_counterfactuals),
    ):
        _finite_numeric(frame, label)

    identity = {
        "status": "PASS",
        "general_baseline": {"version": baseline.version, "sha256": baseline.sha256},
        "research_object_path": str(research["path"]),
        "research_object_sha256": frozen_digest,
        "source_evidence_sha256": source_hashes,
        "equivalence": equivalence,
        "factor_groups": {name: list(values) for name, values in groups.items()},
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
        "variant_count": len(targets),
        "window_count": len(windows),
        "daily_ledger_rows": len(ledger),
        "baseline_episode_count": len(baseline_episodes),
        "decision_event_count": len(decision_events),
        "max_absolute_closure_residual": max(abs(value) for value in closure.values()),
        "closure_residuals": closure,
        "mechanism_classification": classification,
    }
    _write_csv(artifacts / "variant_window_metrics.csv", variant_metrics)
    _write_csv(artifacts / "daily_path_ledger.csv", ledger)
    _write_csv(artifacts / "window_regime_path.csv", window_regime_path)
    _write_csv(artifacts / "baseline_episodes.csv", baseline_episodes)
    _write_csv(artifacts / "mechanism_contributions.csv", mechanism_contributions)
    _write_csv(artifacts / "decision_events.csv", decision_events)
    _write_csv(artifacts / "group_score_contributions.csv", group_contributions)
    _write_csv(artifacts / "hybrid_counterfactuals.csv", hybrid_counterfactuals)
    _write_json(artifacts / "mechanism_classification.json", classification)
    _write_json(artifacts / "identity_audit.json", identity)
    _write_json(artifacts / "metrics.json", metrics)
    return metrics
