from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import czsc._native as czsc_native
import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.data import MarketData
from czsc_trader.experiment_archive import (
    build_experiment_manifest,
    validate_experiment_archive,
)
from czsc_trader.research_backtest import run_period_backtests
from czsc_trader.signal_census import generate_signal_census
from czsc_trader.signal_prototypes import build_minimal_prototype
from czsc_trader.strategy_metrics import strategy_comparison_metrics
from dataflows.tushare_etf import fetch_etf_ohlcv, fetch_etf_unadjusted_daily


EXPERIMENT_ID = "20260910_S002_EX10"


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
    if protocol["holding_horizons"] != [3, 4, 5, 6, 7, 8, 10]:
        raise ValueError("holding horizons differ from frozen protocol")
    if protocol["mechanism_band"] != [4, 5, 6]:
        raise ValueError("mechanism band differs from frozen protocol")
    forbidden = (
        "automatic_acceptance",
        "parameter_selection",
        "candidate_generation",
        "promotion_allowed",
        "mutates_strategy_manager",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX10 may not select parameters, create candidates, or deploy")


def _validate_sources(repo_root: Path, protocol: dict[str, object]) -> None:
    specs = protocol["source_experiments"]
    ident = repo_root / "experiments" / specs["identifiability"]["experiment_id"]
    stats = repo_root / "experiments" / specs["statistics"]["experiment_id"]
    mechanism = repo_root / "experiments" / specs["mechanism"]["experiment_id"]
    validate_experiment_archive(ident)
    validate_experiment_archive(stats)
    validate_experiment_archive(mechanism)
    expected = {
        ident / "experiment_manifest.json": specs["identifiability"]["manifest_sha256"],
        ident / "artifacts" / "trade_horizon_matrix.csv": specs["identifiability"]["trade_horizon_matrix_sha256"],
        stats / "experiment_manifest.json": specs["statistics"]["manifest_sha256"],
        mechanism / "artifacts" / "protocol.json": specs["mechanism"]["protocol_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")


def _vendor_to_internal(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    result = frame.rename(columns={
        "Date": "dt",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "vol",
        "Amount": "amount",
    }).copy()
    result["dt"] = pd.to_datetime(result["dt"]).dt.normalize()
    result.insert(1, "symbol", symbol)
    columns = ["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]
    result = result[columns].sort_values("dt").reset_index(drop=True)
    if result["dt"].duplicated().any() or result.isna().any().any():
        raise ValueError("validation daily data contains duplicates or missing values")
    return result


def _prepare_or_load_validation_data(
    repo_root: Path,
    artifacts: Path,
    protocol: dict[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    spec = protocol["validation_data"]
    adjusted_path = artifacts / "512100_adjusted_daily.csv"
    execution_path = artifacts / "512100_execution_daily.csv"
    manifest_path = artifacts / "512100_data_manifest.json"
    if manifest_path.is_file():
        manifest = _read_json(manifest_path)
        for path, key in ((adjusted_path, "adjusted"), (execution_path, "execution")):
            if _sha256(path) != manifest["files"][key]["sha256"]:
                raise ValueError(f"validation-data snapshot differs: {path.name}")
        adjusted = pd.read_csv(adjusted_path, parse_dates=["dt"])
        execution = pd.read_csv(execution_path, parse_dates=["dt"])
        return adjusted, execution, manifest

    symbol = str(spec["symbol"])
    start = str(spec["prepare_start"])
    end = str(spec["prepare_end"])
    adjusted_vendor, adjusted_metadata = fetch_etf_ohlcv(
        symbol, start, end, "daily", env_file=repo_root / ".env",
    )
    execution_vendor, execution_metadata = fetch_etf_unadjusted_daily(
        symbol, start, end, env_file=repo_root / ".env",
    )
    adjusted = _vendor_to_internal(adjusted_vendor, symbol)
    execution = _vendor_to_internal(execution_vendor, symbol)
    if not adjusted["dt"].equals(execution["dt"]):
        raise ValueError("adjusted and execution daily sessions differ")
    if adjusted["dt"].max() > pd.Timestamp(end):
        raise ValueError("validation data exceeds frozen cutoff")
    _write_csv(adjusted, adjusted_path)
    _write_csv(execution, execution_path)
    manifest = {
        "schema_version": 1,
        "symbol": symbol,
        "requested_start": start,
        "requested_end": end,
        "actual_start": adjusted["dt"].min().date().isoformat(),
        "actual_end": adjusted["dt"].max().date().isoformat(),
        "sessions": len(adjusted),
        "adjusted_metadata": adjusted_metadata,
        "execution_metadata": execution_metadata,
        "intraday_excluded": True,
        "intraday_exclusion_reason": "EX10 freezes a daily-only signal; vendor 30m Open fails strict daily reconciliation",
        "files": {
            "adjusted": {"path": adjusted_path.name, "sha256": _sha256(adjusted_path)},
            "execution": {"path": execution_path.name, "sha256": _sha256(execution_path)},
        },
    }
    _write_json(manifest_path, manifest)
    return adjusted, execution, manifest


def _entry_states(adjusted: pd.DataFrame, protocol: dict[str, object]) -> tuple[pd.Series, dict[str, object]]:
    empty = pd.DataFrame(columns=adjusted.columns)
    data = MarketData(empty, adjusted, empty, {}, symbol=str(adjusted.iloc[0]["symbol"]))
    entry = protocol["signal_generation"]["entry"]
    registry = [item for item in czsc_native.list_all_signals() if item["name"] == entry["name"]]
    if len(registry) != 1:
        raise ValueError("frozen CZSC entry signal is unavailable")
    census = generate_signal_census(
        data,
        protocol["signal_generation"]["frequency_specs"],
        evaluation_start=protocol["validation_data"]["evaluation_start"],
        registry=registry,
    )
    matched = census.catalog.loc[
        census.catalog["frequency"].eq(entry["frequency"])
        & census.catalog["name"].eq(entry["name"])
        & census.catalog["status"].eq("GENERATED")
    ]
    if len(matched) != 1:
        raise AssertionError("frozen signal did not generate exactly once")
    signal_id = str(matched.iloc[0]["signal_id"])
    return census.primary[signal_id], {
        "signal_id": signal_id,
        "output_key": str(matched.iloc[0]["output_key"]),
        "config_json": str(matched.iloc[0]["config_json"]),
    }


def _features(prices: pd.DataFrame, evaluation_start: pd.Timestamp) -> pd.DataFrame:
    close = prices["close"].astype(float)
    returns = close.pct_change()
    three = returns.rolling(3)
    frame = pd.DataFrame(index=prices.index)
    frame["year"] = frame.index.year
    frame["three_session_return"] = close / close.shift(3) - 1.0
    frame["negative_session_count_3"] = three.apply(lambda values: float((values < 0).sum()), raw=True)
    absolute_sum = returns.abs().rolling(3).sum()
    frame["path_monotonicity_3"] = frame["three_session_return"].abs() / absolute_sum
    frame["largest_move_share_3"] = returns.abs().rolling(3).max() / absolute_sum
    frame["volatility_20"] = returns.rolling(20).std(ddof=1) * np.sqrt(252.0)
    eligible = frame.index >= evaluation_start
    quantiles = frame.loc[eligible, "volatility_20"].quantile([1 / 3, 2 / 3])
    frame["volatility_bucket"] = pd.cut(
        frame["volatility_20"],
        [-np.inf, float(quantiles.iloc[0]), float(quantiles.iloc[1]), np.inf],
        labels=["LOW", "MEDIUM", "HIGH"],
    ).astype("string")
    sma60 = close.rolling(60).mean()
    frame["downtrend_60"] = (close < sma60) & (sma60 < sma60.shift(20))
    return frame


def _outcome_arrays(prices: pd.DataFrame, horizons: list[int], fee: float) -> dict[int, np.ndarray]:
    opens = prices["open"].astype(float).to_numpy()
    outcomes: dict[int, np.ndarray] = {}
    for horizon in horizons:
        values = np.full(len(prices), np.nan)
        positions = np.arange(len(prices) - 1 - horizon)
        entry = positions + 1
        exit_positions = entry + horizon
        values[positions] = opens[exit_positions] * (1.0 - fee) / (opens[entry] * (1.0 + fee)) - 1.0
        outcomes[horizon] = values
    return outcomes


def _validation_events(
    states: pd.Series,
    prices: pd.DataFrame,
    features: pd.DataFrame,
    protocol: dict[str, object],
) -> tuple[pd.DataFrame, dict[int, np.ndarray]]:
    entry = protocol["signal_generation"]["entry"]
    rules = protocol["shared_rules"]
    horizons = [int(value) for value in protocol["holding_horizons"]]
    representative = int(protocol["representative_horizon"])
    prototype = build_minimal_prototype(
        states,
        str(entry["state"]),
        [],
        max_holding_sessions=representative,
        target_position=float(rules["target_position"]),
    )
    decisions = prototype.decisions.set_index("date")
    index_positions = pd.Series(np.arange(len(prices)), index=prices.index)
    outcomes = _outcome_arrays(prices, horizons, float(rules["fee_rate_one_way"]))
    evaluation_start = pd.Timestamp(protocol["validation_data"]["evaluation_start"])
    rows: list[dict[str, object]] = []
    for signal_date in decisions.index[decisions["action"].eq("ENTER")]:
        if signal_date < evaluation_start or signal_date not in index_positions:
            continue
        position = int(index_positions.loc[signal_date])
        if position + 1 + max(horizons) >= len(prices):
            continue
        row: dict[str, object] = {
            "entry_signal_date": signal_date,
            "entry_date": prices.index[position + 1],
            "year": int(signal_date.year),
            "entry_price": float(prices.iloc[position + 1]["open"]),
        }
        for column in (
            "three_session_return",
            "negative_session_count_3",
            "path_monotonicity_3",
            "largest_move_share_3",
            "volatility_20",
            "volatility_bucket",
            "downtrend_60",
        ):
            row[column] = features.loc[signal_date, column]
        for horizon in horizons:
            exit_position = position + 1 + horizon
            row[f"exit_date_{horizon}"] = prices.index[exit_position]
            row[f"exit_price_{horizon}"] = float(prices.iloc[exit_position]["open"])
            row[f"net_return_{horizon}"] = float(outcomes[horizon][position])
        row["band_return_4_6"] = float(np.mean([row[f"net_return_{h}"] for h in (4, 5, 6)]))
        rows.append(row)
    return pd.DataFrame(rows), outcomes


def _strategy_metrics(
    states: pd.Series,
    execution: pd.DataFrame,
    protocol: dict[str, object],
) -> pd.DataFrame:
    rules = protocol["shared_rules"]
    start = pd.Timestamp(protocol["validation_data"]["evaluation_start"])
    end = pd.Timestamp(protocol["validation_data"]["prepare_end"])
    rows = []
    for horizon in protocol["holding_horizons"]:
        prototype = build_minimal_prototype(
            states,
            str(protocol["signal_generation"]["entry"]["state"]),
            [],
            max_holding_sessions=int(horizon),
            target_position=float(rules["target_position"]),
        )
        result = run_period_backtests(
            execution,
            prototype.target_position,
            {"continuous": (start, end)},
            fee_rate=float(rules["fee_rate_one_way"]),
            init_cash=float(rules["initial_cash"]),
        )["continuous"]
        metrics = strategy_comparison_metrics(result.equity, result.orders, float(rules["initial_cash"]))
        rows.append({"holding_sessions": int(horizon), **metrics, "exposure": result.metrics["exposure"]})
    return pd.DataFrame(rows)


def _peak_bootstrap(source_events: pd.DataFrame, protocol: dict[str, object]) -> dict[str, object]:
    config = protocol["primary_diagnostics"]
    horizons = [int(value) for value in protocol["holding_horizons"]]
    matrix = source_events[[f"net_return_{h}" for h in horizons]].to_numpy(dtype=float)
    rng = np.random.default_rng(int(config["trade_bootstrap_seed"]))
    exact = near = band_positive = all_band_positive = 0
    for _ in range(int(config["trade_bootstrap_repetitions"])):
        means = matrix[rng.integers(0, len(matrix), len(matrix))].mean(axis=0)
        best = float(means.max())
        five = float(means[horizons.index(5)])
        exact += int(np.isclose(five, best, rtol=0.0, atol=1e-15))
        near += int(five >= best - abs(best) * float(config["near_best_relative_tolerance"]))
        band = means[[horizons.index(h) for h in (4, 5, 6)]]
        band_positive += int(float(band.mean()) > 0.0)
        all_band_positive += int(bool(np.all(band > 0.0)))
    repetitions = int(config["trade_bootstrap_repetitions"])
    return {
        "repetitions": repetitions,
        "five_exact_peak_frequency": exact / repetitions,
        "five_within_10pct_of_peak_frequency": near / repetitions,
        "band_mean_positive_frequency": band_positive / repetitions,
        "all_band_horizons_positive_frequency": all_band_positive / repetitions,
    }


def _leave_two_out(events: pd.DataFrame) -> float:
    if len(events) < 3:
        return float("nan")
    values = events["band_return_4_6"].to_numpy(dtype=float)
    minima = [np.delete(values, omitted).mean() for omitted in itertools.combinations(range(len(values)), 2)]
    return float(min(minima))


def _matched_control(
    events: pd.DataFrame,
    prices: pd.DataFrame,
    features: pd.DataFrame,
    outcomes: dict[int, np.ndarray],
    protocol: dict[str, object],
) -> tuple[pd.DataFrame, dict[str, object]]:
    config = protocol["matched_control"]
    match_columns = list(config["match_features"])
    max_horizon = max(int(value) for value in protocol["holding_horizons"])
    positions = pd.Series(np.arange(len(prices)), index=prices.index)
    true_positions = {int(positions.loc[date]) for date in pd.to_datetime(events["entry_signal_date"])}
    eligible_positions = np.arange(len(prices) - 1 - max_horizon)
    pools: list[np.ndarray] = []
    pool_rows = []
    feature_scale = features[match_columns].std(ddof=1).replace(0.0, 1.0)
    for _, event in events.iterrows():
        signal_date = pd.Timestamp(event["entry_signal_date"])
        mask = (
            (features.index.year == signal_date.year)
            & features["volatility_bucket"].eq(str(event["volatility_bucket"]))
            & features[match_columns].notna().all(axis=1)
        )
        candidates = [
            int(positions.loc[date]) for date in features.index[mask]
            if date in positions and int(positions.loc[date]) in eligible_positions
            and int(positions.loc[date]) not in true_positions
        ]
        if not candidates:
            raise ValueError(f"no matched candidates for {signal_date.date()}")
        candidate_frame = features.iloc[candidates][match_columns]
        target = pd.Series({column: event[column] for column in match_columns}, dtype=float)
        distance = (((candidate_frame - target) / feature_scale) ** 2).sum(axis=1).pow(0.5)
        selected = positions.reindex(distance.nsmallest(min(int(config["nearest_pool_size"]), len(distance))).index).astype(int).to_numpy()
        pools.append(selected)
        pool_rows.append({
            "entry_signal_date": signal_date,
            "pool_size": len(selected),
            "nearest_distance": float(distance.min()),
            "farthest_selected_distance": float(distance.nsmallest(len(selected)).max()),
        })

    rng = np.random.default_rng(int(config["seed"]))
    null = []
    for _ in range(int(config["repetitions"])):
        chosen: list[int] = []
        for pool in pools:
            available = np.asarray([position for position in pool if int(position) not in chosen], dtype=int)
            if available.size == 0:
                raise ValueError("matched control cannot preserve unique dates")
            chosen.append(int(rng.choice(available)))
        band_values = [np.mean([outcomes[h][position] for h in (4, 5, 6)]) for position in chosen]
        null.append(float(np.mean(band_values)))
    actual = float(events["band_return_4_6"].mean())
    array = np.asarray(null)
    summary = {
        "actual_band_mean": actual,
        "null_median": float(np.median(array)),
        "null_lower_90": float(np.quantile(array, 0.05)),
        "null_upper_90": float(np.quantile(array, 0.95)),
        "actual_percentile": float(np.mean(array < actual) + 0.5 * np.mean(array == actual)),
        "minimum_pool_size": min(len(pool) for pool in pools),
    }
    distribution = pd.DataFrame({"repetition": np.arange(1, len(array) + 1), "band_mean": array})
    return pd.DataFrame(pool_rows), {**summary, "distribution": distribution}


def _diagnostic_tables(repo_root: Path, protocol: dict[str, object]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    source = repo_root / "experiments" / "20260910_S002_EX08" / "artifacts" / "trade_horizon_matrix.csv"
    events = pd.read_csv(source, parse_dates=["entry_signal_date", "entry_date"])
    context = RepositoryContext.discover(repo_root, explicit_root=repo_root)
    replay = load_replay_data(context, "research", "510500.SH", "etf", pd.Timestamp("2026-09-08").date())
    daily = replay.adjusted.daily.copy()
    daily["dt"] = pd.to_datetime(daily["dt"]).dt.normalize()
    prices = daily.set_index("dt").sort_index()
    features = _features(prices, pd.Timestamp("2021-01-04"))
    events["downtrend_60"] = events["entry_signal_date"].map(features["downtrend_60"])
    trend_rows = []
    for value, group in events.groupby("downtrend_60", sort=True):
        trend_rows.append({
            "downtrend_60": bool(value),
            "trades": len(group),
            "mean_return_5": float(group["net_return_5"].mean()),
            "win_rate_5": float(group["net_return_5"].gt(0).mean()),
            "mean_band_return": float(group["band_return_4_6"].mean()),
        })
    columns = [
        "entry_signal_date", "entry_date", "exit_date_5", "net_return_4",
        "net_return_5", "net_return_6", "band_return_4_6", "volatility_20",
        "volatility_bucket", "downtrend_60",
    ]
    return events, pd.DataFrame(trend_rows), events.loc[events["year"].eq(2023), columns].copy()


def _label(events: pd.DataFrame, matched: dict[str, object], protocol: dict[str, object]) -> tuple[str, list[str]]:
    band_means = {h: float(events[f"net_return_{h}"].mean()) for h in (4, 5, 6)}
    leave_two = _leave_two_out(events)
    reasons = []
    non_positive = sum(value <= 0.0 for value in band_means.values())
    if float(np.mean(list(band_means.values()))) <= 0.0:
        reasons.append("band_mean_non_positive")
    if non_positive >= 2:
        reasons.append("at_least_two_band_horizons_non_positive")
    if float(matched["actual_percentile"]) < 0.5:
        reasons.append("matched_band_percentile_below_50pct")
    if reasons:
        return "WEAK", reasons
    favorable = (
        non_positive == 0
        and float(matched["actual_percentile"]) >= 0.75
        and leave_two > 0.0
        and len(events) >= 15
    )
    return ("FAVORABLE" if favorable else "MIXED"), ([] if favorable else ["favorable_conditions_incomplete"])


def _pct(value: float) -> str:
    return f"{value:.2%}"


def main() -> None:
    experiment_dir = Path(__file__).resolve().parent
    repo_root = experiment_dir.parents[1]
    artifacts = experiment_dir / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    _validate_protocol(protocol)
    _validate_sources(repo_root, protocol)

    adjusted, execution, data_manifest = _prepare_or_load_validation_data(repo_root, artifacts, protocol)
    execution_prices = execution.set_index("dt").sort_index()
    states, signal_evidence = _entry_states(adjusted, protocol)
    features = _features(execution_prices, pd.Timestamp(protocol["validation_data"]["evaluation_start"]))
    events, outcomes = _validation_events(states, execution_prices, features, protocol)
    if events.empty:
        raise ValueError("frozen signal produced no complete validation events")
    metrics = _strategy_metrics(states, execution, protocol)
    primary_events, trend_summary, ledger_2023 = _diagnostic_tables(repo_root, protocol)
    peak = _peak_bootstrap(primary_events, protocol)
    pool_audit, matched = _matched_control(events, execution_prices, features, outcomes, protocol)
    distribution = matched.pop("distribution")
    leave_two = _leave_two_out(events)
    label, reasons = _label(events, matched, protocol)

    annual_rows = []
    for year, group in events.groupby("year", sort=True):
        annual_rows.append({
            "year": int(year),
            "trades": len(group),
            "mean_return_4": float(group["net_return_4"].mean()),
            "mean_return_5": float(group["net_return_5"].mean()),
            "mean_return_6": float(group["net_return_6"].mean()),
            "mean_band_return": float(group["band_return_4_6"].mean()),
        })
    annual = pd.DataFrame(annual_rows)
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "evidence_label": label,
        "label_reasons": reasons,
        "closed_trades": len(events),
        "mean_returns": {str(h): float(events[f"net_return_{h}"].mean()) for h in protocol["holding_horizons"]},
        "band_mean_return": float(events["band_return_4_6"].mean()),
        "leave_two_out_minimum_band_mean": leave_two,
        "matched_control": matched,
        "primary_peak_bootstrap": peak,
        "primary_downtrend_trades": int(primary_events["downtrend_60"].sum()),
        "signal_evidence": signal_evidence,
    }
    _write_csv(events, artifacts / "512100_trade_horizon_matrix.csv")
    _write_csv(metrics, artifacts / "512100_strategy_metrics.csv")
    _write_csv(annual, artifacts / "512100_annual_summary.csv")
    _write_csv(pool_audit, artifacts / "512100_matched_pool_audit.csv")
    _write_csv(distribution, artifacts / "512100_matched_control_distribution.csv")
    _write_csv(trend_summary, artifacts / "510500_downtrend_summary.csv")
    _write_csv(ledger_2023, artifacts / "510500_2023_trade_ledger.csv")
    _write_json(artifacts / "validation_summary.json", summary)
    output_names = [
        "512100_data_manifest.json", "512100_adjusted_daily.csv", "512100_execution_daily.csv",
        "512100_trade_horizon_matrix.csv", "512100_strategy_metrics.csv",
        "512100_annual_summary.csv", "512100_matched_pool_audit.csv",
        "512100_matched_control_distribution.csv", "510500_downtrend_summary.csv",
        "510500_2023_trade_ledger.csv", "validation_summary.json",
    ]
    _write_json(artifacts / "run_evidence.json", {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "data_snapshot": data_manifest,
        "evidence_label": label,
        "outputs": {
            name: {"bytes": (artifacts / name).stat().st_size, "sha256": _sha256(artifacts / name)}
            for name in output_names
        },
    })

    attempt_log = _read_json(artifacts / "attempt_log.json")
    attempt_log["attempts"].append({
        "attempt": len(attempt_log["attempts"]) + 1,
        "stage": "daily_only_cross_sectional_validation",
        "status": "COMPLETE",
        "scientific_results_observed": True,
        "protocol_changed": False,
    })
    _write_json(artifacts / "attempt_log.json", attempt_log)
    downtrend_count = int(primary_events["downtrend_60"].sum())
    (experiment_dir / "03_execution.md").write_text(
        f"# {EXPERIMENT_ID} 执行\n\n状态：COMPLETE。\n\n"
        f"协议先于512100数据冻结。标准三频准备因供应商30分钟与日线开盘价不一致而停止；"
        f"随后仅获取本轮实际使用的后复权日线和未复权执行日线，并将快照和来源元数据纳入归档。"
        f"完成{len(events)}笔512100闭合事件、10,000次路径匹配随机对照，以及510500峰值、"
        f"60日趋势和2023年逐笔归因。未调参、未创建候选、未调用SM或PTE。\n\n"
        f"证据标签：`{label}`。\n",
        encoding="utf-8",
    )
    means = summary["mean_returns"]
    (experiment_dir / "04_conclusion.md").write_text(
        f"# {EXPERIMENT_ID} 结论\n\n状态：COMPLETE。横截面机制证据标签为`{label}`。\n\n"
        "## 512100横截面验证\n\n"
        f"冻结规则共产生{len(events)}笔完整事件。4、5、6日平均净收益分别为"
        f"{_pct(means['4'])}、{_pct(means['5'])}、{_pct(means['6'])}，机制带均值为"
        f"{_pct(summary['band_mean_return'])}。任意删除两笔后的机制带最低均值为"
        f"{_pct(leave_two)}。\n\n"
        f"路径匹配随机对照中，实际机制带位于{_pct(float(matched['actual_percentile']))}分位；"
        f"零假设中位数为{_pct(float(matched['null_median']))}，90%区间为"
        f"[{_pct(float(matched['null_lower_90']))}, {_pct(float(matched['null_upper_90']))}]。\n\n"
        "## 510500补充诊断\n\n"
        f"5日成为3至10日观察集合最高点的重采样频率为"
        f"{_pct(float(peak['five_exact_peak_frequency']))}，距离最高点不超过10%的频率为"
        f"{_pct(float(peak['five_within_10pct_of_peak_frequency']))}；4至6日机制带均值保持为正的"
        f"频率为{_pct(float(peak['band_mean_positive_frequency']))}。\n\n"
        f"25笔510500交易中有{downtrend_count}笔发生在预注册的60日下行趋势；明细和2023年"
        "逐笔账本均已归档。趋势和波动率只作诊断，没有形成过滤器。\n\n"
        "## 裁决\n\n"
        + (
            "横截面证据满足预注册的全部有利条件，可以进入首个S002正式候选的人工评审。"
            if label == "FAVORABLE"
            else "横截面证据未满足全部有利条件，按预注册规则保留风险后进入人工评审。"
            if label == "MIXED"
            else "横截面证据触发停止条件，当前三连跌机制路线不应创建候选。"
        )
        + "本轮没有自动创建、冻结或部署策略。\n\n"
        "## 边界\n\n512100是在规则冻结后读取的跨标的机制证据，但与510500同属A股中小盘宽基，市场状态"
        "并不独立；本结果不能替代未来时间前瞻观察。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment_dir,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S002",
            "symbol": "510500.SH",
            "validation_symbol": "512100.SH",
            "development_cutoff": protocol["research_target"]["development_cutoff"],
            "evidence_label": label,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment_dir)


if __name__ == "__main__":
    main()
