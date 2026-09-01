"""The evaluator: resolve (prompt_id, flags) -> Resolution against an EnvSnapshot.

Evaluation order per prompt (first match wins): the prompt's rules by priority, then
the environment default. A rule that resolves to something unservable is *skipped*
(counted by the caller via the returned skip list) and evaluation continues. Archived
versions do not serve: a rule targeting one is skipped and an archived environment
default is Unservable. The environment default serves a version at its live pointer,
with the §10 within-version fallback as the only permitted content degradation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .clauses import eval_condition
from .errors import UnresolvedPrompt, Unservable
from .model import EnvSnapshot, Resolution, Rule, VersionInfo


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
    snap: EnvSnapshot, prompt_id: str, rule: Rule,
) -> tuple[Resolution | None, str | None]:
    """Turn a rule's serve target into a Resolution. Returns ``(resolution,
    skip_reason)``; ``(None, reason)`` means "matched but unservable, skip and count"."""

    serve = rule.serve
    vinfo = snap.version_info(prompt_id, serve.version)
    if vinfo is None:
        return None, f"version {serve.version} does not exist"
    if vinfo.status == "archived":
        return None, f"version {serve.version} is archived"
    if serve.at == "tip":
        sha = vinfo.tip_sha
        if not sha or not snap.servable(prompt_id, serve.version, sha):
            return None, "tip unservable"
        return Resolution(prompt_id, serve.version, sha, "tip", "prompt", rule.id), None
    if serve.at == "sha":
        sha = serve.sha
        if not sha or not snap.servable(prompt_id, serve.version, sha):
            return None, "pinned sha unservable"
        return Resolution(prompt_id, serve.version, sha, "sha", "prompt", rule.id), None
    # at == "live"
    sha, fallback = _servable_sha_for_version(snap, prompt_id, vinfo)
    if sha is None:
        return None, "no servable pointer in history"
    return (
        Resolution(prompt_id, serve.version, sha, "live", "prompt", rule.id,
                   content_fallback=fallback),
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

    for rule in () if killed else snap.prompt_rules(prompt_id):
        if not eval_condition(rule.when, flags):
            continue
        res, reason = _resolve_serve(snap, prompt_id, rule)
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
        prompt_id, default_version, sha, "live", "default", None, content_fallback=fallback,
    )
