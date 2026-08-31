"""Serving API — the memory-only hot path. RBAC: renderer on (project, environment)."""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..core.model import ServeLabel, ServeRollout, ServeVersion
from ..service import AppContext, ServingError
from ..targeting.observed import collect_rule_flags, typed_value
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


_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _parse_pin(pin: dict | None) -> tuple[dict | None, int | None]:
    """Parse a §9 pin into ``(versions_map, rules_version)`` for the render engine.

    Accepted shapes: ``{"versions": {...}}``, ``{"rules_version": N}``, both
    together (``versions`` entries override per prompt), or — back-compat — a bare
    versions map. Anything else is a 422: a pin field the server would ignore is a
    caller believing they replayed something they didn't.

    Pins must carry FULL 40-char SHAs (the serving `versions[].commit` a caller feeds
    back). Abbreviated SHAs are rejected with 422 — an ambiguous prefix must never
    silently resolve to the wrong content (§4, §9)."""
    if not pin:
        return None, None
    rules_version: int | None = None
    if "versions" in pin or "rules_version" in pin:
        unknown = set(pin) - {"versions", "rules_version"}
        if unknown:
            raise HTTPException(
                422, f"unknown pin field(s) {sorted(unknown)!r}; a pin carries "
                     "\"versions\" and/or \"rules_version\"")
        versions = pin.get("versions") or {}
        if "rules_version" in pin:
            rv = pin["rules_version"]
            if isinstance(rv, bool) or not isinstance(rv, int) or rv < 1:
                raise HTTPException(
                    422, f"pin.rules_version must be a positive integer, got {rv!r}")
            rules_version = rv
    else:
        versions = pin  # bare versions map (back-compat)
    out: dict[str, tuple[int, str]] = {}
    for pid, entry in (versions or {}).items():
        try:
            version = int(entry["version"])
            commit = str(entry["commit"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, f"invalid pin entry for {pid!r}")
        if not _FULL_SHA.fullmatch(commit):
            raise HTTPException(
                422,
                f"pin for {pid!r}: commit must be a full 40-character SHA, "
                f"got {commit!r}",
            )
        out[pid] = (version, commit)
    return out or None, rules_version


@router.post("/prompt/{prompt_id:path}/evaluate", summary="Resolve without rendering")
def evaluate_prompt(
    prompt_id: str, req: EvaluateRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_readonly_session),
    ident: Identity = Depends(serving_identity),
):
    """Which version (at which commit) these flags would get — no variables needed,
    nothing rendered. Requires `renderer` on the prompt's (project, environment)."""
    env = _env(app, req.environment)
    _require_render(ident, prompt_id, env)
    try:
        res = app.evaluate(session, env, prompt_id, req.flags)
    except ServingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    app.observer.observe(env, req.flags)  # §7 observed flags (memory only)
    return {
        "prompt_id": prompt_id, "version": res.version, "commit": res.commit,
        "label": res.label, "matched_rule": (
            "default" if res.match_scope == "default"
            else {"scope": res.match_scope, "id": res.rule_id}
        ), "environment": env,
    }


@router.get("/prompt/{prompt_id:path}/spec", summary="What to pass to render this prompt")
def prompt_spec(
    prompt_id: str, environment: str | None = None,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_readonly_session),
    ident: Identity = Depends(serving_identity),
):
    """SDK discovery: everything a caller needs to know BEFORE rendering — the
    variables (names, types, required/optional, defaults, descriptions; merged
    across every version targeting can currently serve) and the flags that the
    active rules governing this prompt actually consult (with their enumerable
    values from eq/in-style clauses, plus any rollout bucketing flag). Renderer-
    scoped, same as render: this describes only what the credential could
    already observe by rendering."""
    env = _env(app, environment)
    _require_render(ident, prompt_id, env)
    try:
        snap = app.get_snapshot(session, env)
    except ServingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    default_v = snap.defaults.get(prompt_id)
    known = snap.versions.get(prompt_id, {})
    if default_v is None and not known:
        raise HTTPException(404, f"unknown prompt {prompt_id!r} in {env!r} — it has "
                                 "no versions and no default here")

    # Versions a render could currently resolve to: the default plus anything an
    # active rule serves (explicit version, label, or rollout band).
    rules = snap.prompt_rules(prompt_id) + snap.global_rules()
    candidates: set[int] = set()
    if default_v is not None:
        candidates.add(default_v)
    flags: dict[str, set] = collect_rule_flags(snap, rules)
    for r in rules:
        serve = r.serve
        if isinstance(serve, ServeVersion):
            candidates.add(serve.version)
        elif isinstance(serve, ServeLabel):
            v = snap.version_for_label(prompt_id, serve.label)
            if v is not None:
                candidates.add(v)
        elif isinstance(serve, ServeRollout):
            for band in serve.weights:
                if band.version is not None:
                    candidates.add(band.version)
                elif band.label is not None:
                    v = snap.version_for_label(prompt_id, band.label)
                    if v is not None:
                        candidates.add(v)
    candidates &= set(known)  # spec only versions that actually exist here

    # Variables: the mgmt-side merge (refinements + template extraction, includes
    # walked) per candidate version, folded by name. Required anywhere ⇒ required
    # (over-passing is harmless; under-passing 422s for some cohort).
    from .mgmt.helpers import _effective_variables, _includes_of
    merged: dict[str, dict] = {}
    for v in sorted(candidates):
        for var in _effective_variables(app, session, prompt_id, v):
            row = merged.setdefault(var["name"], {**var, "versions": []})
            row["versions"].append(v)
            row["required"] = row["required"] or var["required"]
            row["inferred_required"] = row["inferred_required"] or var["inferred_required"]
            for k in ("type", "default", "description"):
                if not row.get(k) and var.get(k):
                    row[k] = var[k]

    # §7 observed flags: values real traffic has sent for the flags these rules consult
    # (top 25 by recency) — so a caller can see `plan in [pro, team]` from real values.
    # Renderer keys pushed these values in the first place; same environment scope.
    flags_out = []
    for name, vals in sorted(flags.items()):
        suppressed = app.observer.is_suppressed(env, name) or session.get(
            models.ObservedFlagSuppression, (env, name)) is not None
        observed: list = []
        if not suppressed:
            rows = session.execute(
                select(models.ObservedFlag.value, models.ObservedFlag.value_type)
                .where(models.ObservedFlag.environment_id == env, models.ObservedFlag.flag == name)
                .order_by(models.ObservedFlag.last_seen.desc()).limit(25)
            ).all()
            observed = [typed_value(v, t) for v, t in rows]
        merged_vals = set(vals) | set(observed)
        flags_out.append({
            "name": name,
            "values": sorted(merged_vals, key=lambda v: (type(v).__name__, str(v))),
            "observed": bool(observed),
            "suppressed": suppressed,
        })
    return {
        "prompt_id": prompt_id,
        "environment": env,
        "default_version": default_v,
        "resolvable_versions": sorted(candidates),
        "variables": sorted(merged.values(), key=lambda r: r["name"]),
        "flags": flags_out,
        "includes": _includes_of(app, prompt_id, default_v) if default_v else [],
    }


@router.post("/prompt/{prompt_id:path}", summary="Render a prompt")
def render_prompt(
    prompt_id: str, req: RenderRequest, response: Response,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_readonly_session),
    ident: Identity = Depends(serving_identity),
):
    """Resolve this prompt through targeting for the given `flags`, render it with
    `variables`, and report exactly what was served: the version AND commit SHA of
    the prompt and every included fragment (`versions`), plus `rules_version`. Log
    that tuple beside your LLM call; feed it back as `pin` to reproduce the render
    exactly. Requires `renderer` on the prompt's (project, environment). Errors:
    422 names the missing variable; 404 explains whether the prompt is unknown or
    simply not yet targeted in this environment."""
    env = _env(app, req.environment)
    _require_render(ident, prompt_id, env)
    pin, pin_rules_version = _parse_pin(req.pin)
    start = time.perf_counter()
    try:
        resp = app.serve(session, env, prompt_id, req.flags, req.variables, pin=pin,
                         pin_rules_version=pin_rules_version)
    except ServingError as exc:
        raise HTTPException(status_code=exc.status, detail={"detail": exc.detail, **exc.extra})
    metrics.render_seconds.observe(time.perf_counter() - start)
    app.observer.observe(env, req.flags)  # §7 observed flags (memory only, never a DB write)
    metrics.renders_total.labels(prompt_id, env, str(resp["stale_rules"]).lower()).inc()
    if resp["content_fallback"]:
        metrics.content_fallbacks_total.labels(prompt_id, env).inc()
        response.headers["X-Incant-Content-Fallback"] = "true"
    # §7 "skipped, counted": rules that matched but could not serve on this render.
    if resp["skipped_rules"]:
        metrics.rule_skips_total.inc(len(resp["skipped_rules"]))
    # §14 dead-rule telemetry: the render fell through EVERY in-play rule to the
    # environment default. Sustained growth = rules that never match anything.
    if resp.pop("_rules_considered", False) and resp["matched_rule"] == "default":
        metrics.flag_eval_fallthrough_total.inc()
    return resp


@router.post("/evaluate", summary="Resolve every prompt for one user")
def evaluate_all(
    req: EvaluateRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_readonly_session),
    ident: Identity = Depends(serving_identity),
):
    """One call answering "what does this experiment change?": the resolved version
    and commit for EVERY prompt in the environment under these flags, filtered to
    the prompts your credential can render."""
    env = _env(app, req.environment)
    try:
        results = app.evaluate_all(session, env, req.flags)
    except ServingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    app.observer.observe(env, req.flags)  # §7 observed flags (memory only)
    out = {}
    for pid, res in results.items():
        if not ident.has("renderer", project=_project_of(pid), environment=env):
            continue
        out[pid] = {
            "version": res.version, "commit": res.commit, "label": res.label,
            "matched_rule": ("default" if res.match_scope == "default"
                             else {"scope": res.match_scope, "id": res.rule_id}),
        }
    return {"environment": env, "resolutions": out}


@router.get("/prompts", summary="List renderable prompts")
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
    # Renderer-scoped (not viewer): a production render key must be able to
    # DISCOVER what it can render — this lists only ids/versions/labels of
    # prompts the credential could already render one by one.
    descriptions = {p.id: p.description or "" for p in
                    session.execute(select(models.Prompt)).scalars()}
    out = []
    for pid in snap.all_prompt_ids():
        if not ident.has("renderer", project=_project_of(pid), environment=env):
            continue
        vers = snap.versions.get(pid, {})
        default_v = snap.defaults.get(pid)
        out.append({
            "prompt_id": pid,
            "description": descriptions.get(pid, ""),
            "versions": sorted(vers.keys()),
            "default": default_v,
            "labels": {v.version: v.label for v in vers.values() if v.label},
        })
    return {"environment": env, "prompts": out}
