from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from dataflows import DataRequest, DataStatus, Dataflows
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260923_S008_EX54"


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


def _fetch(dataflows: Dataflows, specification: dict[str, object], repo: Path):
    result = dataflows.fetch(
        DataRequest(
            dataset=str(specification["dataset"]),
            symbol=str(specification["symbol"]),
            start=str(specification["start"]),
            end=str(specification["cutoff"]),
            required_cutoff=str(specification["cutoff"]),
            frequency="daily",
            options={"env_file": repo / ".env"},
        )
    )
    if result.status is not DataStatus.READY:
        code = result.error.code if result.error else "UNKNOWN"
        raise RuntimeError(f"DFLS {specification['dataset']} is {result.status.value}: {code}")
    return result


def _frame(result) -> pd.DataFrame:
    frame = result.dataframe.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="raise")
    return frame.sort_values("Date").set_index("Date")


def _strict_prior(series: pd.Series, target: pd.DatetimeIndex) -> pd.Series:
    source = pd.DataFrame(
        {"SourceDate": series.index, "Value": pd.to_numeric(series, errors="raise").to_numpy()}
    ).sort_values("SourceDate")
    aligned = pd.merge_asof(
        pd.DataFrame({"Date": target}).sort_values("Date"),
        source,
        left_on="Date",
        right_on="SourceDate",
        direction="backward",
        allow_exact_matches=False,
    ).set_index("Date")
    if (aligned["SourceDate"].dropna() >= aligned["SourceDate"].dropna().index).any():
        raise ValueError("global source date is not strictly prior to China target date")
    return aligned["Value"]


def _spearman(left: pd.Series, right: pd.Series) -> float | None:
    frame = pd.concat([left.rename("left"), right.rename("right")], axis=1, sort=False).dropna()
    if len(frame) < 20 or frame["left"].nunique() < 2 or frame["right"].nunique() < 2:
        return None
    return float(frame["left"].corr(frame["right"], method="spearman"))


def _partial_rank_ic(feature: pd.Series, core: pd.Series, outcome: pd.Series) -> float | None:
    frame = pd.concat(
        [feature.rename("feature"), core.rename("core"), outcome.rename("outcome")],
        axis=1,
        sort=False,
    ).dropna()
    if len(frame) < 20:
        return None
    ranked = frame.rank(method="average", pct=True)
    design = np.column_stack([np.ones(len(ranked)), ranked["core"].to_numpy(dtype=float)])
    residual = ranked["feature"].to_numpy(dtype=float) - design @ np.linalg.lstsq(
        design,
        ranked["feature"].to_numpy(dtype=float),
        rcond=None,
    )[0]
    return float(pd.Series(residual).corr(ranked["outcome"].reset_index(drop=True)))


def _components(features: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    neighbors = {feature: set() for feature in features}
    for left, right in edges:
        neighbors[left].add(right)
        neighbors[right].add(left)
    seen: set[str] = set()
    result: list[list[str]] = []
    for feature in sorted(features):
        if feature in seen:
            continue
        stack, component = [feature], []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.append(current)
            stack.extend(sorted(neighbors[current] - seen))
        result.append(sorted(component))
    return result


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo, artifacts = experiment.parents[2], experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    forbidden = (
        "reads_sealed_validation",
        "template_selection",
        "starts_search",
        "candidate_generation",
        "promotion_allowed",
        "mutates_catalog",
        "mutates_platform",
        "mutates_pte",
    )
    if not protocol.get("reads_development_returns") or not protocol.get("input_selection"):
        raise ValueError("role review must explicitly permit development input selection")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("role review permissions differ from frozen protocol")

    source_paths = {
        "family_sha256": repo / "research/registrations/S008/family.json",
        "materials_sha256": repo / "research/S008/materials.json",
        "ex53_manifest_sha256": repo
        / "experiments/S008/20260923_S008_EX53/experiment_manifest.json",
        "dfls_contract_sha256": repo / "packages/dataflows/src/dataflows/contract.py",
        "dfls_facade_sha256": repo / "packages/dataflows/src/dataflows/facade.py",
        "dfls_adapter_sha256": repo
        / "packages/dataflows/src/dataflows/tushare_strategy_data.py",
    }
    for key, path in source_paths.items():
        if _sha256(path) != str(protocol["sources"][key]):
            raise ValueError(f"frozen source differs: {path}")
    validate_experiment_archive(repo / "experiments/S008/20260923_S008_EX53")

    dataflows = Dataflows()
    results = {
        key: _fetch(dataflows, dict(value), repo) for key, value in protocol["datasets"].items()
    }
    frames = {key: _frame(result) for key, result in results.items()}
    expected_minimum = {"sge_gold": 5390, "etf": 2780, "xau": 5400, "xag": 5400, "spx": 5400, "rut": 5400}
    if not all(len(frames[key]) >= minimum for key, minimum in expected_minimum.items()):
        raise ValueError(protocol["adjudication"]["data_failure"])

    target_index = frames["sge_gold"].index
    xau = _strict_prior((frames["xau"]["BidClose"] + frames["xau"]["AskClose"]) / 2, target_index)
    xag = _strict_prior((frames["xag"]["BidClose"] + frames["xag"]["AskClose"]) / 2, target_index)
    spx_source = (1 + pd.to_numeric(frames["spx"]["PercentChange"], errors="raise")).cumprod()
    rut_source = (1 + pd.to_numeric(frames["rut"]["PercentChange"], errors="raise")).cumprod()
    spx, rut = _strict_prior(spx_source, target_index), _strict_prior(rut_source, target_index)
    panel = pd.DataFrame(index=target_index)
    panel["gold_minus_silver_return_20"] = xau.pct_change(20, fill_method=None) - xag.pct_change(20, fill_method=None)
    panel["gold_minus_silver_return_60"] = xau.pct_change(60, fill_method=None) - xag.pct_change(60, fill_method=None)
    ratio = xau / xag
    panel["gold_silver_ratio_distance_120"] = ratio / ratio.rolling(120).mean() - 1
    panel["rut_minus_spx_return_20"] = (
        rut.pct_change(20, fill_method=None) - spx.pct_change(20, fill_method=None)
    ) * -1
    if set(panel.columns) != set(protocol["feature_orientation"]):
        raise ValueError("implemented features differ from frozen protocol")

    horizon = int(protocol["primary_horizon_sessions"])
    sge_close = pd.to_numeric(frames["sge_gold"]["Close"], errors="raise")
    sge_outcome = sge_close.shift(-horizon) / sge_close - 1
    sge_exit = pd.Series(target_index, index=target_index).shift(-horizon)
    etf_close = pd.to_numeric(frames["etf"]["Close"], errors="raise")
    etf_outcome = (etf_close.shift(-horizon) / etf_close - 1).reindex(target_index)
    etf_exit = pd.Series(frames["etf"].index, index=frames["etf"].index).shift(-horizon).reindex(target_index)

    discovery = target_index.to_series().between("2003-01-02", "2012-12-31") & sge_exit.le(pd.Timestamp("2012-12-31"))
    confirmation = target_index.to_series().between("2013-07-29", "2024-12-31") & sge_exit.le(pd.Timestamp("2024-12-31"))
    etf_confirmation = confirmation & etf_exit.le(pd.Timestamp("2024-12-31"))
    core_features = [str(value) for value in protocol["core_features"]]
    correlation_rows: list[dict[str, object]] = []
    edges: list[tuple[str, str]] = []
    threshold = float(protocol["redundancy"]["minimum_stable_absolute_spearman"])
    for ordinal, left in enumerate(core_features):
        for right in core_features[ordinal + 1 :]:
            discovery_correlation = _spearman(panel[left].loc[discovery], panel[right].loc[discovery])
            confirmation_correlation = _spearman(
                panel[left].loc[confirmation], panel[right].loc[confirmation]
            )
            stable_absolute = min(abs(discovery_correlation), abs(confirmation_correlation))
            redundant = stable_absolute >= threshold
            if redundant:
                edges.append((left, right))
            correlation_rows.append(
                {
                    "left": left,
                    "right": right,
                    "discovery_spearman": discovery_correlation,
                    "confirmation_spearman": confirmation_correlation,
                    "stable_absolute_spearman": stable_absolute,
                    "redundant": redundant,
                }
            )

    ex53_ledger = pd.read_csv(
        repo / "experiments/S008/20260923_S008_EX53/artifacts/global_risk_path_ledger.csv.gz"
    )
    primary = ex53_ledger.loc[ex53_ledger["horizon_sessions"].eq(horizon)].set_index("feature")
    supported_counts = ex53_ledger.groupby("feature")["evidence_label"].apply(
        lambda values: int(values.isin(["DIRECTIONALLY_STABLE", "NOMINAL_SUPPORT", "FDR_SUPPORTED"]).sum())
    )
    representatives: list[str] = []
    components = _components(core_features, edges)
    for component in components:
        ranking = []
        for feature in component:
            row = primary.loc[feature]
            minimum_ic = min(
                float(row["discovery_sge_ic"]),
                float(row["confirmation_sge_ic"]),
                float(row["confirmation_etf_ic"]),
            )
            ranking.append(
                (
                    -int(supported_counts.loc[feature]),
                    -minimum_ic,
                    -float(row["bootstrap_positive_probability"]),
                    feature,
                )
            )
        representatives.append(sorted(ranking)[0][3])

    segment_rows: list[dict[str, object]] = []
    for raw_segment in protocol["segments"]:
        segment = dict(raw_segment)
        mask = target_index.to_series().between(segment["start"], segment["end"]) & sge_exit.le(
            pd.Timestamp(segment["end"])
        )
        etf_mask = mask & etf_exit.le(pd.Timestamp(segment["end"]))
        for feature in [*representatives, str(protocol["risk_feature"])]:
            segment_rows.append(
                {
                    "segment": segment["name"],
                    "feature": feature,
                    "sge_observations": int(pd.concat([panel[feature].loc[mask], sge_outcome.loc[mask]], axis=1, sort=False).dropna().shape[0]),
                    "sge_ic": _spearman(panel[feature].loc[mask], sge_outcome.loc[mask]),
                    "etf_observations": int(pd.concat([panel[feature].loc[etf_mask], etf_outcome.loc[etf_mask]], axis=1, sort=False).dropna().shape[0]),
                    "etf_ic": _spearman(panel[feature].loc[etf_mask], etf_outcome.loc[etf_mask]),
                }
            )
    segment_frame = pd.DataFrame(segment_rows)

    core_rule = protocol["core_stability"]
    core_pass: dict[str, bool] = {}
    for feature in representatives:
        values = segment_frame.loc[segment_frame["feature"].eq(feature), "sge_ic"]
        core_pass[feature] = bool(
            values.gt(0).sum() >= int(core_rule["minimum_positive_segments"])
            and values.ge(float(core_rule["segment_ic_floor"])).sum()
            >= int(core_rule["minimum_segments_at_ic_floor"])
        )
    eligible_representatives = [feature for feature in representatives if core_pass[feature]]

    risk = str(protocol["risk_feature"])
    risk_rule = protocol["risk_robustness"]
    risk_segments = segment_frame.loc[segment_frame["feature"].eq(risk), "sge_ic"]
    segment_pass = bool(
        risk_segments.gt(0).sum() >= int(risk_rule["minimum_positive_segments"])
        and risk_segments.ge(float(risk_rule["segment_ic_floor"])).sum()
        >= int(risk_rule["minimum_segments_at_ic_floor"])
    )
    non_crisis = ~target_index.year.isin([int(value) for value in risk_rule["excluded_crisis_years"]])
    non_crisis_ics = {
        "discovery": _spearman(panel[risk].loc[discovery & non_crisis], sge_outcome.loc[discovery & non_crisis]),
        "confirmation": _spearman(panel[risk].loc[confirmation & non_crisis], sge_outcome.loc[confirmation & non_crisis]),
    }
    non_crisis_pass = all(
        value is not None and value >= float(risk_rule["minimum_non_crisis_ic"])
        for value in non_crisis_ics.values()
    )
    partial_rows: list[dict[str, object]] = []
    for core in eligible_representatives:
        for boundary, mask, outcome in (
            ("DISCOVERY_SGE", discovery, sge_outcome),
            ("CONFIRMATION_SGE", confirmation, sge_outcome),
            ("CONFIRMATION_ETF", etf_confirmation, etf_outcome),
        ):
            partial_rows.append(
                {
                    "core_feature": core,
                    "boundary": boundary,
                    "risk_partial_ic": _partial_rank_ic(
                        panel[risk].loc[mask], panel[core].loc[mask], outcome.loc[mask]
                    ),
                }
            )
    partial_frame = pd.DataFrame(partial_rows)
    partial_pass = bool(
        not partial_frame.empty
        and partial_frame["risk_partial_ic"].ge(float(risk_rule["minimum_partial_ic"])).all()
    )
    risk_pass = segment_pass and non_crisis_pass and partial_pass

    if not eligible_representatives:
        decision = protocol["adjudication"]["no_core"]
    elif risk_pass:
        decision = protocol["adjudication"]["combined"]
    else:
        decision = protocol["adjudication"]["core_only"]

    pd.DataFrame(correlation_rows).to_csv(
        artifacts / "redundancy_matrix.csv", index=False, encoding="utf-8", lineterminator="\n"
    )
    segment_frame.to_csv(
        artifacts / "segment_stability.csv", index=False, encoding="utf-8", lineterminator="\n"
    )
    partial_frame.to_csv(
        artifacts / "risk_partial_information.csv", index=False, encoding="utf-8", lineterminator="\n"
    )
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "decision": decision,
        "redundancy_components": components,
        "selected_representatives": representatives,
        "eligible_core_representatives": eligible_representatives,
        "core_stability_pass": core_pass,
        "risk_segment_pass": segment_pass,
        "risk_non_crisis_ics": non_crisis_ics,
        "risk_non_crisis_pass": non_crisis_pass,
        "risk_partial_information_pass": partial_pass,
        "risk_confirmation_admissible": risk_pass,
        "reads_sealed_validation": False,
        "input_selected": True,
        "template_selected": False,
        "search_started": False,
        "candidate_created": False,
        "platform_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "role_review_summary.json", summary)
    (experiment / "03_execution.md").write_text(
        "# S008 EX54 执行记录\n\n"
        f"三条金银路径形成{len(components)}个冗余簇，选择代表={representatives}；通过四段稳定门的"
        f"核心代表={eligible_representatives}。罗素相对标普路径分段通过={segment_pass}，剔除危机"
        f"年份通过={non_crisis_pass}，相对核心的增量通过={partial_pass}。未读取封存池、实例化原型"
        "或启动搜索。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S008 EX54 结论\n\n"
        f"机器裁决：`{decision}`。可承担核心职责的金银代表={eligible_representatives or '无'}；"
        f"罗素相对标普风险确认可采纳={risk_pass}。本实验只确定下一原型允许使用的信息职责，不构成"
        "策略、参数或候选证据。\n",
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
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
