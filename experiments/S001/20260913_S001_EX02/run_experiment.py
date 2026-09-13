from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import (
    BacktestRequestV2,
    load_replay_data,
    resolve_registered_strategy,
    run_backtest_v2,
)
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260913_S001_EX02"


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
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compound(values: pd.Series) -> float:
    if values.empty:
        return 0.0
    return float((1.0 + values.astype(float)).prod() - 1.0)


def _trade_attribution(
    trades: pd.DataFrame,
    orders: pd.DataFrame,
    decisions: pd.DataFrame,
    episodes: pd.DataFrame,
) -> pd.DataFrame:
    buy_orders = orders.loc[orders["side"].eq("BUY")].copy()
    attempts = (
        buy_orders.groupby("cycle_id", as_index=False)
        .size()
        .rename(columns={"size": "entry_order_attempts"})
    )
    entries = buy_orders.loc[buy_orders["status"].eq("FILLED")].copy()
    if entries["cycle_id"].duplicated().any():
        raise AssertionError("a cycle has more than one BUY order")
    entries = entries.merge(
        decisions[["decision_id", "factor_score", "regime"]],
        on="decision_id",
        how="left",
        validate="many_to_one",
    )
    entries = entries.rename(
        columns={
            "signal_date": "entry_signal_date",
            "execution_date": "entry_execution_date",
            "factor_score": "entry_factor_score",
            "regime": "entry_regime",
        }
    )
    columns = [
        "cycle_id",
        "decision_id",
        "entry_signal_date",
        "entry_execution_date",
        "entry_factor_score",
        "entry_regime",
    ]
    result = trades.loc[trades["status"].eq("CLOSED")].merge(
        entries[columns], on="cycle_id", how="left", validate="one_to_one"
    )
    result = result.merge(attempts, on="cycle_id", how="left", validate="one_to_one")
    if result["entry_regime"].isna().any():
        raise AssertionError("closed trade is missing its entry regime")
    entry_dates = pd.to_datetime(result["entry_date"], errors="raise").dt.normalize()
    exit_dates = pd.to_datetime(result["exit_date"], errors="raise").dt.normalize()
    for episode_id, row in episodes.iterrows():
        start = pd.Timestamp(row["peak_date"])
        end = pd.Timestamp(row["trough_date"])
        result[f"overlaps_drawdown_{episode_id + 1}"] = (
            entry_dates.le(end) & exit_dates.ge(start)
        )
    return result


def _daily_attribution(account: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    result = account.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise").dt.normalize()
    result["signal_date"] = pd.to_datetime(result["signal_date"], errors="raise").dt.normalize()
    decisions = decisions.copy()
    decisions["signal_date"] = pd.to_datetime(
        decisions["signal_date"], errors="raise"
    ).dt.normalize()
    if decisions["signal_date"].duplicated().any():
        raise AssertionError("signal_date does not identify one decision")
    result = result.merge(
        decisions[["signal_date", "factor_score", "regime"]],
        on="signal_date",
        how="left",
        validate="many_to_one",
    )
    if result["regime"].isna().any():
        raise AssertionError("account day is missing its decision regime")
    result = result.sort_values("date").reset_index(drop=True)
    result["daily_return"] = result["equity"].astype(float).pct_change()
    result.loc[0, "daily_return"] = float(result.loc[0, "equity"]) / 100000.0 - 1.0
    result["peak_equity"] = result["equity"].astype(float).cummax()
    result["drawdown"] = result["equity"].astype(float) / result["peak_equity"] - 1.0
    result["in_position"] = result["quantity"].astype(float).gt(0)
    return result


def _regime_summary(daily: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for regime, frame in daily.groupby("regime", sort=True):
        positioned = frame.loc[frame["in_position"]]
        regime_trades = trades.loc[trades["entry_regime"].eq(regime)]
        rows.append(
            {
                "regime": regime,
                "sessions": len(frame),
                "positioned_sessions": len(positioned),
                "positioned_compound_return": _compound(positioned["daily_return"]),
                "positioned_loss_sessions": int(positioned["daily_return"].lt(0).sum()),
                "entry_trade_count": len(regime_trades),
                "entry_trade_loss_count": int(regime_trades["net_return"].lt(0).sum()),
                "entry_trade_compound_return": _compound(regime_trades["net_return"]),
                "entry_trade_mean_return": float(regime_trades["net_return"].mean())
                if not regime_trades.empty
                else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _episode_attribution(
    daily: pd.DataFrame, episodes: pd.DataFrame
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for episode_id, episode in episodes.iterrows():
        start = pd.Timestamp(episode["peak_date"])
        end = pd.Timestamp(episode["trough_date"])
        frame = daily.loc[daily["date"].between(start, end)].copy()
        gross_losses = float(-frame.loc[frame["daily_return"].lt(0), "daily_return"].sum())
        for regime, group in frame.groupby("regime", sort=True):
            negative = float(-group.loc[group["daily_return"].lt(0), "daily_return"].sum())
            rows.append(
                {
                    "episode": episode_id + 1,
                    "peak_date": episode["peak_date"],
                    "trough_date": episode["trough_date"],
                    "maximum_drawdown": float(episode["maximum_drawdown"]),
                    "regime": regime,
                    "sessions": len(group),
                    "positioned_sessions": int(group["in_position"].sum()),
                    "compound_return": _compound(group["daily_return"]),
                    "gross_negative_daily_return": negative,
                    "gross_loss_share": negative / gross_losses if gross_losses > 0 else 0.0,
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")

    source = repo / str(protocol["source_experiment"])
    validate_experiment_archive(source)
    gap_path = source / "artifacts/gap_summary.json"
    episodes_path = source / "artifacts/drawdown_episodes.csv"
    if _sha256(gap_path) != protocol["source_gap_summary_sha256"]:
        raise ValueError("source gap summary differs from protocol")
    if _sha256(episodes_path) != protocol["source_drawdown_episodes_sha256"]:
        raise ValueError("source drawdown episodes differ from protocol")
    source_gap = _read(gap_path)
    if source_gap.get("route_decision") != "PROCEED_TO_DRAWDOWN_ATTRIBUTION":
        raise ValueError("source experiment did not route to drawdown attribution")

    context = RepositoryContext.discover(repo)
    snapshot = resolve_registered_strategy(
        context, str(protocol["strategy_id"]), str(protocol["strategy_version"])
    )
    if snapshot.source_hash != protocol["strategy_release_hash"]:
        raise ValueError("strategy release differs from protocol")
    cutoff = pd.Timestamp(str(protocol["development_cutoff"])).date()
    replay_data = load_replay_data(
        context,
        str(protocol["dataset"]),
        str(protocol["symbol"]),
        str(protocol["asset_type"]),
        cutoff,
    )
    output_root = repo / ".tmp/s001-ex02-backtest"
    shutil.rmtree(output_root, ignore_errors=True)
    summary = run_backtest_v2(
        snapshot=snapshot,
        replay_data=replay_data,
        request=BacktestRequestV2(
            symbol=str(protocol["symbol"]),
            asset_type=str(protocol["asset_type"]),
            dataset=replay_data.dataset,
            start=pd.Timestamp(str(protocol["evaluation_start"])).date(),
            end=pd.Timestamp(str(protocol["evaluation_end"])).date(),
            initial_cash=float(protocol["initial_cash"]),
        ),
        outputs_root=output_root,
        run_date=cutoff,
        repository_root=repo,
    )
    decisions = pd.read_csv(summary.output_dir / "decisions.csv")
    orders = pd.read_csv(summary.output_dir / "orders.csv")
    account = pd.read_csv(summary.output_dir / "account_daily.csv")
    trades = pd.read_csv(summary.output_dir / "trades.csv")
    episodes = pd.read_csv(episodes_path).head(int(protocol["top_drawdown_episodes"]))

    trade_attribution = _trade_attribution(trades, orders, decisions, episodes)
    daily_attribution = _daily_attribution(account, decisions)
    regime_summary = _regime_summary(daily_attribution, trade_attribution)
    episode_attribution = _episode_attribution(daily_attribution, episodes)

    top_episode_ids = set(range(1, len(episodes) + 1))
    top_rows = episode_attribution.loc[
        episode_attribution["episode"].isin(top_episode_ids)
    ]
    loss_by_regime = (
        top_rows.groupby("regime", sort=True)["gross_negative_daily_return"].sum()
    )
    total_loss = float(loss_by_regime.sum())
    shares = (loss_by_regime / total_loss).sort_values(ascending=False)
    dominant_regime = str(shares.index[0])
    dominant_share = float(shares.iloc[0])
    threshold = float(protocol["dominant_loss_share_threshold"])
    if dominant_share >= threshold:
        route = f"PROCEED_TO_{dominant_regime.upper()}_DRAWDOWN_MECHANISM_RESEARCH"
    else:
        route = "PROCEED_TO_MIXED_DRAWDOWN_MECHANISM_RESEARCH"

    result = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "strategy_reference": snapshot.identity.reference,
        "strategy_release_hash": snapshot.source_hash,
        "data_fingerprint": replay_data.fingerprint,
        "se_replay_audit": summary.manifest["audit"]["status"],
        "source_maximum_drawdown": source_gap["full_window"]["strategy_max_drawdown"],
        "top_episode_count": len(episodes),
        "dominant_daily_loss_regime": dominant_regime,
        "dominant_daily_loss_share": dominant_share,
        "daily_loss_shares": {str(key): float(value) for key, value in shares.items()},
        "attribution_scope": "descriptive_not_causal",
        "route_decision": route,
        "candidate_created": False,
    }
    decisions.to_csv(artifacts / "decisions.csv", index=False, lineterminator="\n")
    orders.to_csv(artifacts / "orders.csv", index=False, lineterminator="\n")
    daily_attribution.to_csv(
        artifacts / "daily_regime_attribution.csv", index=False, lineterminator="\n"
    )
    trade_attribution.to_csv(
        artifacts / "trade_attribution.csv", index=False, lineterminator="\n"
    )
    regime_summary.to_csv(
        artifacts / "regime_summary.csv", index=False, lineterminator="\n"
    )
    episode_attribution.to_csv(
        artifacts / "drawdown_regime_attribution.csv", index=False, lineterminator="\n"
    )
    _write(artifacts / "drawdown_summary.json", result)

    (experiment / "03_execution.md").write_text(
        "# S001 EX02 执行\n\n"
        f"状态：`COMPLETE`。TDR 与 SE 账本审计 `{result['se_replay_audit']}`；"
        f"归因前三大回撤区间和 {len(trade_attribution)} 笔闭合交易。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX02 结论\n\n"
        f"前三大回撤区间的负收益日损失中，`{dominant_regime}` 状态占"
        f" {dominant_share:.2%}。按预注册的 60% 集中度阈值，裁决为"
        f" `{route}`。该结论是描述性归因，不证明因果；下一轮只研究回撤机制，"
        "不以本实验直接生成规则或候选。\n",
        encoding="utf-8",
    )
    shutil.rmtree(output_root, ignore_errors=True)
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "status": "COMPLETE",
            "experiment_type": protocol["experiment_type"],
            "strategy_id": protocol["strategy_id"],
            "strategy_version": protocol["strategy_version"],
            "symbol": protocol["symbol"],
            "development_cutoff": protocol["development_cutoff"],
            "decision": route,
            "candidate_generation": False,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
