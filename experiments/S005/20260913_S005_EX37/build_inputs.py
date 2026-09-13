from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from czsc_trader.experiment_archive import validate_experiment_archive
from dataflows.tushare_common import get_tushare_pro


EXPERIMENT_ID = "20260913_S005_EX37"


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = json.loads((artifacts / "protocol.json").read_text(encoding="utf-8"))
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_event_prices"):
        raise ValueError("EX37 may not read post-event prices")

    ex18 = repo / "experiments/S005/20260913_S005_EX18"
    validate_experiment_archive(ex18)
    panel = pd.read_csv(ex18 / "artifacts/constituent_panel.csv.gz")
    weights = (
        panel.groupby("con_code", as_index=False)
        .agg(max_weight=("weight", "max"), observed_sessions=("dt", "nunique"))
    )
    minimum = float(protocol["important_component_max_weight_min"])
    weights = weights.loc[weights["max_weight"].ge(minimum)].copy()

    pro = get_tushare_pro(repo / ".env")
    basic = pro.stock_basic(
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name,fullname,industry,market",
    )
    basic = pd.DataFrame(basic)
    required = {"ts_code", "symbol", "name", "fullname", "industry", "market"}
    if not required.issubset(basic.columns):
        raise ValueError(f"stock_basic response missing fields: {sorted(required.difference(basic.columns))}")
    entities = weights.merge(basic, left_on="con_code", right_on="ts_code", how="left", validate="one_to_one")
    if entities[["name", "fullname"]].isna().any(axis=None):
        missing = entities.loc[entities["name"].isna(), "con_code"].tolist()
        raise ValueError(f"stock_basic misses historical important components: {missing}")
    entities = entities[
        ["con_code", "symbol", "name", "fullname", "industry", "market", "max_weight", "observed_sessions"]
    ].sort_values("con_code")
    entities.to_csv(artifacts / "important_component_entities.csv", index=False, lineterminator="\n")
    print(entities.to_json(orient="records", force_ascii=True))


if __name__ == "__main__":
    main()

