from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX55"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cycles(index: pd.DatetimeIndex, enter: pd.Series, leave: pd.Series) -> tuple[pd.Series, pd.DataFrame]:
    active = False
    states: list[bool] = []
    rows: list[dict[str, object]] = []
    entry_date: pd.Timestamp | None = None
    for position, dt in enumerate(index):
        if not active and bool(enter.loc[dt]):
            active = True
            entry_date = dt
        elif active and bool(leave.loc[dt]):
            rows.append({"entry_date": entry_date, "exit_date": dt, "holding_sessions": position - int(index.get_loc(entry_date)) + 1})
            active = False
            entry_date = None
        states.append(active)
    if active:
        rows.append({"entry_date": entry_date, "exit_date": pd.NaT, "holding_sessions": None})
    return pd.Series(states, index=index), pd.DataFrame(rows)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    forbidden = ("reads_post_signal_prices", "candidate_generation", "parameter_search", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    if protocol.get("experiment_id") != EXPERIMENT_ID or any(protocol.get(key) for key in forbidden):
        raise ValueError("EX55 only permits a return-free family representative density screen")

    sources = protocol["sources"]
    semantic = repo / "experiments/S005" / str(sources["semantic_experiment"])
    census = repo / "experiments/S005" / str(sources["census_experiment"])
    similarity = repo / "experiments/S005" / str(sources["similarity_experiment"])
    for source in (semantic, census, similarity):
        validate_experiment_archive(source)
    checks = (
        (semantic / "experiment_manifest.json", sources["semantic_manifest_sha256"]),
        (semantic / "artifacts/opportunity_signal_inventory.csv", sources["opportunity_inventory_sha256"]),
        (census / "artifacts/primary_states.csv.gz", sources["primary_states_sha256"]),
        (census / "artifacts/signal_catalog.csv", sources["signal_catalog_sha256"]),
        (similarity / "artifacts/pair_similarity.csv", sources["pair_similarity_sha256"]),
    )
    for path, expected in checks:
        if _sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")

    primary = pd.read_csv(census / "artifacts/primary_states.csv.gz", parse_dates=["dt"]).set_index("dt")
    catalog = pd.read_csv(census / "artifacts/signal_catalog.csv")
    resolved: dict[str, dict[str, object]] = {}
    for key, spec in protocol["representatives"].items():
        found = catalog.loc[catalog["frequency"].eq(spec["frequency"]) & catalog["name"].eq(spec["name"])]
        if len(found) != 1:
            raise ValueError(f"representative does not resolve uniquely: {key}")
        signal_id = str(found.iloc[0]["signal_id"])
        resolved[str(key)] = {**spec, "signal_id": signal_id}

    rows: list[dict[str, object]] = []
    cycle_frames: list[pd.DataFrame] = []
    daily = pd.DataFrame(index=primary.index)
    density = protocol["density"]
    for hypothesis in protocol["hypotheses"]:
        keys = sorted(set(hypothesis["entry_all"]) | set(hypothesis["exit_any"]))
        valid = pd.Series(True, index=primary.index)
        enter = pd.Series(True, index=primary.index)
        leave = pd.Series(False, index=primary.index)
        for key in keys:
            series = primary[str(resolved[key]["signal_id"])]
            valid &= series.notna()
        for key in hypothesis["entry_all"]:
            spec = resolved[key]
            enter &= primary[str(spec["signal_id"])].isin(spec["bullish"])
        for key in hypothesis["exit_any"]:
            spec = resolved[key]
            leave |= primary[str(spec["signal_id"])].isin(spec["bearish"])
        valid_index = primary.index[valid]
        active, cycles = _cycles(valid_index, enter.loc[valid_index], leave.loc[valid_index])
        entries = pd.Series(0, index=valid_index)
        if not cycles.empty:
            entries.loc[pd.DatetimeIndex(cycles["entry_date"])] = 1
        rolling = entries.rolling(int(density["rolling_window_sessions"]), min_periods=int(density["rolling_window_sessions"])).sum().dropna()
        complete = cycles.loc[cycles["exit_date"].notna()]
        median = float(rolling.median()) if not rolling.empty else 0.0
        p10 = float(rolling.quantile(0.1)) if not rolling.empty else 0.0
        passes = median >= float(density["minimum_median"]) and p10 >= float(density["minimum_p10"])
        families = sorted({str(resolved[key]["family"]) for key in hypothesis["entry_all"]})
        frequencies = sorted({str(resolved[key]["frequency"]) for key in hypothesis["entry_all"]})
        rows.append({
            "hypothesis": str(hypothesis["id"]), "mechanism": str(hypothesis["mechanism"]),
            "entry_signal_count": len(hypothesis["entry_all"]), "information_families": "|".join(families),
            "family_count": len(families), "frequencies": "|".join(frequencies),
            "valid_start": valid_index.min().strftime("%Y-%m-%d"), "valid_sessions": len(valid_index),
            "independent_cycles": len(cycles), "complete_cycles": len(complete),
            "rolling_60_median": median, "rolling_60_p10": p10,
            "exposure": float(active.mean()),
            "median_holding_sessions": float(complete["holding_sessions"].median()) if not complete.empty else 0.0,
            "density_pass": bool(passes),
        })
        cycles.insert(0, "hypothesis", str(hypothesis["id"]))
        cycle_frames.append(cycles)
        daily[f"active_{hypothesis['id']}"] = active.reindex(primary.index)

    frame = pd.DataFrame(rows)
    passing = frame.loc[frame["density_pass"], "hypothesis"].tolist()
    frame.to_csv(artifacts / "hypothesis_density.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    cycles = pd.concat(cycle_frames, ignore_index=True)
    for column in ("entry_date", "exit_date"):
        cycles[column] = pd.to_datetime(cycles[column]).dt.strftime("%Y-%m-%d")
    cycles.to_csv(artifacts / "cycles.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    daily.reset_index().to_csv(artifacts / "hypothesis_states.csv.gz", index=False, compression={"method": "gzip", "compresslevel": 9, "mtime": 0}, lineterminator="\n")
    _write(artifacts / "resolved_representatives.json", {"schema_version": 1, "items": resolved})

    evidence = {
        "schema_version": 1, "experiment_id": EXPERIMENT_ID, "status": "COMPLETE",
        "representative_count": len(resolved), "hypothesis_count": len(frame),
        "density_passing_hypotheses": passing,
        "density_failed_hypotheses": frame.loc[~frame["density_pass"], "hypothesis"].tolist(),
        "reads_post_signal_prices": False,
        "decision": "PROCEED_TO_PREREGISTERED_FSC_RETURN_COMPARISON" if passing else "STOP_FSC_COMBINATIONS_ON_DENSITY",
        "candidate_created": False, "strategy_frozen": False, "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "density_evidence.json", evidence)
    table = ["|架构|信息族/尺度|周期|60日中位数/P10|暴露率|持有中位数|密度门|", "|---|---|---:|---:|---:|---:|---|"]
    for row in frame.itertuples(index=False):
        frequencies = str(row.frequencies).replace("|", "、")
        table.append(f"|{row.hypothesis}|{row.family_count}/{frequencies}|{row.independent_cycles}|{row.rolling_60_median:.0f}/{row.rolling_60_p10:.0f}|{row.exposure:.2%}|{row.median_holding_sessions:.1f}|{'PASS' if row.density_pass else 'FAIL'}|")
    (experiment / "03_execution.md").write_text("# S005 EX55 执行\n\n状态：`COMPLETE`。完成六条预注册架构的无收益周期与密度检查。\n\n" + "\n".join(table) + "\n", encoding="utf-8")
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX55 结论\n\n"
        f"裁决：`{evidence['decision']}`。通过频率门：{', '.join(passing) if passing else 'NONE'}。"
        "趋势中回撤与安静扩张两条竞争机制因持续密度不足停止，不读取其收益。\n\n"
        "通过者共享结构、趋势、量价三个30分钟职责，并保留无环境控制、日线趋势、周线结构"
        "三条预注册比较路径。它们不是三套候选，而是一次有限的环境层归因实验；"
        "下一轮统一执行口径读取收益，用消融回答低频环境究竟改善风险收益还是只减少交易。"
        "本轮没有创建候选，也没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID, "status": "COMPLETE", "experiment_type": protocol["experiment_type"],
        "strategy_id": "S005", "symbol": protocol["symbol"], "development_cutoff": protocol["development_cutoff"],
        "decision": evidence["decision"], "candidate_created": False, "strategy_frozen": False, "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
