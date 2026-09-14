from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

import pandas as pd

from factor_signal_catalog import CatalogRegistry

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S006_EX03"
INDUSTRY_FACTOR = "F-PROJECT-EXTERNAL-INDUSTRY-MONEYFLOW"
ANALYST_FACTOR = "F-PROJECT-SELL-SIDE-REVISION-BREADTH"


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


def _detail_map(registry: CatalogRegistry) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for item in registry.factors:
        rows[item.factor_id] = item.to_dict()
    for item in registry.signals:
        rows[item.signal_id] = item.to_dict()
    return rows


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_new_returns"):
        raise ValueError("applicability audit must not read new returns")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("applicability audit cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    source_paths = {
        "EX02": repo / "experiments/S006" / str(sources["current_contract_experiment_id"]),
        "EX49": repo / "experiments/S005" / str(sources["czsc_materialization_experiment_id"]),
        "EX58": repo / "experiments/S005" / str(sources["project_factor_experiment_id"]),
        "EX64": repo / "experiments/S005" / str(sources["industry_factor_experiment_id"]),
        "EX67": repo / "experiments/S005" / str(sources["analyst_factor_experiment_id"]),
    }
    hash_keys = {
        "EX02": "current_contract_manifest_sha256",
        "EX49": "czsc_materialization_manifest_sha256",
        "EX58": "project_factor_manifest_sha256",
        "EX64": "industry_factor_manifest_sha256",
        "EX67": "analyst_factor_manifest_sha256",
    }
    for key, path in source_paths.items():
        validate_experiment_archive(path)
        if _sha256(path / "experiment_manifest.json") != str(sources[hash_keys[key]]):
            raise ValueError(f"frozen source differs: {path}")

    registry = CatalogRegistry(repo / "catalog")
    if registry.digest != protocol["catalog_digest"]:
        raise ValueError("FSC differs from frozen S006 catalog digest")
    definitions = list(registry.list_definitions())
    details = _detail_map(registry)

    czsc_catalog = pd.read_csv(source_paths["EX49"] / "artifacts/signal_catalog.csv")
    project_inventory = pd.read_csv(source_paths["EX58"] / "artifacts/factor_inventory.csv")
    factor_inventory = {
        str(row.factor_id): {
            "source_experiment": "20260914_S005_EX58",
            "observation_count": int(row.observations),
        }
        for row in project_inventory.itertuples(index=False)
    }
    industry = pd.read_csv(source_paths["EX64"] / "artifacts/industry_daily_factors.csv.gz")
    analyst = pd.read_csv(source_paths["EX67"] / "artifacts/expectation_revision_ledger.csv.gz")
    factor_inventory[INDUSTRY_FACTOR] = {
        "source_experiment": "20260914_S005_EX64",
        "observation_count": int(industry["trade_date"].nunique()),
    }
    factor_inventory[ANALYST_FACTOR] = {
        "source_experiment": "20260914_S005_EX67",
        "observation_count": int(analyst["trade_date"].nunique()),
    }

    rows: list[dict[str, object]] = []
    for definition in definitions:
        definition_id = str(definition["id"])
        detail = details[definition_id]
        base = {
            "definition_id": definition_id,
            "kind": definition["kind"],
            "name": definition["name"],
            "information_family": definition["information_family"],
            "provider": definition["provider"],
            "catalog_status": definition["status"],
            "availability": detail["availability"],
            "causality": detail["causality"],
            "applicability": "APPLICABLE",
            "materialization_status": "",
            "evaluation_route": "",
            "source_experiment": "",
            "observation_count": 0,
            "generated_configurations": 0,
            "failed_configurations": 0,
            "disposition_reason": "",
        }
        if definition["kind"] == "factor":
            material = factor_inventory.get(definition_id)
            if material is None:
                raise ValueError(f"project factor lacks materialization evidence: {definition_id}")
            base.update({
                "materialization_status": "READY_FOR_TYPE_SPECIFIC_AUDIT",
                "evaluation_route": "EVENT_STUDY" if definition["information_family"] == "EVENT_CATALYST" else "CONTINUOUS_FACTOR_AUDIT",
                "source_experiment": material["source_experiment"],
                "observation_count": material["observation_count"],
                "disposition_reason": "validated historical point-in-time material exists",
            })
        elif definition["provider"] == "czsc":
            signal_name = definition_id.removeprefix("SIG-CZSC-")
            configs = czsc_catalog.loc[czsc_catalog["name"].eq(signal_name)]
            if configs.empty:
                raise ValueError(f"CZSC definition absent from generation audit: {definition_id}")
            generated = int(configs["status"].eq("GENERATED").sum())
            failed = int(configs["status"].eq("FAILED").sum())
            excluded = int(configs["status"].eq("EXCLUDED_SCOPE").sum())
            if generated:
                base.update({
                    "materialization_status": "READY_FOR_TYPE_SPECIFIC_AUDIT",
                    "evaluation_route": "CATEGORICAL_SIGNAL_AUDIT",
                    "source_experiment": "20260914_S005_EX49",
                    "observation_count": int(configs.loc[configs["status"].eq("GENERATED"), "observed_days"].max()),
                    "generated_configurations": generated,
                    "failed_configurations": failed,
                    "disposition_reason": "at least one causal K-line configuration materialized",
                })
            elif excluded:
                base.update({
                    "materialization_status": "TRADER_CONTEXT_REQUIRED",
                    "evaluation_route": "CONTEXTUAL_SIGNAL_AUDIT",
                    "source_experiment": "20260914_S005_EX49",
                    "failed_configurations": failed,
                    "disposition_reason": "requires position, event, or multi-frequency trader context",
                })
            else:
                raise ValueError(f"CZSC definition has no explicit disposition: {definition_id}")
        else:
            dependencies = [str(value) for value in detail.get("factor_ids", [])]
            if not dependencies or any(value not in factor_inventory for value in dependencies):
                raise ValueError(f"project signal lacks materialized factor dependency: {definition_id}")
            base.update({
                "materialization_status": "SIGNAL_MATERIALIZATION_REQUIRED",
                "evaluation_route": "CATEGORICAL_SIGNAL_AUDIT",
                "source_experiment": ";".join(sorted({str(factor_inventory[value]["source_experiment"]) for value in dependencies})),
                "disposition_reason": "factor dependency exists; uniform S006 state series still required",
            })
        rows.append(base)

    expected_ids = {str(row["id"]) for row in definitions}
    actual_ids = {str(row["definition_id"]) for row in rows}
    if expected_ids != actual_ids or len(rows) != len(definitions):
        raise ValueError("applicability ledger does not cover the complete FSC snapshot")
    if any(row["applicability"] == "EXCLUDED_WITH_REASON" and not row["disposition_reason"] for row in rows):
        raise ValueError("catalog definition excluded without a reason")

    fields = list(rows[0])
    ledger_path = artifacts / "applicability_ledger.csv"
    with ledger_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    status_counts = Counter(str(row["materialization_status"]) for row in rows)
    route_counts = Counter(str(row["evaluation_route"]) for row in rows)
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "definition_count": len(rows),
        "materialization_status_counts": dict(sorted(status_counts.items())),
        "evaluation_route_counts": dict(sorted(route_counts.items())),
        "missing_definition_ids": sorted(expected_ids - actual_ids),
        "unexpected_definition_ids": sorted(actual_ids - expected_ids),
        "excluded_definition_count": sum(row["applicability"] == "EXCLUDED_WITH_REASON" for row in rows),
        "exclusion_without_reason_count": sum(row["applicability"] == "EXCLUDED_WITH_REASON" and not row["disposition_reason"] for row in rows),
        "configuration_failures_preserved": int(sum(int(row["failed_configurations"]) for row in rows)),
        "new_return_paths_read": 0,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "applicability_evidence.json", evidence)
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": "PROCEED_TO_PROJECT_SIGNAL_MATERIALIZATION_AND_EVALUATION_PROTOCOL",
            "promotion_allowed": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
