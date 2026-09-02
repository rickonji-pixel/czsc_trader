from __future__ import annotations

from pathlib import Path

import pytest

from czsc_trader.application.baseline_service import show_baseline
from czsc_trader.application.context import RepositoryContext
from czsc_trader.application.data_service import validate_data
from czsc_trader.cli.main import build_parser


REPO_ROOT = Path(__file__).resolve().parents[1]
TRACKED_SYMBOLS = ("159352.SZ", "159516.SZ", "515050.SH", "588080.SH")


def test_cli_exposes_only_supported_resources() -> None:
    help_text = build_parser().format_help()
    resource_line = next(
        line.strip()
        for line in help_text.splitlines()
        if line.strip().startswith("{") and line.strip().endswith("}")
    )
    assert resource_line == "{data,baseline,backtest,advice,archive}"
    assert "experiment" not in help_text


def test_data_and_advice_accept_explicit_runtime_data_directory(tmp_path: Path) -> None:
    parser = build_parser()
    data = parser.parse_args(
        [
            "data", "prepare", "--symbol", "588080.SH", "--asset", "etf",
            "--start", "2020-01-01", "--end", "2026-09-02", "--data-dir", str(tmp_path),
        ]
    )
    advice = parser.parse_args(
        [
            "advice", "run", "--symbol", "588080.SH", "--asset", "etf",
            "--actual-quantity", "0", "--position-size", "50000", "--data-dir", str(tmp_path),
        ]
    )
    assert data.data_dir == tmp_path
    assert advice.data_dir == tmp_path


def test_all_tracked_market_data_validates_through_latest_session() -> None:
    context = RepositoryContext.discover(REPO_ROOT)
    for symbol in TRACKED_SYMBOLS:
        result = validate_data(context, symbol).result

        assert result["symbol"] == symbol
        assert result["requested_end"] == "2026-09-02"
        assert result["validation_status"] == "PASS"
        assert result["frequencies"] == ["30m", "daily", "weekly"]


def test_active_baseline_is_complete_candidate143() -> None:
    result = show_baseline(
        RepositoryContext.discover(REPO_ROOT),
        "baseline_20260903",
        symbol="588080.SH",
    )

    assert result.status == "PASS"
    payload = result.result
    assert payload["version"] == "baseline_20260903"
    assert payload["strategy"] == "czsc_regime_weight"
    assert payload["status"] == "active"
    assert payload["rule"]["candidate_id"] == 143
    execution = payload["rule"]["execution"]
    assert execution["instrument"] == {
        "symbol": "588080.SH",
        "market": "CN",
        "asset_type": "etf",
        "price_tick": 0.001,
        "lot_size": 100,
        "maximum_order_quantity": 1_000_000,
        "price_limit_ratio": 0.2,
    }
    assert execution["capital"]["target_scope"] == "entry_cycle"
    assert execution["virtual_fill"]["touch_only"] == "uncertain_unfilled"


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
    assert policy.exit_limit_ratio == pytest.approx(0.2)
    assert policy.exit_price_rounding == "nearest_half_up"
    assert policy.source_path == "experiments/0902_EX02/artifacts/frozen_execution_policy.json"
    assert resolve_execution_policy(
        REPO_ROOT / "configs" / "execution_policies",
        symbol="159352.SZ",
        baseline_version="baseline_20260901",
        baseline_sha256="711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993",
    ) is None


def test_production_services_do_not_resolve_independent_execution_policy() -> None:
    for relative in (
        "src/czsc_trader/application/advice_service.py",
        "src/czsc_trader/backtest_runner.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "resolve_execution_policy(" not in source
