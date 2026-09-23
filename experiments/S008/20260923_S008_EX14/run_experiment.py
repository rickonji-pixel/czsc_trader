from __future__ import annotations

from collections import Counter
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from tsfresh.feature_extraction import feature_calculators as fc

from dataflows import DataRequest, DataStatus, Dataflows

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX14"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _load_target(repo: Path, protocol: dict[str, object]) -> pd.DataFrame:
    manifest_path = repo / "data/raw/518880_manifest.json"
    if _sha256(manifest_path) != protocol["sources"]["target_manifest_sha256"]:
        raise ValueError("target manifest differs from frozen identity")
    manifest = _read(manifest_path)
    frames: list[pd.DataFrame] = []
    for year in range(2013, 2025):
        name = f"518880_daily_{year}.csv"
        path = repo / "data/raw" / name
        record = manifest["files"][name]
        if _sha256(path) != record["sha256"]:
            raise ValueError(f"target daily file differs from manifest: {name}")
        frames.append(pd.read_csv(path))
    frame = pd.concat(frames, ignore_index=True)
    frame["Date"] = pd.to_datetime(frame.pop("date"), errors="raise")
    frame = frame.loc[
        frame["Date"].between(
            pd.Timestamp(protocol["development_start"]),
            pd.Timestamp(protocol["development_cutoff"]),
        )
    ].sort_values("Date")
    if len(frame) != 2781 or frame["Date"].duplicated().any():
        raise ValueError("target development sessions differ from frozen data gate")
    return frame.set_index("Date")


def _daily(frame: pd.DataFrame, sessions: pd.DatetimeIndex, shift: int) -> pd.DataFrame:
    aligned = frame.set_index("Date").sort_index().reindex(sessions).ffill()
    return aligned.shift(shift) if shift else aligned


def _monthly(frame: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    values = frame.copy().sort_values("Date")
    available_calendar = values["Date"] + pd.offsets.MonthBegin(2)
    effective_sessions = sessions.searchsorted(available_calendar)
    if (effective_sessions >= len(sessions)).any():
        raise ValueError("monthly availability exceeds development sessions")
    values.index = sessions[effective_sessions]
    values = values.drop(columns=["Date"])
    if values.index.duplicated().any():
        raise ValueError("monthly effective sessions are not unique")
    return values.reindex(sessions).ffill()


def _zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window).mean()
    std = series.rolling(window).std(ddof=0).replace(0, np.nan)
    return (series - mean) / std


def _efficiency(close: pd.Series, window: int) -> pd.Series:
    return close.diff(window).abs() / close.diff().abs().rolling(window).sum().replace(0, np.nan)


def _add(
    panel: pd.DataFrame,
    catalog: list[dict[str, str]],
    name: str,
    values: pd.Series,
    hypothesis: str,
    family: str,
    role: str,
    formula: str,
    availability: str,
) -> None:
    panel[name] = pd.to_numeric(values, errors="coerce").astype(float)
    catalog.append(
        {
            "feature": name,
            "hypothesis_id": hypothesis,
            "information_family": family,
            "financial_role": role,
            "formula": formula,
            "availability": availability,
        }
    )


def _rolling_tsfresh(series: pd.Series, window: int, calculator: Callable) -> pd.Series:
    return series.rolling(window, min_periods=window).apply(
        lambda values: float(calculator(np.asarray(values, dtype=float))), raw=True
    )


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    prohibited = (
        "reads_future_return_labels",
        "selects_input",
        "selects_template",
        "starts_search",
        "candidate_generation",
        "reads_sealed_validation",
        "mutates_catalog",
        "mutates_platform",
        "mutates_pte",
    )
    if any(protocol.get(key) for key in prohibited):
        raise ValueError("materialization cannot read labels, select, search or mutate")

    source_paths = {
        "materials_sha256": repo / "research/S008/materials.json",
        "target_manifest_sha256": repo / "data/raw/518880_manifest.json",
        "ex13_manifest_sha256": repo
        / "experiments/S008/20260923_S008_EX13/experiment_manifest.json",
        "ex13_gate_sha256": repo
        / "experiments/S008/20260923_S008_EX13/artifacts/dfls_gate.csv",
        "dfls_contract_sha256": repo / "packages/dataflows/src/dataflows/contract.py",
        "dfls_facade_sha256": repo / "packages/dataflows/src/dataflows/facade.py",
        "dfls_adapter_sha256": repo
        / "packages/dataflows/src/dataflows/tushare_strategy_data.py",
    }
    for key, path in source_paths.items():
        if _sha256(path) != protocol["sources"][key]:
            raise ValueError(f"frozen source differs: {path}")
    validate_experiment_archive(
        repo / "experiments/S008/20260923_S008_EX13"
    )

    target = _load_target(repo, protocol)
    sessions = pd.DatetimeIndex(target.index, name="Date")
    ex13_gate = pd.read_csv(source_paths["ex13_gate_sha256"])
    expected_identities = dict(zip(ex13_gate["dataset"], ex13_gate["content_sha256"]))
    frames: dict[str, pd.DataFrame] = {}
    identity_rows: list[dict[str, object]] = []
    dataflows = Dataflows()
    for item in protocol["inputs"]:
        dataset = str(item["dataset"])
        result = dataflows.fetch(
            DataRequest(
                dataset=dataset,
                symbol=item["symbol"],
                start=str(protocol["development_start"]),
                end=str(protocol["development_cutoff"]),
                required_cutoff=str(protocol["development_cutoff"]),
                frequency="monthly" if dataset.endswith("_monthly") else "daily",
                options={"env_file": repo / ".env"},
            )
        )
        if result.status is not DataStatus.READY or result.identity is None:
            code = result.error.code if result.error else "UNKNOWN"
            raise ValueError(f"DFLS input not READY: {dataset} {code}")
        identity = result.identity
        expected = expected_identities.get(dataset)
        if expected is not None and identity.content_sha256 != expected:
            raise ValueError(f"DFLS input identity differs from EX13: {dataset}")
        frames[dataset] = result.dataframe
        identity_rows.append(
            {
                "dataset": dataset,
                "symbol": item["symbol"] or "",
                "alignment": item["alignment"],
                "rows": len(result.dataframe),
                "data_start": identity.data_start,
                "data_cutoff": identity.data_cutoff,
                "content_sha256": identity.content_sha256,
                "source": identity.source,
            }
        )
    pd.DataFrame(identity_rows).to_csv(
        artifacts / "input_identities.csv", index=False, encoding="utf-8"
    )

    panel = pd.DataFrame(index=sessions)
    catalog: list[dict[str, str]] = []
    close = pd.to_numeric(target["close"])
    high = pd.to_numeric(target["high"])
    low = pd.to_numeric(target["low"])
    volume = pd.to_numeric(target["volume"])
    amount = pd.to_numeric(target["amount"])
    target_returns = {h: close.pct_change(h, fill_method=None) for h in (1, 5, 20, 60, 120)}

    # H01: lower real rates reduce the opportunity cost of holding non-yielding gold.
    real_yield = _daily(frames["macro.us_real_yield_daily"], sessions, 1)
    shibor = _daily(frames["macro.shibor_daily"], sessions, 0)
    for tenor, column in (("5y", "RealYield5YPercent"), ("10y", "RealYield10YPercent")):
        values = pd.to_numeric(real_yield[column])
        _add(panel, catalog, f"rate_us_real_{tenor}_level", values, "H01-REAL-RATE-OPPORTUNITY-COST", "REAL_RATE", "OPPORTUNITY", column, "prior China session")
        for horizon in (5, 20):
            _add(panel, catalog, f"rate_us_real_{tenor}_change_{horizon}d", values.diff(horizon), "H01-REAL-RATE-OPPORTUNITY-COST", "REAL_RATE", "OPPORTUNITY", f"diff({horizon})", "prior China session")
    _add(panel, catalog, "rate_us_real_curve_10y_5y", real_yield["RealYield10YPercent"] - real_yield["RealYield5YPercent"], "H01-REAL-RATE-OPPORTUNITY-COST", "REAL_RATE_CURVE", "RISK_CONTEXT", "10Y minus 5Y real yield", "prior China session")
    _add(panel, catalog, "rate_us_real_10y_z120", _zscore(real_yield["RealYield10YPercent"], 120), "H01-REAL-RATE-OPPORTUNITY-COST", "REAL_RATE", "RISK_CONTEXT", "zscore(10Y,120)", "prior China session")
    overnight = pd.to_numeric(shibor["OvernightRate"])
    _add(panel, catalog, "rate_shibor_overnight_level", overnight, "H01-REAL-RATE-OPPORTUNITY-COST", "DOMESTIC_LIQUIDITY", "RISK_CONTEXT", "SHIBOR overnight", "T 12:00")
    for horizon in (5, 20):
        _add(panel, catalog, f"rate_shibor_overnight_change_{horizon}d", overnight.diff(horizon), "H01-REAL-RATE-OPPORTUNITY-COST", "DOMESTIC_LIQUIDITY", "RISK_CONTEXT", f"diff({horizon})", "T 12:00")

    # H02: currency translation and domestic gold repricing.
    fx = _daily(frames["fx.usdcnh_daily"], sessions, 1)
    fx_mid = (pd.to_numeric(fx["BidClose"]) + pd.to_numeric(fx["AskClose"])) / 2
    sge = _daily(frames["metal.sge_gold_daily"], sessions, 1)
    sge_close = pd.to_numeric(sge["Close"])
    for horizon in (1, 5, 20):
        fx_ret = fx_mid.pct_change(horizon, fill_method=None)
        sge_ret = sge_close.pct_change(horizon, fill_method=None)
        _add(panel, catalog, f"currency_usdcnh_return_{horizon}d", fx_ret, "H02-CURRENCY-TRANSLATION", "CURRENCY_TRANSLATION", "OPPORTUNITY", f"USDCNH mid return {horizon}d", "prior China session")
        _add(panel, catalog, f"gold_sge_return_{horizon}d", sge_ret, "H02-CURRENCY-TRANSLATION", "GOLD_BENCHMARK", "CONFIRMATION", f"SGE close return {horizon}d", "prior China session")
    _add(panel, catalog, "currency_usdcnh_z120", _zscore(fx_mid, 120), "H02-CURRENCY-TRANSLATION", "CURRENCY_TRANSLATION", "RISK_CONTEXT", "USDCNH mid zscore 120", "prior China session")
    _add(panel, catalog, "gold_sge_trend_distance_60", sge_close / sge_close.rolling(60).mean() - 1, "H02-CURRENCY-TRANSLATION", "GOLD_BENCHMARK", "CONFIRMATION", "SGE close/MA60-1", "prior China session")
    _add(panel, catalog, "translation_joint_impulse_5d", fx_mid.pct_change(5, fill_method=None) + sge_close.pct_change(5, fill_method=None), "H02-CURRENCY-TRANSLATION", "CURRENCY_GOLD_INTERACTION", "OPPORTUNITY", "USDCNH return5 + SGE return5", "prior China session")
    _add(panel, catalog, "translation_etf_sge_gap_5d", target_returns[5] - sge_close.pct_change(5, fill_method=None), "H02-CURRENCY-TRANSLATION", "CURRENCY_GOLD_INTERACTION", "ENTRY_TIMING", "ETF return5 - causal SGE return5", "T close")

    # H03: safe-haven demand competes with liquidity-driven selling.
    index = _daily(frames["index.domestic_daily"], sessions, 0)
    index_close = pd.to_numeric(index["Close"])
    index_returns = index_close.pct_change(fill_method=None)
    for horizon in (1, 5, 20):
        _add(panel, catalog, f"risk_sse_return_{horizon}d", index_close.pct_change(horizon, fill_method=None), "H03-SAFE-HAVEN-RISK-APPETITE", "EQUITY_RISK_APPETITE", "RISK_CONTEXT", f"SSE return {horizon}d", "T close")
    index_vol20 = index_returns.rolling(20).std(ddof=0)
    _add(panel, catalog, "risk_sse_volatility_20", index_vol20, "H03-SAFE-HAVEN-RISK-APPETITE", "EQUITY_RISK_APPETITE", "RISK_CONTEXT", "std(return1,20)", "T close")
    _add(panel, catalog, "risk_sse_drawdown_60", index_close / index_close.rolling(60).max() - 1, "H03-SAFE-HAVEN-RISK-APPETITE", "EQUITY_STRESS", "RISK_CONTEXT", "close/max60-1", "T close")
    _add(panel, catalog, "risk_sse_intraday_range", (index["High"] - index["Low"]) / index["Close"], "H03-SAFE-HAVEN-RISK-APPETITE", "EQUITY_STRESS", "RISK_CONTEXT", "(high-low)/close", "T close")
    index_basic = _daily(frames["index.daily_basic"], sessions, 0)
    turnover = pd.to_numeric(index_basic["TurnoverRateFreeFloat"])
    _add(panel, catalog, "risk_sse_turnover_z20", _zscore(turnover, 20), "H03-SAFE-HAVEN-RISK-APPETITE", "LIQUIDITY_PRESSURE", "RISK_CONTEXT", "turnover zscore20", "T close")
    _add(panel, catalog, "risk_liquidity_selloff_interaction", (-index_returns).clip(lower=0) * _zscore(index_vol20, 60).clip(lower=0), "H03-SAFE-HAVEN-RISK-APPETITE", "LIQUIDITY_PRESSURE", "CONFIRMATION", "negative equity return times positive vol zscore", "T close")

    # H04: strategic flows may confirm demand or reveal crowding.
    shares = _daily(frames["etf.share_size"], sessions, 1)
    total_share = pd.to_numeric(shares["TotalShare"])
    for horizon in (1, 5, 20):
        _add(panel, catalog, f"flow_etf_share_change_{horizon}d", total_share.pct_change(horizon, fill_method=None), "H04-STRATEGIC-FLOW-POSITIONING", "ETF_PRIMARY_FLOW", "CONFIRMATION", f"share return {horizon}d", "T uses T-1 publication")
    _add(panel, catalog, "flow_etf_share_z120", _zscore(total_share, 120), "H04-STRATEGIC-FLOW-POSITIONING", "ETF_PRIMARY_FLOW", "CROWDING_RISK", "share zscore120", "T uses T-1 publication")
    _add(panel, catalog, "flow_price_divergence_5d", total_share.pct_change(5, fill_method=None) - target_returns[5], "H04-STRATEGIC-FLOW-POSITIONING", "FLOW_PRICE_DIVERGENCE", "CROWDING_RISK", "share return5 - ETF return5", "T close")
    _add(panel, catalog, "flow_price_confirmation_20d", total_share.pct_change(20, fill_method=None) * target_returns[20], "H04-STRATEGIC-FLOW-POSITIONING", "FLOW_PRICE_INTERACTION", "CONFIRMATION", "share return20 times ETF return20", "T close")
    for window in (20, 60):
        _add(panel, catalog, f"flow_etf_volume_ratio_{window}", volume / volume.rolling(window).mean(), "H04-STRATEGIC-FLOW-POSITIONING", "TRADING_FLOW", "CONFIRMATION", f"volume/mean{window}", "T close")
    _add(panel, catalog, "flow_etf_amount_ratio_20", amount / amount.rolling(20).mean(), "H04-STRATEGIC-FLOW-POSITIONING", "TRADING_FLOW", "CROWDING_RISK", "amount/mean20", "T close")
    sge_volume = pd.to_numeric(sge["Volume"])
    _add(panel, catalog, "flow_sge_volume_ratio_20", sge_volume / sge_volume.rolling(20).mean(), "H04-STRATEGIC-FLOW-POSITIONING", "GOLD_MARKET_FLOW", "CONFIRMATION", "SGE volume/mean20", "prior China session")

    # H05: low-frequency inflation and money states use the conservative M+2 rule.
    cpi = _monthly(frames["macro.cn_cpi_monthly"], sessions)
    ppi = _monthly(frames["macro.cn_ppi_monthly"], sessions)
    money = _monthly(frames["macro.cn_money_monthly"], sessions)
    macro_series = {
        "macro_cpi_yoy": cpi["NationalYoYPercent"],
        "macro_cpi_mom": cpi["NationalMoMPercent"],
        "macro_ppi_yoy": ppi["ProducerYoYPercent"],
        "macro_ppi_mom": ppi["ProducerMoMPercent"],
        "macro_m1_yoy": money["M1YoYPercent"],
        "macro_m2_yoy": money["M2YoYPercent"],
    }
    for name, values in macro_series.items():
        _add(panel, catalog, name, values, "H05-INFLATION-MONETARY-REGIME", "MACRO_REGIME", "OPPORTUNITY", name, "reference month M usable M+2")
    for name, values in (("cpi_yoy", cpi["NationalYoYPercent"]), ("ppi_yoy", ppi["ProducerYoYPercent"]), ("m1_yoy", money["M1YoYPercent"]), ("m2_yoy", money["M2YoYPercent"])):
        _add(panel, catalog, f"macro_{name}_change_3m", values.diff().rolling(3).sum(), "H05-INFLATION-MONETARY-REGIME", "MACRO_IMPULSE", "RISK_CONTEXT", "three released monthly changes", "reference month M usable M+2")
    _add(panel, catalog, "macro_money_scissors_m1_m2", money["M1YoYPercent"] - money["M2YoYPercent"], "H05-INFLATION-MONETARY-REGIME", "MONETARY_REGIME", "OPPORTUNITY", "M1 YoY - M2 YoY", "reference month M usable M+2")
    _add(panel, catalog, "macro_real_liquidity_m2_cpi", money["M2YoYPercent"] - cpi["NationalYoYPercent"], "H05-INFLATION-MONETARY-REGIME", "MONETARY_REGIME", "OPPORTUNITY", "M2 YoY - CPI YoY", "reference month M usable M+2")
    _add(panel, catalog, "macro_pipeline_inflation_ppi_cpi", ppi["ProducerYoYPercent"] - cpi["NationalYoYPercent"], "H05-INFLATION-MONETARY-REGIME", "INFLATION_REGIME", "RISK_CONTEXT", "PPI YoY - CPI YoY", "reference month M usable M+2")
    _add(panel, catalog, "macro_domestic_real_cash_proxy", overnight - cpi["NationalYoYPercent"], "H05-INFLATION-MONETARY-REGIME", "REAL_LIQUIDITY", "RISK_CONTEXT", "SHIBOR overnight - CPI YoY", "T 12:00 with causal CPI")

    # H06: price and volatility state is a role component, not an isolated strategy.
    for horizon in (5, 20, 60, 120):
        _add(panel, catalog, f"price_return_{horizon}d", target_returns[horizon], "H06-PRICE-VOLATILITY-STATE", "TREND_MOMENTUM", "ENTRY_TIMING", f"ETF close return {horizon}d", "T close")
    for window in (20, 60, 120):
        _add(panel, catalog, f"price_trend_distance_{window}", close / close.rolling(window).mean() - 1, "H06-PRICE-VOLATILITY-STATE", "TREND_STATE", "ENTRY_TIMING", f"close/MA{window}-1", "T close")
    for window in (60, 120):
        high_roll = high.rolling(window).max()
        low_roll = low.rolling(window).min()
        _add(panel, catalog, f"price_breakout_position_{window}", (close - low_roll) / (high_roll - low_roll).replace(0, np.nan), "H06-PRICE-VOLATILITY-STATE", "BREAKOUT_STATE", "ENTRY_TIMING", f"position in {window}d range", "T close")
        _add(panel, catalog, f"price_drawdown_{window}", close / close.rolling(window).max() - 1, "H06-PRICE-VOLATILITY-STATE", "DRAWDOWN_STATE", "EXIT_RISK", f"close/max{window}-1", "T close")
    volatility_20 = target_returns[1].rolling(20).std(ddof=0)
    volatility_60 = target_returns[1].rolling(60).std(ddof=0)
    _add(panel, catalog, "price_volatility_20", volatility_20, "H06-PRICE-VOLATILITY-STATE", "VOLATILITY_STATE", "EXIT_RISK", "std(return1,20)", "T close")
    _add(panel, catalog, "price_volatility_60", volatility_60, "H06-PRICE-VOLATILITY-STATE", "VOLATILITY_STATE", "EXIT_RISK", "std(return1,60)", "T close")
    _add(panel, catalog, "price_volatility_ratio_20_60", volatility_20 / volatility_60.replace(0, np.nan), "H06-PRICE-VOLATILITY-STATE", "VOLATILITY_STATE", "RISK_CONTEXT", "vol20/vol60", "T close")
    for window in (20, 60):
        _add(panel, catalog, f"price_efficiency_{window}", _efficiency(close, window), "H06-PRICE-VOLATILITY-STATE", "TREND_QUALITY", "CONFIRMATION", f"efficiency ratio {window}", "T close")
    intraday_range = (high - low) / close
    _add(panel, catalog, "price_intraday_range", intraday_range, "H06-PRICE-VOLATILITY-STATE", "VOLATILITY_STATE", "EXIT_RISK", "(high-low)/close", "T close")
    _add(panel, catalog, "price_volume_z20", _zscore(volume, 20), "H06-PRICE-VOLATILITY-STATE", "VOLUME_LIQUIDITY", "CONFIRMATION", "volume zscore20", "T close")
    _add(panel, catalog, "price_amount_z20", _zscore(amount, 20), "H06-PRICE-VOLATILITY-STATE", "VOLUME_LIQUIDITY", "CONFIRMATION", "amount zscore20", "T close")

    ts_inputs = {
        "price_return_1d": target_returns[1],
        "price_intraday_range": intraday_range,
        "log_volume_change": np.log(volume.replace(0, np.nan)).diff(),
    }
    calculators: dict[str, Callable] = {
        "absolute_sum_of_changes": fc.absolute_sum_of_changes,
        "mean_abs_change": fc.mean_abs_change,
        "cid_ce_normalized": lambda values: fc.cid_ce(values, normalize=True),
        "count_above_mean": fc.count_above_mean,
        "longest_strike_above_mean": fc.longest_strike_above_mean,
        "skewness": fc.skewness,
    }
    for source_name, series in ts_inputs.items():
        role = "ENTRY_TIMING" if source_name == "price_return_1d" else "RISK_CONTEXT"
        for lookback in protocol["feature_contract"]["tsfresh_lookbacks"]:
            for calculator_name in protocol["feature_contract"]["tsfresh_calculators"]:
                feature_name = f"tsfresh_{source_name}_{calculator_name}_{lookback}"
                _add(panel, catalog, feature_name, _rolling_tsfresh(series, int(lookback), calculators[calculator_name]), "H06-PRICE-VOLATILITY-STATE", "TSFRESH_PROPOSAL", role, f"tsfresh.{calculator_name} trailing {lookback}", "T close")

    if not panel.index.is_unique or panel.columns.duplicated().any():
        raise ValueError("feature panel identities must be unique")
    if np.isinf(panel.to_numpy(dtype=float)).any():
        raise ValueError("feature panel contains infinite values")
    metadata = pd.DataFrame(catalog)
    if metadata["feature"].duplicated().any() or set(metadata["feature"]) != set(panel.columns):
        raise ValueError("feature metadata does not exactly cover panel")
    hypotheses = set(protocol["feature_contract"]["hypotheses"])
    if set(metadata["hypothesis_id"]) != hypotheses:
        raise ValueError("feature panel does not cover every frozen hypothesis")
    coverage = panel.notna().mean().rename("coverage").rename_axis("feature").reset_index()
    coverage = coverage.merge(metadata, on="feature", validate="one_to_one")
    minimum_coverage = float(coverage["coverage"].min())
    if minimum_coverage < float(protocol["feature_contract"]["minimum_feature_coverage"]):
        raise ValueError(f"feature coverage below frozen floor: {minimum_coverage}")

    panel.reset_index().to_csv(
        artifacts / "causal_feature_panel.csv.gz", index=False, compression="gzip"
    )
    metadata.to_csv(artifacts / "feature_catalog.csv", index=False, encoding="utf-8")
    coverage.to_csv(artifacts / "feature_coverage.csv", index=False, encoding="utf-8")
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": "PROCEED_TO_INFORMATION_VALUE_AUDIT",
        "session_count": len(panel),
        "first_session": sessions.min().date().isoformat(),
        "last_session": sessions.max().date().isoformat(),
        "feature_count": panel.shape[1],
        "hypothesis_feature_counts": dict(sorted(Counter(metadata["hypothesis_id"]).items())),
        "role_feature_counts": dict(sorted(Counter(metadata["financial_role"]).items())),
        "minimum_feature_coverage": minimum_coverage,
        "features_below_95pct_coverage": sorted(coverage.loc[coverage["coverage"] < 0.95, "feature"].tolist()),
        "tsfresh_version": version("tsfresh"),
        "panel_sha256": _sha256(artifacts / "causal_feature_panel.csv.gz"),
        "catalog_sha256": _sha256(artifacts / "feature_catalog.csv"),
        "future_return_labels_created": 0,
        "input_selected": False,
        "template_selected": False,
        "search_started": False,
        "candidate_created": False,
        "catalog_mutated": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "materialization_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S008 EX14 执行记录\n\n"
        f"六类假设共同物化{panel.shape[1]}项因果候选特征，覆盖{len(panel)}个开发池交易日；"
        f"最低单项覆盖率为{minimum_coverage:.2%}。tsfresh固定使用20日和60日尾随窗口。"
        "没有生成未来收益标签、筛选输入或启动策略搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX14 结论\n\n"
        "裁决：`PROCEED_TO_INFORMATION_VALUE_AUDIT`。统一因果面板覆盖六类黄金经济假设，"
        f"包含{panel.shape[1]}项候选特征；物化成功只证明计算、身份和因果合同可执行，"
        "不构成Alpha证据。下一步必须一次性审计全部特征，不得按已见收益增删输入。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "credential_id": protocol["credential_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": evidence["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
