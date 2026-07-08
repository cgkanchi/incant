"""Serving API — the memory-only hot path. RBAC: renderer on (project, environment)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from .. import models
from ..service import AppContext, ServingError
from . import metrics
from .auth import AuthError, Identity
from .deps import app_context, get_readonly_session, serving_identity
from .schemas import EvaluateRequest, RenderRequest

router = APIRouter(tags=["serving"])


def _project_of(prompt_id: str) -> str:
    return prompt_id.split("/", 1)[0]


def _require_render(ident: Identity, prompt_id: str, env: str) -> None:
    try:
        ident.require("renderer", project=_project_of(prompt_id), environment=env)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


def _env(app: AppContext, req_env: str | None) -> str:
    return req_env or app.settings.default_environment


def _parse_pin(pin: dict | None) -> dict | None:
    """Turn a request pin ({"versions": {pid: {version, commit}}}) into the render
    engine's shape (pid -> (version, commit))."""
    if not pin:
        return None
    versions = pin.get("versions", pin)  # accept the bare versions map too
    out: dict[str, tuple[int, str]] = {}
    for pid, entry in (versions or {}).items():
        try:
            out[pid] = (int(entry["version"]), str(entry["commit"]))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, f"invalid pin entry for {pid!r}")
    return out or None


@router.post("/prompt/{prompt_id:path}/evaluate")
def evaluate_prompt(
    prompt_id: str, req: EvaluateRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_readonly_session),
    ident: Identity = Depends(serving_identity),
):
    env = _env(app, req.environment)
    _require_render(ident, prompt_id, env)
    try:
        res = app.evaluate(session, env, prompt_id, req.flags)
    except ServingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return {
        "prompt_id": prompt_id, "version": res.version, "commit": res.commit[:7],
        "label": res.label, "matched_rule": (
            "default" if res.match_scope == "default"
            else {"scope": res.match_scope, "id": res.rule_id}
        ), "environment": env,
    }


@router.post("/prompt/{prompt_id:path}")
def render_prompt(
    prompt_id: str, req: RenderRequest, response: Response,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_readonly_session),
    ident: Identity = Depends(serving_identity),
):
    env = _env(app, req.environment)
    _require_render(ident, prompt_id, env)
    pin = _parse_pin(req.pin)
    start = time.perf_counter()
    try:
        resp = app.serve(session, env, prompt_id, req.flags, req.variables, pin=pin)
    except ServingError as exc:
        raise HTTPException(status_code=exc.status, detail={"detail": exc.detail, **exc.extra})
    metrics.render_seconds.observe(time.perf_counter() - start)
    metrics.renders_total.labels(prompt_id, env, str(resp["stale_rules"]).lower()).inc()
    if resp["content_fallback"]:
        metrics.content_fallbacks_total.labels(prompt_id, env).inc()
        response.headers["X-Incant-Content-Fallback"] = "true"
    return resp


@router.post("/evaluate")
def evaluate_all(
    req: EvaluateRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_readonly_session),
    ident: Identity = Depends(serving_identity),
):
    env = _env(app, req.environment)
    try:
        results = app.evaluate_all(session, env, req.flags)
    except ServingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    out = {}
    for pid, res in results.items():
        if not ident.has("renderer", project=_project_of(pid), environment=env):
            continue
        out[pid] = {
            "version": res.version, "commit": res.commit[:7], "label": res.label,
            "matched_rule": ("default" if res.match_scope == "default"
                             else {"scope": res.match_scope, "id": res.rule_id}),
        }
    return {"environment": env, "resolutions": out}


@router.get("/prompts")
def list_prompts(
    environment: str | None = None,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_readonly_session),
    ident: Identity = Depends(serving_identity),
):
    env = _env(app, environment)
    try:
        snap = app.get_snapshot(session, env)
    except ServingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    out = []
    for pid in snap.all_prompt_ids():
        if not ident.has("viewer", project=_project_of(pid), environment=env):
            continue
        vers = snap.versions.get(pid, {})
        default_v = snap.defaults.get(pid)
        out.append({
            "prompt_id": pid,
            "versions": sorted(vers.keys()),
            "default": default_v,
            "labels": {v.version: v.label for v in vers.values() if v.label},
        })
    return {"environment": env, "prompts": out}
