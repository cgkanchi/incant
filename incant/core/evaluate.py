"""The evaluator: resolve (prompt_id, flags) -> Resolution against an EnvSnapshot.

Evaluation order per prompt (first match wins):
    global rules -> prompt rules -> environment default.

A rule that resolves to something unservable is *skipped* (counted by the caller
via the returned skip list), and evaluation continues. So is a rule whose condition
is broken (a missing segment, a segment cycle) — never silently False. Archived
versions do not serve: a rule targeting one is skipped, labels ignore them, and an
archived environment default is Unservable. The environment default serves a version
at its live pointer, with the §10 within-version fallback as the only permitted
content degradation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .clauses import eval_condition
from .errors import MissingSegment, SegmentCycle, UnresolvedPrompt, Unservable
from .model import (
    EnvSnapshot,
    Resolution,
    Rule,
    Serve,
    ServeLabel,
    ServeRollout,
    ServeVersion,
    VersionInfo,
)
from .rollout import pick_band


@dataclass
class Skip:
    """Records a rule that was matched but could not serve (surfaced as a metric)."""

    rule_id: str
    prompt_id: str
    reason: str


def _servable_sha_for_version(
    snap: EnvSnapshot,
    prompt_id: str,
    vinfo: VersionInfo,
) -> tuple[str | None, bool]:
    """Resolve a version's live SHA, applying the within-version fallback.

    Returns ``(sha, content_fallback)``. ``sha`` is ``None`` if nothing in the
    version's pointer history is servable.
    """

    if vinfo.live_sha and snap.servable(prompt_id, vinfo.version, vinfo.live_sha):
        return vinfo.live_sha, False
    # §10 within-version fallback: newest previous-live SHA that is still servable.
    for sha in vinfo.previous_live:
        if snap.servable(prompt_id, vinfo.version, sha):
            return sha, True
    return None, False


def _resolve_serve(
    snap: EnvSnapshot,
    prompt_id: str,
    flags: Mapping[str, Any],
    rule: Rule,
) -> tuple[Resolution | None, str | None]:
    """Turn a rule's serve target into a Resolution for this prompt.

    Returns ``(resolution, skip_reason)``. ``(None, None)`` means "this rule does
    not apply to this prompt, continue" (e.g. global label not present). ``(None,
    reason)`` means "matched but unservable, skip and count".
    """

    serve: Serve = rule.serve
    scope_prompt_id = None if rule.scope == "global" else prompt_id

    # -- rollout --------------------------------------------------------
    if isinstance(serve, ServeRollout):
        if serve.bucket_by not in flags:
            return None, None  # missing bucket_by flag -> rule falls through
        band = pick_band(serve.weights, rule.id, flags[serve.bucket_by], scope_prompt_id)
        if band is None:
            return None, None
        if band.is_default:
            return None, None  # bucketed into "default" -> fall through to next rule/default
        if band.label is not None:
            version = snap.version_for_label(prompt_id, band.label)
            if version is None:
                return None, None  # prompt doesn't participate; continue
            label = band.label
        else:
            version = band.version
            label = None
        return _serve_version_live(snap, prompt_id, rule, version, label)

    # -- label (global) -------------------------------------------------
    if isinstance(serve, ServeLabel):
        version = snap.version_for_label(prompt_id, serve.label)
        if version is None:
            return None, None  # prompt has no version with this label; continue
        return _serve_version_live(snap, prompt_id, rule, version, serve.label)

    # -- explicit version (live / tip / pinned SHA) ---------------------
    if isinstance(serve, ServeVersion):
        vinfo = snap.version_info(prompt_id, serve.version)
        if vinfo is None:
            return None, f"version {serve.version} does not exist"
        if vinfo.status == "archived":
            return None, f"version {serve.version} is archived"
        if serve.at == "tip":
            sha = vinfo.tip_sha
            if not sha or not snap.servable(prompt_id, serve.version, sha):
                return None, "tip unservable"
            return (
                Resolution(prompt_id, serve.version, sha, "tip", rule.scope, rule.id, vinfo.label),
                None,
            )
        if serve.at == "sha":
            sha = serve.sha
            if not sha or not snap.servable(prompt_id, serve.version, sha):
                return None, "pinned sha unservable"
            return (
                Resolution(prompt_id, serve.version, sha, "sha", rule.scope, rule.id, vinfo.label),
                None,
            )
        # at == "live"
        return _serve_version_live(snap, prompt_id, rule, serve.version, vinfo.label)

    return None, "unknown serve target"


def _serve_version_live(
    snap: EnvSnapshot,
    prompt_id: str,
    rule: Rule,
    version: int | None,
    label: str | None,
) -> tuple[Resolution | None, str | None]:
    if version is None:
        return None, "no version"
    vinfo = snap.version_info(prompt_id, version)
    if vinfo is None:
        return None, f"version {version} does not exist"
    if vinfo.status == "archived":
        return None, f"version {version} is archived"
    sha, fallback = _servable_sha_for_version(snap, prompt_id, vinfo)
    if sha is None:
        return None, "no servable pointer in history"
    return (
        Resolution(
            prompt_id, version, sha, "live", rule.scope, rule.id,
            label or vinfo.label, content_fallback=fallback,
        ),
        None,
    )


def resolve(
    snap: EnvSnapshot,
    prompt_id: str,
    flags: Mapping[str, Any],
    *,
    skips: list[Skip] | None = None,
) -> Resolution:
    """Resolve a prompt to a concrete version+SHA. Raises on unresolved/unservable."""

    if skips is None:
        skips = []

    killed = prompt_id in snap.killed

    def matches(rule: Rule) -> bool:
        # A broken condition (missing segment / cycle) is a counted skip, never a
        # match and never a silent False that `not:` could invert.
        try:
            return eval_condition(rule.when, flags, snap.segments)
        except (MissingSegment, SegmentCycle) as exc:
            skips.append(Skip(rule.id, prompt_id, str(exc)))
            return False

    for rule in () if killed else snap.global_rules():
        if not matches(rule):
            continue
        res, reason = _resolve_serve(snap, prompt_id, flags, rule)
        if res is not None:
            return res
        if reason is not None:
            skips.append(Skip(rule.id, prompt_id, reason))

    for rule in () if killed else snap.prompt_rules(prompt_id):
        if not matches(rule):
            continue
        res, reason = _resolve_serve(snap, prompt_id, flags, rule)
        if res is not None:
            return res
        if reason is not None:
            skips.append(Skip(rule.id, prompt_id, reason))

    # environment default (a version, at its live pointer)
    default_version = snap.defaults.get(prompt_id)
    if default_version is None:
        raise UnresolvedPrompt(prompt_id, snap.environment)
    vinfo = snap.version_info(prompt_id, default_version)
    if vinfo is None:
        raise UnresolvedPrompt(prompt_id, snap.environment)
    if vinfo.status == "archived":
        raise Unservable(prompt_id, default_version, reason="archived")
    sha, fallback = _servable_sha_for_version(snap, prompt_id, vinfo)
    if sha is None:
        raise Unservable(prompt_id, default_version)
    return Resolution(
        prompt_id, default_version, sha, "live", "default", None,
        vinfo.label, content_fallback=fallback,
    )
