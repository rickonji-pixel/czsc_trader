"""Deterministic CZSC information inventory for the terminal research route."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Iterable


FREQUENCIES = ("30分钟", "日线", "周线")
EVENT_NAME_TOKENS = (
    "_first_buy_",
    "_first_sell_",
    "_second_bs_",
    "_third_bs_",
    "_third_buy_",
    "_bs_V",
)


@dataclass(frozen=True)
class ParsedSignalValue:
    primary: str | None
    secondary: str | None
    tertiary: str | None
    original: str | None


@dataclass(frozen=True)
class SignalClassification:
    status: str
    family: str | None
    reason: str | None


def parse_signal_value(value: object) -> ParsedSignalValue:
    """Preserve the first three semantic fields of a CZSC signal value."""
    if value is None:
        return ParsedSignalValue(None, None, None, None)
    original = str(value)
    parts = original.split("_")
    fields: list[str | None] = [part if part else None for part in parts[:3]]
    fields.extend([None] * (3 - len(fields)))
    return ParsedSignalValue(fields[0], fields[1], fields[2], original)


def classify_signal_family(name: str) -> SignalClassification:
    """Classify one registered native signal without hiding exclusions."""
    if name.startswith(("cxt_", "byi_")):
        family = (
            "sparse_event"
            if any(token in name for token in EVENT_NAME_TOKENS)
            else "atomic_structure"
        )
        return SignalClassification("candidate", family, None)
    if name.startswith(("zdy_bi_end_", "zdy_zs_")):
        return SignalClassification("candidate", "atomic_structure", None)
    if name.startswith("zdy_"):
        return SignalClassification(
            "excluded", None, "hybrid_ta_not_pure_czsc_structure"
        )
    return SignalClassification("excluded", None, "non_czsc_structure_signal")


def inventory_records(names: Iterable[str]) -> list[dict[str, object]]:
    """Return a stable, complete accounting of registered CZSC signals."""
    records: list[dict[str, object]] = []
    for name in sorted(set(map(str, names))):
        classification = classify_signal_family(name)
        row = {
            "name": name,
            **asdict(classification),
            "frequencies": list(FREQUENCIES),
            "default_parameters": {"di": 1},
        }
        records.append(row)
    return records


def probe_signal_frequencies(
    records: Iterable[dict[str, object]],
    execute: Callable[[str, str], object],
) -> list[dict[str, object]]:
    """Probe every planned frequency while preserving every failure reason."""
    probes: list[dict[str, object]] = []
    for record in sorted(records, key=lambda row: str(row["name"])):
        if record.get("status") != "candidate":
            continue
        for frequency in FREQUENCIES:
            row: dict[str, object] = {
                "name": str(record["name"]),
                "family": str(record["family"]),
                "frequency": frequency,
            }
            try:
                execute(str(record["name"]), frequency)
            except Exception as exc:  # the audit must retain native failure identity
                row.update(
                    {
                        "status": "unavailable",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                row.update({"status": "generated", "error": None})
            probes.append(row)
    return probes
