"""Pure reconstruction of a stored score breakdown.

The function intentionally knows nothing about properties or scorer config.
It uses only the public breakdown contract, making it suitable for publisher
validation and for the web's independent implementation.
"""

from typing import Any, Mapping, Sequence

from aevorex.scoring.curves import clamp


def recompose(breakdown: Mapping[str, Any]) -> float:
    """Reproduce the rounded absolute score from components and adjustments."""
    components = breakdown.get("components", [])
    if not isinstance(components, Sequence):
        raise ValueError("breakdown components must be a sequence")

    used_weight = 0.0
    weighted_total = 0.0
    for item in components:
        if not isinstance(item, Mapping) or not item.get("available", True):
            continue
        weight = float(item["weight"])
        used_weight += weight
        weighted_total += float(item["subscore"]) * weight
    if used_weight <= 0:
        raise ValueError("cannot recompose a breakdown with no available components")

    score = weighted_total / used_weight
    adjustments = breakdown.get("adjustments", [])
    if not isinstance(adjustments, Sequence):
        raise ValueError("breakdown adjustments must be a sequence")

    for adjustment in adjustments:
        if not isinstance(adjustment, Mapping) or not adjustment.get("applied", False):
            continue
        kind = adjustment.get("type")
        value = float(adjustment["value"])
        if kind == "multiplier":
            score *= value
        elif kind == "bonus":
            score += value
        elif kind == "penalty":
            score -= value
        elif kind == "cap":
            # Scoring gates compress [0, 100] into [0, cap], preserving rank.
            score *= value / 100.0
        elif kind == "confidence_shrink":
            floor = float(adjustment.get("floor", 0.55))
            confidence = clamp(value, 0.0, 1.0)
            weight = floor + (1.0 - floor) * confidence
            score = 50.0 + (score - 50.0) * weight
        elif kind in {"component_bonus", "input_haircut", "gate", "note"}:
            # Informational anatomy already reflected in a component subscore.
            continue
        else:
            raise ValueError(f"unknown applied adjustment type: {kind!r}")

    return round(clamp(score), 2)
