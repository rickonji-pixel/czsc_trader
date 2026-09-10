from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)


EXPERIMENT_ID = "20260910_S002_EX08"
EVALUATION_START = pd.Timestamp("2021-01-04")


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_protocol(protocol: dict[str, object]) -> None:
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "automatic_acceptance",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("identifiability audit may not select, promote, or deploy")
    sample = protocol["evaluation_sample"]
    if sample["holding_horizons"] != [3, 4, 5, 6, 7, 8, 10]:
        raise ValueError("holding horizons differ from frozen protocol")
    if sample["mechanism_band"] != [4, 5, 6]:
        raise ValueError("mechanism band differs from frozen protocol")


def _validate_experiment_source(
    repo_root: Path,
    spec: dict[str, object],
    artifact_hashes: dict[str, str],
) -> Path:
    source = repo_root / "experiments" / str(spec["experiment_id"])
    validate_experiment_archive(source)
    expected = {source / "experiment_manifest.json": str(spec["manifest_sha256"])}
    expected.update({source / "artifacts" / name: digest for name, digest in artifact_hashes.items()})
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")
    return source


def _validate_sources(repo_root: Path, protocol: dict[str, object]) -> tuple[Path, Path]:
    source_spec = protocol["source_experiment"]
    source = _validate_experiment_source(
        repo_root,
        source_spec,
        {
            "protocol.json": str(source_spec["protocol_sha256"]),
            "closed_trades.csv": str(source_spec["closed_trades_sha256"]),
            "prototype_daily.csv": str(source_spec["prototype_daily_sha256"]),
            "window_metrics.csv": str(source_spec["window_metrics_sha256"]),
        },
    )
    filter_spec = protocol["filter_source_experiment"]
    filter_source = _validate_experiment_source(
        repo_root,
        filter_spec,
        {
            "closed_trades.csv": str(filter_spec["closed_trades_sha256"]),
            "window_metrics.csv": str(filter_spec["window_metrics_sha256"]),
        },
    )
    execution_spec = protocol["execution_data"]
    execution_manifest = repo_root / str(execution_spec["manifest"])
    if _sha256(execution_manifest) != execution_spec["manifest_sha256"]:
        raise ValueError("execution-data manifest differs from frozen protocol")
    return source, filter_source


def _prices(replay_data) -> pd.DataFrame:
    prices = replay_data.execution_daily.copy()
    if "dt" in prices.columns:
        prices = prices.set_index("dt")
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index).normalize(), name="dt")
    prices = prices.sort_index()
    if prices.index.has_duplicates or not {"open", "close"} <= set(prices.columns):
        raise ValueError("execution prices must be unique and contain open/close")
    return prices


def _features(
    prices: pd.DataFrame,
    protocol: dict[str, object],
    max_horizon: int,
) -> tuple[pd.DataFrame, dict[str, float]]:
    settings = protocol["volatility_slice"]
    close = prices["close"].astype(float)
    frame = pd.DataFrame(index=prices.index)
    frame["year"] = frame.index.year
    frame["three_session_return"] = close / close.shift(3) - 1.0
    frame["volatility_20"] = (
        close.pct_change().rolling(int(settings["lookback_sessions"])).std(ddof=1)
        * np.sqrt(float(settings["annualization_sessions"]))
    )
    positions = np.arange(len(frame))
    eligible = (
        (frame.index >= EVALUATION_START)
        & frame["three_session_return"].notna()
        & frame["volatility_20"].notna()
        & (positions + 1 + max_horizon < len(frame))
    )
    quantiles = frame.loc[eligible, "volatility_20"].quantile([1 / 3, 2 / 3])
    lower, upper = float(quantiles.iloc[0]), float(quantiles.iloc[1])
    frame["volatility_bucket"] = pd.cut(
        frame["volatility_20"],
        [-np.inf, lower, upper, np.inf],
        labels=["LOW", "MEDIUM", "HIGH"],
    ).astype("string")
    frame["eligible"] = eligible
    frame["position"] = positions
    return frame, {"lower": lower, "upper": upper}


def _net_return(entry_price: float, exit_price: float, fee_rate: float) -> float:
    return float(exit_price * (1.0 - fee_rate) / (entry_price * (1.0 + fee_rate)) - 1.0)


def _outcome_arrays(
    prices: pd.DataFrame,
    features: pd.DataFrame,
    horizons: list[int],
    fee_rate: float,
) -> dict[int, np.ndarray]:
    opens = prices["open"].astype(float).to_numpy()
    outcomes: dict[int, np.ndarray] = {}
    for horizon in horizons:
        values = np.full(len(prices), np.nan, dtype=float)
        positions = np.arange(len(prices) - 1 - horizon)
        entry = positions + 1
        exit_positions = entry + horizon
        values[positions] = (
            opens[exit_positions] * (1.0 - fee_rate)
            / (opens[entry] * (1.0 + fee_rate))
            - 1.0
        )
        values[~features["eligible"].to_numpy(dtype=bool)] = np.nan
        outcomes[horizon] = values
    return outcomes


def _event_matrix(
    source: Path,
    prices: pd.DataFrame,
    features: pd.DataFrame,
    outcomes: dict[int, np.ndarray],
    protocol: dict[str, object],
) -> pd.DataFrame:
    sample = protocol["evaluation_sample"]
    trades = pd.read_csv(source / "artifacts" / "closed_trades.csv")
    scoped = trades.loc[
        trades["window"].eq(sample["source_window"])
        & trades["holding_sessions"].eq(sample["source_holding_sessions"])
    ].copy()
    if len(scoped) != int(sample["expected_closed_trades"]):
        raise AssertionError("source trade count differs from frozen protocol")
    scoped["entry_signal_date"] = pd.to_datetime(scoped["entry_signal_date"])
    scoped["entry_date"] = pd.to_datetime(scoped["entry_date"])
    scoped["exit_date"] = pd.to_datetime(scoped["exit_date"])
    scoped = scoped.sort_values("entry_signal_date").reset_index(drop=True)
    index_positions = pd.Series(np.arange(len(prices)), index=prices.index)
    rows: list[dict[str, object]] = []
    for _, trade in scoped.iterrows():
        signal_date = pd.Timestamp(trade["entry_signal_date"])
        signal_position = int(index_positions.loc[signal_date])
        if prices.index[signal_position + 1] != pd.Timestamp(trade["entry_date"]):
            raise AssertionError("source trade does not execute on next session")
        row: dict[str, object] = {
            "entry_signal_date": signal_date,
            "entry_date": pd.Timestamp(trade["entry_date"]),
            "year": int(signal_date.year),
            "three_session_return": float(features.loc[signal_date, "three_session_return"]),
            "volatility_20": float(features.loc[signal_date, "volatility_20"]),
            "volatility_bucket": str(features.loc[signal_date, "volatility_bucket"]),
            "entry_price": float(prices.iloc[signal_position + 1]["open"]),
        }
        for horizon in sample["holding_horizons"]:
            exit_position = signal_position + 1 + int(horizon)
            row[f"exit_date_{horizon}"] = prices.index[exit_position]
            row[f"exit_price_{horizon}"] = float(prices.iloc[exit_position]["open"])
            row[f"net_return_{horizon}"] = float(outcomes[int(horizon)][signal_position])
        if not np.isclose(row["net_return_5"], float(trade["net_return"]), rtol=1e-12, atol=1e-12):
            raise AssertionError(f"five-day trade does not reproduce EX07: {signal_date.date()}")
        if row["exit_date_5"] != pd.Timestamp(trade["exit_date"]):
            raise AssertionError(f"five-day exit date does not reproduce EX07: {signal_date.date()}")
        row["delta_5_vs_4"] = float(row["net_return_5"] - row["net_return_4"])
        row["delta_5_vs_6"] = float(row["net_return_5"] - row["net_return_6"])
        row["delta_5_vs_neighbor_mean"] = float(
            row["net_return_5"] - (row["net_return_4"] + row["net_return_6"]) / 2.0
        )
        row["band_return_4_6"] = float(
            np.mean([row["net_return_4"], row["net_return_5"], row["net_return_6"]])
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _slice_summary(events: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for dimension, column in (("YEAR", "year"), ("VOLATILITY", "volatility_bucket")):
        for value, group in events.groupby(column, sort=True):
            returns = group["net_return_5"].astype(float)
            band = group["band_return_4_6"].astype(float)
            rows.append({
                "dimension": dimension,
                "slice": str(value),
                "trades": len(group),
                "mean_return_5": float(returns.mean()),
                "median_return_5": float(returns.median()),
                "win_rate_5": float(returns.gt(0).mean()),
                "mean_band_return": float(band.mean()),
                "minimum_return_5": float(returns.min()),
                "maximum_return_5": float(returns.max()),
            })
    return pd.DataFrame(rows)


def _contribution_audit(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    contribution = events[[
        "entry_signal_date",
        "net_return_4",
        "net_return_5",
        "net_return_6",
        "delta_5_vs_4",
        "delta_5_vs_6",
        "delta_5_vs_neighbor_mean",
        "band_return_4_6",
    ]].copy()
    contribution["absolute_peak_contribution"] = contribution[
        "delta_5_vs_neighbor_mean"
    ].abs()
    absolute_total = float(contribution["absolute_peak_contribution"].sum())
    ordered = contribution.sort_values("absolute_peak_contribution", ascending=False)
    top_one = float(ordered.iloc[0]["absolute_peak_contribution"] / absolute_total)
    top_two = float(ordered.iloc[:2]["absolute_peak_contribution"].sum() / absolute_total)

    rows: list[dict[str, object]] = []
    for omitted_count in (1, 2):
        for omitted in itertools.combinations(range(len(events)), omitted_count):
            kept = events.drop(index=list(omitted))
            rows.append({
                "omitted_count": omitted_count,
                "omitted_signal_dates": "|".join(
                    events.loc[index, "entry_signal_date"].strftime("%Y-%m-%d")
                    for index in omitted
                ),
                "remaining_trades": len(kept),
                "mean_return_4": float(kept["net_return_4"].mean()),
                "mean_return_5": float(kept["net_return_5"].mean()),
                "mean_return_6": float(kept["net_return_6"].mean()),
                "mean_band_return": float(kept["band_return_4_6"].mean()),
                "mean_peak_advantage": float(kept["delta_5_vs_neighbor_mean"].mean()),
            })
    leave_out = pd.DataFrame(rows)
    summary = {
        "top_one_peak_absolute_contribution_share": top_one,
        "top_two_peak_absolute_contribution_share": top_two,
        "leave_one_out_minimum_band_mean": float(
            leave_out.loc[leave_out["omitted_count"].eq(1), "mean_band_return"].min()
        ),
        "leave_two_out_minimum_band_mean": float(
            leave_out.loc[leave_out["omitted_count"].eq(2), "mean_band_return"].min()
        ),
        "leave_two_out_minimum_peak_advantage": float(
            leave_out.loc[leave_out["omitted_count"].eq(2), "mean_peak_advantage"].min()
        ),
    }
    return contribution, leave_out, summary


def _filter_tail_audit(filter_source: Path) -> pd.DataFrame:
    trades = pd.read_csv(filter_source / "artifacts" / "closed_trades.csv")
    metrics = pd.read_csv(filter_source / "artifacts" / "window_metrics.csv")
    trades = trades.loc[trades["window"].eq("2021_2026YTD")]
    metrics = metrics.loc[metrics["window"].eq("2021_2026YTD")].set_index("prototype_id")
    rows = []
    for prototype, group in trades.groupby("prototype_id", sort=True):
        values = group["net_return"].astype(float).sort_values()
        row = metrics.loc[prototype]
        rows.append({
            "prototype_id": prototype,
            "closed_trades": len(values),
            "minimum_trade_return": float(values.iloc[0]),
            "worst_three_mean_return": float(values.iloc[: min(3, len(values))].mean()),
            "maximum_drawdown": float(row["max_drawdown"]),
            "calmar": float(row["calmar"]),
            "win_loss_ratio": float(row["win_loss_ratio"]),
            "total_return": float(row["return"]),
        })
    return pd.DataFrame(rows)


def _non_overlapping_sample(
    pool: np.ndarray,
    count: int,
    rng: np.random.Generator,
    gap: int,
) -> list[int] | None:
    for _ in range(50):
        chosen: list[int] = []
        for position in rng.permutation(pool):
            value = int(position)
            if all(abs(value - existing) > gap for existing in chosen):
                chosen.append(value)
                if len(chosen) == count:
                    return chosen
    return None


def _calendar_sample(
    pools: dict[int, np.ndarray],
    annual_counts: dict[int, int],
    rng: np.random.Generator,
    gap: int,
) -> list[int]:
    selected: list[int] = []
    for year, count in sorted(annual_counts.items()):
        sample = _non_overlapping_sample(pools[year], count, rng, gap)
        if sample is None:
            raise RuntimeError(f"unable to draw calendar-matched sample for {year}")
        selected.extend(sample)
    return selected


def _matched_candidate_lists(
    events: pd.DataFrame,
    features: pd.DataFrame,
    true_positions: set[int],
    protocol: dict[str, object],
) -> list[np.ndarray]:
    settings = protocol["random_controls"]["matched_pullback"]
    exclusion = int(settings["exclude_within_sessions_of_true_signal"])
    available = features.loc[
        features["eligible"]
        & features["three_session_return"].lt(0)
        & ~features["position"].isin(true_positions)
    ].copy()
    available = available.loc[
        ~available["position"].map(
            lambda value: any(abs(int(value) - position) <= exclusion for position in true_positions)
        )
    ]
    return_scale = max(float(available["three_session_return"].std(ddof=1)), 1e-12)
    vol_scale = max(float(available["volatility_20"].std(ddof=1)), 1e-12)
    lists: list[np.ndarray] = []
    for _, event in events.iterrows():
        candidates = available.loc[
            available["year"].eq(int(event["year"]))
            & available["volatility_bucket"].eq(event["volatility_bucket"])
        ].copy()
        if candidates.empty:
            raise RuntimeError(f"no matched candidates for {event['entry_signal_date']}")
        candidates["distance"] = (
            (candidates["three_session_return"] - float(event["three_session_return"])).abs()
            / return_scale
            + (candidates["volatility_20"] - float(event["volatility_20"])).abs()
            / vol_scale
        )
        lists.append(
            candidates.nsmallest(min(int(settings["nearest_pool_size"]), len(candidates)), "distance")[
                "position"
            ].astype(int).to_numpy()
        )
    return lists


def _matched_sample(
    candidate_lists: list[np.ndarray],
    rng: np.random.Generator,
    gap: int,
) -> list[int]:
    for _ in range(100):
        chosen: list[int] = []
        for event_index in rng.permutation(len(candidate_lists)):
            candidates = candidate_lists[int(event_index)]
            valid = [
                int(value)
                for value in rng.permutation(candidates)
                if all(abs(int(value) - existing) > gap for existing in chosen)
            ]
            if not valid:
                break
            chosen.append(valid[0])
        if len(chosen) == len(candidate_lists):
            return chosen
    raise RuntimeError("unable to draw pullback-volatility-matched sample")


def _sample_metrics(positions: list[int], outcomes: dict[int, np.ndarray]) -> dict[str, float]:
    selected = np.asarray(positions, dtype=int)
    h4 = outcomes[4][selected]
    h5 = outcomes[5][selected]
    h6 = outcomes[6][selected]
    if not np.isfinite(np.concatenate([h4, h5, h6])).all():
        raise AssertionError("control sample contains unavailable outcomes")
    band = np.column_stack([h4, h5, h6]).mean(axis=1)
    return {
        "mean_return_4": float(h4.mean()),
        "mean_return_5": float(h5.mean()),
        "mean_return_6": float(h6.mean()),
        "mean_band_return": float(band.mean()),
        "win_rate_5": float((h5 > 0).mean()),
        "compound_return_5": float(np.prod(1.0 + h5) - 1.0),
    }


def _random_controls(
    events: pd.DataFrame,
    features: pd.DataFrame,
    outcomes: dict[int, np.ndarray],
    protocol: dict[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    settings = protocol["random_controls"]
    rng = np.random.default_rng(int(settings["seed"]))
    repetitions = int(settings["repetitions"])
    gap = int(settings["non_overlap_holding_sessions"])
    true_positions = set(
        features.loc[pd.to_datetime(events["entry_signal_date"]), "position"].astype(int)
    )
    annual_counts = events.groupby("year").size().astype(int).to_dict()
    base_pool = features.loc[
        features["eligible"] & ~features["position"].isin(true_positions)
    ]
    calendar_pools = {
        year: group["position"].astype(int).to_numpy()
        for year, group in base_pool.groupby("year")
        if int(year) in annual_counts
    }
    if set(calendar_pools) != set(annual_counts):
        raise RuntimeError("calendar control is missing one evaluation year")
    matched_lists = _matched_candidate_lists(events, features, true_positions, protocol)
    pool_audit = pd.DataFrame({
        "entry_signal_date": pd.to_datetime(events["entry_signal_date"]),
        "year": events["year"].astype(int),
        "volatility_bucket": events["volatility_bucket"].astype(str),
        "matched_candidate_count": [len(values) for values in matched_lists],
    })
    rows: list[dict[str, object]] = []
    for repetition in range(repetitions):
        calendar = _calendar_sample(calendar_pools, annual_counts, rng, gap)
        matched = _matched_sample(matched_lists, rng, 0)
        for control, positions in (("CALENDAR_MATCHED", calendar), ("PULLBACK_VOL_MATCHED", matched)):
            rows.append({
                "control": control,
                "repetition": repetition,
                **_sample_metrics(positions, outcomes),
            })
    distribution = pd.DataFrame(rows)
    actual_positions = sorted(true_positions)
    actual = _sample_metrics(actual_positions, outcomes)
    summary_rows = []
    for control, group in distribution.groupby("control", sort=True):
        for metric, actual_value in actual.items():
            null = group[metric].astype(float)
            summary_rows.append({
                "control": control,
                "metric": metric,
                "actual": actual_value,
                "null_p05": float(null.quantile(0.05)),
                "null_median": float(null.median()),
                "null_p95": float(null.quantile(0.95)),
                "actual_percentile": float(null.le(actual_value).mean()),
            })
    return distribution, pd.DataFrame(summary_rows), pool_audit


def _evidence_label(
    events: pd.DataFrame,
    contribution: dict[str, object],
    control_summary: pd.DataFrame,
    matched_pool_audit: pd.DataFrame,
) -> tuple[str, dict[str, object]]:
    band_means = {
        horizon: float(events[f"net_return_{horizon}"].mean())
        for horizon in (4, 5, 6)
    }
    percentiles = control_summary.loc[
        control_summary["metric"].eq("mean_band_return")
    ].set_index("control")["actual_percentile"].astype(float).to_dict()
    leave_two_min = float(contribution["leave_two_out_minimum_band_mean"])
    top_two = float(contribution["top_two_peak_absolute_contribution_share"])
    minimum_matched_candidates = int(matched_pool_audit["matched_candidate_count"].min())
    weak_reasons = []
    if any(value <= 0.0 for value in band_means.values()):
        weak_reasons.append("one_or_more_band_means_non_positive")
    if percentiles["PULLBACK_VOL_MATCHED"] < 0.5:
        weak_reasons.append("matched_control_band_percentile_below_50pct")
    if leave_two_min <= 0.0:
        weak_reasons.append("leave_two_out_band_mean_non_positive")
    favorable = (
        not weak_reasons
        and all(value >= 0.9 for value in percentiles.values())
        and top_two <= 0.5
        and minimum_matched_candidates >= 10
    )
    label = "WEAK" if weak_reasons else ("FAVORABLE" if favorable else "MIXED")
    return label, {
        "band_means": band_means,
        "control_band_percentiles": percentiles,
        "leave_two_out_minimum_band_mean": leave_two_min,
        "top_two_peak_absolute_contribution_share": top_two,
        "minimum_matched_candidates_per_event": minimum_matched_candidates,
        "weak_reasons": weak_reasons,
    }


def _text(value: object, *, percent: bool = False) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):.2%}" if percent else f"{float(value):.4f}"


def _render_conclusion(
    events: pd.DataFrame,
    contribution: dict[str, object],
    control_summary: pd.DataFrame,
    filter_tail: pd.DataFrame,
    label: str,
    label_details: dict[str, object],
) -> str:
    mean_returns = {
        horizon: float(events[f"net_return_{horizon}"].mean())
        for horizon in (3, 4, 5, 6, 7, 8, 10)
    }
    control = control_summary.loc[
        control_summary["metric"].eq("mean_band_return")
    ].set_index("control")
    tails = filter_tail.set_index("prototype_id")
    no_filter = tails.loc["P-A00-NO-FILTER"]
    top_filter = tails.loc["P-A10-TOP-DIVERGENCE"]
    lines = [
        f"# {EXPERIMENT_ID} 结论",
        "",
        f"状态：COMPLETE。机制可辨识性证据标签为`{label}`。",
        "",
        "## 共同事件持有期曲线",
        "",
        "| 持有期 | 25笔共同事件平均净收益 |",
        "|---:|---:|",
    ]
    for horizon, value in mean_returns.items():
        lines.append(f"| {horizon}日 | {_text(value, percent=True)} |")
    lines.extend([
        "",
        "五日相对四日和六日均值的增量中，最大一笔和最大两笔的绝对贡献占比分别为"
        f"{_text(contribution['top_one_peak_absolute_contribution_share'], percent=True)}和"
        f"{_text(contribution['top_two_peak_absolute_contribution_share'], percent=True)}。任意删除"
        "两笔交易后，4至6日带平均收益的最低值为"
        f"{_text(contribution['leave_two_out_minimum_band_mean'], percent=True)}。",
        "",
        "## 随机对照",
        "",
        "| 对照 | 4至6日带实际均值 | 零假设中位数 | 90%区间 | 实际百分位 |",
        "|---|---:|---:|---:|---:|",
    ])
    labels = {
        "CALENDAR_MATCHED": "年度匹配随机",
        "PULLBACK_VOL_MATCHED": "跌幅波动匹配",
    }
    for name, row in control.iterrows():
        lines.append(
            f"| {labels[name]} | {_text(row['actual'], percent=True)} | "
            f"{_text(row['null_median'], percent=True)} | "
            f"[{_text(row['null_p05'], percent=True)}, {_text(row['null_p95'], percent=True)}] | "
            f"{_text(row['actual_percentile'], percent=True)} |"
        )
    lines.extend([
        "",
        "## 顶背驰尾部审计",
        "",
        "| 版本 | 最差单笔 | 最差三笔均值 | 最大回撤 | 卡玛 |",
        "|---|---:|---:|---:|---:|",
        f"| 无过滤 | {_text(no_filter['minimum_trade_return'], percent=True)} | "
        f"{_text(no_filter['worst_three_mean_return'], percent=True)} | "
        f"{_text(no_filter['maximum_drawdown'], percent=True)} | {_text(no_filter['calmar'])} |",
        f"| 仅顶背驰 | {_text(top_filter['minimum_trade_return'], percent=True)} | "
        f"{_text(top_filter['worst_three_mean_return'], percent=True)} | "
        f"{_text(top_filter['maximum_drawdown'], percent=True)} | {_text(top_filter['calmar'])} |",
        "",
        "顶背驰过滤没有改善当前样本的最差单笔或最大回撤；这只说明当前25笔交易没有提供"
        "该过滤器的正向尾部证据，不能外推为所有市场状态下永久无效。",
        "",
        "## 裁决",
        "",
    ])
    if label == "WEAK":
        lines.append(
            "当前机制出现预注册的弱证据条件，停止进入EX09，不生成S002候选。具体触发项："
            + "、".join(label_details["weak_reasons"]) + "。"
        )
    elif label == "FAVORABLE":
        lines.append(
            "当前机制在共同事件、交易剔除和两类随机对照中均保持有利，允许进入EX09统计"
            "稳健性审计；本轮仍不生成候选。"
        )
    else:
        lines.append(
            "当前机制未触发弱证据，但证据尚未达到预注册的有利条件，允许带着明确风险进入"
            "EX09统计稳健性审计；本轮仍不生成候选。"
        )
    lines.extend([
        "",
        "## 边界",
        "",
        "所有检验均使用截至2026-09-08的同一开发池。随机对照量化相对零假设的位置，不是"
        "新的样本外证据。波动率切片、过滤尾部审计和逐笔归因只作诊断，不产生新规则。",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    experiment_dir = Path(__file__).resolve().parent
    repo_root = experiment_dir.parents[1]
    artifacts = experiment_dir / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    _validate_protocol(protocol)
    source, filter_source = _validate_sources(repo_root, protocol)

    context = RepositoryContext.discover(repo_root, explicit_root=repo_root)
    target = protocol["research_target"]
    replay_data = load_replay_data(
        context,
        "research",
        str(target["symbol"]),
        str(target["asset_type"]),
        pd.Timestamp(target["development_cutoff"]).date(),
    )
    prices = _prices(replay_data)
    horizons = [int(value) for value in protocol["evaluation_sample"]["holding_horizons"]]
    features, volatility_thresholds = _features(prices, protocol, max(horizons))
    outcomes = _outcome_arrays(
        prices,
        features,
        horizons,
        float(protocol["shared_rules"]["fee_rate_one_way"]),
    )
    events = _event_matrix(source, prices, features, outcomes, protocol)
    slices = _slice_summary(events)
    contributions, leave_out, contribution_summary = _contribution_audit(events)
    filter_tail = _filter_tail_audit(filter_source)
    control_distribution, control_summary, matched_pool_audit = _random_controls(
        events,
        features,
        outcomes,
        protocol,
    )
    label, label_details = _evidence_label(
        events, contribution_summary, control_summary, matched_pool_audit
    )

    _write_csv(events, artifacts / "trade_horizon_matrix.csv")
    _write_csv(contributions, artifacts / "peak_contributions.csv")
    _write_csv(leave_out, artifacts / "leave_out_audit.csv")
    _write_csv(slices, artifacts / "slice_summary.csv")
    _write_csv(filter_tail, artifacts / "filter_tail_audit.csv")
    _write_csv(control_distribution, artifacts / "random_control_distribution.csv")
    _write_csv(control_summary, artifacts / "random_control_summary.csv")
    _write_csv(matched_pool_audit, artifacts / "matched_control_pool_audit.csv")
    _write_json(artifacts / "identifiability_summary.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "evidence_label": label,
        "contribution": contribution_summary,
        "label_details": label_details,
        "volatility_thresholds": volatility_thresholds,
    })
    output_names = [
        "trade_horizon_matrix.csv",
        "peak_contributions.csv",
        "leave_out_audit.csv",
        "slice_summary.csv",
        "filter_tail_audit.csv",
        "random_control_distribution.csv",
        "random_control_summary.csv",
        "matched_control_pool_audit.csv",
        "identifiability_summary.json",
    ]
    _write_json(artifacts / "run_evidence.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "dataset_fingerprint": replay_data.fingerprint,
        "cutoff": replay_data.cutoff.isoformat(),
        "source_trade_count": len(events),
        "five_day_reproduction_passed": True,
        "random_seed": protocol["random_controls"]["seed"],
        "random_repetitions_per_control": protocol["random_controls"]["repetitions"],
        "evidence_label": label,
        "outputs": {
            name: {
                "bytes": (artifacts / name).stat().st_size,
                "sha256": _sha256(artifacts / name),
            }
            for name in output_names
        },
    })
    (experiment_dir / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。\n\n"
        f"EX07五日版本的{len(events)}笔闭合交易逐笔复现通过。完成7个持有期的共同事件"
        f"收益矩阵、逐笔和任意两笔剔除、年度与波动率切片、过滤器尾部审计，以及每类"
        f"{int(protocol['random_controls']['repetitions']):,}次随机对照。前两次执行均在统计"
        "输出前因近邻池实现问题失败，修复和无结果泄漏证据见`attempt_log.json`。\n\n"
        f"证据标签：`{label}`。未选择参数、未生成候选，也未调用SE排名、SM或PTE。\n",
        encoding="utf-8",
    )
    (experiment_dir / "04_conclusion.md").write_text(
        _render_conclusion(
            events,
            contribution_summary,
            control_summary,
            filter_tail,
            label,
            label_details,
        ),
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": target["development_cutoff"],
            "evidence_label": label,
            "promotion_allowed": False,
        },
    )


if __name__ == "__main__":
    main()
