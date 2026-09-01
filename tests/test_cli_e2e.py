from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd
import numpy as np
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = Path(sys.executable).with_name(
    "czsc-trader.exe" if sys.platform == "win32" else "czsc-trader"
)
STRATEGY_METRIC_KEYS = {
    "max_drawdown",
    "calmar",
    "win_loss_ratio",
    "return",
    "sharpe",
}
TRACKED_SYMBOLS = (
    "159352.SZ",
    "159516.SZ",
    "515050.SH",
    "588080.SH",
)


def test_portable_identity_separates_json_text_and_raw_bytes(tmp_path: Path) -> None:
    from czsc_trader.identity import (
        canonical_json_sha256,
        normalized_text_sha256,
        raw_file_sha256,
    )

    first_json = tmp_path / "first.json"
    second_json = tmp_path / "second.json"
    first_json.write_bytes('{\r\n  "b": "中",\r\n  "a": 1\r\n}\r\n'.encode("utf-8"))
    second_json.write_bytes('{"a":1,"b":"中"}\n'.encode("utf-8"))

    expected_json = "2831299868169bc527f55f88ebbdcd8b785d78d9e7dc64e6887dfbd2825dd247"
    assert canonical_json_sha256(first_json) == expected_json
    assert canonical_json_sha256(second_json) == expected_json
    assert canonical_json_sha256({"b": "中", "a": 1}) == expected_json
    assert raw_file_sha256(first_json) != raw_file_sha256(second_json)

    lf = tmp_path / "lf.txt"
    crlf = tmp_path / "crlf.txt"
    cr = tmp_path / "cr.txt"
    lf.write_bytes(b"alpha\nbeta\n")
    crlf.write_bytes(b"alpha\r\nbeta\r\n")
    cr.write_bytes(b"alpha\rbeta\r")
    expected_text = "e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee"

    assert normalized_text_sha256(lf) == expected_text
    assert normalized_text_sha256(crlf) == expected_text
    assert normalized_text_sha256(cr) == expected_text
    assert len({raw_file_sha256(path) for path in (lf, crlf, cr)}) == 3


def test_execution_policy_rounds_buy_limit_down_to_etf_tick() -> None:
    from czsc_trader.execution_policy import floor_to_tick

    assert floor_to_tick(1.68899) == pytest.approx(1.688)
    assert floor_to_tick(1.68900) == pytest.approx(1.689)


def test_execution_policy_retries_entry_without_changing_actual_position() -> None:
    from czsc_trader.execution_policy import simulate_limit_policy

    dates = pd.bdate_range("2026-01-02", periods=5)
    daily = pd.DataFrame(
        {
            "dt": dates,
            "open": [10.0, 10.0, 11.0, 10.8, 10.4],
            "high": [10.2, 10.2, 11.2, 11.0, 10.6],
            "low": [9.8, 9.8, 10.5, 10.1, 10.2],
            "close": [10.0, 10.0, 10.8, 10.5, 10.3],
        }
    )
    intraday_rows = []
    session_times = ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")
    for date in dates:
        for position, value in enumerate(session_times):
            low = 10.4
            if date == dates[3] and position == 1:
                low = 10.1
            intraday_rows.append(
                {
                    "dt": pd.Timestamp(f"{date.date()} {value}"),
                    "open": 10.5,
                    "high": 10.7,
                    "low": low,
                    "close": 10.5,
                    "vol": 1_000_000.0,
                }
            )
    intraday = pd.DataFrame(intraday_rows)
    target = pd.Series([0.0, 1.0, 1.0, 0.0, 0.0], index=dates)
    limits = pd.Series([9.9, 10.0, 10.2, 10.0, 10.0], index=dates)

    result = simulate_limit_policy(daily, intraday, target, limits)

    assert result.daily_state.loc[dates[2], "actual_position"] == 0.0
    assert result.daily_state.loc[dates[3], "actual_position"] == 1.0
    assert result.daily_state.loc[dates[4], "actual_position"] == 0.0
    assert result.orders["side"].tolist() == ["Buy", "Sell"]
    assert result.orders.iloc[0]["execution_date"] == pd.Timestamp("2026-01-07 10:30")
    assert result.orders.iloc[0]["price"] == pytest.approx(10.2)
    assert result.cycles.iloc[0]["wait_sessions"] == 2
    assert bool(result.cycles.iloc[0]["filled"])


def test_execution_policy_selector_enforces_service_level_then_price() -> None:
    from czsc_trader.execution_policy import select_execution_candidate

    candidates = pd.DataFrame(
        [
            {
                "candidate_id": "cheap",
                "t1_fill_rate": 0.89,
                "cap_p95": 0.001,
                "cap_mean": 0.0,
                "worst_calmar": 3.0,
                "worst_drawdown": -0.05,
                "family_rank": 0,
                "parameter": 0.0,
            },
            {
                "candidate_id": "fixed",
                "t1_fill_rate": 0.91,
                "cap_p95": 0.010,
                "cap_mean": 0.005,
                "worst_calmar": 1.0,
                "worst_drawdown": -0.10,
                "family_rank": 0,
                "parameter": 0.5,
            },
            {
                "candidate_id": "atr",
                "t1_fill_rate": 0.95,
                "cap_p95": 0.012,
                "cap_mean": 0.004,
                "worst_calmar": 2.0,
                "worst_drawdown": -0.08,
                "family_rank": 1,
                "parameter": 0.5,
            },
        ]
    )

    assert select_execution_candidate(candidates)["candidate_id"] == "fixed"


def test_execution_experiment_requires_frozen_policy_before_test_access(tmp_path: Path) -> None:
    runner_path = REPO_ROOT / "experiments" / "0902_EX02" / "run_experiment.py"
    spec = importlib.util.spec_from_file_location("execution_policy_experiment", runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    frozen = tmp_path / "frozen_execution_policy.json"

    with pytest.raises(ValueError, match="frozen execution policy"):
        module.assert_freeze_before_test(frozen, test_accessed=False)

    frozen.write_text('{"status":"FROZEN"}\n', encoding="utf-8")
    module.assert_freeze_before_test(frozen, test_accessed=False)
    with pytest.raises(ValueError, match="already accessed"):
        module.assert_freeze_before_test(frozen, test_accessed=True)


def test_experiment_archive_normalizes_python_line_endings(tmp_path: Path) -> None:
    from czsc_trader.experiment_archive import (
        build_experiment_manifest,
        validate_experiment_archive,
    )

    for name in ("01_goal.md", "02_design.md", "03_execution.md", "04_conclusion.md"):
        (tmp_path / name).write_text("document\n", encoding="utf-8")
    runner = tmp_path / "run_experiment.py"
    runner.write_bytes(b"print('research')\n")
    build_experiment_manifest(tmp_path, {"experiment_id": "test"})

    runner.write_bytes(b"print('research')\r\n")

    validate_experiment_archive(tmp_path)


def test_experiment_archive_ignores_runtime_python_cache(tmp_path: Path) -> None:
    from czsc_trader.experiment_archive import (
        build_experiment_manifest,
        validate_experiment_archive,
    )

    for name in ("01_goal.md", "02_design.md", "03_execution.md", "04_conclusion.md"):
        (tmp_path / name).write_text("document\n", encoding="utf-8")
    (tmp_path / "run_experiment.py").write_text("print('research')\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    bytecode = cache / "run_experiment.cpython-312.pyc"
    bytecode.write_bytes(b"first-runtime-cache")

    manifest = build_experiment_manifest(tmp_path, {"experiment_id": "test"})

    assert "__pycache__/run_experiment.cpython-312.pyc" not in manifest["files"]
    bytecode.write_bytes(b"changed-runtime-cache")
    validate_experiment_archive(tmp_path)


def test_robustness_cscv_uses_all_complementary_splits() -> None:
    from czsc_trader.robustness import contiguous_blocks, cscv_pbo

    blocks = contiguous_blocks(20, 10)
    assert len(blocks) == 10
    assert np.array_equal(np.sort(np.concatenate(blocks)), np.arange(20))

    index = pd.date_range("2025-01-01", periods=20, freq="D")
    returns = pd.DataFrame(
        {
            "candidate_0": np.tile([0.01, -0.002], 10),
            "candidate_1": np.r_[np.full(10, 0.02), np.full(10, -0.02)],
            "candidate_2": np.r_[np.full(10, -0.02), np.full(10, 0.02)],
        },
        index=index,
    )
    splits, summary = cscv_pbo(returns, block_count=10)

    assert len(splits) == 252
    assert summary["split_count"] == 252
    assert set(splits["selected_candidate"]) <= set(returns.columns)
    assert splits["validation_percentile"].between(0.0, 1.0).all()
    assert np.isfinite(splits["logit"]).all()

    tied = pd.DataFrame(
        {"10": np.tile([0.01, -0.002], 10), "2": np.tile([0.01, -0.002], 10)},
        index=index,
    )
    tied_splits, _ = cscv_pbo(tied, block_count=10)
    assert set(tied_splits["selected_candidate"]) == {"2"}


def test_robustness_cyclic_shifts_are_complete_and_unique() -> None:
    from czsc_trader.robustness import cyclic_shifts, placebo_signal_paths

    source = pd.Series([0, 0, 1, 1, 0], index=pd.date_range("2026-01-01", periods=5))
    shifts = cyclic_shifts(source)

    assert len(shifts) == 4
    assert len({tuple(item.tolist()) for item in shifts}) == 4
    assert all(sorted(item.tolist()) == sorted(source.tolist()) for item in shifts)
    assert all(item.index.equals(source.index) for item in shifts)
    with pytest.raises(ValueError, match="unique"):
        cyclic_shifts(pd.Series([1, 1, 1]))

    paths = placebo_signal_paths(source, initial_target=1.0)
    assert paths[0][0] == 0
    assert paths[0][1].equals(source)
    assert all(initial == 1.0 for _, _, initial in paths)


def test_robustness_deflated_sharpe_increases_with_selected_sharpe() -> None:
    from czsc_trader.robustness import candidate_sharpes, deflated_sharpe_ratio
    from scipy.stats import norm

    index = pd.date_range("2021-01-01", periods=500, freq="B")
    low = pd.Series(np.tile([0.003, -0.002], 250), index=index)
    high = pd.Series(np.tile([0.006, -0.002], 250), index=index)
    trials = pd.Series(np.linspace(0.1, 1.1, 625))

    matrix = pd.DataFrame({"low": low, "high": high})
    same_scale = candidate_sharpes(matrix)
    assert same_scale["low"] == pytest.approx(
        deflated_sharpe_ratio(low, same_scale)["observed_sharpe"]
    )
    identical_trials = pd.Series([0.2, 0.4, 0.6, 0.8])
    expected_standard_max = (
        (1.0 - 0.5772156649015329)
        * norm.ppf(1.0 - 1.0 / len(identical_trials))
        + 0.5772156649015329
        * norm.ppf(
            1.0 - 1.0 / (len(identical_trials) * np.e)
        )
    )
    formula = deflated_sharpe_ratio(high, identical_trials)
    assert formula["expected_max_sharpe"] == pytest.approx(
        identical_trials.std(ddof=1) * expected_standard_max
    )

    low_result = deflated_sharpe_ratio(low, trials)
    high_result = deflated_sharpe_ratio(high, trials)

    required = {
        "observations",
        "trial_count",
        "observed_sharpe",
        "expected_max_sharpe",
        "skew",
        "pearson_kurtosis",
        "dsr_probability",
    }
    assert required <= set(low_result)
    assert all(np.isfinite(float(low_result[key])) for key in required)
    assert high_result["dsr_probability"] > low_result["dsr_probability"]


def test_robustness_parameter_geometry_finds_grid_neighbors() -> None:
    from czsc_trader.robustness import parameter_geometry

    rows = []
    candidate_id = 0
    for first in (0.5, 0.75, 1.0):
        for second in (0.5, 0.75, 1.0):
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "first": first,
                    "second": second,
                    "sharpe": first + second,
                }
            )
            candidate_id += 1
    candidates = pd.DataFrame(rows)
    selected_id = int(
        candidates.loc[
            candidates["first"].eq(0.75) & candidates["second"].eq(0.75),
            "candidate_id",
        ].iloc[0]
    )

    surface, neighbors = parameter_geometry(
        candidates, selected_id, ("first", "second")
    )

    assert len(surface) == 9
    assert (surface["manhattan_distance"] == 1.0).sum() == 4
    assert set(neighbors["manhattan_distance"]) == {1.0, 2.0}
    assert len(neighbors.loc[neighbors["manhattan_distance"].eq(1.0)]) == 4


def _run_cli_json(*arguments: str) -> dict[str, object]:
    completed = subprocess.run(
        [str(CLI), *arguments, "--repo-root", str(REPO_ROOT), "--format", "json"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def test_cli_exposes_only_supported_resources() -> None:
    completed = subprocess.run(
        [str(CLI), "--help"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    resource_line = next(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith("{") and line.strip().endswith("}")
    )
    assert resource_line == "{data,baseline,backtest,advice,archive}"
    assert "experiment" not in completed.stdout


def test_dataflows_package_imports_outside_the_checkout(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import dataflows; "
                "print(Path(dataflows.__file__).resolve())"
            ),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    module_path = Path(completed.stdout.strip())
    assert module_path.is_relative_to(REPO_ROOT / "packages" / "dataflows")


@pytest.mark.parametrize("symbol", TRACKED_SYMBOLS)
def test_tracked_market_data_validates_through_latest_session(symbol: str) -> None:
    payload = _run_cli_json("data", "validate", "--symbol", symbol)

    assert payload["status"] == "PASS"
    result = payload["result"]
    assert result["symbol"] == symbol
    assert result["requested_end"] == "2026-09-01"
    assert result["validation_status"] == "PASS"
    assert result["frequencies"] == ["30m", "daily", "weekly"]


def test_active_baseline_is_candidate143() -> None:
    payload = _run_cli_json(
        "baseline",
        "show",
        "--version",
        "baseline_20260901",
        "--symbol",
        "588080.SH",
    )

    assert payload["status"] == "PASS"
    result = payload["result"]
    assert result["version"] == "baseline_20260901"
    assert result["strategy"] == "czsc_regime_weight"
    assert result["status"] == "active"
    assert result["rule"]["candidate_id"] == 143


def test_active_execution_policy_is_frozen_ex02_winner() -> None:
    from czsc_trader.execution_policies import resolve_execution_policy

    policy = resolve_execution_policy(
        REPO_ROOT / "configs" / "execution_policies",
        symbol="588080.SH",
        baseline_version="baseline_20260901",
        baseline_sha256="711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
        required=True,
    )

    assert policy.version == "execution_policy_20260902"
    assert policy.status == "active"
    assert policy.family == "fixed"
    assert policy.parameter == 0.0
    assert policy.warning_gap_q05 == pytest.approx(-0.006797902176638775)
    assert policy.source_path == "experiments/0902_EX02/artifacts/frozen_execution_policy.json"
    assert resolve_execution_policy(
        REPO_ROOT / "configs" / "execution_policies",
        symbol="159352.SZ",
        baseline_version="baseline_20260901",
        baseline_sha256="711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
    ) is None


def test_active_identities_resolve_after_cross_platform_lf_checkout(tmp_path: Path) -> None:
    from czsc_trader.baselines import resolve_baseline
    from czsc_trader.execution_policies import resolve_execution_policy

    relative_files = (
        "configs/rule_baselines/registry.json",
        "configs/rule_baselines/baseline_20260823.json",
        "configs/rule_baselines/baseline_20260826.json",
        "configs/rule_baselines/baseline_20260901.json",
        "configs/execution_policies/registry.json",
        "configs/execution_policies/execution_policy_20260902.json",
        "experiments/0824_EX04/artifacts/frozen_challenger.json",
        "experiments/0901_EX20/artifacts/frozen_challenger.json",
        "experiments/0902_EX02/artifacts/frozen_execution_policy.json",
    )
    for relative in relative_files:
        source = REPO_ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))

    baseline = resolve_baseline(
        tmp_path / "configs" / "rule_baselines",
        "baseline_20260901",
        symbol="588080.SH",
    )
    policy = resolve_execution_policy(
        tmp_path / "configs" / "execution_policies",
        symbol="588080.SH",
        baseline_version=baseline.version,
        baseline_sha256=baseline.sha256,
        required=True,
    )

    assert baseline.sha256 == "711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993"
    assert policy is not None
    assert policy.version == "execution_policy_20260902"


def test_daily_advice_maps_target_and_actual_position_to_manual_action() -> None:
    from czsc_trader.application.advice_service import build_advice
    from czsc_trader.execution_policies import resolve_execution_policy

    policy = resolve_execution_policy(
        REPO_ROOT / "configs" / "execution_policies",
        symbol="588080.SH",
        baseline_version="baseline_20260901",
        baseline_sha256="711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
        required=True,
    )
    assert policy is not None
    common = {
        "signal_date": pd.Timestamp("2026-09-01"),
        "close": 1.688,
        "quantity": 50_000,
        "policy": policy,
    }

    cash = build_advice(target_position=0, actual_position=0, **common)
    entry = build_advice(target_position=1, actual_position=0, **common)
    holding = build_advice(target_position=1, actual_position=1, **common)
    exit_advice = build_advice(target_position=0, actual_position=1, **common)

    assert (cash["state"], cash["action"]) == ("CASH", "WAIT")
    assert (entry["state"], entry["action"]) == ("PENDING_ENTRY", "BUY")
    assert entry["order"]["order_type"] == "限价委托"
    assert entry["order"]["maximum_buy_price"] == pytest.approx(1.688)
    assert entry["order"]["low_open_warning_price"] == pytest.approx(1.676)
    assert entry["order"]["valid_for"] == "NEXT_TRADING_SESSION"
    assert (holding["state"], holding["action"]) == ("HOLDING", "HOLD")
    assert (exit_advice["state"], exit_advice["action"]) == ("PENDING_EXIT", "SELL")
    assert exit_advice["order"]["primary_order_type"] == "限价委托"
    assert "券商显示的当日合法价格下限" in exit_advice["order"]["price_instruction"]

    with pytest.raises(ValueError, match="100-share lots"):
        build_advice(target_position=1, actual_position=0, quantity=50_050, **{
            key: value for key, value in common.items() if key != "quantity"
        })


def test_installed_cli_generates_stateless_daily_advice() -> None:
    payload = _run_cli_json(
        "advice",
        "run",
        "--symbol",
        "588080.SH",
        "--asset",
        "etf",
        "--actual-position",
        "1",
        "--quantity",
        "50000",
    )

    assert payload["status"] == "PASS"
    result = payload["result"]
    assert result["data_cutoff"] == "2026-09-01"
    assert result["baseline"]["version"] == "baseline_20260901"
    assert result["execution_policy"]["version"] == "execution_policy_20260902"
    assert result["actual_position"] == 1
    assert result["quantity"] == 50000
    assert result["valid_for"] == "NEXT_TRADING_SESSION"
    assert result["confirmation_rule"] == "未收到明确成交回报时，实际仓位保持不变。"


def test_all_frozen_experiment_archives_validate() -> None:
    payload = _run_cli_json("archive", "validate", "--all")

    assert payload["status"] == "PASS"
    assert payload["result"]["validated_count"] == 48
    assert payload["result"]["experiments"][-3:] == [
        "0901_EX21",
        "0902_EX01",
        "0902_EX02",
    ]


def test_installed_cli_runs_audited_backtest(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            str(CLI),
            "backtest",
            "run",
            "--symbol",
            "588080.SH",
            "--asset",
            "etf",
            "--start",
            "2026-01-01",
            "--end",
            "2026-08-21",
            "--outputs-root",
            str(tmp_path),
            "--repo-root",
            str(REPO_ROOT),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    output_dir = Path(payload["artifacts"]["output_dir"])
    full = payload["result"]["windows"]["full"]
    assert output_dir.name.startswith("588080_")
    assert output_dir.name.endswith("_BT01")
    assert payload["result"]["baseline"] == "baseline_20260901"
    assert set(full) == {"start", "end", "strategies"}
    strategies = full["strategies"]
    assert set(strategies) == {
        "active_baseline",
        "active_baseline_execution_policy",
        "buyhold",
        "ma5_ma20",
    }
    assert all(set(metrics) == STRATEGY_METRIC_KEYS for metrics in strategies.values())
    assert strategies["active_baseline"]["return"] == pytest.approx(
        0.7528525916956634
    )
    assert strategies["ma5_ma20"] == pytest.approx(
        {
            "max_drawdown": -0.22945460734778733,
            "calmar": 1.520558369439513,
            "win_loss_ratio": 3.2767930702460384,
            "return": 0.20069278199087237,
            "sharpe": 1.234024171393839,
        }
    )
    assert strategies["buyhold"]["win_loss_ratio"] is None

    assert (output_dir / "audit.json").is_file()
    assert (output_dir / "report.md").is_file()
    assert (output_dir / "chart.html").is_file()
    assert (output_dir / "ma_signals.csv").is_file()
    assert (output_dir / "ma_orders.csv").is_file()
    assert (output_dir / "ma_equity.csv").is_file()
    assert (output_dir / "ma_chart.html").is_file()
    assert (output_dir / "execution_orders.csv").is_file()
    assert (output_dir / "execution_equity.csv").is_file()

    ma_signals = pd.read_csv(output_dir / "ma_signals.csv", parse_dates=["dt"])
    assert list(ma_signals.columns) == ["dt", "close", "ma5", "ma20", "target_position"]
    first_session = ma_signals.loc[ma_signals["dt"] == "2026-01-05"].iloc[0]
    assert first_session["target_position"] == 1.0
    assert first_session["ma5"] == pytest.approx(1.40065584)
    assert first_session["ma20"] == pytest.approx(1.37753371)

    ma_orders = pd.read_csv(
        output_dir / "ma_orders.csv",
        parse_dates=["signal_date", "execution_date"],
    )
    assert ma_orders.iloc[0]["side"] == "Buy"
    assert ma_orders.iloc[0]["signal_date"] == pd.Timestamp("2025-12-31")
    assert ma_orders.iloc[0]["execution_date"] == pd.Timestamp("2026-01-05")
    sessions = ma_signals["dt"].tolist()
    next_session = dict(zip(sessions, sessions[1:]))
    for order in ma_orders.itertuples():
        assert next_session[order.signal_date] == order.execution_date

    report = (output_dir / "report.md").read_text(encoding="utf-8")
    assert "## full" in report
    assert report.count("| 活动基线·次日开盘 |") == 1
    assert report.count("| 活动基线·执行规则 |") == 1
    assert report.count("| BuyHold |") == 1
    assert report.count("| MA5/MA20 |") == 1
    assert "| 策略 | 最大回撤 | 卡玛比率 | 盈亏比 | 收益率 | 夏普率 |" in report

    ma_chart = (output_dir / "ma_chart.html").read_text(encoding="utf-8")
    assert all(
        label in ma_chart
        for label in ("MA5", "MA20", "MA\\u4e70\\u5165", "MA\\u5356\\u51fa")
    )
    assert '"hovermode":"x unified"' in ma_chart
