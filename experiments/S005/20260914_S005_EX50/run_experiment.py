from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX50"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cycles(index: pd.DatetimeIndex, enter: pd.Series, leave: pd.Series) -> tuple[pd.Series, pd.DataFrame]:
    active = False
    states: list[bool] = []
    rows: list[dict[str, object]] = []
    entry_date: pd.Timestamp | None = None
    for position, date in enumerate(index):
        if not active and bool(enter.iloc[position]):
            active = True
            entry_date = date
        elif active and bool(leave.iloc[position]):
            rows.append({
                "entry_date": entry_date,
                "exit_date": date,
                "holding_sessions": position - int(index.get_loc(entry_date)) + 1,
            })
            active = False
            entry_date = None
        states.append(active)
    if active:
        rows.append({"entry_date": entry_date, "exit_date": pd.NaT, "holding_sessions": np.nan})
    return pd.Series(states, index=index, name="active"), pd.DataFrame(rows)


def _phi(left: pd.Series, right: pd.Series) -> float:
    return float(left.astype(int).corr(right.astype(int)))


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = ("reads_post_signal_prices", "candidate_generation", "parameter_search", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("combination density study may not read outcomes, select, promote, or deploy")

    sources = protocol["sources"]
    czsc_dir = repo / "experiments/S005" / str(sources["czsc_experiment"])
    risk_dir = repo / "experiments/S005" / str(sources["risk_experiment"])
    validate_experiment_archive(czsc_dir)
    validate_experiment_archive(risk_dir)
    source_checks = (
        (czsc_dir / "experiment_manifest.json", sources["czsc_manifest_sha256"]),
        (czsc_dir / "artifacts/primary_states.csv.gz", sources["primary_states_sha256"]),
        (czsc_dir / "artifacts/state_eligibility.csv.gz", sources["eligibility_sha256"]),
        (risk_dir / "experiment_manifest.json", sources["risk_manifest_sha256"]),
        (risk_dir / "artifacts/state_ledger.csv.gz", sources["risk_ledger_sha256"]),
    )
    for path, expected in source_checks:
        if _sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")

    primary = pd.read_csv(czsc_dir / "artifacts/primary_states.csv.gz", parse_dates=["dt"]).set_index("dt")
    risk = pd.read_csv(risk_dir / "artifacts/state_ledger.csv.gz", parse_dates=["dt"]).set_index("dt")
    index = primary.index.intersection(risk.index).sort_values()
    primary = primary.reindex(index)
    risk = risk.reindex(index)
    opportunity = protocol["factors"]["opportunity"]
    confirmation = protocol["factors"]["confirmation"]
    core = primary[str(opportunity["signal_id"])]
    confirm = primary[str(confirmation["signal_id"])]
    core_up = core.eq(str(opportunity["bullish_state"]))
    core_down = core.eq(str(opportunity["bearish_state"]))
    confirm_up = confirm.eq(str(confirmation["bullish_state"]))
    risk_ready = risk[["coherence_exit", "leadership_exit"]].notna().all(axis=1)
    fragile = risk_ready & (
        risk["coherence_20"].le(risk["coherence_exit"])
        | risk["leadership_concentration"].ge(risk["leadership_exit"])
    )

    density = protocol["density"]
    summaries: list[dict[str, object]] = []
    cycle_frames: list[pd.DataFrame] = []
    daily = pd.DataFrame({
        "dt": index,
        "core_up": core_up.to_numpy(),
        "core_down": core_down.to_numpy(),
        "confirmation_up": confirm_up.to_numpy(),
        "internal_fragility": fragile.to_numpy(),
    }).set_index("dt")
    for variant in protocol["variants"]:
        use_confirmation = bool(variant["confirmation"])
        use_risk = bool(variant["risk"])
        enter = core_up.copy()
        if use_confirmation:
            enter &= confirm_up
        if use_risk:
            enter &= ~fragile
        leave = core_down.copy()
        if use_risk:
            leave |= fragile
        active, cycles = _cycles(index, enter, leave)
        entry_flags = pd.Series(False, index=index)
        if not cycles.empty:
            entry_flags.loc[pd.DatetimeIndex(cycles["entry_date"])] = True
        rolling = entry_flags.astype(int).rolling(
            int(density["rolling_window_sessions"]),
            min_periods=int(density["rolling_window_sessions"]),
        ).sum().dropna()
        complete = cycles.loc[cycles["exit_date"].notna()]
        summaries.append({
            "variant": str(variant["id"]),
            "uses_confirmation": use_confirmation,
            "uses_risk": use_risk,
            "independent_cycles": int(len(cycles)),
            "complete_cycles": int(len(complete)),
            "rolling_60_median": float(rolling.median()),
            "rolling_60_p10": float(rolling.quantile(0.1)),
            "rolling_60_minimum": float(rolling.min()),
            "state_exposure": float(active.mean()),
            "median_holding_sessions": float(complete["holding_sessions"].median()),
            "density_pass": bool(
                rolling.median() >= float(density["minimum_median"])
                and rolling.quantile(0.1) >= float(density["minimum_p10"])
            ),
        })
        cycles.insert(0, "variant", str(variant["id"]))
        cycle_frames.append(cycles)
        daily[f"active_{variant['id']}"] = active

    summary = pd.DataFrame(summaries)
    full = summary.loc[summary["variant"].eq("FULL")].iloc[0]
    diagnostics = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "state_sessions": int(len(index)),
        "core_up_ratio": float(core_up.mean()),
        "confirmation_up_ratio": float(confirm_up.mean()),
        "internal_fragility_ratio": float(fragile.mean()),
        "core_confirmation_phi": _phi(core_up, confirm_up),
        "core_fragility_phi": _phi(core_up, fragile),
        "confirmation_rate_when_core_up": float(confirm_up.loc[core_up].mean()),
        "fragility_rate_when_core_up": float(fragile.loc[core_up].mean()),
        "full_density_pass": bool(full["density_pass"]),
        "decision": "PROCEED_TO_FIXED_COMBINATION_RETURN_TEST" if bool(full["density_pass"]) else "STOP_THREE_ROLE_COMBINATION_ON_DENSITY",
        "reads_post_signal_prices": False,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    summary.to_csv(artifacts / "variant_density.csv", index=False, lineterminator="\n")
    cycles_out = pd.concat(cycle_frames, ignore_index=True)
    for column in ("entry_date", "exit_date"):
        cycles_out[column] = pd.to_datetime(cycles_out[column]).dt.strftime("%Y-%m-%d")
    cycles_out.to_csv(artifacts / "cycles.csv", index=False, lineterminator="\n")
    daily_out = daily.reset_index()
    daily_out["dt"] = daily_out["dt"].dt.strftime("%Y-%m-%d")
    daily_out.to_csv(
        artifacts / "combination_states.csv.gz",
        index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    (artifacts / "combination_evidence.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    table = ["|版本|确认层|风险层|周期|60日中位数/P10|暴露率|持有中位数|密度门|", "|---|---|---|---:|---:|---:|---:|---|"]
    for row in summary.itertuples(index=False):
        table.append(
            f"|{row.variant}|{'是' if row.uses_confirmation else '否'}|{'是' if row.uses_risk else '否'}|"
            f"{row.independent_cycles}|{row.rolling_60_median:.1f}/{row.rolling_60_p10:.1f}|"
            f"{row.state_exposure:.2%}|{row.median_holding_sessions:.1f}|{'PASS' if row.density_pass else 'FAIL'}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S005 EX50 执行\n\n状态：`COMPLETE`。按预注册三层组合与四个消融版本完成无收益"
        "持仓周期和频率审计。没有读取信号后行情或收益。\n\n" + "\n".join(table) + "\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX50 结论\n\n"
        f"裁决：`{diagnostics['decision']}`。完整组合形成{int(full['independent_cycles'])}个周期，"
        f"滚动60日中位数/P10为{full['rolling_60_median']:.1f}/{full['rolling_60_p10']:.1f}，"
        f"暴露率{full['state_exposure']:.2%}。核心结构与量价确认的Phi相关系数为"
        f"{diagnostics['core_confirmation_phi']:.3f}，核心向上期间确认通过率"
        f"{diagnostics['confirmation_rate_when_core_up']:.2%}；内部脆弱状态占"
        f"{diagnostics['internal_fragility_ratio']:.2%}。\n\n"
        "消融版本只用于说明确认层与风险层如何改变周期，不参与择优。本轮没有创建候选，"
        "没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": protocol["development_cutoff"],
            "decision": diagnostics["decision"],
            "candidate_created": False,
            "strategy_frozen": False,
            "pte_mutated": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
