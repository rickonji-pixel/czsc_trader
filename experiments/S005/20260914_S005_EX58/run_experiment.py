from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.intraday_data import load_intraday_research_data
from czsc_trader.regime_weight import lagged_efficiency_ratio


EXPERIMENT_ID = "20260914_S005_EX58"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _numeric_observations(
    factor_id: str,
    series: pd.Series,
    information_dates: pd.Series | pd.DatetimeIndex,
    usable_dates: pd.Series | pd.DatetimeIndex,
    usable_clock: str,
    source: str,
) -> pd.DataFrame:
    frame = pd.DataFrame({
        "factor_id": factor_id,
        "information_date": pd.to_datetime(information_dates).to_numpy(),
        "first_usable_date": pd.to_datetime(usable_dates).to_numpy(),
        "first_usable_clock": usable_clock,
        "value_numeric": pd.to_numeric(series, errors="coerce").to_numpy(),
        "value_text": pd.NA,
        "observation_key": pd.NA,
        "source": source,
    })
    return frame.dropna(subset=["first_usable_date", "value_numeric"]).reset_index(drop=True)


def _first_hour_features(one_minute: pd.DataFrame, protocol: dict[str, object]) -> pd.DataFrame:
    rules = protocol["intraday_observation"]
    bars = one_minute.copy()
    bars["trade_date"] = bars["Date"].dt.normalize()
    bars["clock"] = bars["Date"].dt.strftime("%H:%M")
    observed = bars.loc[bars["clock"].between(rules["start_clock"], rules["end_clock"], inclusive="both")].copy()
    sizes = observed.groupby("trade_date", observed=True).size()
    if not sizes.eq(int(rules["expected_bars"])).all():
        raise ValueError(f"first-hour bar count differs: {sizes.loc[~sizes.eq(int(rules['expected_bars']))].head().to_dict()}")
    observed["typical_price"] = observed[["Open", "High", "Low", "Close"]].mean(axis=1)
    observed["weighted_typical"] = observed["typical_price"] * observed["Volume"]
    observed["minute_direction"] = np.sign(observed["Close"] - observed["Open"])
    observed["signed_volume"] = observed["minute_direction"] * observed["Volume"]
    grouped = observed.groupby("trade_date", observed=True, sort=True)
    output = pd.DataFrame(index=sizes.index)
    output["first_hour_open"] = grouped["Open"].first().astype(float)
    output["first_hour_close"] = grouped["Close"].last().astype(float)
    output["first_hour_return"] = output["first_hour_close"].div(output["first_hour_open"]).sub(1.0)
    output["first_hour_vwap"] = grouped["weighted_typical"].sum().div(grouped["Volume"].sum())
    output["first_hour_vwap_deviation"] = output["first_hour_close"].div(output["first_hour_vwap"]).sub(1.0)
    first_bar_return = observed["Close"].div(observed["Open"]).sub(1.0)
    later_return = grouped["Close"].pct_change(fill_method=None)
    observed["path_return"] = later_return.fillna(first_bar_return)
    variation = observed["path_return"].abs().groupby(observed["trade_date"]).sum()
    output["first_hour_path_efficiency"] = output["first_hour_return"].abs().div(variation.replace(0.0, np.nan))
    output["first_hour_volume"] = grouped["Volume"].sum().astype(float)
    baseline = int(rules["activity_baseline_sessions"])
    output["first_hour_activity"] = output["first_hour_volume"].div(output["first_hour_volume"].shift(1).rolling(baseline, min_periods=baseline).median())
    output["signed_volume_imbalance"] = grouped["signed_volume"].sum().div(grouped["Volume"].sum())
    return output.reset_index()


def _next_sessions(calendar: pd.DatetimeIndex) -> dict[pd.Timestamp, pd.Timestamp]:
    return {calendar[index]: calendar[index + 1] for index in range(len(calendar) - 1)}


def _inventory(observations: pd.DataFrame, definitions: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    independence = {
        "F-PROJECT-ER60": False,
        "F-PROJECT-FIRST-HOUR-RETURN": False,
        "F-PROJECT-FIRST-HOUR-VWAP-DEVIATION": False,
        "F-PROJECT-FIRST-HOUR-PATH-EFFICIENCY": False,
        "F-PROJECT-FIRST-HOUR-ACTIVITY": False,
        "F-PROJECT-SIGNED-VOLUME-IMBALANCE": False,
        "F-PROJECT-BREADTH-BALANCE": True,
        "F-PROJECT-BREADTH-THRUST-5": True,
        "F-PROJECT-MONEYFLOW-BREADTH": True,
        "F-PROJECT-ETF-SHARE-CHANGE": True,
        "F-PROJECT-ETF-NAV-PREMIUM": True,
        "F-PROJECT-EARNINGS-ACCELERATION-BREADTH": True,
        "F-PROJECT-STRUCTURED-NEWS-EVENT": True,
    }
    prior_evidence = {
        "F-PROJECT-ER60": "S001 regime component; not an independent S005 alpha",
        "F-PROJECT-FIRST-HOUR-RETURN": "not independently tested for S005",
        "F-PROJECT-FIRST-HOUR-VWAP-DEVIATION": "not independently tested for S005",
        "F-PROJECT-FIRST-HOUR-PATH-EFFICIENCY": "not independently tested for S005",
        "F-PROJECT-FIRST-HOUR-ACTIVITY": "not independently tested for S005",
        "F-PROJECT-SIGNED-VOLUME-IMBALANCE": "not independently tested for S005",
        "F-PROJECT-BREADTH-BALANCE": "EX21 standalone family failed return gates",
        "F-PROJECT-BREADTH-THRUST-5": "EX21 standalone family failed return gates",
        "F-PROJECT-MONEYFLOW-BREADTH": "EX21 standalone family failed return gates",
        "F-PROJECT-ETF-SHARE-CHANGE": "EX28 standalone family failed return gates",
        "F-PROJECT-ETF-NAV-PREMIUM": "EX33 standalone family failed return gates",
        "F-PROJECT-EARNINGS-ACCELERATION-BREADTH": "EX44 standalone mechanism failed density gate without reading returns",
        "F-PROJECT-STRUCTURED-NEWS-EVENT": "EX41 generic positive continuation failed; event data remains reusable",
    }
    rows: list[dict[str, object]] = []
    for definition in definitions.itertuples(index=False):
        selected = observations.loc[observations["factor_id"].eq(definition.factor_id)].copy()
        numeric = selected["value_numeric"].notna()
        observed_dates = pd.DatetimeIndex(selected["first_usable_date"].dropna().unique()).sort_values()
        if len(observed_dates):
            span = calendar[(calendar >= observed_dates.min()) & (calendar <= observed_dates.max())]
            session_coverage = float(len(observed_dates.intersection(calendar)) / len(span)) if len(span) else 0.0
            gaps = pd.Series(observed_dates).diff().dt.days.dropna()
        else:
            session_coverage = 0.0
            gaps = pd.Series(dtype=float)
        rows.append({
            "factor_id": definition.factor_id,
            "name": definition.name,
            "information_family": definition.information_family,
            "catalog_status": definition.status,
            "observations": int(len(selected)),
            "numeric_observations": int(numeric.sum()),
            "first_usable_date": observed_dates.min().date().isoformat() if len(observed_dates) else None,
            "last_usable_date": observed_dates.max().date().isoformat() if len(observed_dates) else None,
            "session_coverage_within_span": session_coverage,
            "median_calendar_gap_days": float(gaps.median()) if len(gaps) else None,
            "distinct_numeric_values": int(selected.loc[numeric, "value_numeric"].nunique()),
            "independent_from_target_ohlcv": independence[definition.factor_id],
            "prior_s005_evidence": prior_evidence[definition.factor_id],
        })
    return pd.DataFrame(rows)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_factor_returns") or protocol.get("generates_signals"):
        raise ValueError("EX58 is a return-free factor materialization audit")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX58 cannot create, promote, or deploy a candidate")

    fixed = (
        (repo / "catalog/information_families.json", protocol["catalog"]["information_families_sha256"]),
        (repo / "catalog/factors/project.json", protocol["catalog"]["project_factors_sha256"]),
        (repo / "catalog/signals/project.json", protocol["catalog"]["project_signals_sha256"]),
        (repo / "research/S005/materials.json", protocol["catalog"]["materials_sha256"]),
        (repo / "data/raw/588080_intraday_manifest.json", protocol["dataset"]["intraday_manifest_sha256"]),
    )
    for path, expected in fixed:
        if _sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")
    source_paths: dict[str, Path] = {}
    for experiment_id, source in protocol["sources"].items():
        source_dir = repo / "experiments/S005" / experiment_id
        validate_experiment_archive(source_dir)
        if _sha256(source_dir / "experiment_manifest.json") != source["manifest_sha256"]:
            raise ValueError(f"{experiment_id}: manifest differs")
        artifact_path = source_dir / source["artifact"]
        if _sha256(artifact_path) != source["artifact_sha256"]:
            raise ValueError(f"{experiment_id}: artifact differs")
        source_paths[experiment_id] = artifact_path

    factor_payload = _read(repo / "catalog/factors/project.json")
    definitions = pd.DataFrame(factor_payload["items"])
    if len(definitions) != 13 or definitions["factor_id"].duplicated().any():
        raise ValueError("project factor catalog must contain 13 unique definitions")
    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(context, str(protocol["dataset"]["name"]), str(protocol["symbol"]), str(protocol["asset_type"]), date.fromisoformat(str(protocol["development_cutoff"])))
    if replay.fingerprint != protocol["dataset"]["fingerprint"]:
        raise ValueError("research dataset differs from frozen protocol")
    prices = replay.adjusted.daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.loc[prices["dt"] <= pd.Timestamp(protocol["development_cutoff"])].set_index("dt").sort_index()
    calendar = pd.DatetimeIndex(prices.index)
    next_session = _next_sessions(calendar)
    observations: list[pd.DataFrame] = []

    er = lagged_efficiency_ratio(prices["close"], 60)
    observations.append(_numeric_observations("F-PROJECT-ER60", er, er.index, er.index, "09:30", "research/588080 adjusted daily"))

    intraday = load_intraday_research_data(repo / "data/raw", str(protocol["symbol"]))
    features = _first_hour_features(intraday.frames["1m"], protocol)
    features = features.loc[features["trade_date"] <= pd.Timestamp(protocol["development_cutoff"])]
    intraday_columns = {
        "F-PROJECT-FIRST-HOUR-RETURN": "first_hour_return",
        "F-PROJECT-FIRST-HOUR-VWAP-DEVIATION": "first_hour_vwap_deviation",
        "F-PROJECT-FIRST-HOUR-PATH-EFFICIENCY": "first_hour_path_efficiency",
        "F-PROJECT-FIRST-HOUR-ACTIVITY": "first_hour_activity",
        "F-PROJECT-SIGNED-VOLUME-IMBALANCE": "signed_volume_imbalance",
    }
    for factor_id, column in intraday_columns.items():
        observations.append(_numeric_observations(factor_id, features[column], features["trade_date"], features["trade_date"], "10:30", "data/raw/588080 1m"))

    panel = pd.read_csv(source_paths["20260913_S005_EX18"])
    panel["dt"] = pd.to_datetime(panel["dt"]).dt.normalize()
    panel = panel.loc[panel["dt"] <= pd.Timestamp(protocol["development_cutoff"])]
    panel["weight"] = pd.to_numeric(panel["weight"], errors="raise")
    observed_price = panel["observed_daily"].astype(bool) & panel["pct_chg"].notna()
    observed_flow = panel["observed_moneyflow"].astype(bool) & panel["net_mf_amount"].notna()
    panel["observed_price_weight"] = panel["weight"].where(observed_price, 0.0)
    panel["advance_weight"] = panel["weight"].where(observed_price & panel["pct_chg"].gt(0), 0.0)
    panel["decline_weight"] = panel["weight"].where(observed_price & panel["pct_chg"].lt(0), 0.0)
    panel["observed_flow_weight"] = panel["weight"].where(observed_flow, 0.0)
    panel["positive_flow_weight"] = panel["weight"].where(observed_flow & panel["net_mf_amount"].gt(0), 0.0)
    breadth = panel.groupby("dt", sort=True).agg(
        observed_price_weight=("observed_price_weight", "sum"),
        advance_weight=("advance_weight", "sum"),
        decline_weight=("decline_weight", "sum"),
        observed_flow_weight=("observed_flow_weight", "sum"),
        positive_flow_weight=("positive_flow_weight", "sum"),
    )
    breadth["weighted_breadth_balance"] = breadth["advance_weight"].sub(breadth["decline_weight"]).div(breadth["observed_price_weight"])
    breadth["weighted_breadth_thrust_5"] = breadth["weighted_breadth_balance"].rolling(5, min_periods=5).mean()
    breadth["moneyflow_breadth"] = breadth["positive_flow_weight"].div(breadth["observed_flow_weight"])
    breadth = breadth.reset_index()
    breadth["first_usable_date"] = breadth["dt"].map(next_session)
    observations.append(_numeric_observations("F-PROJECT-BREADTH-BALANCE", breadth["weighted_breadth_balance"], breadth["dt"], breadth["first_usable_date"], "09:30", "20260913_S005_EX18"))
    observations.append(_numeric_observations("F-PROJECT-BREADTH-THRUST-5", breadth["weighted_breadth_thrust_5"], breadth["dt"], breadth["first_usable_date"], "09:30", "20260913_S005_EX18"))
    observations.append(_numeric_observations("F-PROJECT-MONEYFLOW-BREADTH", breadth["moneyflow_breadth"], breadth["dt"], breadth["first_usable_date"], "09:00", "20260913_S005_EX18"))

    etf = pd.read_csv(source_paths["20260913_S005_EX26"])
    etf["trade_date"] = pd.to_datetime(etf["trade_date"]).dt.normalize()
    etf = etf.loc[etf["trade_date"] <= pd.Timestamp(protocol["development_cutoff"])].sort_values("trade_date")
    etf["first_usable_date"] = etf["trade_date"].map(next_session)
    etf["share_change"] = pd.to_numeric(etf["total_share"], errors="raise").pct_change(fill_method=None)
    etf["nav_premium"] = pd.to_numeric(etf["close"], errors="raise").div(pd.to_numeric(etf["nav"], errors="coerce")).sub(1.0)
    observations.append(_numeric_observations("F-PROJECT-ETF-SHARE-CHANGE", etf["share_change"], etf["trade_date"], etf["first_usable_date"], "09:00", "20260913_S005_EX26"))
    observations.append(_numeric_observations("F-PROJECT-ETF-NAV-PREMIUM", etf["nav_premium"], etf["trade_date"], etf["first_usable_date"], "09:00", "20260913_S005_EX26"))

    earnings = pd.read_csv(source_paths["20260913_S005_EX44"])
    earnings["eligible_session"] = pd.to_datetime(earnings["eligible_session"]).dt.normalize()
    earnings = earnings.loc[earnings["eligible_session"] <= pd.Timestamp(protocol["development_cutoff"])]
    observations.append(_numeric_observations("F-PROJECT-EARNINGS-ACCELERATION-BREADTH", earnings["net_breadth"], earnings["eligible_session"], earnings["eligible_session"], "09:30", "20260913_S005_EX44"))

    news = pd.read_csv(source_paths["20260913_S005_EX40"])
    news["published_at"] = pd.to_datetime(news["published_at"])
    news = news.loc[news["published_at"].dt.normalize() <= pd.Timestamp(protocol["development_cutoff"])]
    news_rows = pd.DataFrame({
        "factor_id": "F-PROJECT-STRUCTURED-NEWS-EVENT",
        "information_date": news["published_at"].dt.normalize(),
        "first_usable_date": news["published_at"].dt.normalize(),
        "first_usable_clock": news["published_at"].dt.strftime("%H:%M:%S"),
        "value_numeric": np.nan,
        "value_text": news["direction"].astype(str) + "|" + news["event_type"].astype(str),
        "observation_key": news["duplicate_event_key"],
        "source": news["source_experiment"],
    })
    observations.append(news_rows)

    observation_ledger = pd.concat(observations, ignore_index=True)
    observation_ledger["information_date"] = pd.to_datetime(observation_ledger["information_date"]).dt.strftime("%Y-%m-%d")
    observation_ledger["first_usable_date"] = pd.to_datetime(observation_ledger["first_usable_date"]).dt.strftime("%Y-%m-%d")
    if set(definitions["factor_id"]) != set(observation_ledger["factor_id"]):
        raise ValueError("not every project factor was materialized")
    numeric = observation_ledger["value_numeric"].dropna().astype(float)
    if not np.isfinite(numeric.to_numpy()).all():
        raise ValueError("materialized numeric values must be finite")
    inventory = _inventory(observation_ledger, definitions, calendar)

    contract_rows = [
        {
            "factor_id": "F-PROJECT-ER60",
            "severity": "BLOCKING",
            "issue": "catalog availability says session close, implementation shifts close by one session and labels the next session",
            "required_action": "state explicitly that the value on session T uses closes through T-1 and is available before T open",
        },
        {
            "factor_id": "F-PROJECT-BREADTH-BALANCE",
            "severity": "BLOCKING",
            "issue": "catalog formula is weighted advance-minus-decline, registered implementation computes equal-count breadth",
            "required_action": "register weighted breadth as a distinct implementation or revise the definition",
        },
        {
            "factor_id": "F-PROJECT-BREADTH-THRUST-5",
            "severity": "BLOCKING",
            "issue": "inherits the breadth-balance weighting mismatch",
            "required_action": "bind the rolling factor to the corrected breadth definition",
        },
        {
            "factor_id": "F-PROJECT-ETF-NAV-PREMIUM",
            "severity": "BLOCKING",
            "issue": "catalog declares fund_nav.unit_nav while accepted S005 evidence uses etf_share_size.nav",
            "required_action": "freeze one source and its publication timing",
        },
        {
            "factor_id": "F-PROJECT-FIRST-HOUR-ACTIVITY",
            "severity": "NON_BLOCKING",
            "issue": "catalog inputs include amount while implementation uses volume only",
            "required_action": "remove the unused input or extend the formula",
        },
        {
            "factor_id": "F-PROJECT-ETF-SHARE-CHANGE",
            "severity": "TIMING_RISK",
            "issue": "09:00 T+1 availability is a current research assumption awaiting arrival monitoring",
            "required_action": "keep status DISCOVERED until operational timing evidence accumulates",
        },
        {
            "factor_id": "F-PROJECT-STRUCTURED-NEWS-EVENT",
            "severity": "INFORMATIONAL",
            "issue": "event extraction is not a numeric directional factor",
            "required_action": "preserve event semantics; define a mechanism before any score conversion",
        },
    ]
    contract_audit = pd.DataFrame(contract_rows)
    blocking = contract_audit["severity"].eq("BLOCKING")
    decision = "REPAIR_FSC_CONTRACTS_BEFORE_PROJECT_FACTOR_COMBINATION" if blocking.any() else "PROCEED_TO_PROJECT_FACTOR_COMBINATION_DESIGN"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "catalog_factor_count": int(len(definitions)),
        "materialized_factor_count": int(inventory["observations"].gt(0).sum()),
        "numeric_factor_count": int(inventory["numeric_observations"].gt(0).sum()),
        "target_ohlcv_independent_factor_count": int(inventory["independent_from_target_ohlcv"].sum()),
        "observation_count": int(len(observation_ledger)),
        "blocking_contract_issue_count": int(blocking.sum()),
        "contract_issue_count": int(len(contract_audit)),
        "common_independent_daily_numeric_factors": [
            "F-PROJECT-BREADTH-BALANCE",
            "F-PROJECT-BREADTH-THRUST-5",
            "F-PROJECT-MONEYFLOW-BREADTH",
            "F-PROJECT-ETF-SHARE-CHANGE",
            "F-PROJECT-ETF-NAV-PREMIUM"
        ],
        "common_independent_daily_window": {"start": "2021-02-02", "end": "2026-09-02"},
        "reads_post_factor_returns": False,
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    observation_ledger.to_csv(artifacts / "factor_observation_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")
    inventory.to_csv(artifacts / "factor_inventory.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    contract_audit.to_csv(artifacts / "contract_audit.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    _write(artifacts / "materialization_evidence.json", evidence)

    table = ["|因子|信息族|观测|数值|覆盖|独立增量|既有S005证据|", "|---|---|---:|---:|---:|---|---|"]
    for row in inventory.itertuples(index=False):
        table.append(f"|{row.factor_id}|{row.information_family}|{row.observations}|{row.numeric_observations}|{row.session_coverage_within_span:.1%}|{'是' if row.independent_from_target_ohlcv else '否'}|{row.prior_s005_evidence}|")
    (experiment / "03_execution.md").write_text("# S005 EX58 执行\n\n状态：`COMPLETE`。13个项目级因子全部完成物化或事件入账，未读取后续收益。\n\n" + "\n".join(table) + "\n", encoding="utf-8")
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX58 结论\n\n"
        f"13个项目因子全部入账，其中{evidence['numeric_factor_count']}个可形成数值，"
        f"{evidence['target_ohlcv_independent_factor_count']}个具有588080自身OHLCV之外的信息来源。"
        f"发现{evidence['blocking_contract_issue_count']}项阻断性契约偏差，裁决为`{decision}`。\n\n"
        "可形成共同日频样本的独立增量因子共有5个：成分涨跌宽度、五日宽度推动、成分资金流宽度、"
        "ETF份额变化和净值折溢价。它们此前作为独立机制均未通过S005收益门，因此未来组合只能以"
        "新的金融机制预注册，不能把旧失败路径直接堆叠后重新解释。盈利预期与新闻保持稀疏事件语义。"
        "修复FSC定义、实现和可用时间契约之前，禁止进入组合收益研究。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": "S005",
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
