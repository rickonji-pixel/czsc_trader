from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from czsc_trader.application.context import RepositoryContext
from czsc_trader.backtesting.datasets import load_replay_data
from czsc_trader.backtesting.intraday_overlay_replay import (
    build_moneyflow_breadth_signals,
    replay_intraday_overlay,
)
from czsc_trader.backtesting.strategy_source import resolve_candidate_snapshot
from czsc_trader.experiment_archive import build_experiment_manifest, validate_experiment_archive
from czsc_trader.identity import canonical_json_sha256


EXPERIMENT_ID = "20260911_S003_EX63"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _metrics(equity: pd.Series, initial_cash: float) -> dict[str, float]:
    values = equity.astype(float)
    total_return = float(values.iloc[-1] / initial_cash - 1.0)
    years = max((values.index[-1] - values.index[0]).days / 365.25, 1.0 / 252.0)
    annual_return = float((values.iloc[-1] / initial_cash) ** (1.0 / years) - 1.0)
    drawdown = values.div(values.cummax()).sub(1.0)
    maximum_drawdown = float(drawdown.min())
    calmar = float(annual_return / abs(maximum_drawdown)) if maximum_drawdown < 0 else 0.0
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "maximum_drawdown": maximum_drawdown,
        "calmar": calmar,
    }


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    artifacts = experiment / "artifacts"
    protocol = _read_json(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol["interpretation"].get("no_ratio_ranking") is not True:
        raise ValueError("EX63 must not rank allocation ratios")
    if any(
        protocol.get(key)
        for key in (
            "parameter_selection", "candidate_generation", "promotion_allowed",
            "mutates_strategy_manager", "mutates_pte",
        )
    ):
        raise ValueError("EX63 is diagnostic only")

    dataset = protocol["dataset"]
    ex56 = repo / "experiments/S003/20260911_S003_EX56"
    ex62 = repo / "experiments/S003/20260911_S003_EX62"
    validate_experiment_archive(ex56)
    validate_experiment_archive(ex62)
    expected = {
        ex56 / "experiment_manifest.json": dataset["ex56_manifest_sha256"],
        ex56 / "candidate_payload.json": dataset["candidate_payload_sha256"],
        ex56 / "artifacts/account_daily.csv": dataset["account_daily_sha256"],
        ex56 / "artifacts/trades.csv": dataset["trades_sha256"],
        ex62 / "experiment_manifest.json": dataset["ex62_manifest_sha256"],
    }
    for path, digest in expected.items():
        if _sha256(path) != digest:
            raise ValueError(f"source evidence hash differs: {path}")

    context = RepositoryContext.discover(repo)
    cutoff = pd.Timestamp(dataset["development_cutoff"])
    start = pd.Timestamp(dataset["evaluation_start"])
    data = load_replay_data(
        context, "research", "510500.SH", "etf", cutoff.date(),
        include_five_minute=True,
    )
    initial_cash = float(protocol["execution"]["initial_cash"])
    base_payload = _read_json(ex56 / "candidate_payload.json")
    authority = pd.read_csv(ex56 / "artifacts/account_daily.csv")
    authority["date"] = pd.to_datetime(authority["date"]).dt.normalize()
    authority_trades = pd.read_csv(ex56 / "artifacts/trades.csv")

    rows = []
    daily_outputs = []
    event_dates_reference = None
    for ratio in protocol["execution"]["ratios"]:
        core = float(ratio["core_fraction"])
        event = float(ratio["event_fraction"])
        if not np.isclose(core + event, 1.0) or core < event:
            raise ValueError("diagnostic ratio cannot complete T+1 inventory rotation")
        payload = deepcopy(base_payload)
        payload["candidate_id"] = f"S003-DIAG-{int(core * 100):02d}{int(event * 100):02d}"
        payload["source_candidate_hash"] = base_payload["candidate_hash"]
        payload["candidate_hash"] = canonical_json_sha256({
            "source": base_payload["candidate_hash"], "core": core, "event": event,
        })
        payload["rule"]["execution"]["core_fraction"] = core
        payload["rule"]["execution"]["event_fraction"] = event
        snapshot = resolve_candidate_snapshot(
            context,
            payload["candidate_id"],
            payload,
            canonical_json_sha256(payload),
            f"{EXPERIMENT_ID}:{core:.1f}/{event:.1f}",
        )
        signals = build_moneyflow_breadth_signals(snapshot, data, repo, start, cutoff)
        result = replay_intraday_overlay(signals, data, initial_cash)
        event_dates = pd.DatetimeIndex(
            pd.to_datetime(signals.decisions["valid_session"]).dt.normalize()
        )
        if event_dates_reference is None:
            event_dates_reference = event_dates
        elif not event_dates.equals(event_dates_reference):
            raise ValueError("allocation diagnostic changed event dates")
        account = result.account_daily.copy()
        account.index = pd.DatetimeIndex(pd.to_datetime(account["date"]).dt.normalize())
        equity = account["equity"].astype(float)

        execution_daily = data.execution_daily.set_index("dt").sort_index()
        prior = execution_daily.loc[execution_daily.index < signals.evaluation_start]
        core_price = float(prior.iloc[-1]["close"])
        fee_rate = float(payload["rule"]["execution"]["one_way_cost"])
        lot = int(payload["rule"]["execution"]["lot_size"])
        core_quantity = int(initial_cash * core / (core_price * (1.0 + fee_rate)) // lot * lot)
        static_cash = initial_cash - core_quantity * core_price * (1.0 + fee_rate)
        static_equity = static_cash + core_quantity * account["close"].astype(float)
        static_equity.index = equity.index
        alpha_difference = equity - static_equity
        alpha_wealth = initial_cash + alpha_difference
        total_metrics = _metrics(equity, initial_cash)
        static_metrics = _metrics(static_equity, initial_cash)
        alpha_metrics = _metrics(alpha_wealth, initial_cash)
        event_fees = float(result.fills["fees"].sum())
        average_event_notional = float(
            (result.trades["quantity"] * result.trades["entry_price"]).mean()
        )
        row = {
            "core_fraction": core,
            "event_fraction": event,
            "role": ratio["role"],
            "core_quantity": core_quantity,
            "event_count": int(len(result.trades)),
            "average_event_notional_fraction": average_event_notional / initial_cash,
            "event_fees": event_fees,
            "account_total_return": total_metrics["total_return"],
            "account_annual_return": total_metrics["annual_return"],
            "account_maximum_drawdown": total_metrics["maximum_drawdown"],
            "account_calmar": total_metrics["calmar"],
            "static_core_total_return": static_metrics["total_return"],
            "static_core_maximum_drawdown": static_metrics["maximum_drawdown"],
            "event_alpha_terminal_return_on_account": float(alpha_difference.iloc[-1] / initial_cash),
            "event_alpha_terminal_return_on_event_budget": float(
                alpha_difference.iloc[-1] / (initial_cash * event)
            ),
            "event_alpha_maximum_drawdown_on_account": alpha_metrics["maximum_drawdown"],
        }
        rows.append(row)
        daily_outputs.append(pd.DataFrame({
            "date": equity.index.strftime("%Y-%m-%d"),
            "core_fraction": core,
            "account_equity": equity.to_numpy(),
            "static_core_equity": static_equity.to_numpy(),
            "event_alpha_difference": alpha_difference.to_numpy(),
        }))
        if ratio["role"] == "FROZEN_AUTHORITY":
            if not np.allclose(
                equity.to_numpy(), authority.set_index("date").loc[equity.index, "equity"].to_numpy(),
                rtol=0.0, atol=1e-8,
            ):
                raise ValueError("50/50 replay differs from EX56 authority")
            formal_returns = result.trades["net_return"].to_numpy(dtype=float)
            authority_returns = authority_trades["net_return"].to_numpy(dtype=float)
            if not np.allclose(formal_returns, authority_returns, rtol=0.0, atol=1e-12):
                raise ValueError("50/50 trade returns differ from EX56 authority")

    metrics = pd.DataFrame(rows)
    same_direction = bool(metrics["event_alpha_terminal_return_on_account"].gt(0).all())
    authority_row = metrics.loc[metrics["role"].eq("FROZEN_AUTHORITY")].iloc[0]
    summary = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "PASS",
        "frozen_ratio": "50/50",
        "event_alpha_positive_in_all_fixed_ratios": same_direction,
        "frozen_attribution": {
            "account_total_return": float(authority_row["account_total_return"]),
            "static_core_total_return": float(authority_row["static_core_total_return"]),
            "event_alpha_terminal_return_on_account": float(authority_row["event_alpha_terminal_return_on_account"]),
            "account_maximum_drawdown": float(authority_row["account_maximum_drawdown"]),
            "static_core_maximum_drawdown": float(authority_row["static_core_maximum_drawdown"]),
            "event_alpha_maximum_drawdown_on_account": float(authority_row["event_alpha_maximum_drawdown_on_account"]),
            "event_fees": float(authority_row["event_fees"]),
        },
        "ratio_selected": False,
        "candidate_changed": False,
        "sm_changed": False,
        "pte_changed": False,
    }
    metrics.to_csv(artifacts / "allocation_attribution.csv", index=False, lineterminator="\n")
    pd.concat(daily_outputs, ignore_index=True).to_csv(
        artifacts / "allocation_equity.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    _write_json(artifacts / "attribution_summary.json", summary)
    frozen = summary["frozen_attribution"]
    (experiment / "03_execution.md").write_text(
        "# S003 EX63 执行\n\n"
        f"结构归因：`PASS`。50/50正式账户累计收益{frozen['account_total_return']:.2%}，"
        f"相同核心仓位静态收益{frozen['static_core_total_return']:.2%}，事件轮换贡献"
        f"{frozen['event_alpha_terminal_return_on_account']:.2%}；账户最大回撤"
        f"{frozen['account_maximum_drawdown']:.2%}，静态核心最大回撤"
        f"{frozen['static_core_maximum_drawdown']:.2%}，剥离核心后的事件Alpha最大回撤"
        f"{frozen['event_alpha_maximum_drawdown_on_account']:.2%}。\n",
        encoding="utf-8",
    )
    direction_text = "均为正" if same_direction else "未保持同号"
    (experiment / "04_conclusion.md").write_text(
        "# S003 EX63 结论\n\n"
        f"50/50、60/40、70/30三个固定比例的事件Alpha终值贡献{direction_text}。"
        "核心仓位解释账户主要市场回撤，事件轮换提供独立增量收益。该结果证明结构归因，"
        "不证明任何比例最优，不修改冻结版本。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(experiment, {
        "experiment_id": EXPERIMENT_ID,
        "strategy_id": "S003",
        "release_id": "S003-v1",
        "symbol": "510500.SH",
        "development_cutoff": str(cutoff.date()),
        "status": "PASS",
        "event_alpha_positive_in_all_fixed_ratios": same_direction,
        "ratio_selected": False,
        "candidate_generation": False,
        "mutates_strategy_manager": False,
        "mutates_pte": False,
    })
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
