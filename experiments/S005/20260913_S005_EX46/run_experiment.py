from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


EXPERIMENT_ID = "20260913_S005_EX46"


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


def _cooldown(mask: pd.Series, sessions: int) -> list[int]:
    selected: list[int] = []
    last = -sessions - 1
    for position, flag in enumerate(mask.fillna(False)):
        if bool(flag) and position - last > sessions:
            selected.append(position)
            last = position
    return selected


def main() -> None:
    experiment = Path(__file__).resolve().parent
    repo = experiment.parents[2]
    sys.path.insert(0, str(repo / "src"))
    from czsc_trader.experiment_archive import (  # noqa: PLC0415
        build_experiment_manifest,
        validate_experiment_archive,
    )
    from czsc_trader.identity import raw_file_sha256  # noqa: PLC0415

    artifacts = experiment / "artifacts"
    protocol = _read(artifacts / "protocol.json")
    if protocol.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("experiment id differs from frozen protocol")
    if protocol.get("reads_post_signal_returns"):
        raise ValueError("EX46 may not read post-signal returns")

    daily_parts: list[pd.DataFrame] = []
    for code, spec in protocol["inputs"].items():
        path = repo / str(spec[0])
        if raw_file_sha256(path) != spec[1]:
            raise ValueError(f"input differs from frozen protocol: {code}")
        frame = pd.read_csv(path)
        frame["Date"] = pd.to_datetime(frame["Date"])
        daily = frame.groupby(frame["Date"].dt.normalize()).agg(
            session_open=("Open", "first"), session_close=("Close", "last")
        )
        daily[code] = daily["session_close"] / daily["session_open"] - 1.0
        daily_parts.append(daily[[code]])
    panel = pd.concat(daily_parts, axis=1, join="inner").sort_index()
    semiconductor = list(protocol["semiconductor_sources"])
    broad = str(protocol["broad_control"])
    panel["semiconductor_return"] = panel[semiconductor].mean(axis=1)
    panel["semiconductor_breadth"] = panel[semiconductor].gt(panel[broad], axis=0).sum(axis=1)
    panel["semiconductor_specific_return"] = panel["semiconductor_return"] - panel[broad]
    panel["target_catchup_gap"] = panel["semiconductor_return"] - panel["588080.SH"]

    lookback = int(protocol["lookback_sessions"])
    rolling = int(protocol["density_gate"]["rolling_sessions"])
    ledger_parts: list[pd.DataFrame] = []
    diagnostics: list[dict[str, object]] = []
    for quantile in protocol["quantiles"]:
        suffix = f"q{int(float(quantile) * 100)}"
        specific_cut = panel["semiconductor_specific_return"].rolling(
            lookback, min_periods=lookback
        ).quantile(float(quantile)).shift(1)
        gap_cut = panel["target_catchup_gap"].rolling(
            lookback, min_periods=lookback
        ).quantile(float(quantile)).shift(1)
        panel[f"specific_cut_{suffix}"] = specific_cut
        panel[f"gap_cut_{suffix}"] = gap_cut
        raw = (
            (panel["semiconductor_breadth"] >= int(protocol["minimum_semiconductor_breadth"]))
            & (panel["semiconductor_specific_return"] > specific_cut)
            & (panel["target_catchup_gap"] > gap_cut)
        )
        selected = _cooldown(raw, int(protocol["cooldown_sessions"]))
        event = pd.Series(0, index=panel.index, dtype=int)
        event.iloc[selected] = 1
        counts = event.rolling(rolling, min_periods=rolling).sum()
        median = float(counts.median())
        p10 = float(counts.quantile(0.1))
        passed = bool(
            median >= float(protocol["density_gate"]["minimum_median_events"])
            and p10 >= float(protocol["density_gate"]["minimum_p10_events"])
        )
        diagnostics.append(
            {
                "quantile": float(quantile),
                "events": int(len(selected)),
                "rolling_60_median": median,
                "rolling_60_p10": p10,
                "passed": passed,
            }
        )
        part = panel.iloc[selected][
            ["semiconductor_breadth", "semiconductor_specific_return", "target_catchup_gap"]
        ].copy()
        part["quantile"] = float(quantile)
        part["signal_date"] = part.index
        ledger_parts.append(part.reset_index(drop=True))

    eligible = [item for item in diagnostics if item["passed"]]
    selected_quantile = max(float(item["quantile"]) for item in eligible) if eligible else None
    selected_name = f"SECTOR_LEADS_TARGET_Q{int(selected_quantile * 100)}" if selected_quantile is not None else None
    selection = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "selection_rule": protocol["selection_rule"],
        "selected_quantile": selected_quantile,
        "selected_mechanism": selected_name,
        "diagnostics": diagnostics,
        "passed": selected_quantile is not None,
    }
    _write(artifacts / "selection.json", selection)
    panel_out = panel.copy()
    panel_out.index = panel_out.index.strftime("%Y-%m-%d")
    panel_out.index.name = "dt"
    panel_out.to_csv(
        artifacts / "predictor_panel.csv.gz",
        compression={"method": "gzip", "compresslevel": 9, "mtime": 0},
        lineterminator="\n",
    )
    ledger = pd.concat(ledger_parts, ignore_index=True)
    ledger["signal_date"] = pd.to_datetime(ledger["signal_date"]).dt.strftime("%Y-%m-%d")
    ledger.to_csv(artifacts / "event_ledger.csv", index=False, lineterminator="\n")

    status = "PASS" if selection["passed"] else "FAIL"
    summary = "；".join(
        f"Q{int(item['quantile'] * 100)}={item['events']}笔/60日{item['rolling_60_median']:.0f}/{item['rolling_60_p10']:.0f}"
        for item in diagnostics
    )
    (experiment / "03_execution.md").write_text(
        "# S005 EX46 执行\n\n"
        f"密度门结果：`{status}`。{summary}。"
        f"按预注册规则冻结：`{selected_name or 'NONE'}`。本轮没有读取信号后收益。\n",
        encoding="utf-8",
    )
    decision = "PROCEED_TO_FIXED_RETURN_TEST" if selection["passed"] else "STOP_CROSS_ETF_ROUTE_ON_DENSITY"
    (experiment / "04_conclusion.md").write_text(
        "# S005 EX46 结论\n\n"
        f"裁决：`{decision}`。"
        + (
            f"冻结路径：`{selected_name}`；下一轮只能原样执行单一收益检验。"
            if selection["passed"]
            else "没有路径达到S005的证据速度门。"
        )
        + "本轮不创建候选、不修改SM或PTE。\n",
        encoding="utf-8",
    )
    build_experiment_manifest(
        experiment,
        {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": "S005",
            "symbol": "588080.SH",
            "development_cutoff": str(protocol["development_cutoff"]),
            "status": status,
            "decision": decision,
            "reads_prices_or_returns": False,
            "candidate_created": False,
        },
    )
    validate_experiment_archive(experiment)


if __name__ == "__main__":
    main()
