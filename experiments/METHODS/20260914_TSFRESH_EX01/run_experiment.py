from __future__ import annotations

import json
from pathlib import Path
import time

import numpy as np
import pandas as pd
from scipy.stats import kendalltau

from czsc_trader.experiment_archive import build_experiment_manifest
from czsc_trader.feature_mining import (
    TsfreshFeatureSpec,
    extract_causal_rolling_features,
    screen_relevant_features,
)
from czsc_trader.identity import raw_file_sha256


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_protocol() -> dict[str, object]:
    return json.loads((HERE / "artifacts" / "protocol.json").read_text(encoding="utf-8"))


def _load_market(protocol: dict[str, object]) -> pd.DataFrame:
    frames = []
    expected = dict(protocol["input_sha256"])
    for name, digest in expected.items():
        path = ROOT / "data" / "raw" / name
        actual = raw_file_sha256(path)
        if actual != digest:
            raise ValueError(f"input hash differs: {name}")
        frames.append(pd.read_csv(path, parse_dates=["date"]))
    market = pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)
    if market["date"].duplicated().any() or not market["date"].is_monotonic_increasing:
        raise ValueError("daily market dates must be unique and increasing")
    start = pd.Timestamp(str(protocol["dataset_start"]))
    cutoff = pd.Timestamp(str(protocol["dataset_cutoff"]))
    market = market.loc[market["date"].between(start, cutoff)].reset_index(drop=True)
    if market["date"].iloc[0] != start or market["date"].iloc[-1] != cutoff:
        raise ValueError("market boundaries differ from protocol")
    return market


def _build_inputs(market: pd.DataFrame) -> pd.DataFrame:
    values = pd.DataFrame({"date": market["date"]})
    values["return_1d"] = market["close"].pct_change()
    values["intraday_return"] = market["close"].div(market["open"]).sub(1)
    values["range_pct"] = market["high"].sub(market["low"]).div(market["open"])
    values["log_volume_change"] = np.log(market["volume"]).diff()
    values["log_amount_change"] = np.log(market["amount"]).diff()
    return values.dropna().reset_index(drop=True)


def _target(market: pd.DataFrame) -> pd.Series:
    target = market["open"].shift(-6).div(market["open"].shift(-1)).sub(1)
    return pd.Series(target.to_numpy(), index=pd.DatetimeIndex(market["date"], name="date"), name="forward_open_5d")


def _deduplicate(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    keep: list[str] = []
    removed: list[dict[str, str]] = []
    fingerprints: dict[bytes, str] = {}
    for column in frame.columns:
        raw = np.ascontiguousarray(frame[column].to_numpy(dtype=float)).tobytes()
        previous = fingerprints.get(raw)
        if previous is None:
            fingerprints[raw] = column
            keep.append(column)
        else:
            removed.append({"feature": column, "duplicate_of": previous})
    return frame.loc[:, keep], removed


def _tau(feature: pd.Series, target: pd.Series) -> float:
    joined = pd.concat([feature, target], axis=1).dropna()
    if len(joined) < 10 or joined.iloc[:, 0].nunique() < 2:
        return float("nan")
    value = kendalltau(joined.iloc[:, 0], joined.iloc[:, 1], nan_policy="omit").statistic
    return float(value)


def main() -> None:
    protocol = _load_protocol()
    artifacts = HERE / "artifacts"
    market = _load_market(protocol)
    inputs = _build_inputs(market)
    value_columns = tuple(str(value) for value in protocol["value_columns"])

    started = time.perf_counter()
    matrices: list[pd.DataFrame] = []
    extraction_runs: list[dict[str, object]] = []
    for lookback in protocol["lookbacks"]:
        result = extract_causal_rolling_features(
            inputs,
            TsfreshFeatureSpec("date", value_columns, lookback=int(lookback)),
        )
        matrices.append(result.features.add_prefix(f"w{lookback}__"))
        extraction_runs.append(result.evidence)
    extraction_seconds = time.perf_counter() - started
    raw_features = pd.concat(matrices, axis=1, join="inner").sort_index()

    replay = []
    for lookback in protocol["lookbacks"]:
        result = extract_causal_rolling_features(
            inputs,
            TsfreshFeatureSpec("date", value_columns, lookback=int(lookback)),
        )
        replay.append(result.features.add_prefix(f"w{lookback}__"))
    replay_features = pd.concat(replay, axis=1, join="inner").sort_index()
    deterministic = raw_features.equals(replay_features)

    target = _target(market).reindex(raw_features.index)
    usable = target.notna()
    raw_features = raw_features.loc[usable]
    target = target.loc[usable]
    discovery = raw_features.index.to_series().between(
        pd.Timestamp(str(protocol["discovery_start"])),
        pd.Timestamp(str(protocol["discovery_end"])),
    ).to_numpy()
    confirmation = raw_features.index.to_series().between(
        pd.Timestamp(str(protocol["confirmation_start"])),
        pd.Timestamp(str(protocol["confirmation_end"])),
    ).to_numpy()
    discovery_features = raw_features.loc[discovery]
    discovery_target = target.loc[discovery]

    nonconstant = discovery_features.nunique(dropna=False).gt(1)
    technical = raw_features.loc[:, nonconstant]
    discovery_technical = technical.loc[discovery]
    discovery_technical, duplicates = _deduplicate(discovery_technical)
    technical = technical.loc[:, discovery_technical.columns]

    screen_spec = dict(protocol["screen"])
    screen = screen_relevant_features(
        discovery_technical,
        discovery_target,
        fdr_level=float(screen_spec["fdr_level"]),
        ml_task=str(screen_spec["ml_task"]),
        n_jobs=0,
    )
    relevance = screen.relevance.copy()
    relevance.to_csv(artifacts / "relevance_ledger.csv", index=False)

    reviews = []
    for feature in screen.selected_features.columns:
        discovery_tau = _tau(technical.loc[discovery, feature], target.loc[discovery])
        confirmation_tau = _tau(technical.loc[confirmation, feature], target.loc[confirmation])
        yearly = {
            year: _tau(
                technical.loc[technical.index.year == year, feature],
                target.loc[target.index.year == year],
            )
            for year in (2024, 2025, 2026)
        }
        sign = np.sign(discovery_tau)
        same_sign_years = sum(np.isfinite(value) and np.sign(value) == sign for value in yearly.values())
        proposal = bool(
            sign != 0
            and np.isfinite(confirmation_tau)
            and np.sign(confirmation_tau) == sign
            and same_sign_years >= int(screen_spec["confirmation_minimum_same_sign_years"])
        )
        reviews.append(
            {
                "feature": feature,
                "discovery_tau": discovery_tau,
                "confirmation_tau": confirmation_tau,
                "tau_2024": yearly[2024],
                "tau_2025": yearly[2025],
                "tau_2026": yearly[2026],
                "same_sign_confirmation_years": same_sign_years,
                "fsc_review_proposal": proposal,
            }
        )
    direction_review = pd.DataFrame(reviews)
    if direction_review.empty:
        direction_review = pd.DataFrame(
            columns=[
                "feature", "discovery_tau", "confirmation_tau", "tau_2024",
                "tau_2025", "tau_2026", "same_sign_confirmation_years",
                "fsc_review_proposal",
            ]
        )
    direction_review.to_csv(artifacts / "direction_review.csv", index=False)
    technical.to_csv(artifacts / "candidate_feature_matrix.csv.gz", compression="gzip")
    pd.DataFrame(duplicates).to_csv(artifacts / "exact_duplicates.csv", index=False)

    acceptance = dict(protocol["tool_acceptance"])
    tool_pass = bool(
        deterministic
        and np.isfinite(raw_features.to_numpy(dtype=float)).all()
        and raw_features.shape[1] <= int(acceptance["maximum_raw_features"])
        and extraction_seconds <= float(acceptance["maximum_extraction_seconds"])
    )
    proposal_count = int(direction_review["fsc_review_proposal"].sum())
    manual_review_path = artifacts / "manual_catalog_review.json"
    manual_review = (
        json.loads(manual_review_path.read_text(encoding="utf-8"))
        if manual_review_path.is_file()
        else None
    )
    evidence = {
        "experiment_id": protocol["experiment_id"],
        "evidence_scope": protocol["evidence_scope"],
        "input_rows": len(market),
        "feature_input_rows": len(inputs),
        "raw_feature_rows": len(raw_features),
        "raw_feature_count": raw_features.shape[1],
        "technical_feature_count": technical.shape[1],
        "constant_removed_count": int((~nonconstant).sum()),
        "exact_duplicate_removed_count": len(duplicates),
        "discovery_samples": int(discovery.sum()),
        "confirmation_samples": int(confirmation.sum()),
        "fdr_selected_count": screen.selected_features.shape[1],
        "fsc_review_proposal_count": proposal_count,
        "extraction_seconds": round(extraction_seconds, 6),
        "deterministic_replay": deterministic,
        "tool_acceptance": "PASS" if tool_pass else "FAIL",
        "catalog_action": "MANUAL_REVIEW_PROPOSALS" if proposal_count else "NO_PROPOSAL",
        "manual_catalog_review": manual_review,
        "extraction_runs": extraction_runs,
        "screen_evidence": screen.evidence,
    }
    _write_json(artifacts / "feasibility_evidence.json", evidence)

    (HERE / "03_execution.md").write_text(
        "# TSFRESH EX01 执行\n\n"
        f"加载{len(market)}个交易日，三个窗口生成{raw_features.shape[1]}个原始特征；"
        f"删除{int((~nonconstant).sum())}个常量和{len(duplicates)}个完全重复特征后剩"
        f"{technical.shape[1]}个。发现期样本{int(discovery.sum())}个，复核期样本"
        f"{int(confirmation.sum())}个。\n\n"
        f"首次提取耗时{extraction_seconds:.3f}秒，重复提取哈希一致性："
        f"{'通过' if deterministic else '失败'}。FDR初筛得到{screen.selected_features.shape[1]}项，"
        f"其中{proposal_count}项达到FSC人工审查提案条件。\n",
        encoding="utf-8",
    )
    conclusion = "PASS" if tool_pass else "FAIL"
    catalog = "进入人工审查" if proposal_count else "本轮不产生FSC提案"
    manual_summary = ""
    if manual_review is not None:
        manual_summary = (
            f"人工审查最终以`{manual_review['catalog_status']}`状态登记"
            f"{manual_review['registered_count']}项定义，详见`05_manual_review.md`。\n"
        )
    (HERE / "04_conclusion.md").write_text(
        "# TSFRESH EX01 结论\n\n"
        f"工具接入裁决：`{conclusion}`。tsfresh在当前环境中的固定因果窗口提取"
        f"{'可稳定复跑' if tool_pass else '未达到预注册可用条件'}。\n\n"
        f"目录动作：`{evidence['catalog_action']}`。共有{proposal_count}项候选{catalog}；"
        "机器门本身不代表存在Alpha，也不自动写入FSC。"
        f"{manual_summary}全部统计结果属于开发证据。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        HERE,
        {
            "experiment_id": protocol["experiment_id"],
            "method": "TSFRESH_CAUSAL_ROLLING_FEATURE_MINING",
            "symbol": protocol["symbol"],
            "dataset_cutoff": protocol["dataset_cutoff"],
            "evidence_scope": protocol["evidence_scope"],
            "tool_acceptance": evidence["tool_acceptance"],
            "catalog_action": evidence["catalog_action"],
        },
    )


if __name__ == "__main__":
    main()
