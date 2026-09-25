"""Scoring policy (spec §3). Lives in code, not in Jev."""

from __future__ import annotations

from typing import Callable

from digest.config import Thresholds
from digest.models import Bucket, Item

# is_ai is a probability; treat >= 0.5 as "yes".
IS_AI_CUTOFF = 0.5

# Hook for a future author-priority boost (out of scope for v1). Receives the item and its Jev
# worthiness score and returns the adjusted score.
ScoreAdjuster = Callable[[Item, float], float]


def no_boost(item: Item, score: float) -> float:
    return score


def bucket_for(score: float, thresholds: Thresholds) -> Bucket:
    if score >= thresholds.high:
        return Bucket.HIGH
    if score >= thresholds.medium:
        return Bucket.MEDIUM
    return Bucket.LOW


def apply_policy(item: Item, thresholds: Thresholds, adjust: ScoreAdjuster = no_boost) -> Bucket:
    if item.is_ai is None or item.worthiness is None:
        raise ValueError("item has not been classified")
    if item.is_ai < IS_AI_CUTOFF:
        item.final_score = item.worthiness
        item.bucket = Bucket.NOT_AI
        return item.bucket
    item.final_score = min(1.0, max(0.0, adjust(item, item.worthiness)))
    item.bucket = bucket_for(item.final_score, thresholds)
    return item.bucket
