"""Build a core EnvSnapshot from control-plane rows + git-derived tips.

This is the bridge from the DB world to the pure evaluator. The result is a
plain-data snapshot the render hot path can evaluate against with no further I/O.
"""

from __future__ import annotations

import time
from collections import OrderedDict, defaultdict

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


def _validated_order(session: Session) -> dict[tuple[str, int], list[str]]:
    """{(prompt,version) -> newest-K validated SHAs}, newest-first — feeds ``tip_sha``
    and the referenced-pair set. Windowed to ``_VALIDATED_ORDER_CAP`` per key so a
    version with a huge validation history doesn't materialise in full on every
    snapshot rebuild."""
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
    return by_version


# Servability fallback memo for (prompt, sha) pairs OUTSIDE the referenced set — an
# exotic old rule pin or request pin reaching past the windowed history. Positive
# answers are immutable facts (validation rows are never deleted) and cache forever;
# negatives expire after a short TTL (the SHA may simply not be validated YET).
# Bounded LRU; a DB outage during a lookup returns False (fail closed: §5's "only
# validated SHAs serve" beats availability for a pin no warm state can vouch for)
# and is never cached.
_FALLBACK_MEMO: "OrderedDict[tuple[str, str], tuple[bool, float]]" = OrderedDict()
_FALLBACK_MEMO_MAX = 8192
_FALLBACK_NEG_TTL = 30.0


def clear_servable_memo() -> None:
    """Reset the fallback memo (tests / service reset — commit SHAs are
    deterministic under INCANT_FIXED_GIT_DATE, so entries could leak across
    freshly-reset databases)."""
    _FALLBACK_MEMO.clear()


def _validated_in_db(prompt_id: str, sha: str) -> bool:
    key = (prompt_id, sha)
    hit = _FALLBACK_MEMO.get(key)
    if hit is not None:
        ok, at = hit
        if ok or (time.time() - at) < _FALLBACK_NEG_TTL:
            _FALLBACK_MEMO.move_to_end(key)
            return ok
    from ..db import session_factory  # lazy: keep module import-light for pure tests
    try:
        s = session_factory()()
        try:
            ok = s.execute(
                select(models.CommitValidation.id).where(
                    models.CommitValidation.prompt_id == prompt_id,
                    models.CommitValidation.sha == sha,
                    models.CommitValidation.status == "valid",
                ).limit(1)
            ).first() is not None
        finally:
            s.close()
    except Exception:
        return False  # outage: fail closed, cache nothing
    _FALLBACK_MEMO[key] = (ok, time.time())
    _FALLBACK_MEMO.move_to_end(key)
    if len(_FALLBACK_MEMO) > _FALLBACK_MEMO_MAX:
        _FALLBACK_MEMO.popitem(last=False)
    return ok


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


def snapshot_from_state(
    state: dict, env_id: str, rules_version: int, servable,
) -> EnvSnapshot:
    """Rebuild an evaluable :class:`EnvSnapshot` from a revision's captured state
    (``TargetingService.capture_state``) — the §9 ``pin.rules_version`` replay.

    Two deliberate departures from a live snapshot:

    * ``previous_live`` is empty — a replay never degrades to a §10 fallback; if
      the recorded SHA's content is gone, the render 409s rather than lies;
    * ``servable`` is the CURRENT validated set (validation history only grows,
      so anything servable then is servable now).

    Tips are the tips AS OF THE TARGETING CHANGE that produced the revision — a
    later commit that moved a tip under an unchanged ``rules_version`` is not
    recoverable from targeting state alone. Exact content replay is what
    ``pin.versions`` is for; this replays *targeting*.
    """
    rules = [parse_rule(r) for r in state.get("rules", [])]
    segments = {
        s["name"]: CoreSegment(name=s["name"], condition=parse_condition(s["clauses"]),
                               version=s.get("version", 1))
        for s in state.get("segments", [])
    }
    versions: dict[str, dict[int, VersionInfo]] = defaultdict(dict)
    for key, vinfo in state.get("versions", {}).items():
        prompt_id, _, vpart = key.rpartition("@v")
        number = int(vpart)
        versions[prompt_id][number] = VersionInfo(
            version=number,
            live_sha=vinfo.get("live"),
            tip_sha=vinfo.get("tip"),
            label=vinfo.get("label"),
            status=vinfo.get("status", "active"),
            previous_live=(),
        )
    return EnvSnapshot(
        environment=env_id,
        rules_version=rules_version,
        rules=rules,
        segments=segments,
        defaults=dict(state.get("defaults", {})),
        refinement_defaults={},   # supplied by the caller from the live snapshot
        versions={k: dict(v) for k, v in versions.items()},
        stale=False,
        killed=set(state.get("kills", [])),
        servable=servable,
    )


def build_snapshot(session: Session, env_id: str, *, stale: bool = False) -> EnvSnapshot:
    env = session.get(models.Environment, env_id)
    if env is None:
        raise KeyError(f"unknown environment {env_id!r}")

    validated_by_version = _validated_order(session)
    pointer_hist = _pointer_history(session, env_id)

    # Servability (§7 defense-in-depth, read-side backstop to the write-time
    # (prompt, version, SHA) integrity checks in make_live/_validate_rule_targets).
    # The closure answers from the REFERENCED pair set — every (prompt, sha) this
    # snapshot itself enumerates: recent validated history (tips), the live-pointer
    # history (§10 fallbacks), and explicit rule SHA pins. That bounds the per-
    # rebuild work to what targeting references, O(referenced) instead of O(every
    # commit ever). A (prompt, sha) OUTSIDE the set — an exotic pin deeper than the
    # windowed history — falls through to a memoized one-row DB check
    # (`_validated_in_db`), mirroring §8's content-cache-miss exception; during a DB
    # outage that fallback fails CLOSED (the §10 rules-freeze posture protects
    # everything warm; a pin nothing warm can vouch for gets a 409, not a guess).
    referenced: set[tuple[str, str]] = set()
    for (pid, _ver), shas in validated_by_version.items():
        referenced.update((pid, sha) for sha in shas)
    for (pid, _ver), shas in pointer_hist.items():
        referenced.update((pid, sha) for sha in shas)

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

    # Rules — explicit SHA pins join the referenced servable set (they may reach
    # past the windowed history above and must not depend on the DB fallback).
    rules: list[CoreRule] = []
    for r in session.execute(
        select(models.Rule).where(models.Rule.environment_id == env_id)
    ).scalars().all():
        serve = r.serve if isinstance(r.serve, dict) else {}
        if r.prompt_id and serve.get("at") == "sha" and serve.get("sha"):
            referenced.add((r.prompt_id, serve["sha"]))
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
        servable=lambda prompt_id, sha: (
            (prompt_id, sha) in referenced or _validated_in_db(prompt_id, sha)
        ),
    )
