"""Build core model objects from JSON-shaped dicts (DB rows, API payloads)."""

from __future__ import annotations

import math
import re
from typing import Any

from .model import (
    All,
    Any_,
    Clause,
    Condition,
    Not,
    RolloutBand,
    Rule,
    Segment,
    SegmentRef,
    Serve,
    ServeLabel,
    ServeRollout,
    ServeVersion,
)

_OPERATORS = {
    "eq", "neq", "in", "not_in", "contains", "starts_with", "ends_with",
    "gt", "gte", "lt", "lte", "semver_gt", "semver_lt", "exists",
}
_AT_VALUES = {"live", "tip", "sha"}
_SCOPES = {"global", "prompt"}
_RULE_STATUSES = {"active", "paused", "archived"}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def parse_condition(data: Any) -> Condition:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"condition must be an object or null, got {type(data).__name__}")
    node_keys = {k for k in ("all", "any", "not", "segment", "flag") if k in data}
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
    if "segment" in data:
        if set(data) != {"segment"} or not isinstance(data["segment"], str) \
                or not data["segment"].strip():
            raise ValueError("a segment condition requires one non-empty segment name")
        return SegmentRef(data["segment"].strip())
    if "flag" in data:
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
    raise ValueError(f"unrecognized condition node: {data!r}")


def parse_serve(data: dict[str, Any]) -> Serve:
    if not isinstance(data, dict):
        raise ValueError("serve target must be an object")
    target_keys = {k for k in ("rollout", "label", "version") if k in data}
    if len(target_keys) != 1:
        raise ValueError("serve target must contain exactly one of rollout, label, or version")
    if "rollout" in data:
        if set(data) != {"rollout"} or not isinstance(data["rollout"], dict):
            raise ValueError("rollout serve target must contain only a rollout object")
        r = data["rollout"]
        if set(r) - {"bucket_by", "weights"}:
            raise ValueError(f"unknown rollout fields: {sorted(set(r) - {'bucket_by', 'weights'})!r}")
        bucket_by = r.get("bucket_by")
        weights = r.get("weights")
        if not isinstance(bucket_by, str) or not bucket_by.strip():
            raise ValueError("rollout requires a non-empty bucket_by flag")
        if not isinstance(weights, list) or not weights:
            raise ValueError("rollout requires at least one weighted arm")
        bands = []
        for w in weights:
            if not isinstance(w, dict) or set(w) - {"weight", "label", "version", "default"}:
                raise ValueError(f"invalid rollout arm {w!r}")
            targets = int(bool(w.get("default"))) + int(w.get("label") is not None) \
                + int(w.get("version") is not None)
            if targets != 1:
                raise ValueError("each rollout arm needs exactly one label, version, or default")
            weight = float(w.get("weight", 0))
            if not math.isfinite(weight) or weight <= 0 or weight > 100:
                raise ValueError("rollout arm weights must be finite and in (0, 100]")
            version = w.get("version")
            if version is not None and (isinstance(version, bool) or int(version) < 1):
                raise ValueError("rollout version must be a positive integer")
            label = w.get("label")
            if label is not None and (not isinstance(label, str) or not label.strip()):
                raise ValueError("rollout label must be a non-empty string")
            bands.append(RolloutBand(
                weight=weight, label=label.strip() if label else None,
                version=int(version) if version is not None else None,
                is_default=bool(w.get("default", False)),
            ))
        if not math.isclose(sum(b.weight for b in bands), 100.0, abs_tol=1e-6):
            raise ValueError("rollout arm weights must sum to 100")
        return ServeRollout(bucket_by=bucket_by.strip(), weights=tuple(bands))
    if "label" in data:
        if set(data) != {"label"} or not isinstance(data["label"], str) \
                or not data["label"].strip():
            raise ValueError("label serve target requires one non-empty label")
        return ServeLabel(label=data["label"].strip())
    if "version" in data:
        if set(data) - {"version", "at", "sha"}:
            raise ValueError(f"unknown version target fields: {sorted(set(data) - {'version', 'at', 'sha'})!r}")
        if isinstance(data["version"], bool) or int(data["version"]) < 1:
            raise ValueError("serve version must be a positive integer")
        at = data.get("at", "live")
        if at not in _AT_VALUES:
            raise ValueError(f"unknown serve mode {at!r}")
        sha = data.get("sha")
        if at == "sha" and (not isinstance(sha, str) or not _SHA_RE.fullmatch(sha)):
            raise ValueError("a SHA-pinned target requires a full 40-character lowercase SHA")
        if at != "sha" and sha is not None:
            raise ValueError("sha is only valid when at='sha'")
        return ServeVersion(
            version=int(data["version"]),
            at=at,
            sha=sha,
        )
    raise ValueError(f"unrecognized serve target: {data!r}")


def parse_rule(data: dict[str, Any]) -> Rule:
    if not isinstance(data, dict):
        raise ValueError("rule must be an object")
    rid = data.get("id")
    if not isinstance(rid, str) or not rid.strip() or len(rid) > 255:
        raise ValueError("rule id must be a non-empty string of at most 255 characters")
    scope = data.get("scope")
    if scope not in _SCOPES:
        raise ValueError(f"unknown rule scope {scope!r}")
    status = data.get("status", "active")
    if status not in _RULE_STATUSES:
        raise ValueError(f"unknown rule status {status!r}")
    prompt_id = data.get("prompt_id")
    if scope == "prompt" and (not isinstance(prompt_id, str) or not prompt_id.strip()):
        raise ValueError("prompt-scoped rules require prompt_id")
    if scope == "global" and prompt_id is not None:
        raise ValueError("global rules must not set prompt_id")
    priority = int(data.get("priority", 0))
    if not 0 <= priority <= 1_000_000:
        raise ValueError("rule priority must be between 0 and 1000000")
    return Rule(
        id=rid.strip(),
        scope=scope,
        priority=priority,
        when=parse_condition(data.get("when")),
        serve=parse_serve(data["serve"]),
        status=status,
        prompt_id=prompt_id,
        comment=data.get("comment", ""),
    )


def parse_segment(data: dict[str, Any]) -> Segment:
    return Segment(
        name=str(data["name"]),
        condition=parse_condition(data.get("when") or data.get("condition")),
        version=int(data.get("version", 1)),
    )
