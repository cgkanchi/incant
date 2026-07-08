"""Build core model objects from JSON-shaped dicts (DB rows, API payloads)."""

from __future__ import annotations

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


def parse_condition(data: Any) -> Condition:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"condition must be an object or null, got {type(data).__name__}")
    if "all" in data:
        return All(tuple(parse_condition(c) for c in data["all"]))
    if "any" in data:
        return Any_(tuple(parse_condition(c) for c in data["any"]))
    if "not" in data:
        return Not(parse_condition(data["not"]))
    if "segment" in data:
        return SegmentRef(str(data["segment"]))
    if "flag" in data:
        return Clause(
            flag=str(data["flag"]),
            op=data["op"],
            value=data.get("value"),
            values=tuple(data.get("values", ())),
        )
    raise ValueError(f"unrecognized condition node: {data!r}")


def parse_serve(data: dict[str, Any]) -> Serve:
    if "rollout" in data:
        r = data["rollout"]
        bands = tuple(
            RolloutBand(
                weight=float(w.get("weight", 0)),
                label=w.get("label"),
                version=w.get("version"),
                is_default=bool(w.get("default", False)),
            )
            for w in r.get("weights", [])
        )
        return ServeRollout(bucket_by=str(r["bucket_by"]), weights=bands)
    if "label" in data:
        return ServeLabel(label=str(data["label"]))
    if "version" in data:
        return ServeVersion(
            version=int(data["version"]),
            at=data.get("at", "live"),
            sha=data.get("sha"),
        )
    raise ValueError(f"unrecognized serve target: {data!r}")


def parse_rule(data: dict[str, Any]) -> Rule:
    return Rule(
        id=str(data["id"]),
        scope=data["scope"],
        priority=int(data.get("priority", 0)),
        when=parse_condition(data.get("when")),
        serve=parse_serve(data["serve"]),
        status=data.get("status", "active"),
        prompt_id=data.get("prompt_id"),
        comment=data.get("comment", ""),
    )


def parse_segment(data: dict[str, Any]) -> Segment:
    return Segment(
        name=str(data["name"]),
        condition=parse_condition(data.get("when") or data.get("condition")),
        version=int(data.get("version", 1)),
    )
