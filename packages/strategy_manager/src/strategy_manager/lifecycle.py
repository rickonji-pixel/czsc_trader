from __future__ import annotations

from .errors import InvalidTransitionError
from .models import Qualification


ALLOWED_TRANSITIONS: dict[Qualification, set[Qualification]] = {
    Qualification.RESEARCH: {Qualification.PAPER_READY, Qualification.RETIRED},
    Qualification.PAPER_READY: {Qualification.LIVE_READY, Qualification.RETIRED},
    Qualification.LIVE_READY: {Qualification.PAPER_READY, Qualification.RETIRED},
    Qualification.RETIRED: set(),
}


def validate_transition(current: Qualification, target: Qualification) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"transition {current.value} -> {target.value} is not allowed")


def qualification_is_deployable(qualification: Qualification, environment: str) -> bool:
    normalized = environment.strip().upper()
    if normalized == "PAPER":
        return qualification in {Qualification.PAPER_READY, Qualification.LIVE_READY}
    if normalized == "LIVE":
        return qualification is Qualification.LIVE_READY
    raise InvalidTransitionError(f"unknown deployment environment: {environment}")
