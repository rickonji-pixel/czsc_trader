from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from czsc_trader.application.errors import ExecutionError, UsageError
from czsc_trader.news_events.maas import MaaSResponse, MaaSSettings, TencentMaaSClient
from czsc_trader.news_events.service import NewsExtractionCommand, run_news_extraction


class FakeGateway:
    settings = MaaSSettings(
        endpoint="https://maas.example/v1/chat/completions",
        api_key="secret-not-for-artifacts",
        model="deepseek/deepseek-flash",
    )

    def __init__(self, outputs: dict[str, dict[str, object]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, object]] = []

    def complete(self, request_payload: dict[str, object]) -> MaaSResponse:
        self.calls.append(request_payload)
        article = json.loads(request_payload["messages"][1]["content"])["article"]  # type: ignore[index]
        output = self.outputs[article["sample_id"]]
        return MaaSResponse(
            payload={
                "id": f"response-{article['sample_id']}",
                "choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
            request_id=f"request-{article['sample_id']}",
        )


def _scope(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope_id": "TEST-SCOPE-v1",
                "target": {"symbol": "588080.SH", "name": "科创50ETF"},
                "relevance_definition": "提取半导体产业新增事实",
                "include": ["半导体订单"],
                "exclude": ["行情复述"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_ft_t09_news_extraction_is_resumable_auditable_and_fails_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "articles.csv.gz"
    pd.DataFrame(
        [
            {
                "sample_id": "A-001",
                "pub_time": "2026-08-18 09:00:00",
                "src": "新浪财经",
                "title": "公司获得AI芯片订单",
                "content": "<p>甲公司公告新获得十亿元AI<span>芯片</span>订单，预计明年交付。</p>",
            },
            {
                "sample_id": "A-002",
                "pub_time": "2026-08-18 09:01:00",
                "src": "新浪财经",
                "title": "市场午后上涨",
                "content": "午后市场上涨，多个板块活跃。",
            },
        ]
    ).to_csv(source, index=False, compression="gzip")
    scope = _scope(tmp_path / "scope.json")
    output = tmp_path / "output"
    gateway = FakeGateway(
        {
            "A-001": {
                "relevance": "CATALYST_RELEVANT",
                "review_reason": "包含新增订单事实",
                "relevance_evidence": "新获得十亿元AI芯片订单",
                "events": [
                    {
                        "primary_entity": "甲公司",
                        "event_type": "ORDER_DEMAND",
                        "direction": "POSITIVE",
                        "event_time_text": "明年",
                        "event_summary": "获得十亿元AI芯片订单",
                        "evidence_excerpt": "甲公司公告新获得十亿元AI芯片订单",
                    }
                ],
            },
            "A-002": {
                "relevance": "NOT_CATALYST",
                "review_reason": "只有行情复述",
                "relevance_evidence": "",
                "events": [],
            },
        }
    )
    command = NewsExtractionCommand(source, scope, output)

    result = run_news_extraction(command, gateway=gateway)

    assert result.result == {
        "article_count": 2,
        "relevant_count": 1,
        "unresolved_count": 0,
        "event_count": 1,
        "reused_count": 0,
    }
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "PASS"
    assert manifest["completed_count"] == 2
    assert "secret-not-for-artifacts" not in "".join(
        path.read_text(encoding="utf-8") for path in output.rglob("*.json*")
    )
    assert len(gateway.calls) == 2

    gateway.calls.clear()
    resumed = run_news_extraction(command, gateway=gateway)
    assert resumed.result["reused_count"] == 2
    assert gateway.calls == []

    bad_gateway = FakeGateway(
        {
            "A-001": {
                "relevance": "CATALYST_RELEVANT",
                "review_reason": "虚构证据",
                "relevance_evidence": "原文没有这句话",
                "events": [],
            }
        }
    )
    with pytest.raises(ExecutionError, match="1 of 1 articles failed") as raised:
        run_news_extraction(
            NewsExtractionCommand(source, scope, tmp_path / "bad-output", limit=1),
            gateway=bad_gateway,
        )
    assert raised.value.code == "news_extraction_incomplete"
    failed_manifest = json.loads(
        (tmp_path / "bad-output" / "manifest.json").read_text(encoding="utf-8")
    )
    assert failed_manifest["status"] == "PARTIAL"
    assert failed_manifest["failure_count"] == 1
    assert failed_manifest["failures"][0]["code"] == "news_model_output_invalid"
    failed_audit = next((tmp_path / "bad-output" / "audit").glob("*.json"))
    failed_record = json.loads(failed_audit.read_text(encoding="utf-8"))
    assert failed_record["request_id"] == "request-A-001"
    assert failed_record["response"]["id"] == "response-A-001"


def test_ft_t10_news_maas_settings_use_process_env_then_local_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CZSC_NEWS_MAAS_ENDPOINT=deprecated-value-must-be-ignored\n"
        "CZSC_NEWS_MAAS_API_URL=https://file.example/v1/chat/completions?token=hidden\n"
        "CZSC_NEWS_MAAS_API_KEY=file-secret\n"
        "CZSC_NEWS_MAAS_MODEL=file-model\n",
        encoding="utf-8",
    )
    for name in (
        "CZSC_NEWS_MAAS_ENDPOINT",
        "CZSC_NEWS_MAAS_API_URL",
        "CZSC_NEWS_MAAS_API_KEY",
        "CZSC_NEWS_MAAS_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    from_file = MaaSSettings.from_environment(env_file)
    assert from_file.api_key == "file-secret"
    assert from_file.model == "file-model"
    assert from_file.endpoint_identity == "https://file.example/v1/chat/completions"

    monkeypatch.setenv("CZSC_NEWS_MAAS_API_URL", "https://process.example/v1/chat/completions")
    monkeypatch.setenv("CZSC_NEWS_MAAS_API_KEY", "process-secret")
    monkeypatch.setenv("CZSC_NEWS_MAAS_MODEL", "ep-process")
    from_process = MaaSSettings.from_environment(env_file)
    assert from_process.endpoint == "https://process.example/v1/chat/completions"
    assert from_process.api_key == "process-secret"
    assert from_process.model == "ep-process"

    env_file.unlink()
    for name in (
        "CZSC_NEWS_MAAS_API_URL",
        "CZSC_NEWS_MAAS_API_KEY",
        "CZSC_NEWS_MAAS_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(UsageError, match="missing environment variables"):
        MaaSSettings.from_environment(env_file)


def test_ft_t11_news_maas_http_adapter_uses_chat_completions_contract() -> None:
    captured: dict[str, object] = {}

    class Response:
        headers = {"x-tc-requestid": "req-001"}

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": '{"relevance":"NOT_CATALYST"}'}}]}
            ).encode("utf-8")

    def opener(request, *, timeout: float):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    settings = MaaSSettings(
        endpoint="https://maas.example/v1/chat/completions",
        api_key="adapter-secret",
        model="deepseek/deepseek-flash",
    )
    response = TencentMaaSClient(settings, opener=opener).complete(
        {"model": settings.model, "messages": []}
    )

    assert response.request_id == "req-001"
    assert captured == {
        "url": "https://maas.example/v1/chat/completions",
        "authorization": "Bearer adapter-secret",
        "body": {"model": "deepseek/deepseek-flash", "messages": []},
        "timeout": 90.0,
    }
