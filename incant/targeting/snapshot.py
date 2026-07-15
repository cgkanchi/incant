"""Build a core EnvSnapshot from control-plane rows + git-derived tips.

This is the bridge from the DB world to the pure evaluator. The result is a
plain-data snapshot the render hot path can evaluate against with no further I/O.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import EnvSnapshot, VersionInfo
from ..core.parse import parse_condition, parse_rule
from ..core.model import Rule as CoreRule
from ..core.model import Segment as CoreSegment
from .. import models

# Newest-K per (prompt, version) kept in the ordering lists these helpers build. Only the
# HEAD of each list is ever read downstream — tip_sha (the newest validated) and the
# `previous_live` §10 fallback, which never reaches past the most recent few moves — so
# windowing to K bounds each snapshot rebuild to K rows per (prompt, version) instead of
# the whole history. (SQLite ≥3.25 / Postgres window functions.)
_VALIDATED_ORDER_CAP = 50     # tip_sha reads only [0]; K is defensive headroom
_POINTER_HISTORY_CAP = 100    # previous_live scans distinct recent moves; nothing past ~K


def _validated_shas(session: Session) -> tuple[set[tuple[str, str]], dict[tuple[str, int], list[str]]]:
    """Return (all validated (prompt_id, sha) pairs, {(prompt,version) -> newest-K SHAs}).

    Two DELIBERATELY different reads:

    * The servable-pair set must stay COMPLETE — correctness over economy. ``servable``
      legitimately answers True for ANY (prompt, sha) ever validated for that prompt: an
      old pinned rule or a rolled-back live pointer can reference a SHA far down the
      history, and warming/serving must still recognise it. So we fetch the full
      (prompt_id, sha) pair set — one two-column indexed scan, deliberately NOT windowed.

    * The per-(prompt,version) ordering list only feeds ``tip_sha`` (its head). Window it
      to the newest ``_VALIDATED_ORDER_CAP`` per (prompt, version) so a version with a huge
      validation history doesn't materialise in full on every snapshot rebuild."""

    # Complete servable pairs — one indexed two-column scan, deliberately unwindowed.
    pairs = session.execute(
        select(models.CommitValidation.prompt_id, models.CommitValidation.sha)
        .where(models.CommitValidation.status == "valid")
    ).all()
    servable_pairs: set[tuple[str, str]] = {(pid, sha) for pid, sha in pairs}

    # Newest-K ordering lists — windowed; only the head (tip_sha) is ever consumed.
    rn = func.row_number().over(
        partition_by=(models.CommitValidation.prompt_id, models.CommitValidation.version_number),
        order_by=(models.CommitValidation.validated_at.desc(), models.CommitValidation.id.desc()),
    ).label("rn")
    ranked = (
        select(models.CommitValidation.prompt_id, models.CommitValidation.version_number,
               models.CommitValidation.sha, rn)
        .where(models.CommitValidation.status == "valid")
        .subquery()
    )
    rows = session.execute(
        select(ranked.c.prompt_id, ranked.c.version_number, ranked.c.sha)
        .where(ranked.c.rn <= _VALIDATED_ORDER_CAP)
        .order_by(ranked.c.prompt_id, ranked.c.version_number, ranked.c.rn)
    ).all()
    by_version: dict[tuple[str, int], list[str]] = defaultdict(list)
    for pid, ver, sha in rows:            # rn-ascending == newest-first within each key
        by_version[(pid, ver)].append(sha)
    return servable_pairs, by_version


def _pointer_history(session: Session, env_id: str) -> dict[tuple[str, int], list[str]]:
    """{(prompt,version) -> [to_sha ...]} newest move first, capped at the newest
    ``_POINTER_HISTORY_CAP`` moves per (prompt, version).

    Only the head (current live) and the recent distinct SHAs behind it (the §10
    ``previous_live`` fallback) are ever read, so windowing to K bounds the per-version
    work without changing that behaviour for recent history. A window function keeps it one
    query with at most K rows per key rather than every historical move."""

    rn = func.row_number().over(
        partition_by=(models.PointerMove.prompt_id, models.PointerMove.version_number),
        order_by=(models.PointerMove.moved_at.desc(), models.PointerMove.id.desc()),
    ).label("rn")
    ranked = (
        select(models.PointerMove.prompt_id, models.PointerMove.version_number,
               models.PointerMove.to_sha, rn)
        .where(models.PointerMove.environment_id == env_id)
        .subquery()
    )
    rows = session.execute(
        select(ranked.c.prompt_id, ranked.c.version_number, ranked.c.to_sha)
        .where(ranked.c.rn <= _POINTER_HISTORY_CAP)
        .order_by(ranked.c.prompt_id, ranked.c.version_number, ranked.c.rn)
    ).all()
    hist: dict[tuple[str, int], list[str]] = defaultdict(list)
    for pid, ver, to_sha in rows:         # rn-ascending == newest move first within each key
        hist[(pid, ver)].append(to_sha)
    return hist


def build_snapshot(session: Session, env_id: str, *, stale: bool = False) -> EnvSnapshot:
    env = session.get(models.Environment, env_id)
    if env is None:
        raise KeyError(f"unknown environment {env_id!r}")

    # `servable_pairs` is the COMPLETE (prompt, sha) validation set (see `_validated_shas`);
    # `validated_by_version` is windowed and feeds only tip_sha. They are two reads on
    # purpose — servability must stay complete while the ordering lists may be capped.
    servable_pairs, validated_by_version = _validated_shas(session)
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
    # The pair set is deliberately unwindowed: an old validated SHA (pinned rule /
    # rolled-back pointer) must remain servable no matter how deep in history it sits.

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
