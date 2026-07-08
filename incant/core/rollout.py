"""Deterministic rollout bucketing.

Coherence rules (from the design):
  * Global rules hash ``sha256(f"{rule_id}:{bucket_value}")`` — no prompt id —
    so an experiment user is bucketed identically across every participating prompt.
  * Prompt-scoped rules hash ``sha256(f"{prompt_id}:{rule_id}:{bucket_value}")``.
  * Rule ids are immutable, so ramps are monotonic and reordering never reshuffles
    cohorts.
  * Missing ``bucket_by`` flag => the rule falls through (handled by the caller).
"""

from __future__ import annotations

import hashlib
from typing import Sequence

from .model import RolloutBand

_MAX = 0x100000000  # 2**32


def bucket_point(rule_id: str, bucket_value: object, prompt_id: str | None) -> float:
    """Return a stable point in [0, 1) for this (rule, subject[, prompt])."""

    if prompt_id is None:
        key = f"{rule_id}:{bucket_value}"
    else:
        key = f"{prompt_id}:{rule_id}:{bucket_value}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / _MAX


def pick_band(
    bands: Sequence[RolloutBand],
    rule_id: str,
    bucket_value: object,
    prompt_id: str | None,
) -> RolloutBand | None:
    """Pick the band a subject falls into. ``None`` if weights are empty/zero."""

    total = sum(max(0.0, b.weight) for b in bands)
    if total <= 0:
        return None
    point = bucket_point(rule_id, bucket_value, prompt_id)
    acc = 0.0
    for band in bands:
        acc += max(0.0, band.weight) / total
        if point < acc:
            return band
    return bands[-1]  # floating-point guard: last band catches point ~1.0
