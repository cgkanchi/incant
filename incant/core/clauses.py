"""Clause and condition evaluation — pure functions over a flag dict.

Semantics (from the design):
  * Operators: eq, neq, in, not_in, contains, starts_with, ends_with, matches,
    gt/gte/lt/lte, semver_gt/semver_lt, exists; all/any/not composition.
  * A clause referencing an absent flag does not match — never errors.
  * Segments are named conditions, referenced from any rule.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .model import All, Any_, Clause, Condition, Not, Segment, SegmentRef

_MISSING = object()


def _semver_tuple(v: Any) -> tuple[int, ...] | None:
    try:
        parts = str(v).lstrip("v").split("-", 1)[0].split(".")
        return tuple(int(p) for p in parts)
    except (ValueError, AttributeError):
        return None


def eval_clause(clause: Clause, flags: Mapping[str, Any]) -> bool:
    op = clause.op
    present = clause.flag in flags
    actual = flags.get(clause.flag, _MISSING)

    if op == "exists":
        return present
    # Absent flag never matches (no error) for every value-comparing operator.
    if not present:
        return False

    v = clause.value
    vs = clause.values

    try:
        if op == "eq":
            return actual == v
        if op == "neq":
            return actual != v
        if op == "in":
            return actual in vs
        if op == "not_in":
            return actual not in vs
        if op == "contains":
            return v in actual  # substring / membership
        if op == "starts_with":
            return str(actual).startswith(str(v))
        if op == "ends_with":
            return str(actual).endswith(str(v))
        if op == "matches":
            return re.search(str(v), str(actual)) is not None
        if op == "gt":
            return actual > v
        if op == "gte":
            return actual >= v
        if op == "lt":
            return actual < v
        if op == "lte":
            return actual <= v
        if op == "semver_gt":
            a, b = _semver_tuple(actual), _semver_tuple(v)
            return a is not None and b is not None and a > b
        if op == "semver_lt":
            a, b = _semver_tuple(actual), _semver_tuple(v)
            return a is not None and b is not None and a < b
    except TypeError:
        # Incomparable types (e.g. str > int) — treat as no match, never raise.
        return False
    return False


def eval_condition(
    cond: Condition,
    flags: Mapping[str, Any],
    segments: Mapping[str, Segment],
    _seen: frozenset[str] = frozenset(),
) -> bool:
    """Evaluate a condition tree. ``None`` means "always matches"."""

    if cond is None:
        return True
    if isinstance(cond, Clause):
        return eval_clause(cond, flags)
    if isinstance(cond, All):
        return all(eval_condition(c, flags, segments, _seen) for c in cond.of)
    if isinstance(cond, Any_):
        return any(eval_condition(c, flags, segments, _seen) for c in cond.of)
    if isinstance(cond, Not):
        return not eval_condition(cond.of, flags, segments, _seen)
    if isinstance(cond, SegmentRef):
        seg = segments.get(cond.name)
        if seg is None or cond.name in _seen:
            # Unknown or self-referential segment never matches.
            return False
        return eval_condition(seg.condition, flags, segments, _seen | {cond.name})
    raise TypeError(f"unknown condition node: {cond!r}")
