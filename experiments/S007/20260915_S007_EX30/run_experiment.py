from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
from dataflows.tushare_common import get_tushare_pro
from dataflows.tushare_etf import fetch_etf_ohlcv
from strategy_evaluator import paired_stationary_bootstrap

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX30"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _portfolio_returns(target: pd.Series, prices: pd.DataFrame, fee: float) -> pd.Series:
    desired = target.reindex(prices.index).fillna(0.0).astype(float)
    cash = 1.0
    shares = 0.0
    previous = 0.0
    values = np.empty(len(prices), dtype=float)
    for index, (flag, open_price, close_price) in enumerate(
        zip(
            desired.to_numpy(dtype=float),
            prices["open"].to_numpy(dtype=float),
            prices["close"].to_numpy(dtype=float),
            strict=True,
        )
    ):
        if flag != previous:
            if flag == 1.0:
                shares = cash / (open_price * (1.0 + fee))
                cash = 0.0
            else:
                cash = shares * open_price * (1.0 - fee)
                shares = 0.0
            previous = flag
        values[index] = cash + shares * close_price
    returns = np.empty(len(values), dtype=float)
    returns[0] = values[0] - 1.0
    returns[1:] = values[1:] / values[:-1] - 1.0
    return pd.Series(returns, index=prices.index)


def _closed_trade_ledger(target: pd.Series, prices: pd.DataFrame, fee: float) -> pd.DataFrame:
    desired = target.reindex(prices.index).fillna(0.0).astype(float)
    previous = 0.0
    entry_date: pd.Timestamp | None = None
    entry_open: float | None = None
    rows: list[dict[str, object]] = []
    for date, flag in desired.items():
        if flag == previous:
            continue
        open_price = float(prices.at[date, "open"])
        if flag == 1.0:
            entry_date = pd.Timestamp(date)
            entry_open = open_price
        elif entry_date is not None and entry_open is not None:
            rows.append({
                "entry_date": entry_date,
                "exit_date": pd.Timestamp(date),
                "entry_open": entry_open,
                "exit_open": open_price,
                "net_return": open_price * (1.0 - fee) / (entry_open * (1.0 + fee)) - 1.0,
                "holding_sessions": int(desired.index.get_loc(date) - desired.index.get_loc(entry_date)),
            })
            entry_date = None
            entry_open = None
        previous = flag
    return pd.DataFrame(rows)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_external_returns"):
        raise ValueError("EX30 protocol identity or external-return declaration differs")
    if protocol.get("parameter_search") or any(
        protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    ):
        raise ValueError("EX30 cannot search, create, promote or deploy a candidate")

    sources = protocol["sources"]
    ex28 = repo / str(sources["ex28_archive"])
    ex29 = repo / str(sources["ex29_archive"])
    ex03 = repo / str(sources["ex03_archive"])
    global_archive = repo / str(sources["global_archive"])
    for archive in (ex28, ex29, ex03, global_archive):
        validate_experiment_archive(archive)
    frozen = {
        ex28 / "experiment_manifest.json": sources["ex28_manifest_sha256"],
        ex28 / "artifacts/selected_configuration.json": sources["selected_configuration_sha256"],
        ex29 / "experiment_manifest.json": sources["ex29_manifest_sha256"],
        ex29 / "artifacts/statistical_summary.json": sources["ex29_statistical_summary_sha256"],
        ex03 / "experiment_manifest.json": sources["ex03_manifest_sha256"],
        ex03 / "artifacts/shibor.csv.gz": sources["shibor_sha256"],
        ex03 / "artifacts/broad_risk_indices.csv.gz": sources["broad_risk_indices_sha256"],
        global_archive / "experiment_manifest.json": sources["global_manifest_sha256"],
        global_archive / "artifacts/global_technology_panel.csv.gz": sources["global_technology_panel_sha256"],
        repo / str(sources["effective_search_protocol"]): sources["effective_search_protocol_sha256"],
        repo / str(sources["ex09_script"]): sources["ex09_script_sha256"],
        repo / str(sources["ex27_script"]): sources["ex27_script_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen EX30 source differs: {path}")

    start = str(protocol["data_acquisition"]["start"])
    end = str(protocol["data_acquisition"]["end"])
    symbol = str(protocol["validation_symbol"])
    fetched, fetch_metadata = fetch_etf_ohlcv(symbol, start, end, "daily", env_file=repo / ".env")
    daily = fetched.rename(
        columns={"Date": "date", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume", "Amount": "amount"}
    )
    daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.normalize()
    daily = daily.sort_values("date").drop_duplicates("date", keep="last").set_index("date")
    required_daily = ["open", "high", "low", "close", "volume", "amount"]
    if daily.empty or not set(required_daily).issubset(daily.columns):
        raise ValueError("588300 daily response is empty or incomplete")
    if not np.isfinite(daily[required_daily].to_numpy(dtype=float)).all():
        raise ValueError("588300 daily response contains non-finite values")
    if (daily[["open", "high", "low", "close", "volume"]] <= 0).any().any():
        raise ValueError("588300 daily response contains non-positive market values")

    pro = get_tushare_pro(repo / ".env")
    share_response = pro.etf_share_size(
        ts_code=symbol,
        start_date=start.replace("-", ""),
        end_date=end.replace("-", ""),
        fields="trade_date,ts_code,etf_name,total_share,total_size,nav,close,exchange",
    )
    share = pd.DataFrame() if share_response is None else pd.DataFrame(share_response)
    if share.empty or "total_share" not in share:
        raise ValueError("Tushare returned no usable 588300 ETF share data")
    share["trade_date"] = pd.to_datetime(share["trade_date"], format="%Y%m%d", errors="raise").dt.normalize()
    share["total_share"] = pd.to_numeric(share["total_share"], errors="raise")
    share = share.sort_values("trade_date").drop_duplicates("trade_date", keep="last").set_index("trade_date")
    if (share["total_share"] <= 0).any():
        raise ValueError("588300 ETF share data contains non-positive total_share")
    missing_share_sessions = daily.index.difference(share.index)
    if len(missing_share_sessions):
        raise ValueError(f"588300 ETF share data misses {len(missing_share_sessions)} trading sessions")

    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    daily.reset_index().to_csv(artifacts / "588300_daily_hfq.csv.gz", index=False, compression=compression, lineterminator="\n")
    share.reset_index().to_csv(artifacts / "588300_etf_share_size.csv.gz", index=False, compression=compression, lineterminator="\n")

    sessions = daily.index
    panel = pd.DataFrame(index=sessions)
    shibor = pd.read_csv(ex03 / "artifacts/shibor.csv.gz")
    shibor["date"] = pd.to_datetime(shibor["date"].astype(str), format="%Y%m%d", errors="raise")
    shibor = shibor.set_index("date").sort_index().reindex(sessions).ffill(limit=4)
    panel["risk_shibor_on_change_5d"] = pd.to_numeric(shibor["on"]).diff(5)

    indices = pd.read_csv(ex03 / "artifacts/broad_risk_indices.csv.gz")
    indices["trade_date"] = pd.to_datetime(indices["trade_date"].astype(str), format="%Y%m%d", errors="raise")
    chinext = indices.loc[indices["ts_code"].eq("399006.SZ")].set_index("trade_date").sort_index().reindex(sessions)
    turnover = pd.to_numeric(chinext["turnover_rate_f"])
    panel["risk_chinext_turnover_z20"] = (turnover - turnover.rolling(20).mean()) / turnover.rolling(20).std(ddof=0)

    global_panel = pd.read_csv(global_archive / "artifacts/global_technology_panel.csv.gz")
    global_panel["a_share_session"] = pd.to_datetime(global_panel["a_share_session"])
    spx = global_panel.loc[global_panel["source_code"].eq("SPX")].set_index("a_share_session").sort_index()
    panel["risk_global_spx_return"] = pd.to_numeric(spx["return_pct"]).reindex(sessions) / 100.0

    close = daily["close"].astype(float)
    volume = daily["volume"].astype(float)
    amount = daily["amount"].astype(float)
    panel["price_intraday_range"] = (daily["high"].astype(float) - daily["low"].astype(float)) / close
    panel["price_close_vwap_deviation"] = close / (amount / volume).replace([np.inf, -np.inf], np.nan) - 1.0
    panel["micro_share_change_5d_lag1"] = share["total_share"].reindex(sessions).pct_change(5, fill_method=None).shift(1)
    panel["tsfresh__log_volume_change__mean__lb20"] = np.log(volume).diff().rolling(20, min_periods=20).mean()

    selected = _read(ex28 / "artifacts/selected_configuration.json")
    effective = _read(repo / str(sources["effective_search_protocol"]))
    helpers = _load_module("s007_ex09_helpers", repo / str(sources["ex09_script"]))
    gated = _load_module("s007_ex27_helpers", repo / str(sources["ex27_script"]))
    normalization = effective["normalization"]
    scores = pd.DataFrame(index=sessions)
    for feature, binding in effective["feature_bindings"].items():
        if feature not in panel:
            raise ValueError(f"588300 feature panel misses frozen feature: {feature}")
        scores[feature] = helpers._causal_percentile(
            panel[feature],
            int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        ) * int(binding["orientation"])

    weights = {name: float(value) for name, value in selected["weights"].items()}
    base_score = scores.mul(pd.Series(weights), axis=1).sum(axis=1).where(scores[list(weights)].notna().all(axis=1))
    params = selected["params"]
    calibration = protocol["calibration"]
    calibration_mask = sessions.to_series().between(calibration["start"], calibration["end"])
    calibration_values = base_score.loc[calibration_mask].dropna()
    if len(calibration_values) < 300:
        raise ValueError("588300 has fewer than 300 valid calibration base scores")
    entry_threshold = float(calibration_values.quantile(float(params["entry_quantile"])))
    exit_threshold = float(calibration_values.quantile(float(params["exit_quantile"])))
    if exit_threshold >= entry_threshold:
        raise ValueError("588300 calibrated exit threshold is not below entry threshold")

    base_decision = helpers._hysteresis(base_score, entry_threshold, exit_threshold)
    base_entries = base_decision.gt(base_decision.shift(1, fill_value=0.0)) & calibration_mask
    confirm_share = float(params["confirm_share_fraction"])
    confirmation_score = (
        scores["micro_share_change_5d_lag1"] * confirm_share
        + scores["tsfresh__log_volume_change__mean__lb20"] * (1.0 - confirm_share)
    )
    threshold_source = confirmation_score.loc[base_entries].dropna()
    if len(threshold_source) < 20:
        raise ValueError("588300 has fewer than 20 calibration entries for the confirmation gate")
    confirmation_threshold = float(threshold_source.quantile(float(params["confirmation_gate_quantile"])))
    decision = gated._gated_hysteresis(
        base_score, confirmation_score, entry_threshold, exit_threshold, confirmation_threshold
    )
    target = decision.shift(1).fillna(0.0)

    evaluation = protocol["external_evaluation"]
    evaluation_mask = sessions.to_series().between(evaluation["start"], evaluation["end"])
    calibration_prices = daily.loc[calibration_mask]
    evaluation_prices = daily.loc[evaluation_mask]
    if len(evaluation_prices) < 500:
        raise ValueError("588300 external evaluation window is unexpectedly short")
    fee = float(protocol["fixed_execution"]["fee_rate_one_way"])
    calibration_metrics = helpers._metrics(target.loc[calibration_mask], calibration_prices, fee)
    evaluation_metrics = helpers._metrics(target.loc[evaluation_mask], evaluation_prices, fee)
    buy_hold_target = pd.Series(1.0, index=evaluation_prices.index)
    buy_hold_metrics = helpers._metrics(buy_hold_target, evaluation_prices, fee)
    strategy_returns = _portfolio_returns(target.loc[evaluation_mask], evaluation_prices, fee)
    buy_hold_returns = _portfolio_returns(buy_hold_target, evaluation_prices, fee)
    trades = _closed_trade_ledger(target.loc[evaluation_mask], evaluation_prices, fee)
    if len(trades) != int(evaluation_metrics["closed_trades"]):
        raise ValueError("588300 closed-trade ledger does not match performance metrics")
    trades.to_csv(artifacts / "external_closed_trades.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    largest_trade = trades.sort_values("net_return", ascending=False).iloc[0]
    without_largest_target = target.loc[evaluation_mask].copy()
    removal_mask = (
        (without_largest_target.index >= pd.Timestamp(largest_trade["entry_date"]))
        & (without_largest_target.index < pd.Timestamp(largest_trade["exit_date"]))
    )
    without_largest_target.loc[removal_mask] = 0.0
    without_largest_metrics = helpers._metrics(without_largest_target, evaluation_prices, fee)
    positive_returns = trades["net_return"].clip(lower=0.0)
    concentration_diagnostic = {
        "role": "POSTHOC_DIAGNOSTIC_NOT_A_PREREGISTERED_GATE",
        "largest_trade_entry": pd.Timestamp(largest_trade["entry_date"]).date().isoformat(),
        "largest_trade_exit": pd.Timestamp(largest_trade["exit_date"]).date().isoformat(),
        "largest_trade_net_return": float(largest_trade["net_return"]),
        "largest_trade_share_of_sum_positive_returns": float(largest_trade["net_return"] / positive_returns.sum()),
        "without_largest_trade_metrics": without_largest_metrics,
        "concentration_warning": bool(
            float(without_largest_metrics["cagr"]) <= float(buy_hold_metrics["cagr"])
            or float(without_largest_metrics["maximum_drawdown"]) < float(protocol["replication_gates"]["maximum_drawdown_limit"])
        ),
    }

    bootstrap = protocol["bootstrap"]
    comparisons = [
        paired_stationary_bootstrap(
            strategy_returns.to_numpy(dtype=float),
            buy_hold_returns.to_numpy(dtype=float),
            champion_id="S007-C001-FIXED-ON-588300",
            comparator_id="BUYHOLD-588300",
            repetitions=int(bootstrap["repetitions"]),
            mean_block_length=int(block_length),
            seed=int(bootstrap["seed"]) + int(block_length),
        )
        for block_length in bootstrap["mean_block_lengths"]
    ]
    primary = next(item for item in comparisons if item.mean_block_length == 21)
    gates_spec = protocol["replication_gates"]
    checks = {
        "cagr_above_buyhold": float(evaluation_metrics["cagr"]) > float(buy_hold_metrics["cagr"]),
        "maximum_drawdown_within_20pct": float(evaluation_metrics["maximum_drawdown"]) >= float(gates_spec["maximum_drawdown_limit"]),
        "maximum_drawdown_better_than_buyhold": float(evaluation_metrics["maximum_drawdown"]) > float(buy_hold_metrics["maximum_drawdown"]),
        "calmar_above_buyhold": float(evaluation_metrics["calmar"]) > float(buy_hold_metrics["calmar"]),
        "rolling_60_closed_trades_median_at_least_5": float(evaluation_metrics["rolling_60_closed_trades_median"]) >= float(gates_spec["minimum_rolling_60_closed_trades_median"]),
        "bootstrap_drawdown_win_probability_at_least_60pct": primary.max_drawdown.probability_favorable >= float(gates_spec["minimum_primary_drawdown_win_probability"]),
        "bootstrap_calmar_win_probability_at_least_60pct": primary.calmar.probability_favorable >= float(gates_spec["minimum_primary_calmar_win_probability"]),
    }
    partial_checks = {
        "positive_cagr": float(evaluation_metrics["cagr"]) > 0.0,
        "maximum_drawdown_better_than_buyhold": checks["maximum_drawdown_better_than_buyhold"],
        "calmar_above_buyhold": checks["calmar_above_buyhold"],
        "bootstrap_calmar_win_probability_above_50pct": primary.calmar.probability_favorable > 0.5,
    }
    if all(checks.values()):
        decision_code = "FULL_REPLICATION"
        evidence_label = "FAVORABLE"
    elif all(partial_checks.values()):
        decision_code = "PARTIAL_REPLICATION"
        evidence_label = "MIXED"
    else:
        decision_code = "NO_REPLICATION"
        evidence_label = "WEAK"

    yearly_rows: list[dict[str, object]] = []
    for year, year_prices in evaluation_prices.groupby(evaluation_prices.index.year):
        year_mask = evaluation_prices.index.year == year
        metrics = helpers._metrics(target.loc[evaluation_mask].loc[year_mask], year_prices, fee)
        yearly_rows.append({"year": int(year), **metrics})
    pd.DataFrame(yearly_rows).to_csv(artifacts / "external_yearly_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    feature_output = panel.join(scores.add_prefix("score."), how="left")
    feature_output["base_score"] = base_score
    feature_output["confirmation_score"] = confirmation_score
    feature_output["decision_target"] = decision
    feature_output["execution_target"] = target
    feature_output.reset_index(names="date").to_csv(
        artifacts / "588300_fixed_formula_panel.csv.gz", index=False, compression=compression, lineterminator="\n"
    )

    data_identity = {
        "schema_version": 1,
        "symbol": symbol,
        "vendor_metadata": fetch_metadata,
        "daily_sessions": len(daily),
        "share_sessions": len(share),
        "first_session": sessions.min().date().isoformat(),
        "last_session": sessions.max().date().isoformat(),
        "daily_sha256": _sha256(artifacts / "588300_daily_hfq.csv.gz"),
        "share_sha256": _sha256(artifacts / "588300_etf_share_size.csv.gz"),
        "feature_panel_sha256": _sha256(artifacts / "588300_fixed_formula_panel.csv.gz"),
        "missing_share_sessions": len(missing_share_sessions),
    }
    _write(artifacts / "external_data_identity.json", data_identity)
    _write(
        artifacts / "external_validation_details.json",
        {
            "schema_version": 1,
            "selected_source_trial_id": selected["source_trial_id"],
            "fixed_params": params,
            "fixed_weights": weights,
            "recalibrated_thresholds": {
                "entry": entry_threshold,
                "exit": exit_threshold,
                "confirmation": confirmation_threshold,
                "valid_calibration_scores": len(calibration_values),
                "confirmation_threshold_source_entries": len(threshold_source),
            },
            "calibration_metrics_diagnostic_only": calibration_metrics,
            "external_evaluation_metrics": evaluation_metrics,
            "buyhold_metrics": buy_hold_metrics,
            "replication_checks": checks,
            "partial_replication_checks": partial_checks,
            "paired_bootstrap": [item.to_dict() for item in comparisons],
            "posthoc_concentration_diagnostic": concentration_diagnostic,
        },
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "decision": decision_code,
        "external_evidence_label": evidence_label,
        "validation_symbol": symbol,
        "calibration_sessions": len(calibration_prices),
        "external_evaluation_sessions": len(evaluation_prices),
        "external_cagr": float(evaluation_metrics["cagr"]),
        "external_maximum_drawdown": float(evaluation_metrics["maximum_drawdown"]),
        "external_calmar": float(evaluation_metrics["calmar"]),
        "external_closed_trades": int(evaluation_metrics["closed_trades"]),
        "external_rolling_60_closed_trades_median": float(evaluation_metrics["rolling_60_closed_trades_median"]),
        "external_rolling_60_closed_trades_p10": float(evaluation_metrics["rolling_60_closed_trades_p10"]),
        "buyhold_cagr": float(buy_hold_metrics["cagr"]),
        "buyhold_maximum_drawdown": float(buy_hold_metrics["maximum_drawdown"]),
        "buyhold_calmar": float(buy_hold_metrics["calmar"]),
        "bootstrap_21d_cagr_win_probability": primary.cagr.probability_favorable,
        "bootstrap_21d_drawdown_win_probability": primary.max_drawdown.probability_favorable,
        "bootstrap_21d_calmar_win_probability": primary.calmar.probability_favorable,
        "largest_trade_net_return": concentration_diagnostic["largest_trade_net_return"],
        "without_largest_trade_cagr": float(without_largest_metrics["cagr"]),
        "without_largest_trade_maximum_drawdown": float(without_largest_metrics["maximum_drawdown"]),
        "posthoc_concentration_warning": concentration_diagnostic["concentration_warning"],
        "candidate_created": False,
        "promotion_allowed": False,
    }
    _write(artifacts / "external_validation_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S007 EX30 执行\n\n"
        f"状态：`COMPLETE`。Tushare返回588300日线与ETF份额各{len(daily)}个交易日。"
        f"冻结公式使用{len(calibration_prices)}个交易日完成无收益选参的分布校准，并在"
        f"{len(evaluation_prices)}个独立评价交易日运行。没有搜索或修改参数。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX30 结论\n\n"
        f"裁决：`{decision_code}`，外部证据标签`{evidence_label}`。588300评价期年化"
        f"{evaluation_metrics['cagr']:.2%}、最大回撤{evaluation_metrics['maximum_drawdown']:.2%}、"
        f"卡玛{evaluation_metrics['calmar']:.2f}、闭合交易{evaluation_metrics['closed_trades']}笔、"
        f"滚动60日闭合交易中位数/P10为{evaluation_metrics['rolling_60_closed_trades_median']:.1f}/"
        f"{evaluation_metrics['rolling_60_closed_trades_p10']:.1f}。同口径BuyHold年化"
        f"{buy_hold_metrics['cagr']:.2%}、最大回撤{buy_hold_metrics['maximum_drawdown']:.2%}、"
        f"卡玛{buy_hold_metrics['calmar']:.2f}。21日区块Bootstrap的年化/回撤/卡玛胜出概率为"
        f"{primary.cagr.probability_favorable:.2%}/{primary.max_drawdown.probability_favorable:.2%}/"
        f"{primary.calmar.probability_favorable:.2%}。事后集中度诊断发现最大单笔收益"
        f"{concentration_diagnostic['largest_trade_net_return']:.2%}；删除该笔后年化"
        f"{without_largest_metrics['cagr']:.2%}、最大回撤{without_largest_metrics['maximum_drawdown']:.2%}。"
        "该诊断不改写预注册裁决，但必须作为候选登记评审的集中度风险。"
        "本轮未创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["validation_symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision_code,
            "external_evidence_label": evidence_label,
            "candidate_created": False,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
