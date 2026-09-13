from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import dotenv_values

from czsc_trader.application.errors import ExecutionError, UsageError


DEFAULT_API_URL = "https://tokenhub.tencentmaas.com/v1/chat/completions"


@dataclass(frozen=True)
class MaaSSettings:
    endpoint: str
    api_key: str
    model: str
    timeout_seconds: float = 90.0

    @classmethod
    def from_environment(cls, env_file: Path | None = None) -> "MaaSSettings":
        file_values: dict[str, object] = {}
        if env_file is not None and env_file.is_file():
            file_values = dict(dotenv_values(env_file))

        def value(name: str, default: str = "") -> str:
            process_value = os.getenv(name)
            if process_value is not None:
                return process_value.strip()
            return str(file_values.get(name) or default).strip()

        endpoint = value("CZSC_NEWS_MAAS_API_URL", DEFAULT_API_URL)
        api_key = value("CZSC_NEWS_MAAS_API_KEY")
        model = value("CZSC_NEWS_MAAS_MODEL")
        missing = [
            name
            for name, value in (
                ("CZSC_NEWS_MAAS_API_KEY", api_key),
                ("CZSC_NEWS_MAAS_MODEL", model),
            )
            if not value
        ]
        if missing:
            raise UsageError(
                "news_maas_configuration_missing",
                f"missing environment variables: {', '.join(missing)}",
            )
        if not endpoint.startswith("https://") and not endpoint.startswith("http://127.0.0.1"):
            raise UsageError(
                "news_maas_endpoint_invalid",
                "MaaS API URL must use HTTPS",
            )
        if not model:
            raise UsageError("news_maas_model_missing", "MaaS model cannot be empty")
        return cls(endpoint=endpoint, api_key=api_key, model=model)

    @property
    def endpoint_identity(self) -> str:
        """Return an audit-safe endpoint identity without query or credentials."""

        parsed = urlsplit(self.endpoint)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


@dataclass(frozen=True)
class MaaSResponse:
    payload: dict[str, object]
    request_id: str


class TencentMaaSClient:
    def __init__(
        self,
        settings: MaaSSettings,
        *,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        self.settings = settings
        self._opener = opener

    def complete(self, request_payload: dict[str, object]) -> MaaSResponse:
        request = Request(
            self.settings.endpoint,
            data=(json.dumps(request_payload, ensure_ascii=False) + "\n").encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.settings.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                request_id = str(
                    response.headers.get("x-tc-requestid")
                    or response.headers.get("x-request-id")
                    or ""
                )
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ExecutionError(
                "news_maas_http_error",
                f"MaaS returned HTTP {exc.code}",
                context={"http_status": exc.code, "response_excerpt": body[:1000]},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ExecutionError(
                "news_maas_unreachable",
                f"MaaS request failed: {exc}",
            ) from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ExecutionError(
                "news_maas_response_invalid",
                "MaaS response is not valid JSON",
                context={"response_excerpt": body[:1000]},
            ) from exc
        if not isinstance(payload, dict):
            raise ExecutionError("news_maas_response_invalid", "MaaS response must be an object")
        return MaaSResponse(payload=payload, request_id=request_id)


def extract_message_json(response: MaaSResponse) -> object:
    try:
        choices = response.payload["choices"]
        content = choices[0]["message"]["content"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as exc:
        raise ExecutionError(
            "news_maas_response_invalid",
            "MaaS response does not contain choices[0].message.content",
            context={"request_id": response.request_id},
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise ExecutionError(
            "news_maas_response_invalid",
            "MaaS response content is empty",
            context={"request_id": response.request_id},
        )
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ExecutionError(
            "news_maas_output_invalid",
            "model output is not valid JSON",
            context={"request_id": response.request_id, "content_excerpt": content[:1000]},
        ) from exc
