from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Protocol

import pandas as pd

from czsc_trader.application.errors import ExecutionError, ValidationError
from czsc_trader.application.results import CommandResult
from czsc_trader.identity import raw_file_sha256

from .maas import MaaSResponse, MaaSSettings, TencentMaaSClient, extract_message_json
from .models import ExtractedEvent, NewsArticle, NewsReview, dedupe_group_key, stable_json_sha256
from .prompt import PROMPT_VERSION, build_request


class MaaSGateway(Protocol):
    settings: MaaSSettings

    def complete(self, request_payload: dict[str, object]) -> MaaSResponse: ...


@dataclass(frozen=True)
class NewsExtractionCommand:
    input_path: Path
    scope_path: Path
    output_dir: Path
    limit: int | None = None
    workers: int = 1
    env_file: Path | None = None


def _read_scope(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("news_scope_invalid", f"cannot read news scope: {exc}") from exc
    required = {"schema_version", "scope_id", "target", "relevance_definition", "include", "exclude"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ValidationError(
            "news_scope_invalid",
            f"news scope missing fields: {sorted(required.difference(payload if isinstance(payload, dict) else {}))}",
        )
    if payload["schema_version"] != 1 or not str(payload["scope_id"]).strip():
        raise ValidationError("news_scope_invalid", "unsupported or empty news scope identity")
    if not isinstance(payload["include"], list) or not isinstance(payload["exclude"], list):
        raise ValidationError("news_scope_invalid", "include and exclude must be arrays")
    return payload


def _read_articles(path: Path, limit: int | None) -> list[NewsArticle]:
    if limit is not None and limit <= 0:
        raise ValidationError("news_input_invalid", "limit must be positive")
    try:
        frame = pd.read_csv(path, keep_default_na=False)
    except (OSError, ValueError) as exc:
        raise ValidationError("news_input_invalid", f"cannot read article cache: {exc}") from exc
    if limit is not None:
        frame = frame.head(limit)
    try:
        articles = [NewsArticle.from_mapping(dict(row)) for row in frame.to_dict("records")]
    except ValueError as exc:
        raise ValidationError("news_input_invalid", str(exc)) from exc
    identities = [article.sample_id for article in articles]
    if not articles:
        raise ValidationError("news_input_invalid", "article cache is empty")
    if len(identities) != len(set(identities)):
        raise ValidationError("news_input_invalid", "sample_id must be unique")
    return articles


def _safe_identity(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", value)[:100] or "article"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)


def _existing_pass(
    path: Path,
    *,
    article: NewsArticle,
    scope_sha256: str,
    model: str,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload.get("status") == "PASS"
            and payload.get("article_sha256") == article.article_sha256
            and payload.get("scope_sha256") == scope_sha256
            and payload.get("prompt_version") == PROMPT_VERSION
            and payload.get("model") == model
        ):
            NewsReview.parse(payload["review"], article)
            return payload
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def run_news_extraction(
    command: NewsExtractionCommand,
    *,
    gateway: MaaSGateway | None = None,
) -> CommandResult:
    input_path = command.input_path.resolve()
    scope_path = command.scope_path.resolve()
    output_dir = command.output_dir.resolve()
    scope = _read_scope(scope_path)
    articles = _read_articles(input_path, command.limit)
    if type(command.workers) is not int or not 1 <= command.workers <= 8:
        raise ValidationError("news_input_invalid", "workers must be an integer from 1 to 8")
    settings = (
        gateway.settings
        if gateway is not None
        else MaaSSettings.from_environment(command.env_file)
    )
    client: MaaSGateway = gateway or TencentMaaSClient(settings)
    scope_sha256 = stable_json_sha256(scope)
    input_sha256 = raw_file_sha256(input_path)
    audit_dir = output_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    def extract_one(
        article: NewsArticle,
    ) -> tuple[dict[str, object] | None, dict[str, object] | None, bool]:
        audit_path = audit_dir / f"{_safe_identity(article.sample_id)}-{article.article_sha256[:12]}.json"
        existing = _existing_pass(
            audit_path,
            article=article,
            scope_sha256=scope_sha256,
            model=settings.model,
        )
        if existing is not None:
            return existing, None, True
        request_payload = build_request(article, scope, model=settings.model)
        started_at = datetime.now().astimezone().isoformat()
        response: MaaSResponse | None = None
        try:
            response = client.complete(request_payload)
            review = NewsReview.parse(extract_message_json(response), article)
            record: dict[str, object] = {
                "schema_version": 1,
                "status": "PASS",
                "sample_id": article.sample_id,
                "article_sha256": article.article_sha256,
                "scope_id": scope["scope_id"],
                "scope_sha256": scope_sha256,
                "prompt_version": PROMPT_VERSION,
                "model": settings.model,
                "started_at": started_at,
                "completed_at": datetime.now().astimezone().isoformat(),
                "request_id": response.request_id,
                "request": request_payload,
                "response": response.payload,
                "review": review.to_dict(),
            }
            _write_json(audit_path, record)
            return record, None, False
        except (ExecutionError, ValueError) as exc:
            code = exc.code if isinstance(exc, ExecutionError) else "news_model_output_invalid"
            message = exc.message if isinstance(exc, ExecutionError) else str(exc)
            failure = {
                "sample_id": article.sample_id,
                "code": code,
                "message": message,
            }
            if isinstance(exc, ExecutionError) and exc.context:
                failure["context"] = exc.context
            failure_record: dict[str, object] = {
                "schema_version": 1,
                "status": "FAIL",
                "sample_id": article.sample_id,
                "article_sha256": article.article_sha256,
                "scope_id": scope["scope_id"],
                "scope_sha256": scope_sha256,
                "prompt_version": PROMPT_VERSION,
                "model": settings.model,
                "started_at": started_at,
                "completed_at": datetime.now().astimezone().isoformat(),
                "request": request_payload,
                "error": failure,
            }
            if response is not None:
                failure_record["request_id"] = response.request_id
                failure_record["response"] = response.payload
            _write_json(audit_path, failure_record)
            return None, failure, False

    if command.workers == 1:
        outcomes = [extract_one(article) for article in articles]
    else:
        with ThreadPoolExecutor(max_workers=command.workers) as executor:
            outcomes = list(executor.map(extract_one, articles))
    audit_records = [record for record, _, _ in outcomes if record is not None]
    failures = [failure for _, failure, _ in outcomes if failure is not None]
    reused = sum(was_reused for _, _, was_reused in outcomes)

    reviews: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    for record in audit_records:
        review = dict(record["review"])  # type: ignore[arg-type]
        event_items = list(review.pop("events"))
        reviews.append(
            {
                "sample_id": record["sample_id"],
                "article_sha256": record["article_sha256"],
                **review,
            }
        )
        for index, event in enumerate(event_items, start=1):
            parsed_event = ExtractedEvent(**event)
            event_row = {
                "event_id": f"{record['sample_id']}-E{index:02d}",
                "sample_id": record["sample_id"],
                "article_sha256": record["article_sha256"],
                "dedupe_group_key": dedupe_group_key(parsed_event),
                **event,
            }
            events.append(event_row)
    _write_jsonl(output_dir / "reviews.jsonl", reviews)
    _write_jsonl(output_dir / "events.jsonl", events)
    manifest = {
        "schema_version": 1,
        "status": "PASS" if not failures else "PARTIAL",
        "scope_id": scope["scope_id"],
        "scope_sha256": scope_sha256,
        "prompt_version": PROMPT_VERSION,
        "model": settings.model,
        "endpoint": settings.endpoint_identity,
        "input_path": str(input_path),
        "input_sha256": input_sha256,
        "article_count": len(articles),
        "workers": command.workers,
        "completed_count": len(audit_records),
        "reused_count": reused,
        "failure_count": len(failures),
        "relevant_count": sum(row["relevance"] == "CATALYST_RELEVANT" for row in reviews),
        "unresolved_count": sum(row["relevance"] == "UNRESOLVED" for row in reviews),
        "event_count": len(events),
        "failures": failures,
    }
    _write_json(output_dir / "manifest.json", manifest)
    if failures:
        raise ExecutionError(
            "news_extraction_incomplete",
            f"news extraction incomplete: {len(failures)} of {len(articles)} articles failed",
            context={"output_dir": str(output_dir), "failures": failures[:20]},
        )
    return CommandResult(
        status="PASS",
        command="news.extract",
        result={
            "article_count": len(articles),
            "relevant_count": manifest["relevant_count"],
            "unresolved_count": manifest["unresolved_count"],
            "event_count": len(events),
            "reused_count": reused,
        },
        artifacts={
            "manifest": str(output_dir / "manifest.json"),
            "reviews": str(output_dir / "reviews.jsonl"),
            "events": str(output_dir / "events.jsonl"),
            "audit_dir": str(audit_dir),
        },
    )
