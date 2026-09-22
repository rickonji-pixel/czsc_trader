"""Strategy-neutral observation facts emitted by deployed SRT releases."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math
import re
from typing import Any

from .errors import RuntimeCompatibilityError, RuntimeContractError


OBSERVATION_CONTRACT_VERSION = "strategy_observation.v1"
_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeCompatibilityError(f"{field} must be a non-empty string")
    return value.strip()


def _key(value: object, field: str) -> str:
    normalized = _text(value, field)
    if _KEY.fullmatch(normalized) is None:
        raise RuntimeCompatibilityError(f"{field} must be a lowercase semantic key")
    return normalized


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise RuntimeContractError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise RuntimeContractError(f"{field} must be finite")
    return number


def validate_observation_descriptor(value: object) -> dict[str, Any]:
    """Validate display-neutral signal semantics stored in an SRT binding."""

    if not isinstance(value, Mapping):
        raise RuntimeCompatibilityError("observation descriptor must be an object")
    descriptor = deepcopy(dict(value))
    if set(descriptor) != {"contract_version", "series"}:
        raise RuntimeCompatibilityError("observation descriptor fields are invalid")
    if descriptor["contract_version"] != OBSERVATION_CONTRACT_VERSION:
        raise RuntimeCompatibilityError(
            f"observation descriptor must use {OBSERVATION_CONTRACT_VERSION}"
        )
    raw_series = descriptor.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raise RuntimeCompatibilityError("observation descriptor requires signal series")
    if len(raw_series) > 4:
        raise RuntimeCompatibilityError("observation descriptor supports at most four series")
    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, raw in enumerate(raw_series):
        if not isinstance(raw, Mapping):
            raise RuntimeCompatibilityError(f"observation series {index} must be an object")
        series = dict(raw)
        if set(series) != {"key", "label", "value_field", "guides"}:
            raise RuntimeCompatibilityError(f"observation series {index} fields are invalid")
        key = _key(series["key"], f"observation series {index} key")
        if key in keys:
            raise RuntimeCompatibilityError("observation series keys must be unique")
        keys.add(key)
        guides = series.get("guides")
        if not isinstance(guides, list) or len(guides) > 4:
            raise RuntimeCompatibilityError(
                f"observation series {key} guides must be a bounded list"
            )
        normalized_guides: list[dict[str, Any]] = []
        guide_keys: set[str] = set()
        for guide_index, raw_guide in enumerate(guides):
            if not isinstance(raw_guide, Mapping):
                raise RuntimeCompatibilityError(
                    f"observation series {key} guide {guide_index} must be an object"
                )
            guide = dict(raw_guide)
            expected = {"key", "label", "value"}
            dynamic = {"key", "label", "value_field"}
            if frozenset(guide) not in {frozenset(expected), frozenset(dynamic)}:
                raise RuntimeCompatibilityError(
                    f"observation series {key} guide {guide_index} fields are invalid"
                )
            guide_key = _key(
                guide["key"], f"observation series {key} guide {guide_index} key"
            )
            if guide_key in guide_keys:
                raise RuntimeCompatibilityError(
                    f"observation series {key} guide keys must be unique"
                )
            guide_keys.add(guide_key)
            normalized_guide = {
                "key": guide_key,
                "label": _text(
                    guide["label"],
                    f"observation series {key} guide {guide_index} label",
                ),
            }
            if "value" in guide:
                normalized_guide["value"] = _finite(
                    guide["value"],
                    f"observation series {key} guide {guide_index} value",
                )
            else:
                normalized_guide["value_field"] = _key(
                    guide["value_field"],
                    f"observation series {key} guide {guide_index} value_field",
                )
            normalized_guides.append(normalized_guide)
        normalized.append(
            {
                "key": key,
                "label": _text(series["label"], f"observation series {index} label"),
                "value_field": _key(
                    series["value_field"], f"observation series {index} value_field"
                ),
                "guides": normalized_guides,
            }
        )
    return {"contract_version": OBSERVATION_CONTRACT_VERSION, "series": normalized}


def unavailable_observation(message: object) -> dict[str, Any]:
    return {
        "contract_version": OBSERVATION_CONTRACT_VERSION,
        "status": "UNAVAILABLE",
        "message": str(message)[:500],
    }


def materialize_observation(
    descriptor: object,
    evidence: Mapping[str, object],
    *,
    action: str,
    target_position: float,
) -> dict[str, Any]:
    """Resolve one immutable descriptor against one strategy decision."""

    normalized = validate_observation_descriptor(descriptor)
    if not isinstance(evidence, Mapping):
        raise RuntimeContractError("strategy observation evidence must be an object")
    series_output: list[dict[str, Any]] = []
    for series in normalized["series"]:
        field = series["value_field"]
        if field not in evidence:
            raise RuntimeContractError(f"strategy observation is missing {field}")
        guides = []
        for guide in series["guides"]:
            if "value" in guide:
                value = guide["value"]
            else:
                guide_field = guide["value_field"]
                if guide_field not in evidence:
                    raise RuntimeContractError(
                        f"strategy observation is missing {guide_field}"
                    )
                value = _finite(evidence[guide_field], f"observation guide {guide_field}")
            guides.append(
                {"key": guide["key"], "label": guide["label"], "value": value}
            )
        series_output.append(
            {
                "key": series["key"],
                "label": series["label"],
                "value": _finite(evidence[field], f"observation value {field}"),
                "guides": guides,
            }
        )
    return {
        "contract_version": OBSERVATION_CONTRACT_VERSION,
        "status": "READY",
        "action": str(action),
        "target_position": _finite(target_position, "observation target_position"),
        "series": series_output,
    }


def validate_observation_payload(value: object) -> dict[str, Any]:
    """Validate facts crossing from SRT into a PTE decision ledger."""

    if not isinstance(value, Mapping):
        raise RuntimeContractError("strategy observation must be an object")
    observation = deepcopy(dict(value))
    if observation.get("contract_version") != OBSERVATION_CONTRACT_VERSION:
        raise RuntimeContractError(
            f"strategy observation must use {OBSERVATION_CONTRACT_VERSION}"
        )
    if observation.get("status") == "UNAVAILABLE":
        if set(observation) != {"contract_version", "status", "message"}:
            raise RuntimeContractError("unavailable strategy observation fields are invalid")
        observation["message"] = str(observation["message"])[:500]
        return observation
    if set(observation) != {
        "contract_version", "status", "action", "target_position", "series",
    } or observation.get("status") != "READY":
        raise RuntimeContractError("strategy observation fields are invalid")
    observation["action"] = str(observation["action"])
    observation["target_position"] = _finite(
        observation["target_position"], "observation target_position"
    )
    series = observation.get("series")
    if not isinstance(series, list) or not series:
        raise RuntimeContractError("strategy observation series are missing")
    for index, item in enumerate(series):
        if not isinstance(item, dict) or set(item) != {"key", "label", "value", "guides"}:
            raise RuntimeContractError(f"strategy observation series {index} is invalid")
        item["value"] = _finite(item["value"], f"observation series {index} value")
        if not isinstance(item["guides"], list):
            raise RuntimeContractError(f"strategy observation series {index} guides are invalid")
        for guide_index, guide in enumerate(item["guides"]):
            if not isinstance(guide, dict) or set(guide) != {"key", "label", "value"}:
                raise RuntimeContractError(
                    f"strategy observation series {index} guide {guide_index} is invalid"
                )
            guide["value"] = _finite(
                guide["value"],
                f"observation series {index} guide {guide_index} value",
            )
    return observation
