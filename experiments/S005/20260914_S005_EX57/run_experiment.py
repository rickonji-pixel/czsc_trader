from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260914_S005_EX57"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profit_factor(values: pd.Series) -> float:
    gains = float(values.loc[values > 0].sum())
    losses = float(-values.loc[values < 0].sum())
    return gains / losses if losses else (float("inf") if gains else 0.0)


def _win_loss_ratio(values: pd.Series) -> float | None:
    wins = values.loc[values > 0]
    losses = values.loc[values < 0]
    if wins.empty or losses.empty:
        return None
    return float(wins.mean() / abs(losses.mean()))


def _feature_matrix(primary: pd.DataFrame, ledger: pd.DataFrame) -> tuple[np.ndarray, list[dict[str, object]]]:
    columns: list[np.ndarray] = []
    metadata: list[dict[str, object]] = []
    for row in ledger.itertuples(index=False):
        state = primary[row.signal_id].eq(row.state_primary).fillna(False).to_numpy(dtype=np.uint8)
        columns.append(state)
        metadata.append({
            "state_id": row.state_id,
            "signal_id": row.signal_id,
            "information_family": row.information_family,
            "frequency": row.frequency,
            "independent_transitions": int(row.independent_transitions),
            "maximum_year_share": float(row.maximum_year_share),
        })
    return np.column_stack(columns), metadata


def _deduplicate_block(values: np.ndarray, candidate_indexes: list[int], metadata: list[dict[str, object]], threshold: float) -> tuple[list[int], dict[int, int]]:
    order = sorted(candidate_indexes, key=lambda index: (-int(metadata[index]["independent_transitions"]), float(metadata[index]["maximum_year_share"]), str(metadata[index]["state_id"])))
    kept: list[int] = []
    redundant_to: dict[int, int] = {}
    for index in order:
        x = values[:, index].astype(float)
        duplicate = None
        for prior in kept:
            y = values[:, prior].astype(float)
            denominator = float(np.sqrt(np.sum((x - x.mean()) ** 2) * np.sum((y - y.mean()) ** 2)))
            phi = float(np.dot(x - x.mean(), y - y.mean()) / denominator) if denominator else 0.0
            if phi >= threshold:
                duplicate = prior
                break
        if duplicate is None:
            kept.append(index)
        else:
            redundant_to[index] = duplicate
    return kept, redundant_to


def _fit_score(
    features: np.ndarray,
    returns: np.ndarray,
    train_mask: np.ndarray,
    apply_mask: np.ndarray,
    metadata: list[dict[str, object]],
    protocol: dict[str, object],
) -> tuple[np.ndarray, float, list[dict[str, object]], dict[str, object]]:
    model = protocol["model"]
    minimum = int(model["minimum_state_occurrences"])
    prior = float(model["shrinkage_prior_count"])
    dedup_threshold = float(model["within_block_positive_phi_dedup_threshold"])
    train_features = features[train_mask]
    train_returns = returns[train_mask]
    baseline = float(np.mean(train_returns))
    counts = train_features.sum(axis=0).astype(int)
    candidate_indexes = [index for index, count in enumerate(counts) if count >= minimum and count < len(train_returns)]
    blocks: dict[tuple[str, str], list[int]] = {}
    for index in candidate_indexes:
        key = (str(metadata[index]["information_family"]), str(metadata[index]["frequency"]))
        blocks.setdefault(key, []).append(index)

    kept: list[int] = []
    redundant_to: dict[int, int] = {}
    for indexes in blocks.values():
        block_kept, block_redundant = _deduplicate_block(train_features, indexes, metadata, dedup_threshold)
        kept.extend(block_kept)
        redundant_to.update(block_redundant)

    edges = np.full(features.shape[1], np.nan)
    edge_rows: list[dict[str, object]] = []
    for index in kept:
        active = train_features[:, index].astype(bool)
        raw_edge = float(np.mean(train_returns[active]) - baseline)
        shrinkage = float(counts[index] / (counts[index] + prior))
        edges[index] = raw_edge * shrinkage
        edge_rows.append({
            **metadata[index],
            "training_occurrences": int(counts[index]),
            "training_baseline_return": baseline,
            "raw_edge": raw_edge,
            "shrinkage": shrinkage,
            "shrunk_edge": float(edges[index]),
        })

    def score(mask: np.ndarray) -> np.ndarray:
        matrix = features[mask]
        block_scores: list[np.ndarray] = []
        for key in sorted(blocks):
            indexes = [index for index in kept if (metadata[index]["information_family"], metadata[index]["frequency"]) == key]
            if not indexes:
                continue
            active = matrix[:, indexes]
            denominator = active.sum(axis=1).astype(float)
            numerator = active @ edges[indexes]
            block_scores.append(np.divide(numerator, denominator, out=np.full(len(matrix), np.nan), where=denominator > 0))
        if not block_scores:
            return np.full(mask.sum(), np.nan)
        block_matrix = np.column_stack(block_scores)
        valid = np.sum(~np.isnan(block_matrix), axis=1)
        return np.divide(np.nansum(block_matrix, axis=1), valid, out=np.full(len(block_matrix), np.nan), where=valid > 0)

    train_scores = score(train_mask)
    valid_train_scores = train_scores[~np.isnan(train_scores)]
    if not len(valid_train_scores):
        raise ValueError("no valid training scores after technical filtering")
    threshold = float(np.quantile(valid_train_scores, float(model["entry_score_quantile"])))
    applied = score(apply_mask)
    diagnostics = {
        "training_sessions": int(train_mask.sum()),
        "candidate_states": len(candidate_indexes),
        "kept_states": len(kept),
        "deduplicated_states": len(redundant_to),
        "score_blocks": len({(metadata[index]["information_family"], metadata[index]["frequency"]) for index in kept}),
        "training_baseline_return": baseline,
        "score_threshold": threshold,
    }
    for index, prior_index in redundant_to.items():
        edge_rows.append({
            **metadata[index],
            "training_occurrences": int(counts[index]),
            "deduplicated_to": metadata[prior_index]["state_id"],
        })
    return applied, threshold, edge_rows, diagnostics


def _make_trades(signal_scores: pd.DataFrame, calendar: pd.DatetimeIndex, opens: pd.Series, cost: float, hold: int) -> pd.DataFrame:
    calendar_position = {session: index for index, session in enumerate(calendar)}
    rows: list[dict[str, object]] = []
    last_exit_index = -1
    for row in signal_scores.sort_values("signal_date").itertuples(index=False):
        signal_index = calendar_position[pd.Timestamp(row.signal_date)]
        entry_index = signal_index + 1
        exit_index = entry_index + hold
        if signal_index < last_exit_index or exit_index >= len(calendar):
            continue
        entry_date = calendar[entry_index]
        exit_date = calendar[exit_index]
        entry_price = float(opens.loc[entry_date])
        exit_price = float(opens.loc[exit_date])
        rows.append({
            "signal_date": row.signal_date,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "score": float(row.score),
            "threshold": float(row.threshold),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_return": exit_price / entry_price - 1.0,
            "stress_return": exit_price * (1.0 - cost) / (entry_price * (1.0 + cost)) - 1.0,
        })
        last_exit_index = exit_index
    return pd.DataFrame(rows)


def _account(calendar: pd.DatetimeIndex, opens: pd.Series, trades: pd.DataFrame, cost: float) -> pd.DataFrame:
    position = pd.Series(0.0, index=calendar)
    for row in trades.itertuples(index=False):
        position.loc[(position.index >= row.entry_date) & (position.index < row.exit_date)] = 1.0
    open_return = opens.pct_change().fillna(0.0)
    held_return = position.shift(1, fill_value=0.0) * open_return
    delta = position.diff().fillna(position.iloc[0])
    transaction_factor = pd.Series(1.0, index=calendar)
    transaction_factor.loc[delta > 0] = 1.0 / (1.0 + cost)
    transaction_factor.loc[delta < 0] = 1.0 - cost
    daily_factor = (1.0 + held_return) * transaction_factor
    return pd.DataFrame({
        "date": calendar,
        "position": position.to_numpy(),
        "open": opens.to_numpy(),
        "daily_return": daily_factor.to_numpy() - 1.0,
        "equity": daily_factor.cumprod().to_numpy(),
    })


def _metrics(trades: pd.DataFrame, account: pd.DataFrame, calendar: pd.DatetimeIndex, recent_sessions: int) -> dict[str, object]:
    values = trades["stress_return"].astype(float)
    equity = account["equity"].astype(float)
    drawdown = equity.div(equity.cummax()).sub(1.0)
    cagr = float(equity.iloc[-1] ** (252.0 / len(equity)) - 1.0)
    annual_factor = account.assign(year=pd.to_datetime(account["date"]).dt.year).groupby("year")["daily_return"].apply(lambda values: float((1.0 + values).prod() - 1.0))
    recent_start = calendar[-recent_sessions]
    recent = trades.loc[pd.to_datetime(trades["entry_date"]) >= recent_start, "stress_return"]
    signal_series = pd.Series(0, index=calendar, dtype=int)
    signal_series.loc[pd.to_datetime(trades["signal_date"])] = 1
    rolling = signal_series.rolling(60, min_periods=60).sum().dropna()
    maximum_drawdown = float(drawdown.min())
    return {
        "closed_trades": int(len(trades)),
        "stress_mean": float(values.mean()),
        "stress_median": float(values.median()),
        "win_rate": float(values.gt(0).mean()),
        "profit_factor": _profit_factor(values),
        "win_loss_ratio": _win_loss_ratio(values),
        "positive_years": int(annual_factor.gt(0).sum()),
        "observed_years": int(len(annual_factor)),
        "recent_trades": int(len(recent)),
        "recent_mean": float(recent.mean()) if len(recent) else None,
        "rolling_60_cycle_median": float(rolling.median()),
        "rolling_60_cycle_p10": float(rolling.quantile(0.10)),
        "total_return": float(equity.iloc[-1] - 1.0),
        "cagr": cagr,
        "maximum_drawdown": maximum_drawdown,
        "calmar": cagr / abs(maximum_drawdown) if maximum_drawdown < 0 else None,
        "exposure": float(account["position"].mean()),
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if any(protocol.get(key) for key in ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")):
        raise ValueError("EX57 cannot create, promote, or deploy a candidate")

    ex49 = repo / "experiments/S005" / str(protocol["sources"]["state_matrix_experiment_id"])
    ex54 = repo / "experiments/S005" / str(protocol["sources"]["semantic_experiment_id"])
    validate_experiment_archive(ex49)
    validate_experiment_archive(ex54)
    frozen_files = (
        (ex49 / "experiment_manifest.json", protocol["sources"]["state_matrix_manifest_sha256"]),
        (ex49 / "artifacts/primary_states.csv.gz", protocol["sources"]["primary_states_sha256"]),
        (ex54 / "experiment_manifest.json", protocol["sources"]["semantic_manifest_sha256"]),
        (ex54 / "artifacts/semantic_state_ledger.csv.gz", protocol["sources"]["semantic_ledger_sha256"]),
    )
    for path, expected in frozen_files:
        if _sha256(path) != expected:
            raise ValueError(f"frozen source differs: {path}")

    context = RepositoryContext.discover(repo, explicit_root=repo)
    replay = load_replay_data(context, str(protocol["dataset"]["name"]), str(protocol["symbol"]), str(protocol["asset_type"]), date.fromisoformat(str(protocol["development_cutoff"])))
    if replay.fingerprint != protocol["dataset"]["fingerprint"]:
        raise ValueError("research dataset differs from the frozen EX57 protocol")
    prices = replay.adjusted.daily.copy()
    prices["dt"] = pd.to_datetime(prices["dt"]).dt.normalize()
    prices = prices.set_index("dt").sort_index()
    cutoff = pd.Timestamp(protocol["development_cutoff"])
    calendar = prices.index[prices.index <= cutoff]
    opens = prices["open"].astype(float).reindex(calendar)

    primary = pd.read_csv(ex49 / "artifacts/primary_states.csv.gz")
    primary["dt"] = pd.to_datetime(primary["dt"]).dt.normalize()
    primary = primary.set_index("dt").reindex(calendar)
    ledger_all = pd.read_csv(ex54 / "artifacts/semantic_state_ledger.csv.gz")
    included_families = set(protocol["included_information_families"])
    ledger_all = ledger_all.loc[ledger_all["information_family"].isin(included_families)].copy()

    hold = int(protocol["model"]["forward_open_intervals"])
    forward_return = opens.shift(-(hold + 1)).div(opens.shift(-1)).sub(1.0)
    outcome_exit_date = pd.Series(calendar, index=calendar).shift(-(hold + 1))
    evaluation_start = pd.Timestamp(protocol["evaluation_start"])
    evaluation_mask = calendar >= evaluation_start
    result_rows: list[dict[str, object]] = []
    all_trades: list[pd.DataFrame] = []
    all_accounts: list[pd.DataFrame] = []
    all_scores: list[pd.DataFrame] = []
    all_edges: list[pd.DataFrame] = []
    all_folds: list[dict[str, object]] = []

    for architecture, architecture_rules in protocol["architectures"].items():
        ledger = ledger_all.loc[
            ledger_all["research_role"].isin(architecture_rules["research_roles"])
            & ledger_all["frequency"].isin(architecture_rules["frequencies"])
        ].sort_values("state_id").reset_index(drop=True)
        features, metadata = _feature_matrix(primary, ledger)
        scores = pd.DataFrame(index=calendar, columns=["score", "threshold", "quarter"], dtype=object)
        quarters = calendar[evaluation_mask].to_period("Q").unique()
        for quarter in quarters:
            quarter_start = quarter.start_time.normalize()
            apply_mask = np.asarray((calendar.to_period("Q") == quarter) & evaluation_mask)
            train_mask = np.asarray(forward_return.notna() & outcome_exit_date.lt(quarter_start))
            if int(train_mask.sum()) < int(protocol["model"]["minimum_training_sessions"]):
                raise ValueError(f"{architecture} {quarter}: insufficient training sessions")
            applied, threshold, edge_rows, diagnostics = _fit_score(
                features,
                forward_return.to_numpy(dtype=float),
                train_mask,
                apply_mask,
                metadata,
                protocol,
            )
            scores.loc[apply_mask, "score"] = applied
            scores.loc[apply_mask, "threshold"] = threshold
            scores.loc[apply_mask, "quarter"] = str(quarter)
            for row in edge_rows:
                row.update({"architecture": architecture, "quarter": str(quarter)})
            all_edges.append(pd.DataFrame(edge_rows))
            all_folds.append({"architecture": architecture, "quarter": str(quarter), **diagnostics})

        scores["score"] = pd.to_numeric(scores["score"], errors="coerce")
        scores["threshold"] = pd.to_numeric(scores["threshold"], errors="coerce")
        eligible_scores = scores.loc[evaluation_mask & scores["score"].ge(scores["threshold"])].reset_index(names="signal_date")
        trades = _make_trades(eligible_scores, calendar, opens, float(protocol["execution"]["stress_one_way_cost"]), hold)
        if trades.empty:
            raise ValueError(f"{architecture}: no executable closed trades")
        trades.insert(0, "architecture", architecture)
        evaluation_calendar = calendar[evaluation_mask]
        account = _account(evaluation_calendar, opens.reindex(evaluation_calendar), trades, float(protocol["execution"]["stress_one_way_cost"]))
        metrics = _metrics(trades, account, evaluation_calendar, int(protocol["acceptance"]["recent_sessions"]))
        acceptance = protocol["acceptance"]
        checks = {
            "mean_trade": metrics["stress_mean"] >= float(acceptance["minimum_mean_trade"]),
            "cagr": metrics["cagr"] >= float(acceptance["minimum_cagr"]),
            "maximum_drawdown": metrics["maximum_drawdown"] >= float(acceptance["maximum_drawdown_floor"]),
            "profit_factor": metrics["profit_factor"] > float(acceptance["profit_factor_min_exclusive"]),
            "positive_years": metrics["positive_years"] >= int(acceptance["positive_years_minimum"]),
            "recent": metrics["recent_mean"] is not None and metrics["recent_mean"] > float(acceptance["recent_mean_min_exclusive"]),
            "density_median": metrics["rolling_60_cycle_median"] >= float(acceptance["rolling_60_cycle_median_minimum"]),
            "density_p10": metrics["rolling_60_cycle_p10"] >= float(acceptance["rolling_60_cycle_p10_minimum"]),
        }
        result_rows.append({
            "architecture": architecture,
            "input_states": int(len(ledger)),
            "input_blocks": int(ledger[["information_family", "frequency"]].drop_duplicates().shape[0]),
            **metrics,
            "passed": bool(all(checks.values())),
            "checks": checks,
        })
        all_trades.append(trades)
        account.insert(0, "architecture", architecture)
        all_accounts.append(account)
        score_output = scores.loc[evaluation_mask].reset_index(names="date")
        score_output.insert(0, "architecture", architecture)
        all_scores.append(score_output)

    evaluation_calendar = calendar[evaluation_mask]
    buyhold_trades = pd.DataFrame([{"entry_date": evaluation_calendar[0], "exit_date": evaluation_calendar[-1]}])
    buyhold_account = _account(evaluation_calendar, opens.reindex(evaluation_calendar), buyhold_trades, float(protocol["execution"]["stress_one_way_cost"]))
    buyhold_drawdown = buyhold_account["equity"].div(buyhold_account["equity"].cummax()).sub(1.0)
    buyhold = {
        "total_return": float(buyhold_account["equity"].iloc[-1] - 1.0),
        "cagr": float(buyhold_account["equity"].iloc[-1] ** (252.0 / len(buyhold_account)) - 1.0),
        "maximum_drawdown": float(buyhold_drawdown.min()),
    }
    buyhold["calmar"] = buyhold["cagr"] / abs(buyhold["maximum_drawdown"])
    passing = [str(row["architecture"]) for row in result_rows if row["passed"]]
    decision = "PROCEED_TO_SYSTEMATIC_MECHANISM_ATTRIBUTION" if passing else "STOP_SYSTEMATIC_FSC_SCORE_ON_RETURN"
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "evaluation_start": evaluation_calendar[0].strftime("%Y-%m-%d"),
        "evaluation_end": evaluation_calendar[-1].strftime("%Y-%m-%d"),
        "dataset_fingerprint": replay.fingerprint,
        "architectures": result_rows,
        "passing_architectures": passing,
        "buyhold": buyhold,
        "fold_count": len(all_folds),
        "learned_state_estimate_count": int(sum(row["kept_states"] for row in all_folds)),
        "return_path_count_this_experiment": len(protocol["architectures"]),
        "prior_cumulative_return_path_count": protocol["prior_cumulative_return_path_count"],
        "cumulative_return_path_count": protocol["cumulative_return_path_count"],
        "statistical_penalty_note": "two return paths plus every quarterly state-edge estimate must remain visible to later SE audit",
        "walk_forward_limit": "outcomes are causal by fold, but architectures and hyperparameters were proposed inside the development pool",
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
        "protocol_sha256": _sha256(protocol_path),
    }
    _write(artifacts / "walk_forward_evidence.json", evidence)
    _write(artifacts / "fold_diagnostics.json", {"folds": all_folds})
    compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    pd.concat(all_trades, ignore_index=True).to_csv(artifacts / "trades.csv.gz", index=False, compression=compression, lineterminator="\n")
    pd.concat(all_accounts, ignore_index=True).to_csv(artifacts / "account_daily.csv.gz", index=False, compression=compression, lineterminator="\n")
    pd.concat(all_scores, ignore_index=True).to_csv(artifacts / "daily_scores.csv.gz", index=False, compression=compression, lineterminator="\n")
    pd.concat(all_edges, ignore_index=True).to_csv(artifacts / "state_edge_ledger.csv.gz", index=False, compression=compression, lineterminator="\n")

    table = [
        "|路径|输入状态|交易|压力均值|盈亏因子|年化|最大回撤|卡玛|60日中位/P10|硬门|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in result_rows:
        table.append(
            f"|{row['architecture']}|{row['input_states']}|{row['closed_trades']}|{row['stress_mean']:.3%}|"
            f"{row['profit_factor']:.2f}|{row['cagr']:.2%}|{row['maximum_drawdown']:.2%}|{row['calmar']:.2f}|"
            f"{row['rolling_60_cycle_median']:.0f}/{row['rolling_60_cycle_p10']:.0f}|{'PASS' if row['passed'] else 'FAIL'}|"
        )
    (experiment / "03_execution.md").write_text(
        "# S005 EX57 执行\n\n状态：`COMPLETE`。两条预注册路径完成季度滚动学习与向前执行。\n\n" + "\n".join(table) + "\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX57 结论\n\n" + "\n".join(table) + "\n\n"
        f"同窗口 BuyHold 年化 {buyhold['cagr']:.2%}、最大回撤 {buyhold['maximum_drawdown']:.2%}、卡玛 {buyhold['calmar']:.2f}。"
        f"裁决：`{decision}`；通过路径：{', '.join(passing) if passing else 'NONE'}。"
        "全部季度状态收益估计和两条收益路径均已留痕，后续 SE 统计惩罚不得只按最终两条路径计数。"
        "本轮不生成候选、不修改 SM 或 PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": "S005",
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": decision,
        "candidate_created": False,
        "strategy_frozen": False,
        "pte_mutated": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
