from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive


EXPERIMENT_ID = "20260915_S007_EX17"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_helpers(path: Path):
    spec = importlib.util.spec_from_file_location("s007_ex09_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load EX09 helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _equity_curve(target: pd.Series, prices: pd.DataFrame, fee: float) -> pd.DataFrame:
    desired = target.reindex(prices.index).fillna(0.0).to_numpy(dtype=float)
    opens = prices["open"].to_numpy(dtype=float)
    closes = prices["close"].to_numpy(dtype=float)
    cash = 1.0
    shares = 0.0
    previous = 0.0
    equity = np.empty(len(prices), dtype=float)
    for index, (position, open_price, close_price) in enumerate(zip(desired, opens, closes, strict=True)):
        if position != previous:
            if position == 1.0:
                shares = cash / (open_price * (1.0 + fee))
                cash = 0.0
            else:
                cash = shares * open_price * (1.0 - fee)
                shares = 0.0
            previous = position
        equity[index] = cash + shares * close_price
    peak = np.maximum.accumulate(np.concatenate(([1.0], equity)))[1:]
    return pd.DataFrame({"equity": equity, "drawdown": equity / peak - 1.0}, index=prices.index)


def _drawdown_episodes(curve: pd.DataFrame, anchor: str, limit: int) -> pd.DataFrame:
    episodes: list[dict[str, object]] = []
    in_episode = False
    start: pd.Timestamp | None = None
    for date, drawdown in curve["drawdown"].items():
        if float(drawdown) < -1e-12 and not in_episode:
            in_episode = True
            start = pd.Timestamp(date)
        elif float(drawdown) >= -1e-12 and in_episode and start is not None:
            segment = curve.loc[start:date]
            trough = pd.Timestamp(segment["drawdown"].idxmin())
            episodes.append({
                "anchor": anchor,
                "start": start,
                "trough": trough,
                "recovery": pd.Timestamp(date),
                "maximum_drawdown": float(segment["drawdown"].min()),
                "calendar_days_to_trough": int((trough - start).days),
                "calendar_days_to_recovery": int((pd.Timestamp(date) - start).days),
            })
            in_episode = False
            start = None
    if in_episode and start is not None:
        segment = curve.loc[start:]
        trough = pd.Timestamp(segment["drawdown"].idxmin())
        episodes.append({
            "anchor": anchor,
            "start": start,
            "trough": trough,
            "recovery": pd.NaT,
            "maximum_drawdown": float(segment["drawdown"].min()),
            "calendar_days_to_trough": int((trough - start).days),
            "calendar_days_to_recovery": np.nan,
        })
    return pd.DataFrame(episodes).sort_values("maximum_drawdown").head(limit)


def _trade_ledger(
    anchor: str,
    target: pd.Series,
    prices: pd.DataFrame,
    fee: float,
    score: pd.Series,
    contributions: pd.DataFrame,
    roles: dict[str, list[str]],
    entry_threshold: float,
    diagnostics: dict[str, object],
) -> tuple[pd.DataFrame, int]:
    prior = target.shift(1, fill_value=0.0)
    entry_dates = list(target.index[target.gt(prior)])
    exit_dates = list(target.index[target.lt(prior)])
    prior_close = prices["close"].shift(1)
    ma_sessions = int(diagnostics["trend_ma_sessions"])
    slope_sessions = int(diagnostics["trend_slope_sessions"])
    prior_ma = prices["close"].shift(1).rolling(ma_sessions, min_periods=ma_sessions).mean()
    falling_trend = prior_close.lt(prior_ma) & prior_ma.lt(prior_ma.shift(slope_sessions))
    rows: list[dict[str, object]] = []
    exit_cursor = 0
    previous_exit_offset: int | None = None
    open_trades = 0
    for ordinal, entry_date in enumerate(entry_dates, start=1):
        while exit_cursor < len(exit_dates) and exit_dates[exit_cursor] <= entry_date:
            exit_cursor += 1
        if exit_cursor >= len(exit_dates):
            open_trades += 1
            continue
        exit_date = exit_dates[exit_cursor]
        exit_cursor += 1
        entry_offset = int(prices.index.get_loc(entry_date))
        exit_offset = int(prices.index.get_loc(exit_date))
        holding_sessions = exit_offset - entry_offset
        flat_sessions_before = np.nan if previous_exit_offset is None else entry_offset - previous_exit_offset
        previous_exit_offset = exit_offset
        entry_price = float(prices.loc[entry_date, "open"])
        exit_price = float(prices.loc[exit_date, "open"])
        gross_return = exit_price / entry_price - 1.0
        net_return = exit_price * (1.0 - fee) / (entry_price * (1.0 + fee)) - 1.0
        signal_offset = entry_offset - 1
        signal_date = prices.index[signal_offset] if signal_offset >= 0 else entry_date
        row: dict[str, object] = {
            "anchor": anchor,
            "trade_number": ordinal,
            "signal_date": signal_date,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "holding_sessions": holding_sessions,
            "flat_sessions_before": flat_sessions_before,
            "entry_open": entry_price,
            "exit_open": exit_price,
            "gross_return": gross_return,
            "net_return": net_return,
            "cost_drag": gross_return - net_return,
            "short_holding": holding_sessions <= int(diagnostics["short_holding_max_sessions"]),
            "rapid_reentry": pd.notna(flat_sessions_before) and flat_sessions_before <= int(diagnostics["rapid_reentry_max_flat_sessions"]),
            "causal_falling_trend": bool(falling_trend.loc[entry_date]) if pd.notna(falling_trend.loc[entry_date]) else False,
            "total_score": float(score.loc[signal_date]),
            "score_margin": float(score.loc[signal_date] - entry_threshold),
        }
        for role, names in roles.items():
            contribution = float(contributions.loc[signal_date, names].sum())
            row[f"role_{role.lower()}_contribution"] = contribution
        row["risk_overridden"] = row.get("role_risk_context_contribution", 0.0) < 0.0
        rows.append(row)
    return pd.DataFrame(rows), open_trades


def _loss_share(frame: pd.DataFrame, mask: pd.Series) -> float:
    losses = frame["net_return"].clip(upper=0.0).abs()
    total = float(losses.sum())
    return float(losses.loc[mask].sum() / total) if total > 0 else 0.0


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID or not protocol.get("reads_new_returns"):
        raise ValueError("EX17 protocol identity or return declaration differs")
    forbidden = ("candidate_generation", "promotion_allowed", "mutates_strategy_manager", "mutates_pte")
    if any(protocol.get(key) for key in forbidden):
        raise ValueError("EX17 cannot create, promote, or deploy a candidate")

    sources = protocol["sources"]
    ex12 = repo / str(sources["ex12_archive"])
    ex14 = repo / str(sources["ex14_archive"])
    validate_experiment_archive(ex12)
    validate_experiment_archive(ex14)
    frozen = {
        ex12 / "experiment_manifest.json": sources["ex12_manifest_sha256"],
        ex12 / "artifacts/robustness_evidence.json": sources["robustness_evidence_sha256"],
        ex14 / "experiment_manifest.json": sources["ex14_manifest_sha256"],
        ex14 / "artifacts/feasible_trials.csv": sources["ex14_feasible_trials_sha256"],
    }
    for path, expected in frozen.items():
        if _sha256(path) != expected:
            raise ValueError(f"frozen attribution source differs: {path}")

    ex12_protocol = _read(ex12 / "artifacts/protocol.json")
    ex09 = repo / str(ex12_protocol["sources"]["ex09_archive"])
    ex09_script = ex09 / "run_experiment.py"
    helpers = _load_helpers(ex09_script)
    ex14_protocol = _read(ex14 / "artifacts/protocol.json")
    ex08 = repo / str(ex14_protocol["sources"]["ex08_archive"])
    effective = _read(ex08 / "artifacts/effective_search_protocol.json")
    panel = pd.read_csv(repo / str(ex14_protocol["sources"]["feature_panel"]), parse_dates=["date"]).set_index("date")
    prices = helpers._raw_daily(repo, ex14_protocol["sources"]["daily_sha256"])
    prices = prices.loc[prices.index <= pd.Timestamp(protocol["development_cutoff"])]
    if not panel.index.equals(prices.index):
        raise ValueError("feature panel and price calendar differ")

    normalization = effective["normalization"]
    scores = pd.DataFrame(index=panel.index)
    roles: dict[str, list[str]] = {}
    for feature, binding in effective["feature_bindings"].items():
        scores[feature] = helpers._causal_percentile(
            panel[feature],
            int(normalization["lookback_sessions"]),
            int(normalization["minimum_observations"]),
        ) * int(binding["orientation"])
        roles.setdefault(str(binding["role"]), []).append(feature)

    robustness = _read(ex12 / "artifacts/robustness_evidence.json")
    ex09_feasible = pd.read_csv(ex09 / "artifacts/feasible_trials.csv")
    medoid = ex09_feasible.loc[ex09_feasible["trial_id"].eq(robustness["medoid_trial_id"])]
    ex14_feasible = pd.read_csv(ex14 / "artifacts/feasible_trials.csv")
    if len(medoid) != 1 or len(ex14_feasible) != 1:
        raise ValueError("EX17 anchor rows are not unique")
    anchors = {
        "EX12_PLATFORM_MEDOID_REPRICED_10BP": medoid.iloc[0],
        "EX14_UNIQUE_FEASIBLE_10BP": ex14_feasible.iloc[0],
    }
    discovery_mask = scores.index.to_series().between(
        ex14_protocol["segments"]["discovery_start"], ex14_protocol["segments"]["discovery_end"]
    )
    fee = float(protocol["cost_policy"]["fee_rate_one_way"])
    diagnostics = protocol["diagnostics"]
    trade_frames: list[pd.DataFrame] = []
    drawdown_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    holding_rows: list[dict[str, object]] = []
    context_rows: list[dict[str, object]] = []
    role_rows: list[dict[str, object]] = []
    curves: dict[str, pd.DataFrame] = {}

    for anchor, source_row in anchors.items():
        metadata = json.loads(source_row["metadata"])
        params = json.loads(source_row["params"])
        weights = metadata["weights"]
        contributions = scores.mul(pd.Series(weights), axis=1)
        combined = contributions.sum(axis=1).where(scores.notna().all(axis=1))
        discovery_values = combined.loc[discovery_mask].dropna()
        entry_threshold = float(discovery_values.quantile(float(params["entry_quantile"])))
        exit_threshold = float(discovery_values.quantile(float(params["exit_quantile"])))
        decision = helpers._hysteresis(combined, entry_threshold, exit_threshold)
        target = decision.shift(1).fillna(0.0)
        metrics_10bp = helpers._metrics(target, prices, fee)
        metrics_0bp = helpers._metrics(target, prices, 0.0)
        curve = _equity_curve(target, prices, fee)
        curves[anchor] = curve
        drawdowns = _drawdown_episodes(
            curve,
            anchor,
            int(diagnostics["maximum_drawdown_episodes_per_anchor"]),
        )
        trades, open_trades = _trade_ledger(
            anchor,
            target,
            prices,
            fee,
            combined,
            contributions,
            roles,
            entry_threshold,
            diagnostics,
        )
        trade_frames.append(trades)
        drawdown_frames.append(drawdowns)
        holding_bucket = pd.cut(
            trades["holding_sessions"],
            bins=[0, 3, 10, float("inf")],
            labels=["1-3", "4-10", "11+"],
            right=True,
        )
        for bucket, group in trades.groupby(holding_bucket, observed=True):
            holding_rows.append({
                "anchor": anchor,
                "holding_bucket": str(bucket),
                "trades": int(len(group)),
                "trade_share": float(len(group) / len(trades)),
                "average_net_return": float(group["net_return"].mean()),
                "median_net_return": float(group["net_return"].median()),
                "win_rate": float(group["net_return"].gt(0.0).mean()),
                "loss_share": _loss_share(trades, trades.index.isin(group.index)),
            })
        for field in ("rapid_reentry", "causal_falling_trend", "risk_overridden"):
            for value, group in trades.groupby(field):
                context_rows.append({
                    "anchor": anchor,
                    "context": field,
                    "value": bool(value),
                    "trades": int(len(group)),
                    "trade_share": float(len(group) / len(trades)),
                    "average_net_return": float(group["net_return"].mean()),
                    "median_net_return": float(group["net_return"].median()),
                    "win_rate": float(group["net_return"].gt(0.0).mean()),
                    "loss_share": _loss_share(trades, trades.index.isin(group.index)),
                })
        contribution_columns = [name for name in trades.columns if name.startswith("role_")]
        for outcome, group in (("WIN", trades.loc[trades["net_return"].gt(0.0)]), ("LOSS", trades.loc[trades["net_return"].lt(0.0)])):
            role_row: dict[str, object] = {"anchor": anchor, "outcome": outcome, "trades": int(len(group))}
            for column in contribution_columns:
                role_row[f"mean_{column}"] = float(group[column].mean())
            role_rows.append(role_row)
        for cost_label, metric in (("0BP", metrics_0bp), ("10BP", metrics_10bp)):
            metric_rows.append({"anchor": anchor, "cost_label": cost_label, **metric})
        losing = trades["net_return"].lt(0.0)
        winners = trades.loc[trades["net_return"].gt(0.0), "net_return"].sort_values(ascending=False)
        losses = trades.loc[losing, "net_return"].abs().sort_values(ascending=False)
        total_profit = float(winners.sum())
        total_loss = float(losses.sum())
        summary_rows.append({
            "anchor": anchor,
            "closed_trades": int(len(trades)),
            "open_trades": int(open_trades),
            "win_rate": float(trades["net_return"].gt(0.0).mean()),
            "average_net_trade_return": float(trades["net_return"].mean()),
            "median_net_trade_return": float(trades["net_return"].median()),
            "worst_net_trade_return": float(trades["net_return"].min()),
            "short_holding_trade_rate": float(trades["short_holding"].mean()),
            "rapid_reentry_trade_rate": float(trades["rapid_reentry"].mean()),
            "falling_trend_entry_rate": float(trades["causal_falling_trend"].mean()),
            "risk_overridden_entry_rate": float(trades["risk_overridden"].mean()),
            "loss_share_short_holding": _loss_share(trades, trades["short_holding"]),
            "loss_share_rapid_reentry": _loss_share(trades, trades["rapid_reentry"]),
            "loss_share_falling_trend": _loss_share(trades, trades["causal_falling_trend"]),
            "loss_share_risk_overridden": _loss_share(trades, trades["risk_overridden"]),
            "top_5_profit_share": float(winners.head(5).sum() / total_profit) if total_profit > 0 else 0.0,
            "top_10_profit_share": float(winners.head(10).sum() / total_profit) if total_profit > 0 else 0.0,
            "worst_5_loss_share": float(losses.head(5).sum() / total_loss) if total_loss > 0 else 0.0,
            "worst_10_loss_share": float(losses.head(10).sum() / total_loss) if total_loss > 0 else 0.0,
            "losing_trades": int(losing.sum()),
            "gross_to_10bp_cagr_drag": float(metrics_0bp["cagr"] - metrics_10bp["cagr"]),
        })

    trade_ledger = pd.concat(trade_frames, ignore_index=True)
    drawdown_episodes = pd.concat(drawdown_frames, ignore_index=True)
    anchor_metrics = pd.DataFrame(metric_rows)
    attribution = pd.DataFrame(summary_rows)
    holding_summary = pd.DataFrame(holding_rows)
    context_summary = pd.DataFrame(context_rows)
    role_summary = pd.DataFrame(role_rows)
    trade_ledger.to_csv(artifacts / "trade_ledger.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    drawdown_episodes.to_csv(artifacts / "drawdown_episodes.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    anchor_metrics.to_csv(artifacts / "anchor_metrics.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    attribution.to_csv(artifacts / "attribution_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    holding_summary.to_csv(artifacts / "holding_bucket_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    context_summary.to_csv(artifacts / "context_outcome_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    role_summary.to_csv(artifacts / "role_outcome_summary.csv", index=False, encoding="utf-8-sig", lineterminator="\n")

    loss_dates = []
    for anchor in anchors:
        dates = set(trade_ledger.loc[(trade_ledger["anchor"].eq(anchor)) & (trade_ledger["net_return"].lt(0)), "entry_date"])
        loss_dates.append(dates)
    common_losing_entry_dates = sorted(loss_dates[0].intersection(loss_dates[1]))
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "decision": "REVIEW_DRAWDOWN_ATTRIBUTION",
        "anchors": summary_rows,
        "common_losing_entry_dates": [pd.Timestamp(value).date().isoformat() for value in common_losing_entry_dates],
        "common_losing_entry_count": len(common_losing_entry_dates),
        "interpretation_is_automatic": False,
        "next_template_selected": False,
        "candidate_created": False,
        "strategy_manager_mutated": False,
        "pte_mutated": False,
    }
    _write(artifacts / "attribution_evidence.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S007 EX17 执行\n\n"
        f"状态：`COMPLETE`。两个固定锚点均按单边10bp重算，共分析{len(trade_ledger)}笔闭合交易、"
        f"{len(drawdown_episodes)}个主要回撤区间；共同亏损入场日期{len(common_losing_entry_dates)}个。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S007 EX17 结论\n\n"
        "裁决：`REVIEW_DRAWDOWN_ATTRIBUTION`。\n\n"
        "两个固定锚点呈现高度一致的交易结构：71.3%—72.5%的闭合交易只持有1—3个交易日，"
        "这组交易扣除单边10bp成本后的平均收益为-0.11%至-0.08%，贡献约68%—69%的总亏损；"
        "持有4—10日的交易平均收益约2.98%，持有11日以上的交易平均收益约4.19%—4.91%。"
        "从0bp改为10bp后，两个锚点的年化收益分别下降7.42和7.79个百分点。\n\n"
        "快速重入、因果下跌趋势和风险信息被其他角色抵消均没有表现出更差的平均收益或超额亏损"
        "占比，因此现有证据不支持直接增加冷却期、趋势过滤或风险否决。两个锚点共有76个亏损"
        "入场日，进一步说明问题属于原型共同结构，而非单个参数偶然。\n\n"
        "最好的5笔交易贡献约38%—39%的总盈利，最差的5笔只贡献约23%—24%的总亏损。当前"
        "原型的主要矛盾是大量短暂阈值穿越产生的低毛利往返交易，而非少数灾难性交易。持有期限"
        "是交易完成后的结果，不能直接当作事前过滤条件；EX13也已经证明强制最短持有期会恶化结果。"
        "下一轮若继续，应研究入场前能否识别“可持续信号”，不能事后删除短交易。\n\n"
        "按照逐轮评审约定，本档案不自动选择下一种F模板，需先与用户评审上述证据及金融含义。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETE",
        "experiment_type": protocol["experiment_type"],
        "strategy_id": protocol["strategy_id"],
        "symbol": protocol["symbol"],
        "development_cutoff": protocol["development_cutoff"],
        "decision": "REVIEW_DRAWDOWN_ATTRIBUTION",
        "promotion_allowed": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
