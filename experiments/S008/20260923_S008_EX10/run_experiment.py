from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX10"


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
    identity: dict[str, object],
    repo: Path,
    expected_start: pd.Timestamp,
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, list[str]]:
    path = repo / str(identity["path"])
    if _sha256(path) != identity["sha256"]:
        raise ValueError(f"manifest identity differs: {path}")
    manifest = _read_object(path)
    requested_start = pd.Timestamp(str(manifest.get("requested_start")))
    requested_end = pd.Timestamp(str(manifest.get("requested_end")))
    if requested_start > expected_start or requested_end < cutoff:
        raise ValueError("manifest does not cover the complete development window")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("manifest files are invalid")
    selected: list[tuple[str, Path]] = []
    for filename, file_identity in files.items():
        if not isinstance(file_identity, dict):
            raise ValueError(f"invalid file identity: {filename}")
        if (
            file_identity.get("frequency") == "daily"
            and expected_start.year <= int(file_identity["year"]) <= cutoff.year
        ):
            selected.append((str(filename), raw / str(filename)))
    frame = pd.concat(
        [pd.read_csv(file_path) for _name, file_path in sorted(selected)], ignore_index=True
    )
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame = frame.loc[frame["date"].between(expected_start, cutoff)].sort_values(
        "date"
    ).reset_index(drop=True)
    if (
        frame.empty
        or not frame["date"].is_unique
        or frame["date"].min() != expected_start
        or frame["date"].max() != cutoff
    ):
        raise ValueError("development data boundary is invalid")
    return frame, [name for name, _path in sorted(selected)]


def _drawdown_recovery_position(
    close: pd.Series,
    rolling_high_window: int,
    drawdown_trigger: float,
    recovery_horizon: int,
    recovery_return: float,
    near_high_exit_drawdown: float,
    failure_return: float,
) -> pd.Series:
    price = close.astype(float)
    rolling_high = price.rolling(rolling_high_window).max()
    drawdown = price / rolling_high - 1.0
    recovery = price / price.shift(recovery_horizon) - 1.0
    values = np.zeros(len(close), dtype=float)
    armed = False
    for index in range(1, len(close)):
        previous = values[index - 1]
        current_drawdown = drawdown.iloc[index]
        current_recovery = recovery.iloc[index]
        if previous == 1.0:
            should_exit = (
                pd.notna(current_drawdown)
                and current_drawdown >= -near_high_exit_drawdown
            ) or (
                pd.notna(current_recovery) and current_recovery <= failure_return
            )
            values[index] = 0.0 if should_exit else 1.0
            if should_exit:
                armed = False
            continue
        if pd.notna(current_drawdown) and current_drawdown <= -drawdown_trigger:
            armed = True
        if (
            armed
            and pd.notna(current_recovery)
            and current_recovery > recovery_return
            and current_drawdown < -near_high_exit_drawdown
        ):
            values[index] = 1.0
            armed = False
        elif pd.notna(current_drawdown) and current_drawdown >= -near_high_exit_drawdown:
            armed = False
            values[index] = previous
    return pd.Series(values, index=close.index)


def _return_frame(
    position: pd.Series,
    forward_open_return: pd.Series,
    performance_date: pd.Series,
    cost_bps: int,
) -> pd.DataFrame:
    turnover = position.diff().abs().fillna(position.abs())
    return pd.DataFrame(
        {
            "date": performance_date,
            "return": position * forward_open_return - turnover * cost_bps / 10_000.0,
            "position": position,
            "turnover": turnover,
            "entry": ((position > 0) & (position.shift(1, fill_value=0) == 0)).astype(int),
        }
    ).dropna(subset=["date", "return"]).reset_index(drop=True)


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
    max_drawdown = float(-(equity / equity.cummax() - 1.0).min())
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


def _largest_component(coordinates: list[tuple[int, int]]) -> int:
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
                if sum(abs(a - b) for a, b in zip(candidate, current)) == 1
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
    if protocol.get("reads_future_data") is not False:
        raise ValueError("future-data boundary is open")
    if protocol.get("selects_final_configuration") is not False:
        raise ValueError("mechanism screen cannot select a final configuration")

    materials = _read_object(repo / "research" / "S008" / "materials.json")
    external = materials.get("external_replication_materials")
    if not isinstance(external, dict):
        raise ValueError("external replication materials are missing")
    identities = {
        "518880.SH": {
            "research": materials["research_data_manifest"],
            "execution": materials["execution_data_manifest"],
        },
        "518800.SH": {
            "research": external["research_data_manifest"],
            "execution": external["execution_data_manifest"],
        },
    }
    cutoff = pd.Timestamp(protocol["development_cutoff"])
    expected_start = pd.Timestamp(protocol["development_start"])
    periods = {
        name: (pd.Timestamp(boundary[0]), pd.Timestamp(boundary[1]))
        for name, boundary in protocol["periods"].items()
    }
    grid = protocol["grid"]
    configurations = [
        {
            "name": f"drawdown_trigger={trigger:g};recovery_return={recovery:g}",
            "coordinates": (trigger_index, recovery_index),
            "drawdown_trigger": float(trigger),
            "recovery_return": float(recovery),
        }
        for trigger_index, trigger in enumerate(grid["drawdown_trigger"])
        for recovery_index, recovery in enumerate(grid["recovery_return"])
    ]

    trial_rows: list[dict[str, object]] = []
    opened_files: dict[str, dict[str, list[str]]] = {}
    for symbol in protocol["symbols"]:
        research, research_files = _load_development(
            raw, identities[symbol]["research"], repo, expected_start, cutoff
        )
        execution, execution_files = _load_development(
            raw, identities[symbol]["execution"], repo, expected_start, cutoff
        )
        merged = research.merge(
            execution[["date", "open"]].rename(columns={"open": "execution_open"}),
            on="date",
            how="inner",
            validate="one_to_one",
        )
        if len(merged) != len(research) or len(merged) != len(execution):
            raise ValueError(f"{symbol}: research and execution sessions differ")
        if merged["date"].max() >= pd.Timestamp(protocol["prohibited_start"]):
            raise ValueError(f"{symbol}: future data was loaded")
        opened_files[symbol] = {"research": research_files, "execution": execution_files}
        close = merged["close"].astype(float)
        forward_open_return = merged["execution_open"].shift(-2) / merged["execution_open"].shift(-1) - 1
        performance_date = merged["date"].shift(-1)
        positions: dict[str, pd.Series] = {}
        for configuration in configurations:
            positions[configuration["name"]] = _drawdown_recovery_position(
                close,
                int(grid["rolling_high_window"]),
                configuration["drawdown_trigger"],
                int(grid["recovery_horizon"]),
                configuration["recovery_return"],
                float(grid["near_high_exit_drawdown"]),
                float(grid["failure_return"]),
            )
        positions["always_long"] = pd.Series(1.0, index=close.index)
        for family, names in (
            ("BUY_HOLD", ["always_long"]),
            ("DRAWDOWN_RECOVERY", [item["name"] for item in configurations]),
        ):
            for name in names:
                for cost in protocol["cost_bps_one_way"]:
                    frame = _return_frame(
                        positions[name], forward_open_return, performance_date, cost
                    )
                    for period, (start, end) in periods.items():
                        trial_rows.append(
                            {
                                "symbol": symbol,
                                "family": family,
                                "configuration": name,
                                "cost_bps_one_way": cost,
                                "period": period,
                                **_metrics(frame, start, end),
                            }
                        )

    ledger = pd.DataFrame(trial_rows)
    ledger.to_csv(artifacts / "trial_ledger.csv", index=False, encoding="utf-8")
    lookup = ledger.set_index(
        ["symbol", "family", "configuration", "cost_bps_one_way", "period"]
    ).to_dict(orient="index")
    qualification_rows: list[dict[str, object]] = []
    joint_coordinates: list[tuple[int, int]] = []
    for configuration in configurations:
        per_symbol: dict[str, bool] = {}
        for symbol in protocol["symbols"]:
            name = configuration["name"]
            baseline = lookup[(symbol, "BUY_HOLD", "always_long", 10, "FULL")]
            full10 = lookup[(symbol, "DRAWDOWN_RECOVERY", name, 10, "FULL")]
            full30 = lookup[(symbol, "DRAWDOWN_RECOVERY", name, 30, "FULL")]
            positive_periods = all(
                lookup[(symbol, "DRAWDOWN_RECOVERY", name, 10, period)][
                    "annualized_return"
                ]
                > 0
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
            per_symbol[symbol] = qualifies
            qualification_rows.append(
                {
                    "symbol": symbol,
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
        if all(per_symbol.values()):
            joint_coordinates.append(configuration["coordinates"])

    qualification = pd.DataFrame(qualification_rows)
    qualification.to_csv(artifacts / "qualification.csv", index=False, encoding="utf-8")
    largest_component = _largest_component(joint_coordinates)
    if largest_component >= protocol["qualification"]["minimum_joint_connected_configs"]:
        decision = protocol["adjudication"]["connected_joint_qualification"]
        evidence_label = "FAVORABLE"
    elif joint_coordinates:
        decision = protocol["adjudication"]["isolated_joint_qualification"]
        evidence_label = "MIXED"
    else:
        decision = protocol["adjudication"]["no_joint_qualification"]
        evidence_label = "ADVERSE"
    joint_names = [
        configuration["name"]
        for configuration in configurations
        if configuration["coordinates"] in joint_coordinates
    ]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "mechanism_sequence": protocol["mechanism_sequence"],
        "decision": decision,
        "evidence_label": evidence_label,
        "configuration_count": len(configurations),
        "joint_qualified_configurations": joint_names,
        "largest_joint_qualified_component": largest_component,
        "trial_rows": int(len(ledger)),
        "reads_future_data": False,
        "selects_final_configuration": False,
    }
    _write_json(artifacts / "mechanism_summary.json", summary)
    _write_json(
        artifacts / "data_identity.json",
        {
            "materials": identities,
            "opened_files": opened_files,
            "latest_opened_session": cutoff.date().isoformat(),
            "reads_future_data": False,
        },
    )
    (experiment / "03_execution.md").write_text(
        "# S008 EX10 执行记录\n\n"
        f"按冻结协议在两只ETF上评价{len(configurations)}个回撤恢复配置、两档成本和"
        f"四个时期，共形成{len(ledger)}行trial ledger。没有读取未来数据、扩大参数网格、选择"
        "最终配置或创建候选。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX10 结论\n\n"
        f"裁决：`{decision}`，证据标签`{evidence_label}`；两只ETF联合合格配置为"
        f"{joint_names or '无'}，最大连续区域为{largest_component}。本实验不选择最终配置、"
        "不证明样本外Alpha，也不创建候选。\n",
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
            "symbol": "518880.SH+518800.SH",
            "symbols": protocol["symbols"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": decision,
            "evidence_label": evidence_label,
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
