from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX16"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper module: {path}")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def _ic(frame: pd.DataFrame) -> tuple[int, float | None, float | None]:
    clean = frame.dropna()
    if len(clean) < 3 or clean["score"].nunique() < 2 or clean["outcome"].nunique() < 2:
        return len(clean), None, None
    result = spearmanr(clean["score"], clean["outcome"])
    return len(clean), float(result.statistic), float(result.pvalue)


def _bh_qvalues(pvalues: pd.Series) -> pd.Series:
    valid = pvalues.dropna().sort_values()
    result = pd.Series(np.nan, index=pvalues.index, dtype=float)
    if valid.empty:
        return result
    ranks = np.arange(1, len(valid) + 1, dtype=float)
    adjusted = (valid.to_numpy(dtype=float) * len(valid) / ranks)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result.loc[valid.index] = np.minimum(adjusted, 1.0)
    return result


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX16 protocol identity or return declaration differs")
    sources = protocol["sources"]
    panel_experiment = repo / "experiments/S006" / str(sources["panel_experiment_id"])
    validate_experiment_archive(panel_experiment)
    frozen = {
        panel_experiment / "experiment_manifest.json": sources["panel_manifest_sha256"],
        repo / str(sources["panel_path"]): sources["panel_sha256"],
        repo / str(sources["ex14_script_path"]): sources["ex14_script_sha256"],
        repo / str(sources["ex12_script_path"]): sources["ex12_script_sha256"],
        repo / str(sources["ex06_script_path"]): sources["ex06_script_sha256"],
        repo / str(sources["primary_states_path"]): sources["primary_states_sha256"],
        repo / str(sources["project_states_path"]): sources["project_states_sha256"],
        repo / str(sources["factor_ledger_path"]): sources["factor_ledger_sha256"],
        repo / str(sources["industry_path"]): sources["industry_sha256"],
        repo / str(sources["analyst_path"]): sources["analyst_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != str(expected):
            raise ValueError(f"frozen source differs: {path}")
    h6 = _module(repo / str(sources["ex06_script_path"]), "s006_ex16_h6")
    h12 = _module(repo / str(sources["ex12_script_path"]), "s006_ex16_h12")
    h14 = _module(repo / str(sources["ex14_script_path"]), "s006_ex16_h14")

    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(
        context,
        str(protocol["dataset"]["name"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        date.fromisoformat(str(protocol["development_cutoff"])),
    )
    if replay.fingerprint != str(protocol["dataset"]["fingerprint"]):
        raise ValueError("research dataset differs from protocol")
    prices = replay.adjusted.daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.set_index("dt").sort_index()
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    calendar = prices.index
    outcome = prices["open"].astype(float).shift(-1).div(prices["open"].astype(float)).sub(1.0)
    dataset = protocol["dataset"]
    discovery = (calendar >= pd.Timestamp(dataset["discovery_start"])) & (calendar <= pd.Timestamp(dataset["discovery_end"]))
    confirmation = (calendar >= pd.Timestamp(dataset["confirmation_start"])) & (calendar <= pd.Timestamp(dataset["confirmation_end"]))
    panel = pd.read_csv(repo / str(sources["panel_path"]))
    raw_components = h14._materialize(repo, protocol, panel, calendar, h6)
    regime_id = str(protocol["regime"]["factor_id"])
    regime_raw = raw_components[regime_id][1]
    excluded = set(map(str, protocol["scoring"]["excluded_information_families"]))
    opportunity_panel = panel.loc[~panel["information_family"].isin(excluded)].copy()
    prior = float(protocol["scoring"]["categorical_shrinkage_prior_count"])

    discovery_family_parts: dict[str, list[pd.Series]] = {
        str(family): [] for family in sorted(opportunity_panel["information_family"].unique())
    }
    discovery_regime_parts: list[pd.Series] = []
    for year in (2021, 2022, 2023):
        train_mask = discovery & (calendar.year != year)
        apply_mask = discovery & (calendar.year == year)
        component_scores: dict[str, pd.Series] = {}
        for component_id, row in opportunity_panel.set_index("component_id").iterrows():
            kind, raw = raw_components[str(component_id)]
            component_scores[str(component_id)] = (
                h12._categorical_score(raw, outcome, train_mask, apply_mask, prior, h6)
                if kind == "CATEGORICAL_SIGNAL"
                else h12._continuous_score(raw, outcome, train_mask, apply_mask, h6)
            )
        score_frame = pd.DataFrame(component_scores)
        for family, group in opportunity_panel.groupby("information_family", sort=True):
            discovery_family_parts[str(family)].append(score_frame[group["component_id"].astype(str).tolist()].median(axis=1, skipna=True))
        threshold = float(regime_raw.loc[train_mask].median())
        discovery_regime_parts.append(pd.Series(np.where(regime_raw.loc[apply_mask] >= threshold, "trend", "range"), index=calendar[apply_mask]))
    discovery_families = pd.DataFrame({family: pd.concat(parts).sort_index() for family, parts in discovery_family_parts.items()})
    discovery_regimes = pd.concat(discovery_regime_parts).sort_index()

    confirmation_scores: dict[str, pd.Series] = {}
    for component_id, row in opportunity_panel.set_index("component_id").iterrows():
        kind, raw = raw_components[str(component_id)]
        confirmation_scores[str(component_id)] = (
            h12._categorical_score(raw, outcome, discovery, confirmation, prior, h6)
            if kind == "CATEGORICAL_SIGNAL"
            else h12._continuous_score(raw, outcome, discovery, confirmation, h6)
        )
    confirmation_frame = pd.DataFrame(confirmation_scores)
    confirmation_families = pd.DataFrame({
        str(family): confirmation_frame[group["component_id"].astype(str).tolist()].median(axis=1, skipna=True)
        for family, group in opportunity_panel.groupby("information_family", sort=True)
    })
    confirmation_threshold = float(regime_raw.loc[discovery].median())
    confirmation_regimes = pd.Series(np.where(regime_raw.loc[confirmation] >= confirmation_threshold, "trend", "range"), index=calendar[confirmation])

    acceptance = protocol["acceptance"]
    rows: list[dict[str, object]] = []
    for family in discovery_families.columns:
        for regime in map(str, protocol["regime"]["states"]):
            disc_index = discovery_regimes.index[discovery_regimes.eq(regime)]
            disc = pd.concat([discovery_families.loc[disc_index, family].rename("score"), outcome.loc[disc_index].rename("outcome")], axis=1)
            n, ic, pvalue = _ic(disc)
            annual: dict[str, dict[str, float | int | None]] = {}
            same_direction = 0
            for year in (2021, 2022, 2023):
                annual_frame = disc.loc[disc.index.year == year]
                annual_n, annual_ic, _ = _ic(annual_frame)
                annual[str(year)] = {"n": annual_n, "ic": annual_ic}
                if ic is not None and annual_ic is not None and np.sign(annual_ic) == np.sign(ic):
                    same_direction += 1
            conf_index = confirmation_regimes.index[confirmation_regimes.eq(regime)]
            conf = pd.concat([confirmation_families.loc[conf_index, family].rename("score"), outcome.loc[conf_index].rename("outcome")], axis=1)
            conf_n, conf_ic, _ = _ic(conf)
            rows.append({
                "path_id": f"{family}:{regime}",
                "information_family": family,
                "regime": regime,
                "discovery_n": n,
                "discovery_ic": ic,
                "discovery_pvalue": pvalue,
                "same_direction_discovery_years": same_direction,
                "annual_discovery_json": json.dumps(annual, sort_keys=True),
                "confirmation_n": conf_n,
                "confirmation_ic": conf_ic,
            })
    ledger = pd.DataFrame(rows)
    if len(ledger) != int(protocol["scoring"]["expected_paths"]):
        raise ValueError("conditional path count differs")
    ledger["discovery_fdr_q"] = _bh_qvalues(ledger["discovery_pvalue"])
    other_ic = ledger.set_index(["information_family", "regime"])["discovery_ic"]
    ledger["other_regime_ic"] = [other_ic.get((row.information_family, "trend" if row.regime == "range" else "range")) for row in ledger.itertuples()]
    ledger["absolute_regime_ic_gap"] = (ledger["discovery_ic"] - ledger["other_regime_ic"]).abs()
    minimum_annual = int(acceptance["minimum_annual_observations"])
    ledger["minimum_annual_observations"] = ledger["annual_discovery_json"].map(
        lambda value: min(int(item["n"]) for item in json.loads(value).values())
    )
    ledger["role"] = np.where(ledger["discovery_ic"] >= 0, "OPPORTUNITY", "VETO")
    ledger["validated"] = (
        ledger["discovery_n"].ge(int(acceptance["minimum_pooled_observations"]))
        & ledger["minimum_annual_observations"].ge(minimum_annual)
        & ledger["discovery_ic"].abs().ge(float(acceptance["minimum_absolute_discovery_ic"]))
        & ledger["discovery_fdr_q"].le(float(acceptance["maximum_discovery_fdr_q"]))
        & ledger["same_direction_discovery_years"].ge(int(acceptance["minimum_same_direction_discovery_years"]))
        & ledger["absolute_regime_ic_gap"].ge(float(acceptance["minimum_absolute_regime_ic_gap"]))
        & ledger["confirmation_ic"].abs().ge(float(acceptance["minimum_absolute_confirmation_ic"]))
        & np.sign(ledger["confirmation_ic"]).eq(np.sign(ledger["discovery_ic"]))
    )
    ledger = ledger.sort_values(["validated", "discovery_fdr_q", "discovery_ic"], ascending=[False, True, False])
    ledger.to_csv(artifacts / "conditional_family_audit.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    validated = ledger.loc[ledger["validated"]]
    family_count = int(validated["information_family"].nunique())
    proceed = family_count >= int(acceptance["minimum_validated_information_families"])
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "regime_threshold_confirmation": confirmation_threshold,
        "tested_paths": len(ledger),
        "validated_paths": len(validated),
        "validated_information_families": family_count,
        "validated_path_ids": validated["path_id"].astype(str).tolist(),
        "decision": "PROCEED_TO_CONDITIONAL_ARCHITECTURE" if proceed else "STOP_ER60_CONDITIONAL_ARCHITECTURE",
        "strategy_returns_generated": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "conditional_family_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        f"# S006 EX16 执行\n\n状态：`COMPLETE`。审计9个非状态信息族在trend/range两档下的18条路径；{len(validated)}条路径、{family_count}个信息族通过预注册条件。未生成策略收益，未创建候选或修改SM/PTE。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        f"# S006 EX16 结论\n\n裁决：`{evidence['decision']}`。通过路径：{', '.join(evidence['validated_path_ids']) if evidence['validated_path_ids'] else '无'}。本轮只验证条件式架构的金融前提，不代表策略有效。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": evidence["decision"],
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
