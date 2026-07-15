"""Build a core EnvSnapshot from control-plane rows + git-derived tips.

This is the bridge from the DB world to the pure evaluator. The result is a
plain-data snapshot the render hot path can evaluate against with no further I/O.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import EnvSnapshot, VersionInfo
from ..core.parse import parse_condition, parse_rule
from ..core.model import Rule as CoreRule
from ..core.model import Segment as CoreSegment
from .. import models


def _validated_shas(session: Session) -> tuple[set[str], dict[tuple[str, int], list[str]]]:
    """Return (all valid SHAs, {(prompt,version) -> validated SHAs newest-first})."""

    rows = session.execute(
        select(models.CommitValidation)
        .where(models.CommitValidation.status == "valid")
        .order_by(models.CommitValidation.validated_at.desc())
    ).scalars().all()
    valid: set[str] = set()
    by_version: dict[tuple[str, int], list[str]] = defaultdict(list)
    for r in rows:
        valid.add(r.sha)
        by_version[(r.prompt_id, r.version_number)].append(r.sha)
    return valid, by_version


def _pointer_history(session: Session, env_id: str) -> dict[tuple[str, int], list[str]]:
    """{(prompt,version) -> [to_sha ...]} newest move first."""

    rows = session.execute(
        select(models.PointerMove)
        .where(models.PointerMove.environment_id == env_id)
        .order_by(models.PointerMove.moved_at.desc(), models.PointerMove.id.desc())
    ).scalars().all()
    hist: dict[tuple[str, int], list[str]] = defaultdict(list)
    for r in rows:
        hist[(r.prompt_id, r.version_number)].append(r.to_sha)
    return hist


def build_snapshot(session: Session, env_id: str, *, stale: bool = False) -> EnvSnapshot:
    env = session.get(models.Environment, env_id)
    if env is None:
        raise KeyError(f"unknown environment {env_id!r}")

    # The global valid-SHA set is no longer used for servability (see the
    # prompt-aware `servable_pairs` below); only the per-(prompt,version) map is.
    _valid_shas, validated_by_version = _validated_shas(session)
    pointer_hist = _pointer_history(session, env_id)

    # Defense-in-depth for the evaluator's servability check (§7). Full
    # (prompt, version, SHA) tuple integrity is enforced at *write* time — by
    # `TargetingService.make_live` and by rule pins in `_validate_rule_targets` —
    # so a live pointer or pinned SHA can never reach a prompt it wasn't validated
    # for. This snapshot check is the read-side backstop: it upgrades the old
    # `sha in valid_shas` (valid for *any* prompt) to `(prompt, sha) validated for
    # *this* prompt`. Version is intentionally absent from the closure: the core
    # evaluator's callback signature is (prompt_id, sha) (see core/evaluate.py and
    # core/model.py), and version integrity is already owned by the write-time
    # checks — so we key on (prompt, sha) and let the evaluator supply the prompt.
    servable_pairs: set[tuple[str, str]] = {
        (prompt_id, sha)
        for (prompt_id, _version), shas in validated_by_version.items()
        for sha in shas
    }

    # Versions
    versions: dict[str, dict[int, VersionInfo]] = defaultdict(dict)
    for v in session.execute(select(models.Version)).scalars().all():
        key = (v.prompt_id, v.number)
        hist = pointer_hist.get(key, [])
        live_sha = hist[0] if hist else None
        # previous distinct live SHAs, newest-first, excluding the current live one
        seen = set()
        previous = []
        for sha in hist[1:]:
            if sha not in seen and sha != live_sha:
                previous.append(sha)
                seen.add(sha)
        validated = validated_by_version.get(key, [])
        tip_sha = validated[0] if validated else None
        versions[v.prompt_id][v.number] = VersionInfo(
            version=v.number,
            live_sha=live_sha,
            tip_sha=tip_sha,
            label=v.label,
            status=v.status,
            previous_live=tuple(previous),
        )

    # Defaults
    defaults: dict[str, int] = {}
    for d in session.execute(
        select(models.EnvDefault).where(models.EnvDefault.environment_id == env_id)
    ).scalars().all():
        defaults[d.prompt_id] = d.version_number

    # Refinement defaults for optional variables — folded in so the render hot
    # path resolves them from memory rather than a per-request DB SELECT.
    refinement_defaults: dict[tuple[str, int], dict] = defaultdict(dict)
    for r in session.execute(
        select(models.VariableRefinement).where(models.VariableRefinement.default.isnot(None))
    ).scalars().all():
        refinement_defaults[(r.prompt_id, r.version_number)][r.name] = r.default

    # Rules
    rules: list[CoreRule] = []
    for r in session.execute(
        select(models.Rule).where(models.Rule.environment_id == env_id)
    ).scalars().all():
        rules.append(parse_rule({
            "id": r.id, "scope": r.scope, "prompt_id": r.prompt_id,
            "priority": r.priority, "when": r.clauses, "serve": r.serve,
            "status": r.status, "comment": r.comment,
        }))

    # Segments
    segments: dict[str, CoreSegment] = {}
    for s in session.execute(
        select(models.Segment).where(models.Segment.environment_id == env_id)
    ).scalars().all():
        segments[s.name] = CoreSegment(
            name=s.name, condition=parse_condition(s.clauses), version=s.version
        )

    # Kill switches
    killed = {
        k.prompt_id
        for k in session.execute(
            select(models.KillSwitch).where(
                models.KillSwitch.environment_id == env_id,
                models.KillSwitch.engaged.is_(True),
            )
        ).scalars().all()
    }

    return EnvSnapshot(
        environment=env_id,
        rules_version=env.rules_version,
        rules=rules,
        segments=segments,
        defaults=defaults,
        refinement_defaults={k: dict(v) for k, v in refinement_defaults.items()},
        versions={k: dict(v) for k, v in versions.items()},
        track_tip=env.track_tip,
        stale=stale,
        killed=killed,
        servable=lambda prompt_id, sha: (prompt_id, sha) in servable_pairs,
    )
