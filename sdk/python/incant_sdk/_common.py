"""Shared, transport-free core: request builders, response parsers, and error
mapping. The sync and async clients are thin transports over these functions,
so wire behavior can never drift between them."""

from __future__ import annotations

import os
from typing import Any

import httpx

from ._errors import (
    IncantError,
    IncantUnavailable,
    MissingVariable,
    NotAuthorized,
    PromptNotFound,
    RenderError,
)
from ._models import (
    Flag,
    PromptInfo,
    PromptSpec,
    RenderResult,
    Resolution,
    RuleMatch,
    SkippedRule,
    Var,
    VersionPin,
)

VERSION = "1.1.0"
USER_AGENT = f"incant-sdk-python/{VERSION}"

# Renders are pure reads of an in-memory snapshot — safe to retry on the gateway
# statuses that mean "the node, not your request".
RETRYABLE_STATUSES = frozenset({502, 503, 504})
BACKOFFS = (0.25, 0.75, 1.5)  # seconds before retry 1, 2, 3…


def resolve_config(url: str | None, key: str | None) -> tuple[str, str]:
    url = url or os.environ.get("INCANT_URL", "")
    key = key or os.environ.get("INCANT_API_KEY", "")
    if not url:
        raise IncantError("no server URL: pass Incant(url=...) or set INCANT_URL")
    if not key:
        raise IncantError("no API key: pass Incant(key=...) or set INCANT_API_KEY")
    return url.rstrip("/"), key


def default_environment(environment: str | None) -> str | None:
    return environment or os.environ.get("INCANT_ENVIRONMENT") or None


def headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT}


# ── request builders ─────────────────────────────────────────────────

def build_render(prompt_id: str, flags: dict | None, variables: dict | None,
                 environment: str | None, pin: dict | None) -> tuple[str, dict]:
    body: dict[str, Any] = {"flags": flags or {}, "variables": variables or {}}
    if environment:
        body["environment"] = environment
    if pin:
        body["pin"] = pin
    return f"/prompt/{prompt_id}", body


def build_evaluate(prompt_id: str | None, flags: dict | None,
                   environment: str | None) -> tuple[str, dict]:
    body: dict[str, Any] = {"flags": flags or {}}
    if environment:
        body["environment"] = environment
    path = f"/prompt/{prompt_id}/evaluate" if prompt_id else "/evaluate"
    return path, body


# ── response parsers ─────────────────────────────────────────────────

def _match(data: Any) -> RuleMatch | str:
    if isinstance(data, dict):
        return RuleMatch(scope=data["scope"], id=data["id"])
    return "default"


def parse_render(data: dict) -> RenderResult:
    return RenderResult(
        text=data["prompt"],
        prompt_id=data["prompt_id"],
        environment=data["environment"],
        matched_rule=_match(data["matched_rule"]),
        versions={pid: VersionPin(version=v["version"], commit=v["commit"])
                  for pid, v in data["versions"].items()},
        rules_version=data["rules_version"],
        stale_rules=bool(data.get("stale_rules")),
        content_fallback=bool(data.get("content_fallback")),
        skipped_rules=tuple(
            SkippedRule(rule_id=s["rule_id"], prompt_id=s["prompt_id"],
                        reason=s["reason"])
            for s in data.get("skipped_rules", [])),
        raw=data,
    )


def parse_resolution(data: dict, prompt_id: str | None = None) -> Resolution:
    return Resolution(
        prompt_id=data.get("prompt_id", prompt_id or ""),
        version=data["version"],
        commit=data["commit"],
        matched_rule=_match(data["matched_rule"]),
        raw=data,
    )


def parse_prompts(data: dict) -> list[PromptInfo]:
    return [
        PromptInfo(
            id=p["prompt_id"],
            description=p.get("description", ""),
            versions=tuple(p.get("versions", [])),
            default_version=p.get("default"),
            raw=p,
        )
        for p in data.get("prompts", [])
    ]


def parse_spec(data: dict) -> PromptSpec:
    return PromptSpec(
        prompt_id=data["prompt_id"],
        environment=data["environment"],
        default_version=data.get("default_version"),
        resolvable_versions=tuple(data.get("resolvable_versions", [])),
        variables=tuple(
            Var(name=v["name"], type=v.get("type") or "string",
                required=bool(v.get("required")), default=v.get("default"),
                description=v.get("description") or "",
                versions=tuple(v.get("versions", [])))
            for v in data.get("variables", [])),
        flags=tuple(
            Flag(name=f["name"], values=tuple(f.get("values", [])))
            for f in data.get("flags", [])),
        includes=tuple(data.get("includes", [])),
        raw=data,
    )


# ── error mapping ────────────────────────────────────────────────────

def _detail_of(payload: Any) -> tuple[str, Any]:
    """The server nests messages as `detail: str` or `detail: {detail: str, ...}`
    (render errors carry structured extras beside the message)."""
    if isinstance(payload, dict):
        d = payload.get("detail", payload)
        if isinstance(d, dict):
            return str(d.get("detail", d)), d
        return str(d), payload
    return str(payload), payload


def map_error(status: int, payload: Any) -> IncantError:
    detail, inner = _detail_of(payload)
    if status in (401, 403):
        return NotAuthorized(detail, status=status, payload=payload)
    if status == 404:
        return PromptNotFound(detail, status=status, payload=payload)
    if status == 422 and isinstance(inner, dict) and inner.get("variable"):
        return MissingVariable(detail, variable=str(inner["variable"]),
                               status=status, payload=payload)
    if status >= 500:
        return IncantUnavailable(detail, status=status, payload=payload)
    return RenderError(detail, status=status, payload=payload)


def response_error(resp: httpx.Response) -> IncantError:
    try:
        payload: Any = resp.json()
    except Exception:
        payload = resp.text
    return map_error(resp.status_code, payload)
