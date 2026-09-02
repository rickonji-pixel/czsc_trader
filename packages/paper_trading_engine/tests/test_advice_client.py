from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest


def payload(*, order: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "status": "PASS",
        "command": "advice.run",
        "result": {
            "contract_version": "advice.v1",
            "decision_id": "DEC-ABC123",
            "symbol": "588080.SH",
            "signal_date": "2026-09-01",
            "valid_session": "2026-09-02",
            "actual_quantity": 0,
            "target_quantity": 0 if order is None else 50_000,
            "position_size": 50_000,
            "delta_quantity": 0 if order is None else 50_000,
            "action": "WAIT" if order is None else "BUY",
            "baseline": {"version": "baseline_20260901", "sha256": "b" * 64},
            "execution_policy": {"version": "execution_policy_20260902", "sha256": "e" * 64},
            "signal_reference_price": 1.7043736,
            "execution_reference_price": 1.688,
            "data_cutoff": "2026-09-01",
            "order": order,
        },
    }


def test_advice_decision_parses_wait_and_limit_order() -> None:
    from paper_trading_engine.contracts import AdviceDecision

    wait = AdviceDecision.from_cli_payload(payload())
    buy = AdviceDecision.from_cli_payload(
        payload(
            order={
                "side": "BUY",
                "quantity": 50_000,
                "order_type": "LIMIT",
                "limit_price": 1.688,
                "time_in_force": "DAY",
            }
        )
    )

    assert wait.order is None
    assert buy.order is not None
    assert buy.order.limit_price == 1.688
    assert buy.order.quantity == 50_000


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["result"].update(contract_version="advice.v2"), "version"),
        (lambda value: value["result"]["order"].update(limit_price="1.688"), "price"),
        (lambda value: value["result"]["order"].update(quantity=50), "100-share"),
        (lambda value: value.update(status="FAIL"), "failed"),
    ],
)
def test_advice_decision_rejects_invalid_contract(mutation, message: str) -> None:
    from paper_trading_engine.contracts import AdviceContractError, AdviceDecision

    value = payload(
        order={
            "side": "BUY",
            "quantity": 50_000,
            "order_type": "LIMIT",
            "limit_price": 1.688,
            "time_in_force": "DAY",
        }
    )
    mutation(value)
    with pytest.raises(AdviceContractError, match=message):
        AdviceDecision.from_cli_payload(value)


def test_cli_advice_client_uses_shell_free_explicit_arguments(tmp_path: Path) -> None:
    from paper_trading_engine.advice_client import CliAdviceClient

    calls = []

    def runner(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return CompletedProcess(arguments, 0, json.dumps(payload()), "")

    client = CliAdviceClient(
        executable=Path("C:/tools/czsc-trader.exe"),
        repo_root=tmp_path,
        data_dir=tmp_path / "runtime-data",
        symbol="588080.SH",
        asset="etf",
        position_size=50_000,
        timeout_seconds=15,
        runner=runner,
    )
    decision = client.get_decision(actual_quantity=0)

    assert decision.decision_id == "DEC-ABC123"
    arguments, options = calls[0]
    assert arguments == [
        "C:\\tools\\czsc-trader.exe",
        "advice",
        "run",
        "--symbol",
        "588080.SH",
        "--asset",
        "etf",
        "--actual-quantity",
        "0",
        "--position-size",
        "50000",
        "--repo-root",
        str(tmp_path.resolve()),
        "--data-dir",
        str((tmp_path / "runtime-data").resolve()),
        "--format",
        "json",
    ]
    assert options["shell"] is False
    assert options["timeout"] == 15
    assert options["encoding"] == "utf-8"


def test_cli_advice_client_rejects_process_failures_and_noisy_stdout(tmp_path: Path) -> None:
    from paper_trading_engine.advice_client import AdviceClientError, CliAdviceClient

    def make_client(runner):
        return CliAdviceClient(
            executable=Path("czsc-trader"),
            repo_root=tmp_path,
            data_dir=tmp_path / "runtime-data",
            symbol="588080.SH",
            asset="etf",
            position_size=50_000,
            runner=runner,
        )

    with pytest.raises(AdviceClientError, match="exit code 5"):
        make_client(lambda args, **kwargs: CompletedProcess(args, 5, "{}", "failed")).get_decision(0)
    with pytest.raises(AdviceClientError, match="single JSON"):
        make_client(
            lambda args, **kwargs: CompletedProcess(args, 0, "log\n" + json.dumps(payload()), "")
        ).get_decision(0)

    def timeout_runner(args, **kwargs):
        raise TimeoutExpired(args, kwargs["timeout"])

    with pytest.raises(AdviceClientError, match="timed out"):
        make_client(timeout_runner).get_decision(0)
