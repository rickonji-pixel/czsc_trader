from dataclasses import replace

from .models import CandidateProfile


def _dominates(left: CandidateProfile, right: CandidateProfile) -> bool:
    a = dict(left.worst_scores)
    b = dict(right.worst_scores)
    keys = set(a) & set(b)
    return bool(keys) and all(a[key] >= b[key] for key in keys) and any(a[key] > b[key] for key in keys)


def pareto_layers(profiles: tuple[CandidateProfile, ...]) -> tuple[CandidateProfile, ...]:
    remaining = list(profiles)
    result: list[CandidateProfile] = []
    layer = 1
    while remaining:
        front = [item for item in remaining if not any(_dominates(other, item) for other in remaining if other != item)]
        result.extend(replace(item, pareto_layer=layer) for item in front)
        remaining = [item for item in remaining if item not in front]
        layer += 1
    return tuple(result)
