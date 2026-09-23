from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX06"


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_development(
    raw: Path,
    manifest: dict[str, object],
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, list[str]]:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("manifest files are invalid")
    selected: list[tuple[str, Path]] = []
    for filename, identity in files.items():
        if not isinstance(identity, dict):
            raise ValueError(f"invalid file identity: {filename}")
        if identity.get("frequency") != "daily" or int(identity["year"]) > cutoff.year:
            continue
        selected.append((str(filename), raw / str(filename)))
    if not selected:
        raise ValueError("manifest has no development daily files")
    frame = pd.concat(
        [pd.read_csv(path) for _filename, path in sorted(selected)], ignore_index=True
    )
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame = frame.loc[frame["date"] <= cutoff].sort_values("date").reset_index(drop=True)
    if frame.empty or frame["date"].max() > cutoff:
        raise ValueError("development boundary is invalid")
    if not frame["date"].is_unique:
        raise ValueError("development dates are not unique")
    return frame, [filename for filename, _path in sorted(selected)]


def _stateful_channel(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    entry: int,
    exit_: int,
) -> pd.Series:
    entry_event = close > high.shift(1).rolling(entry).max()
    exit_event = close < low.shift(1).rolling(exit_).min()
    values = np.zeros(len(close), dtype=float)
    for index in range(1, len(close)):
        previous = values[index - 1]
        if previous == 0.0 and bool(entry_event.iloc[index]):
            values[index] = 1.0
        elif previous == 1.0 and bool(exit_event.iloc[index]):
            values[index] = 0.0
        else:
            values[index] = previous
    return pd.Series(values, index=close.index)


def _return_frame(
    position: pd.Series,
    forward_open_return: pd.Series,
    performance_date: pd.Series,
    cost_bps: int,
) -> pd.DataFrame:
    turnover = position.diff().abs().fillna(position.abs())
    frame = pd.DataFrame(
        {
            "date": performance_date,
            "return": position * forward_open_return - turnover * cost_bps / 10_000.0,
            "position": position,
            "turnover": turnover,
            "entry": ((position > 0) & (position.shift(1, fill_value=0) == 0)).astype(int),
        }
    ).dropna(subset=["date", "return"])
    return frame.reset_index(drop=True)


def _metrics(
    frame: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, float | int]:
    scoped = frame.loc[frame["date"].between(start, end)].copy()
    if scoped.empty:
        raise ValueError(f"empty evaluation period: {start}..{end}")
    returns = scoped.set_index("date")["return"]
    years = max((returns.index[-1] - returns.index[0]).days / 365.25, 1 / 252)
    equity = (1.0 + returns).cumprod()
    annualized = float(equity.iloc[-1] ** (1.0 / years) - 1.0)
    drawdown = equity / equity.cummax() - 1.0
    max_drawdown = float(-drawdown.min())
    annual = returns.groupby(returns.index.year).apply(lambda x: float((1 + x).prod() - 1))
    positives = annual[annual > 0].sort_values(ascending=False)
    concentration = float(positives.head(2).sum() / positives.sum()) if positives.sum() > 0 else 0.0
    return {
        "annualized_return": annualized,
        "max_drawdown": max_drawdown,
        "calmar": annualized / max_drawdown if max_drawdown > 0 else 0.0,
        "entries": int(scoped["entry"].sum()),
        "exposure": float(scoped["position"].mean()),
        "annual_turnover": float(scoped["turnover"].sum() / years),
        "positive_years": int((annual > 0).sum()),
        "evaluated_years": int(len(annual)),
        "worst_year": float(annual.min()),
        "top2_positive_year_concentration": concentration,
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    raw = repo / "data" / "raw"
    artifacts = experiment / "artifacts"
    protocol = _read_object(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("source_sealed_validation_read") is not False:
        raise ValueError("source sealed validation boundary is open")
    if protocol.get("replication_future_data_read") is not False:
        raise ValueError("replication future-data boundary is open")
    if protocol.get("selects_final_configuration") is not False:
        raise ValueError("replication cannot select a final configuration")

    materials = _read_object(repo / "research" / "S008" / "materials.json")
    external = materials.get("external_replication_materials")
    if not isinstance(external, dict) or external.get("symbol") != protocol["symbol"]:
        raise ValueError("external replication materials are missing")
    research_identity = external.get("research_data_manifest")
    execution_identity = external.get("execution_data_manifest")
    if not isinstance(research_identity, dict) or not isinstance(execution_identity, dict):
        raise ValueError("external material identities are invalid")
    research_manifest_path = repo / str(research_identity["path"])
    execution_manifest_path = repo / str(execution_identity["path"])
    if _sha256(research_manifest_path) != research_identity["sha256"]:
        raise ValueError("external research manifest identity differs")
    if _sha256(execution_manifest_path) != execution_identity["sha256"]:
        raise ValueError("external execution manifest identity differs")

    research_manifest = _read_object(research_manifest_path)
    execution_manifest = _read_object(execution_manifest_path)
    if research_manifest.get("requested_end") != protocol["development_cutoff"]:
        raise ValueError("research manifest exceeds development cutoff")
    if execution_manifest.get("requested_end") != protocol["development_cutoff"]:
        raise ValueError("execution manifest exceeds development cutoff")
    cutoff = pd.Timestamp(protocol["development_cutoff"])
    daily, research_files = _load_development(raw, research_manifest, cutoff)
    execution, execution_files = _load_development(raw, execution_manifest, cutoff)
    merged = daily.merge(
        execution[["date", "open"]].rename(columns={"open": "execution_open"}),
        on="date",
        how="inner",
        validate="one_to_one",
    )
    if len(merged) != len(daily) or len(merged) != len(execution):
        raise ValueError("research and execution sessions differ")
    if merged["date"].max() >= pd.Timestamp(protocol["prohibited_start"]):
        raise ValueError("replication future data was loaded")

    close = merged["close"].astype(float)
    high = merged["high"].astype(float)
    low = merged["low"].astype(float)
    forward_open_return = merged["execution_open"].shift(-2) / merged["execution_open"].shift(-1) - 1
    performance_date = merged["date"].shift(-1)
    periods = {
        name: (pd.Timestamp(boundary[0]), pd.Timestamp(boundary[1]))
        for name, boundary in protocol["periods"].items()
    }

    configurations: list[dict[str, object]] = []
    for raw_configuration in protocol["configurations"]:
        entry = int(raw_configuration["entry"])
        exit_ = int(raw_configuration["exit"])
        configurations.append(
            {
                "configuration": f"entry={entry};exit={exit_}",
                "position": _stateful_channel(close, high, low, entry, exit_),
            }
        )

    trial_rows: list[dict[str, object]] = []
    buyhold = pd.Series(1.0, index=close.index)
    for cost in protocol["cost_bps_one_way"]:
        baseline_frame = _return_frame(buyhold, forward_open_return, performance_date, cost)
        for period, (start, end) in periods.items():
            trial_rows.append(
                {
                    "family": "BUY_HOLD",
                    "configuration": "always_long",
                    "cost_bps_one_way": cost,
                    "period": period,
                    **_metrics(baseline_frame, start, end),
                }
            )
    for configuration in configurations:
        for cost in protocol["cost_bps_one_way"]:
            frame = _return_frame(
                configuration["position"], forward_open_return, performance_date, cost
            )
            for period, (start, end) in periods.items():
                trial_rows.append(
                    {
                        "family": "DONCHIAN",
                        "configuration": configuration["configuration"],
                        "cost_bps_one_way": cost,
                        "period": period,
                        **_metrics(frame, start, end),
                    }
                )
    ledger = pd.DataFrame(trial_rows)
    ledger.to_csv(artifacts / "trial_ledger.csv", index=False, encoding="utf-8")
    lookup = ledger.set_index(
        ["family", "configuration", "cost_bps_one_way", "period"]
    ).to_dict(orient="index")
    baseline = lookup[("BUY_HOLD", "always_long", 10, "FULL")]

    qualification_rows: list[dict[str, object]] = []
    for configuration in configurations:
        name = str(configuration["configuration"])
        full10 = lookup[("DONCHIAN", name, 10, "FULL")]
        full30 = lookup[("DONCHIAN", name, 30, "FULL")]
        positive_periods = all(
            lookup[("DONCHIAN", name, 10, period)]["annualized_return"] > 0
            for period in ("P1", "P2", "P3")
        )
        qualifies = bool(
            full10["calmar"] > baseline["calmar"]
            and full10["max_drawdown"] < baseline["max_drawdown"]
            and positive_periods
            and full30["annualized_return"] > 0
            and full10["entries"] >= protocol["qualification"]["minimum_entries"]
            and full10["top2_positive_year_concentration"]
            <= protocol["qualification"]["maximum_top2_positive_year_concentration"]
        )
        qualification_rows.append(
            {
                "configuration": name,
                "qualifies": qualifies,
                "full_calmar_10bp": full10["calmar"],
                "buyhold_calmar_10bp": baseline["calmar"],
                "full_max_drawdown_10bp": full10["max_drawdown"],
                "buyhold_max_drawdown_10bp": baseline["max_drawdown"],
                "positive_all_internal_periods_10bp": positive_periods,
                "full_annualized_return_30bp": full30["annualized_return"],
                "entries": full10["entries"],
                "top2_positive_year_concentration": full10[
                    "top2_positive_year_concentration"
                ],
            }
        )
    qualification = pd.DataFrame(qualification_rows)
    qualification.to_csv(artifacts / "qualification.csv", index=False, encoding="utf-8")
    qualified_count = int(qualification["qualifies"].sum())
    if qualified_count == len(configurations):
        decision = protocol["adjudication"]["all_qualified"]
        evidence_label = "FAVORABLE"
    elif qualified_count == 1:
        decision = protocol["adjudication"]["one_qualified"]
        evidence_label = "MIXED"
    else:
        decision = protocol["adjudication"]["none_qualified"]
        evidence_label = "ADVERSE"

    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": decision,
        "evidence_label": evidence_label,
        "replication_kind": "same_underlying_external_instrument",
        "independent_market_sample": False,
        "buyhold_10bp_full": baseline,
        "configuration_count": len(configurations),
        "qualified_configuration_count": qualified_count,
        "trial_rows": int(len(ledger)),
        "source_sealed_validation_read": False,
        "replication_future_data_read": False,
        "selects_final_configuration": False,
    }
    _write_json(artifacts / "replication_summary.json", summary)
    _write_json(
        artifacts / "data_identity.json",
        {
            "research_manifest_sha256": research_identity["sha256"],
            "execution_manifest_sha256": execution_identity["sha256"],
            "opened_research_files": research_files,
            "opened_execution_files": execution_files,
            "latest_opened_session": merged["date"].max().date().isoformat(),
            "source_sealed_validation_read": False,
            "replication_future_data_read": False,
        },
    )

    (experiment / "03_execution.md").write_text(
        "# S008 EX06 执行记录\n\n"
        f"按冻结协议读取518800.SH截至{cutoff.date().isoformat()}的开发期，原样评价两个Donchian"
        f"配置、两档成本和四个时期，共形成{len(ledger)}行trial ledger。没有读取未来数据、"
        "调整参数、选择最终配置或创建候选。\n",
        encoding="utf-8",
    )
    qualified_names = qualification.loc[qualification["qualifies"], "configuration"].tolist()
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX06 结论\n\n"
        f"裁决：`{decision}`，证据标签`{evidence_label}`；合格配置为"
        f"{qualified_names or '无'}。本实验只提供同源ETF产品复制证据，不构成独立市场样本外验证，"
        "不选择最终配置、不证明Alpha，也不创建候选。下一步必须先人工评审。\n",
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
            "decision": decision,
            "evidence_label": evidence_label,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
