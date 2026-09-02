"""Clause and condition evaluation — pure functions over a flag dict.

Semantics (from the design):
  * Operators: eq, neq, in, not_in, contains, starts_with, ends_with,
    gt/gte/lt/lte, semver_gt/semver_lt, exists; all/any/not composition.
  * A clause referencing an absent flag does not match — never errors.
"""

from __future__ import annotations

from typing import Any, Mapping

from .model import All, Any_, Clause, Condition, Not

_MISSING = object()


def _semver_tuple(v: Any) -> tuple[int, ...] | None:
    """``1.2.3``, ``v1.2.3``, ``1.2.3-rc.1`` (pre-release dropped) and ``1.2.3+build``
    (build metadata dropped, per semver precedence) → ``(1, 2, 3)``; anything that is not
    dotted integers → None (never matches, never raises)."""
    try:
        core = str(v).lstrip("v").split("+", 1)[0].split("-", 1)[0]
        return tuple(int(p) for p in core.split("."))
    except (ValueError, AttributeError):
        return None


def _eq(a: Any, b: Any) -> bool:
    """Equality that never crosses the bool/number line.

    bool is an int subclass, so Python's ``==`` says ``True == 1`` and
    ``False == 0`` — which would silently place a user whose flag is a number
    into a cohort keyed on a boolean (and vice versa). Exactly one side being a
    bool is therefore never a match; otherwise plain ``==`` (which already keeps
    strings distinct from numbers, and int/float numerically comparable).
    """
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


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
        # eq/neq and membership go through _eq so True never matches 1 (nor False, 0);
        # ordering operators (gt/lt/semver) keep Python semantics untouched.
        if op == "eq":
            return _eq(actual, v)
        if op == "neq":
            return not _eq(actual, v)
        if op == "in":
            return any(_eq(actual, x) for x in vs)
        if op == "not_in":
            return not any(_eq(actual, x) for x in vs)
        if op == "contains":
            return v in actual  # substring / membership
        if op == "starts_with":
            return str(actual).startswith(str(v))
        if op == "ends_with":
            return str(actual).endswith(str(v))
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


def eval_condition(cond: Condition, flags: Mapping[str, Any]) -> bool:
    """Evaluate a condition tree. ``None`` means "always matches"."""

    if cond is None:
        return True
    if isinstance(cond, Clause):
        return eval_clause(cond, flags)
    if isinstance(cond, All):
        return all(eval_condition(c, flags) for c in cond.of)
    if isinstance(cond, Any_):
        return any(eval_condition(c, flags) for c in cond.of)
    if isinstance(cond, Not):
        return not eval_condition(cond.of, flags)
    raise TypeError(f"unknown condition node: {cond!r}")
