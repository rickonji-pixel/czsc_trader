from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX03"


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
    frequency: str,
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, list[str]]:
    selected: list[tuple[str, Path]] = []
    for filename, identity in manifest["files"].items():
        if identity["frequency"] != frequency or int(identity["year"]) > cutoff.year:
            continue
        selected.append((filename, raw / filename))
    if not selected:
        raise ValueError(f"manifest has no development {frequency} files")
    frame = pd.concat(
        [pd.read_csv(path) for _filename, path in sorted(selected)], ignore_index=True
    )
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame = frame.loc[frame["date"] <= cutoff].sort_values("date").reset_index(drop=True)
    if frame["date"].max() > cutoff:
        raise ValueError("sealed validation data was loaded")
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


def _metrics(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, float | int]:
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


def _largest_component(coordinates: list[tuple[int, ...]]) -> int:
    remaining = set(coordinates)
    largest = 0
    while remaining:
        seed = remaining.pop()
        queue = deque([seed])
        size = 1
        while queue:
            current = queue.popleft()
            neighbors = {
                candidate
                for candidate in remaining
                if len(candidate) == len(current)
                and sum(abs(a - b) for a, b in zip(candidate, current)) == 1
            }
            remaining.difference_update(neighbors)
            queue.extend(neighbors)
            size += len(neighbors)
        largest = max(largest, size)
    return largest


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    raw = repo / "data" / "raw"
    artifacts = experiment / "artifacts"
    protocol = _read_object(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("sealed_validation_read") is not False:
        raise ValueError("sealed validation boundary is not closed")

    materials = _read_object(repo / "research" / "S008" / "materials.json")
    research_manifest_path = repo / materials["research_data_manifest"]["path"]
    execution_manifest_path = repo / materials["execution_data_manifest"]["path"]
    if _sha256(research_manifest_path) != materials["research_data_manifest"]["sha256"]:
        raise ValueError("research manifest identity differs from materials")
    if _sha256(execution_manifest_path) != materials["execution_data_manifest"]["sha256"]:
        raise ValueError("execution manifest identity differs from materials")

    research_manifest = _read_object(research_manifest_path)
    execution_manifest = _read_object(execution_manifest_path)
    cutoff = pd.Timestamp(protocol["development_cutoff"])
    daily, research_files = _load_development(raw, research_manifest, "daily", cutoff)
    execution, execution_files = _load_development(raw, execution_manifest, "daily", cutoff)
    merged = daily.merge(
        execution[["date", "open"]].rename(columns={"open": "execution_open"}),
        on="date",
        how="inner",
        validate="one_to_one",
    )
    if len(merged) != len(daily):
        raise ValueError("research and execution sessions differ")

    close = merged["close"].astype(float)
    high = merged["high"].astype(float)
    low = merged["low"].astype(float)
    forward_open_return = merged["execution_open"].shift(-2) / merged["execution_open"].shift(-1) - 1
    performance_date = merged["date"].shift(-1)

    prototypes: list[dict[str, object]] = []
    grids = protocol["prototype_grids"]
    donchian_entry = grids["DONCHIAN"]["entry"]
    donchian_exit = grids["DONCHIAN"]["exit"]
    for entry_index, entry in enumerate(donchian_entry):
        for exit_index, exit_ in enumerate(donchian_exit):
            if exit_ >= entry:
                continue
            prototypes.append(
                {
                    "family": "DONCHIAN",
                    "configuration": f"entry={entry};exit={exit_}",
                    "coordinates": (entry_index, exit_index),
                    "position": _stateful_channel(close, high, low, entry, exit_),
                }
            )
    fast_values = grids["DUAL_MA"]["fast"]
    slow_values = grids["DUAL_MA"]["slow"]
    for fast_index, fast in enumerate(fast_values):
        for slow_index, slow in enumerate(slow_values):
            prototypes.append(
                {
                    "family": "DUAL_MA",
                    "configuration": f"fast={fast};slow={slow}",
                    "coordinates": (fast_index, slow_index),
                    "position": (close.rolling(fast).mean() > close.rolling(slow).mean()).astype(float),
                }
            )
    lookbacks = grids["TIME_SERIES_MOMENTUM"]["lookback"]
    for lookback_index, lookback in enumerate(lookbacks):
        prototypes.append(
            {
                "family": "TIME_SERIES_MOMENTUM",
                "configuration": f"lookback={lookback}",
                "coordinates": (lookback_index,),
                "position": (close / close.shift(lookback) > 1).astype(float),
            }
        )

    periods = {
        name: (pd.Timestamp(boundary[0]), pd.Timestamp(boundary[1]))
        for name, boundary in protocol["periods"].items()
    }
    trial_rows: list[dict[str, object]] = []
    return_frames: dict[tuple[str, str, int], pd.DataFrame] = {}
    buyhold = pd.Series(1.0, index=close.index)
    for cost in protocol["cost_bps_one_way"]:
        baseline_frame = _return_frame(buyhold, forward_open_return, performance_date, cost)
        return_frames[("BUY_HOLD", "always_long", cost)] = baseline_frame
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
    for prototype in prototypes:
        for cost in protocol["cost_bps_one_way"]:
            frame = _return_frame(
                prototype["position"], forward_open_return, performance_date, cost
            )
            return_frames[(prototype["family"], prototype["configuration"], cost)] = frame
            for period, (start, end) in periods.items():
                trial_rows.append(
                    {
                        "family": prototype["family"],
                        "configuration": prototype["configuration"],
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
    qualified_coordinates: dict[str, list[tuple[int, ...]]] = {}
    for prototype in prototypes:
        family = str(prototype["family"])
        configuration = str(prototype["configuration"])
        full10 = lookup[(family, configuration, 10, "FULL")]
        full30 = lookup[(family, configuration, 30, "FULL")]
        positive_periods = all(
            lookup[(family, configuration, 10, period)]["annualized_return"] > 0
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
        if qualifies:
            qualified_coordinates.setdefault(family, []).append(prototype["coordinates"])
        qualification_rows.append(
            {
                "family": family,
                "configuration": configuration,
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

    family_rows: list[dict[str, object]] = []
    labels: list[str] = []
    for family in sorted(qualification["family"].unique()):
        qualified = qualification.loc[
            (qualification["family"] == family) & qualification["qualifies"]
        ]
        largest = _largest_component(qualified_coordinates.get(family, []))
        if largest >= protocol["qualification"]["minimum_connected_configs_for_favorable"]:
            label = "FAVORABLE"
        elif not qualified.empty:
            label = "MIXED"
        else:
            label = "ADVERSE"
        labels.append(label)
        full10_rows = ledger.loc[
            (ledger["family"] == family)
            & (ledger["cost_bps_one_way"] == 10)
            & (ledger["period"] == "FULL")
        ]
        family_rows.append(
            {
                "family": family,
                "configurations": int(len(full10_rows)),
                "qualified_configurations": int(len(qualified)),
                "largest_qualified_component": int(largest),
                "evidence_label": label,
                "median_calmar_10bp": float(full10_rows["calmar"].median()),
                "median_max_drawdown_10bp": float(full10_rows["max_drawdown"].median()),
                "median_entries_10bp": float(full10_rows["entries"].median()),
            }
        )
    if "FAVORABLE" in labels:
        decision = "PROCEED_TO_PROTOTYPE_PREREGISTRATION"
        overall_label = "FAVORABLE"
    elif "MIXED" in labels:
        decision = "REVIEW_MIXED_MECHANISM_EVIDENCE"
        overall_label = "MIXED"
    else:
        decision = "STOP_OR_REDESIGN_MECHANISM"
        overall_label = "ADVERSE"

    family_summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "evidence_label": overall_label,
        "decision": decision,
        "buyhold_10bp_full": baseline,
        "families": family_rows,
        "trial_rows": int(len(ledger)),
        "prototype_configurations": int(len(prototypes)),
        "sealed_validation_read": False,
        "selects_final_configuration": False,
    }
    _write_json(artifacts / "family_summary.json", family_summary)
    _write_json(
        artifacts / "data_identity.json",
        {
            "research_manifest_sha256": materials["research_data_manifest"]["sha256"],
            "execution_manifest_sha256": materials["execution_data_manifest"]["sha256"],
            "opened_research_files": research_files,
            "opened_execution_files": execution_files,
            "latest_opened_session": merged["date"].max().date().isoformat(),
            "sealed_validation_read": False,
        },
    )

    (experiment / "03_execution.md").write_text(
        "# S008 EX03 执行记录\n\n"
        f"按冻结协议读取截至{cutoff.date().isoformat()}的开发池，评价"
        f"{len(prototypes)}个趋势配置、2档成本和4个内部窗口，共形成{len(ledger)}行trial ledger。"
        "没有打开封存验证文件，没有选择最终配置、创建候选或修改平台模块。\n",
        encoding="utf-8",
    )
    favorable_families = [
        item["family"] for item in family_rows if item["evidence_label"] == "FAVORABLE"
    ]
    mixed_families = [item["family"] for item in family_rows if item["evidence_label"] == "MIXED"]
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX03 结论\n\n"
        f"裁决：`{decision}`，总体证据标签`{overall_label}`。"
        f"FAVORABLE原型族为{favorable_families or '无'}，MIXED原型族为{mixed_families or '无'}。"
        "本实验只评价开发池中的机制参数带，不选择最终配置，不证明样本外Alpha，也不创建候选。"
        "下一步必须先人工评审。\n",
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
            "evidence_label": overall_label,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
