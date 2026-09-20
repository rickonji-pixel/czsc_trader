from __future__ import annotations

import json

import pytest

from paper_trading_engine import srt_advice_client
from paper_trading_engine.srt_advice_client import AdviceClientError


def test_pte_rejects_pass_without_dfls_validation_contract(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(srt_advice_client, "verify_generation", lambda *_args: None)
    for name, payload in (
        ("510500_manifest.json", {}),
        ("510500_execution_manifest.json", {}),
        ("510500_validation.json", {"status": "PASS"}),
    ):
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdviceClientError, match="not DFLS-owned"):
        srt_advice_client._load_inputs(tmp_path, "510500.SH")


def test_pte_rejects_pass_with_unresolved_findings(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(srt_advice_client, "verify_generation", lambda *_args: None)
    for name, payload in (
        ("510500_manifest.json", {}),
        ("510500_execution_manifest.json", {}),
        (
            "510500_validation.json",
            {
                "status": "PASS",
                "contract": "dfls.history.v1",
                "findings": [{"code": "TEST"}],
            },
        ),
    ):
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdviceClientError, match="unresolved findings"):
        srt_advice_client._load_inputs(tmp_path, "510500.SH")
