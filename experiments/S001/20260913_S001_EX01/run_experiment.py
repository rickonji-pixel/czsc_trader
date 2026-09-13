from __future__ import annotations

import json
from pathlib import Path
import shutil

import pandas as pd
from strategy_evaluator import performance_metrics

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting import (
    BacktestRequestV2,
    load_replay_data,
    resolve_registered_strategy,
    run_backtest_v2,
)
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import raw_file_sha256


EXPERIMENT_ID = "20260913_S001_EX01"


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


def _daily_returns(account: pd.DataFrame, initial_cash: float) -> pd.Series:
    frame = account.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    equity = frame.set_index("date").sort_index()["equity"].astype(float)
    returns = equity.pct_change()
    returns.iloc[0] = equity.iloc[0] / initial_cash - 1.0
    return returns


def _mandate_row(
    label: str,
    candidate: pd.Series,
    buyhold: pd.Series,
    protocol: dict[str, object],
) -> dict[str, object]:
    candidate_metrics = performance_metrics(candidate.to_numpy()).to_dict()
    buyhold_metrics = performance_metrics(buyhold.to_numpy()).to_dict()
    mandate = protocol["mandate"]
    if not isinstance(mandate, dict):
        raise TypeError("mandate must be an object")
    buyhold_cagr = float(buyhold_metrics["cagr"])
    required_cagr = (
        buyhold_cagr * float(mandate["positive_buyhold_cagr_multiple"])
        if buyhold_cagr > 0
        else 0.0
    )
    candidate_cagr = float(candidate_metrics["cagr"])
    return_pass = candidate_cagr >= required_cagr if buyhold_cagr > 0 else candidate_cagr > 0
    drawdown = float(candidate_metrics["max_drawdown"])
    return {
        "window": label,
        "start": candidate.index.min().date().isoformat(),
        "end": candidate.index.max().date().isoformat(),
        "strategy_cagr": candidate_cagr,
        "buyhold_cagr": buyhold_cagr,
        "required_strategy_cagr": required_cagr,
        "return_gate": "PASS" if return_pass else "FAIL",
        "strategy_max_drawdown": drawdown,
        "drawdown_gate": (
            "PASS" if drawdown >= float(mandate["maximum_drawdown_floor"]) else "FAIL"
        ),
    }


def _drawdown_episodes(account: pd.DataFrame) -> pd.DataFrame:
    frame = account[["date", "equity"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    frame = frame.sort_values("date").reset_index(drop=True)
    frame["peak_equity"] = frame["equity"].cummax()
    frame["drawdown"] = frame["equity"] / frame["peak_equity"] - 1.0
    episodes: list[dict[str, object]] = []
    start_index: int | None = None
    for index, value in enumerate(frame["drawdown"].astype(float)):
        if value < 0 and start_index is None:
            start_index = max(0, index - 1)
        recovered = value >= -1e-12 and start_index is not None and index > start_index
        last_row = index == len(frame) - 1 and start_index is not None
        if recovered or last_row:
            end_index = index
            segment = frame.iloc[start_index : end_index + 1]
            trough_index = int(segment["drawdown"].idxmin())
            episodes.append(
                {
                    "peak_date": frame.loc[start_index, "date"].date().isoformat(),
                    "trough_date": frame.loc[trough_index, "date"].date().isoformat(),
                    "end_date": frame.loc[end_index, "date"].date().isoformat(),
                    "maximum_drawdown": float(frame.loc[trough_index, "drawdown"]),
                    "duration_sessions": int(end_index - start_index),
                    "recovered": bool(recovered),
                }
            )
            start_index = None
    return pd.DataFrame(episodes).sort_values("maximum_drawdown").reset_index(drop=True)


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    context = RepositoryContext.discover(repo)
    snapshot = resolve_registered_strategy(
        context,
        str(protocol["strategy_id"]),
        str(protocol["strategy_version"]),
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
    output_root = repo / ".tmp/s001-ex01-backtest"
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
    account = pd.read_csv(summary.output_dir / "account_daily.csv")
    buyhold = pd.read_csv(summary.output_dir / "buyhold_account_daily.csv")
    trades = pd.read_csv(summary.output_dir / "trades.csv")
    initial_cash = float(protocol["initial_cash"])
    candidate_returns = _daily_returns(account, initial_cash)
    buyhold_returns = _daily_returns(buyhold, initial_cash)
    aligned = pd.concat(
        [candidate_returns.rename("strategy"), buyhold_returns.rename("buyhold")],
        axis=1,
        join="inner",
    )
    if aligned.isna().any().any():
        raise AssertionError("strategy and BuyHold returns are not aligned")
    rows = [_mandate_row("FULL", aligned["strategy"], aligned["buyhold"], protocol)]
    for year, group in aligned.groupby(aligned.index.year):
        rows.append(_mandate_row(str(year), group["strategy"], group["buyhold"], protocol))
    annual = pd.DataFrame(rows)
    full = rows[0]
    drawdowns = _drawdown_episodes(account)
    account["date"] = pd.to_datetime(account["date"], errors="raise")
    exposure_ratio = float(account["quantity"].astype(float).gt(0).mean())
    evidence = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "strategy_reference": snapshot.identity.reference,
        "strategy_release_hash": snapshot.source_hash,
        "mandate_sha256": raw_file_sha256(repo / str(protocol["mandate"]["path"])),
        "data_fingerprint": replay_data.fingerprint,
        "se_replay_audit": summary.manifest["audit"]["status"],
        "full_window": full,
        "closed_trades": int(trades["status"].eq("CLOSED").sum()),
        "exposure_session_ratio": exposure_ratio,
        "drawdown_excess_over_limit": max(
            0.0,
            abs(float(full["strategy_max_drawdown"]))
            - abs(float(protocol["mandate"]["maximum_drawdown_floor"])),
        ),
        "primary_gate": (
            "PASS"
            if full["return_gate"] == "PASS" and full["drawdown_gate"] == "PASS"
            else "FAIL"
        ),
        "route_decision": "PROCEED_TO_DRAWDOWN_ATTRIBUTION",
        "candidate_created": False,
    }
    annual.to_csv(artifacts / "window_metrics.csv", index=False, lineterminator="\n")
    drawdowns.to_csv(artifacts / "drawdown_episodes.csv", index=False, lineterminator="\n")
    trades.to_csv(artifacts / "formal_trades.csv", index=False, lineterminator="\n")
    _write(artifacts / "gap_summary.json", evidence)
    (experiment / "03_execution.md").write_text(
        "# S001 EX01 执行\n\n"
        f"状态：`COMPLETE`。TDR与SE账本审计{evidence['se_replay_audit']}；"
        f"闭合交易{evidence['closed_trades']}笔，持仓交易日占比{exposure_ratio:.2%}。\n",
        encoding="utf-8",
    )
    (experiment / "04_conclusion.md").write_text(
        "# S001 EX01 结论\n\n"
        f"完整开发池CAGR为{float(full['strategy_cagr']):.2%}，BuyHold为"
        f"{float(full['buyhold_cagr']):.2%}，收益门为`{full['return_gate']}`；最大回撤为"
        f"{float(full['strategy_max_drawdown']):.2%}，15%回撤门为`{full['drawdown_gate']}`。"
        f"当前需压缩回撤约{float(evidence['drawdown_excess_over_limit']):.2%}，无需优先增加收益。"
        "裁决：`PROCEED_TO_DRAWDOWN_ATTRIBUTION`，不生成候选。\n",
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
            "decision": evidence["route_decision"],
            "candidate_generation": False,
            "mutates_strategy_manager": False,
            "mutates_pte": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
