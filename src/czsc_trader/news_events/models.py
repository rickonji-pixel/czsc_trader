from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from html.parser import HTMLParser
import json
import re
from typing import Any


RELEVANCE_VALUES = frozenset({"CATALYST_RELEVANT", "NOT_CATALYST", "UNRESOLVED"})
DIRECTION_VALUES = frozenset({"POSITIVE", "NEGATIVE", "AMBIGUOUS"})
EVENT_TYPE_VALUES = frozenset(
    {
        "POLICY_REGULATION",
        "EARNINGS_CHANGE",
        "ORDER_DEMAND",
        "PRODUCT_RELEASE",
        "CAPEX",
        "M_AND_A",
        "CAPITAL_ALLOCATION",
        "SUPPLY_DISRUPTION",
        "PRICE_CHANGE",
        "INDUSTRY_DATA",
        "TECHNOLOGY_BREAKTHROUGH",
        "SANCTION_EXPORT_CONTROL",
        "OTHER",
    }
)


def _canonical_article_sha(source: str, published_at: str, title: str, content: str) -> str:
    return sha256(
        f"{source}\n{published_at}\n{title}\n{content}".encode("utf-8")
    ).hexdigest()


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("“", '"').replace("”", '"')


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.parts.append(data.strip())


def visible_text(value: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(value)
    parser.close()
    return "\n".join(parser.parts)


@dataclass(frozen=True)
class NewsArticle:
    sample_id: str
    published_at: str
    source: str
    title: str
    content: str
    article_sha256: str

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "NewsArticle":
        required = {"sample_id", "pub_time", "src", "title", "content"}
        missing = sorted(required.difference(row))
        if missing:
            raise ValueError(f"article missing fields: {missing}")
        values = {name: str(row[name]).strip() for name in required}
        if any(not values[name] for name in required):
            empty = sorted(name for name in required if not values[name])
            raise ValueError(f"article has empty fields: {empty}")
        published_at = values["pub_time"]
        canonical_sha = _canonical_article_sha(
            values["src"], published_at, values["title"], values["content"]
        )
        supplied_sha = str(row.get("raw_sha256", "")).strip().lower()
        if supplied_sha and supplied_sha != canonical_sha:
            # Historical caches may serialize timestamps without the ISO "T" separator.
            alternate = _canonical_article_sha(
                values["src"], published_at.replace(" ", "T", 1), values["title"], values["content"]
            )
            if supplied_sha != alternate:
                raise ValueError(f"article hash mismatch: {values['sample_id']}")
            canonical_sha = supplied_sha
        return cls(
            sample_id=values["sample_id"],
            published_at=published_at,
            source=values["src"],
            title=values["title"],
            content=values["content"],
            article_sha256=supplied_sha or canonical_sha,
        )

    def as_prompt_payload(self) -> dict[str, str]:
        return {
            "sample_id": self.sample_id,
            "published_at": self.published_at,
            "source": self.source,
            "title": self.title,
            "content": visible_text(self.content),
        }


@dataclass(frozen=True)
class ExtractedEvent:
    primary_entity: str
    event_type: str
    direction: str
    event_time_text: str
    event_summary: str
    evidence_excerpt: str


@dataclass(frozen=True)
class NewsReview:
    relevance: str
    review_reason: str
    relevance_evidence: str
    events: tuple[ExtractedEvent, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "relevance": self.relevance,
            "review_reason": self.review_reason,
            "relevance_evidence": self.relevance_evidence,
            "events": [asdict(event) for event in self.events],
        }

    @classmethod
    def parse(cls, payload: object, article: NewsArticle) -> "NewsReview":
        if not isinstance(payload, dict):
            raise ValueError("model output must be a JSON object")
        expected = {"relevance", "review_reason", "relevance_evidence", "events"}
        if set(payload) != expected:
            raise ValueError(
                f"model output fields differ: expected={sorted(expected)}, actual={sorted(payload)}"
            )
        relevance = str(payload["relevance"])
        if relevance not in RELEVANCE_VALUES:
            raise ValueError(f"invalid relevance: {relevance}")
        review_reason = str(payload["review_reason"]).strip()
        relevance_evidence = str(payload["relevance_evidence"]).strip()
        if not review_reason:
            raise ValueError("review_reason cannot be empty")
        raw_events = payload["events"]
        if not isinstance(raw_events, list):
            raise ValueError("events must be an array")
        if relevance == "CATALYST_RELEVANT" and not raw_events:
            raise ValueError("relevant article must contain at least one event")
        if relevance != "CATALYST_RELEVANT" and raw_events:
            raise ValueError("non-relevant or unresolved article cannot contain events")

        article_text = f"{article.title}\n{visible_text(article.content)}"
        if relevance == "CATALYST_RELEVANT":
            _require_evidence(relevance_evidence, article_text, "relevance_evidence")

        event_fields = {
            "primary_entity",
            "event_type",
            "direction",
            "event_time_text",
            "event_summary",
            "evidence_excerpt",
        }
        events: list[ExtractedEvent] = []
        for index, raw_event in enumerate(raw_events):
            if not isinstance(raw_event, dict) or set(raw_event) != event_fields:
                actual = sorted(raw_event) if isinstance(raw_event, dict) else type(raw_event).__name__
                raise ValueError(f"event {index} fields differ: {actual}")
            values = {name: str(raw_event[name]).strip() for name in event_fields}
            for name in ("primary_entity", "event_type", "direction", "event_summary", "evidence_excerpt"):
                if not values[name]:
                    raise ValueError(f"event {index} field cannot be empty: {name}")
            if values["event_type"] not in EVENT_TYPE_VALUES:
                raise ValueError(f"event {index} invalid event_type: {values['event_type']}")
            if values["direction"] not in DIRECTION_VALUES:
                raise ValueError(f"event {index} invalid direction: {values['direction']}")
            _require_evidence(values["evidence_excerpt"], article_text, f"event {index} evidence")
            events.append(ExtractedEvent(**values))
        return cls(relevance, review_reason, relevance_evidence, tuple(events))


def _require_evidence(excerpt: str, article_text: str, field: str) -> None:
    if not excerpt:
        raise ValueError(f"{field} cannot be empty")
    if _normalized_text(excerpt) not in _normalized_text(article_text):
        raise ValueError(f"{field} is not present in the article")


def stable_json_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
