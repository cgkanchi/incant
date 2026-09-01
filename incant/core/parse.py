"""Build core model objects from JSON-shaped dicts (DB rows, API payloads)."""

from __future__ import annotations

import re
from typing import Any

from .model import All, Any_, Clause, Condition, Not, Rule, Serve, ServeVersion

_OPERATORS = {
    "eq", "neq", "in", "not_in", "contains", "starts_with", "ends_with",
    "gt", "gte", "lt", "lte", "semver_gt", "semver_lt", "exists",
}
_AT_VALUES = {"live", "tip", "sha"}
_RULE_STATUSES = {"active", "paused", "archived"}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Targeting constructs removed in 1.1.0. Named so the errors can say what happened
# rather than "unrecognized node" — a rule (or a replayed revision) that carries one
# predates the flags-only model.
_REMOVED_CONDITION_KEYS = {"segment"}
_REMOVED_SERVE_KEYS = {"label", "rollout"}


def parse_condition(data: Any) -> Condition:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"condition must be an object or null, got {type(data).__name__}")
    if _REMOVED_CONDITION_KEYS & set(data):
        raise ValueError("segments were removed in 1.1.0 — express the audience as flag "
                         "clauses (all/any/not)")
    node_keys = {k for k in ("all", "any", "not", "flag") if k in data}
    if len(node_keys) != 1:
        raise ValueError(f"condition must contain exactly one node type, got {data!r}")
    if "all" in data:
        if set(data) != {"all"} or not isinstance(data["all"], list):
            raise ValueError("an 'all' condition must contain only a list-valued 'all'")
        return All(tuple(parse_condition(c) for c in data["all"]))
    if "any" in data:
        if set(data) != {"any"} or not isinstance(data["any"], list):
            raise ValueError("an 'any' condition must contain only a list-valued 'any'")
        return Any_(tuple(parse_condition(c) for c in data["any"]))
    if "not" in data:
        if set(data) != {"not"}:
            raise ValueError("a 'not' condition must contain only 'not'")
        return Not(parse_condition(data["not"]))
    # "flag"
    allowed = {"flag", "op", "value", "values"}
    if set(data) - allowed:
        raise ValueError(f"unknown clause fields: {sorted(set(data) - allowed)!r}")
    if not isinstance(data["flag"], str) or not data["flag"].strip():
        raise ValueError("a flag clause requires a non-empty flag name")
    op = data.get("op")
    if op not in _OPERATORS:
        raise ValueError(f"unknown condition operator {op!r}")
    values = data.get("values", ())
    if op in ("in", "not_in"):
        if not isinstance(values, list) or not values:
            raise ValueError(f"operator {op!r} requires a non-empty 'values' list")
    elif "values" in data:
        raise ValueError(f"operator {op!r} does not accept 'values'")
    if op not in ("exists", "in", "not_in") and "value" not in data:
        raise ValueError(f"operator {op!r} requires 'value'")
    if op == "exists" and ("value" in data or "values" in data):
        raise ValueError("operator 'exists' accepts neither 'value' nor 'values'")
    return Clause(
        flag=data["flag"].strip(),
        op=op,
        value=data.get("value"),
        values=tuple(data.get("values", ())),
    )


def parse_serve(data: dict[str, Any]) -> Serve:
    if not isinstance(data, dict):
        raise ValueError("serve target must be an object")
    if _REMOVED_SERVE_KEYS & set(data):
        raise ValueError("label and rollout serve targets were removed in 1.1.0 — a rule "
                         "serves one version of its prompt; cohort assignment lives in "
                         "your flag system")
    if "version" not in data:
        raise ValueError("serve target must name a version")
    if set(data) - {"version", "at", "sha"}:
        raise ValueError(f"unknown version target fields: {sorted(set(data) - {'version', 'at', 'sha'})!r}")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("serve version must be a positive integer")
    at = data.get("at", "live")
    if at not in _AT_VALUES:
        raise ValueError(f"unknown serve mode {at!r}")
    sha = data.get("sha")
    if at == "sha" and (not isinstance(sha, str) or not _SHA_RE.fullmatch(sha)):
        raise ValueError("a SHA-pinned target requires a full 40-character lowercase SHA")
    if at != "sha" and sha is not None:
        raise ValueError("sha is only valid when at='sha'")
    return ServeVersion(version=version, at=at, sha=sha)


def parse_rule(data: dict[str, Any]) -> Rule:
    if not isinstance(data, dict):
        raise ValueError("rule must be an object")
    rid = data.get("id")
    if not isinstance(rid, str) or not rid.strip() or len(rid) > 255:
        raise ValueError("rule id must be a non-empty string of at most 255 characters")
    # Pre-1.1.0 payloads/states carry `scope`; "prompt" is the only shape that survives.
    if data.get("scope") not in (None, "prompt"):
        raise ValueError("global rules were removed in 1.1.0 — rules are scoped to one prompt")
    status = data.get("status", "active")
    if status not in _RULE_STATUSES:
        raise ValueError(f"unknown rule status {status!r}")
    prompt_id = data.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id.strip():
        raise ValueError("a rule requires prompt_id")
    priority = data.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1_000_000:
        raise ValueError("rule priority must be an integer between 0 and 1000000")
    return Rule(
        id=rid.strip(),
        prompt_id=prompt_id.strip(),
        priority=priority,
        when=parse_condition(data.get("when")),
        serve=parse_serve(data.get("serve")),
        status=status,
        comment=data.get("comment", ""),
    )
