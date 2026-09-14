from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import time

import czsc
import czsc._native as czsc_native
import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.signal_census import generate_signal_census


EXPERIMENT_ID = "20260914_S005_EX49"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state_id(signal_id: str, state: str) -> str:
    payload = json.dumps([signal_id, state], ensure_ascii=False, separators=(",", ":"))
    return "STATE-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16].upper()


def _role_hint(namespace: str) -> str:
    if namespace in {"cxt", "jcc", "pressure"}:
        return "STRUCTURE"
    if namespace in {"vol", "obv", "obvm", "cvolp", "amv", "emv"}:
        return "VOLUME_FLOW"
    if namespace in {"tas", "bar", "bias", "cci", "cmo", "dema", "skdj", "er"}:
        return "TREND_MOMENTUM"
    return "OTHER_TECHNICAL"


def _state_catalog(
    primary: pd.DataFrame,
    catalog: pd.DataFrame,
    evaluation_start: pd.Timestamp,
    eligibility: dict[str, object],
    density: dict[str, object],
) -> tuple[pd.DataFrame, dict[str, object]]:
    visible_index = primary.index[primary.index >= evaluation_start]
    generated = catalog.loc[catalog["status"].eq("GENERATED")].set_index("signal_id")
    rows: list[dict[str, object]] = []
    behavior_members: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    placeholder = {str(value) for value in eligibility["excluded_placeholder_states"]}
    for signal_id in primary.columns:
        config = generated.loc[signal_id]
        series = primary.loc[visible_index, signal_id]
        signal_coverage = float(series.notna().mean())
        for state_value in sorted(series.dropna().astype(str).unique()):
            active = series.eq(state_value).fillna(False)
            transitions = active & ~active.shift(1, fill_value=False)
            dates = pd.DatetimeIndex(transitions.index[transitions])
            years = Counter(int(value.year) for value in dates)
            rolling = transitions.astype(int).rolling(
                int(density["rolling_window_sessions"]),
                min_periods=int(density["rolling_window_sessions"]),
            ).sum().dropna()
            behavior = hashlib.sha256(active.astype(np.uint8).to_numpy().tobytes()).hexdigest()
            state_id = _state_id(str(signal_id), state_value)
            behavior_members[behavior].append((state_id, str(signal_id)))
            active_ratio = float(active.mean())
            technical_checks = {
                "coverage": signal_coverage >= float(eligibility["minimum_signal_coverage"]),
                "semantic": state_value not in placeholder,
                "active_ratio": (
                    active_ratio >= float(eligibility["minimum_active_ratio"])
                    and active_ratio <= float(eligibility["maximum_active_ratio"])
                ),
                "transitions": len(dates) >= int(eligibility["minimum_transitions"]),
                "years": len(years) >= int(eligibility["minimum_coverage_years"]),
            }
            median = float(rolling.median()) if not rolling.empty else 0.0
            p10 = float(rolling.quantile(0.1)) if not rolling.empty else 0.0
            rows.append({
                "state_id": state_id,
                "signal_id": signal_id,
                "frequency": str(config["frequency"]),
                "namespace": str(config["namespace"]),
                "name": str(config["name"]),
                "state_primary": state_value,
                "role_hint": _role_hint(str(config["namespace"])),
                "s001_reference_function": bool(config["s001_reference_function"]),
                "signal_coverage": signal_coverage,
                "active_days": int(active.sum()),
                "active_ratio": active_ratio,
                "independent_transitions": int(len(dates)),
                "coverage_years": int(len(years)),
                "maximum_year_share": float(max(years.values(), default=0) / len(dates)) if len(dates) else 0.0,
                "rolling_60_median": median,
                "rolling_60_p10": p10,
                "technical_eligible_before_dedup": bool(all(technical_checks.values())),
                "density_eligible": bool(
                    median >= float(density["minimum_median"])
                    and p10 >= float(density["minimum_p10"])
                ),
                "behavior_sha256": behavior,
                **{f"check_{key}": value for key, value in technical_checks.items()},
            })
    frame = pd.DataFrame(rows).sort_values(
        ["behavior_sha256", "s001_reference_function", "frequency", "name", "state_primary"],
        ascending=[True, True, True, True, True],
    ).reset_index(drop=True)
    frame["canonical_state"] = ~frame.duplicated("behavior_sha256", keep="first")
    frame["technical_eligible"] = frame["technical_eligible_before_dedup"] & frame["canonical_state"]
    frame["opportunity_pool"] = frame["technical_eligible"] & frame["density_eligible"]
    frame["supporting_review_pool"] = frame["technical_eligible"]
    groups = []
    for digest, members in sorted(behavior_members.items()):
        if len(members) > 1:
            groups.append({
                "behavior_sha256": digest,
                "members": [{"state_id": item[0], "signal_id": item[1]} for item in members],
            })
    redundancy = {
        "schema_version": 1,
        "definition": "exact equality of daily primary-state active vectors",
        "duplicate_group_count": len(groups),
        "duplicate_member_count": sum(len(item["members"]) for item in groups),
        "groups": groups,
    }
    return frame, redundancy


def _summary_table(frame: pd.DataFrame) -> str:
    scoped = frame.loc[frame["opportunity_pool"]].sort_values(
        ["rolling_60_p10", "rolling_60_median", "independent_transitions"],
        ascending=False,
    ).head(20)
    lines = [
        "|频率|信号函数|主状态|角色提示|60日中位数/P10|切换次数|S001重合|",
        "|---|---|---|---|---:|---:|---|",
    ]
    for row in scoped.itertuples(index=False):
        lines.append(
            f"|{row.frequency}|`{row.name}`|{row.state_primary}|{row.role_hint}|"
            f"{row.rolling_60_median:.0f}/{row.rolling_60_p10:.0f}|"
            f"{row.independent_transitions}|{'是' if row.s001_reference_function else '否'}|"
        )
    if scoped.empty:
        lines.append("|—|—|—|—|0/0|0|—|")
    return "\n".join(lines)


def main() -> None:
    started = time.perf_counter()
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = ("reads_post_signal_prices", "candidate_generation", "parameter_search", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("CZSC eligibility census may not read outcomes, select, promote, or deploy")
    if str(czsc.__version__) != str(protocol["signal_universe"]["czsc_version"]):
        raise ValueError("CZSC version differs from frozen protocol")

    context = RepositoryContext.discover(repo, explicit_root=repo)
    target = protocol["research_target"]
    dataset = protocol["dataset"]
    replay = load_replay_data(
        context,
        str(dataset["name"]),
        str(target["symbol"]),
        str(target["asset_type"]),
        date.fromisoformat(str(dataset["cutoff"])),
    )
    registry = [dict(item) for item in czsc_native.list_all_signals()]
    census = generate_signal_census(
        replay.adjusted,
        protocol["signal_universe"]["frequencies"],
        evaluation_start=str(dataset["evaluation_start"]),
        registry=registry,
    )
    states, redundancy = _state_catalog(
        census.primary,
        census.catalog,
        pd.Timestamp(dataset["evaluation_start"]),
        protocol["eligibility"],
        protocol["opportunity_density"],
    )
    catalog = census.catalog.sort_values(["status", "frequency", "name"]).reset_index(drop=True)
    catalog.to_csv(artifacts / "signal_catalog.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    for name, frame in (("primary_states", census.primary), ("full_states", census.full)):
        output = frame.loc[frame.index >= pd.Timestamp(dataset["evaluation_start"])].reset_index()
        output["dt"] = pd.to_datetime(output["dt"]).dt.strftime("%Y-%m-%d")
        output.to_csv(
            artifacts / f"{name}.csv.gz",
            index=False,
            encoding="utf-8",
            compression=compression,
            lineterminator="\n",
        )
    states.to_csv(
        artifacts / "state_eligibility.csv.gz",
        index=False,
        encoding="utf-8",
        compression=compression,
        lineterminator="\n",
    )
    _write(artifacts / "redundancy_map.json", redundancy)
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "dataset_fingerprint": replay.fingerprint,
        "czsc_version": str(czsc.__version__),
        **census.registry_summary,
        "primary_states": int(len(states)),
        "technical_eligible_states": int(states["technical_eligible"].sum()),
        "opportunity_pool_states": int(states["opportunity_pool"].sum()),
        "supporting_review_pool_states": int(states["supporting_review_pool"].sum()),
        "s001_reference_states": int(states["s001_reference_function"].sum()),
        "s001_reference_opportunity_states": int((states["s001_reference_function"] & states["opportunity_pool"]).sum()),
        "non_s001_opportunity_states": int((~states["s001_reference_function"] & states["opportunity_pool"]).sum()),
        "role_hint_counts": {
            str(key): int(value)
            for key, value in states.loc[states["supporting_review_pool"], "role_hint"].value_counts().sort_index().items()
        },
        "failures": list(census.failures),
        "reads_post_signal_prices": False,
        "decision": "PROCEED_TO_SEMANTIC_AND_COMPLEMENTARITY_REVIEW" if states["opportunity_pool"].any() else "STOP_CZSC_AS_S005_OPPORTUNITY_SOURCE_ON_DENSITY",
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "census_evidence.json", summary)
    elapsed = time.perf_counter() - started
    (experiment / "03_execution.md").write_text(
        "# S005 EX49 执行\n\n"
        f"状态：`COMPLETE`。CZSC注册表{summary['registered_total']}项，其中"
        f"{summary['included_functions']}个K线函数进入三个频率普查；请求"
        f"{summary['requested_configurations']}个默认配置，成功生成"
        f"{summary['generated_configurations']}个，失败或无法映射"
        f"{summary['failed_or_unmapped_configurations']}个，耗时{elapsed:.1f}秒。\n\n"
        "本轮没有读取信号后行情或收益，没有形成候选，也没有修改SM或PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX49 结论\n\n"
        f"裁决：`{summary['decision']}`。共观察{summary['primary_states']}个主状态；"
        f"技术准入{summary['technical_eligible_states']}项，其中满足S005证据速度门的机会池"
        f"{summary['opportunity_pool_states']}项，非S001函数贡献"
        f"{summary['non_s001_opportunity_states']}项。支持因子人工复核池"
        f"{summary['supporting_review_pool_states']}项。\n\n"
        "## 中频机会池前20项\n\n"
        + _summary_table(states)
        + "\n\n这些结果只说明因子可生成、可观察且频率足够，不代表其具有收益或方向性。"
        "下一轮须先审核语义和互补性，再冻结极少数组合进行收益评价。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": target["strategy_id"],
            "symbol": target["symbol"],
            "development_cutoff": dataset["cutoff"],
            "decision": summary["decision"],
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
