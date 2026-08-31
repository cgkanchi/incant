"""TargetingService — per-environment rules, segments, pointers, defaults, kills.

Every mutation snapshots to rule_revisions and bumps the environment's monotonic
rules_version. Pointer-class changes (make-live, default) are the governed acts;
rule/segment edits are low-friction. Rules and pointers may only reference
validated SHAs.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..core.parse import parse_condition, parse_rule as parse_core_rule
from .audit import record_audit


def _version_key(prompt_id: str, version_number: int) -> str:
    """JSON object keys must be strings — ``(prompt, version)`` flattens to
    ``"<prompt_id>@v<N>"`` in a revision's captured state."""
    return f"{prompt_id}@v{version_number}"


def capture_state(session: Session, env_id: str) -> dict:
    """The environment's COMPLETE targeting state, as one JSON-serializable dict —
    recorded on every revision (``_bump``) so rollback can restore everything and
    ``pin.rules_version`` can replay it (§7, §9). Content is still only SHAs.

    ``versions`` folds live pointers (newest move), tips (newest validated), labels
    and status per ``(prompt, version)`` — the same inputs an ``EnvSnapshot``'s
    ``VersionInfo`` is built from, minus ``previous_live`` (a replay never falls
    back within a version: degraded replay would be a lie, it 409s instead)."""
    rules = [
        _rule_snapshot(r)
        for r in session.execute(
            select(models.Rule).where(models.Rule.environment_id == env_id)
            .order_by(models.Rule.priority, models.Rule.id)
        ).scalars()
    ]
    segments = [
        {"name": s.name, "clauses": s.clauses, "version": s.version}
        for s in session.execute(
            select(models.Segment).where(models.Segment.environment_id == env_id)
        ).scalars()
    ]
    defaults = {
        d.prompt_id: d.version_number
        for d in session.execute(
            select(models.EnvDefault).where(models.EnvDefault.environment_id == env_id)
        ).scalars()
    }
    kills = sorted(
        k.prompt_id
        for k in session.execute(
            select(models.KillSwitch).where(
                models.KillSwitch.environment_id == env_id,
                models.KillSwitch.engaged.is_(True),
            )
        ).scalars()
    )

    # Current live pointer per (prompt, version): newest move, one windowed query.
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
    live = {
        (pid, ver): sha
        for pid, ver, sha in session.execute(
            select(ranked.c.prompt_id, ranked.c.version_number, ranked.c.to_sha)
            .where(ranked.c.rn == 1)
        )
    }

    # Tip per (prompt, version): newest validated commit, one windowed query.
    vrn = func.row_number().over(
        partition_by=(models.CommitValidation.prompt_id,
                      models.CommitValidation.version_number),
        order_by=(models.CommitValidation.validated_at.desc(),
                  models.CommitValidation.id.desc()),
    ).label("rn")
    vranked = (
        select(models.CommitValidation.prompt_id, models.CommitValidation.version_number,
               models.CommitValidation.sha, vrn)
        .where(models.CommitValidation.status == "valid")
        .subquery()
    )
    tips = {
        (pid, ver): sha
        for pid, ver, sha in session.execute(
            select(vranked.c.prompt_id, vranked.c.version_number, vranked.c.sha)
            .where(vranked.c.rn == 1)
        )
    }

    versions: dict[str, dict] = {}
    for v in session.execute(select(models.Version)).scalars():
        key = (v.prompt_id, v.number)
        versions[_version_key(*key)] = {
            "live": live.get(key), "tip": tips.get(key),
            "label": v.label, "status": v.status,
        }

    return {"rules": rules, "segments": segments, "defaults": defaults,
            "kills": kills, "versions": versions}


class TargetingError(Exception):
    pass


@dataclass
class MakeLiveOutcome:
    status: str          # always "live" — pointer moves are unilateral
    move_id: int | None
    rules_version: int


class TargetingService:
    def __init__(self, session: Session, actor: str = "system") -> None:
        self.s = session
        self.actor = actor

    # ── helpers ──────────────────────────────────────────────────────

    def _env(self, env_id: str) -> models.Environment:
        # Serialize the COMPLETE mutation before touching any targeting child row.
        # Locking only while incrementing the counter lets two writers capture
        # mutually-incomplete states even though their version numbers are unique.
        env = self.s.execute(
            select(models.Environment)
            .where(models.Environment.id == env_id)
            .with_for_update()
        ).scalar_one_or_none()
        if env is None:
            raise TargetingError(f"unknown environment {env_id!r}")
        # Environments created before state revisions existed (and direct model
        # creations in integrations) get an exact baseline lazily, before their
        # first serialized mutation.
        has_revision = self.s.execute(
            select(models.RuleRevision.id).where(
                models.RuleRevision.environment_id == env_id
            ).limit(1)
        ).first()
        if has_revision is None:
            self.s.add(models.RuleRevision(
                environment_id=env.id,
                kind="baseline",
                rules_version=env.rules_version,
                snapshot={"environment_id": env.id},
                state=capture_state(self.s, env.id),
                actor=self.actor,
                comment="initial targeting state",
            ))
            self.s.flush()
        return env

    def ensure_baseline(self, env_id: str) -> int:
        """Materialize an environment's exact initial revision under its row lock."""
        return self._env(env_id).rules_version

    def _bump(self, env: models.Environment, kind: str, snapshot: dict,
              rule_id: str | None = None, comment: str = "") -> int:
        # ``_env`` holds the environment row lock from before the mutation, so this
        # Python increment and the captured post-change state are one serial history
        # point.  The unique constraint is the database backstop.
        env.rules_version += 1
        self.s.flush()
        # CHECKPOINTING: the full state is materialized every Kth revision (plus
        # always on baseline/rollback — anchors whose per-object snapshot cannot
        # describe their whole effect). Everything else stores only its per-object
        # change; ``state_at`` reconstructs the in-between states by forward-
        # applying at most K-1 changes from the nearest older checkpoint. O(1)
        # recent, bounded slow path old, O(N/K) storage.
        from ..config import get_settings
        interval = get_settings().revision_checkpoint_interval
        checkpoint = (kind in ("baseline", "rollback")
                      or env.rules_version % interval == 0)
        rev = models.RuleRevision(
            environment_id=env.id, rule_id=rule_id, kind=kind,
            rules_version=env.rules_version,
            snapshot=snapshot, actor=self.actor, comment=comment,
            state=capture_state(self.s, env.id) if checkpoint else None,
        )
        self.s.add(rev)
        self.s.flush()
        return env.rules_version

    def _version_exists(self, prompt_id: str, version_number: int) -> bool:
        return self.s.execute(
            select(models.Version.id).where(
                models.Version.prompt_id == prompt_id,
                models.Version.number == version_number,
                models.Version.status == "active",
            )
        ).first() is not None

    def _is_validated_for(self, prompt_id: str, version_number: int, sha: str) -> bool:
        return self.s.execute(
            select(models.CommitValidation).where(
                models.CommitValidation.sha == sha,
                models.CommitValidation.prompt_id == prompt_id,
                models.CommitValidation.version_number == version_number,
                models.CommitValidation.status == "valid",
            )
        ).first() is not None

    def _segment_names(self, env_id: str) -> set[str]:
        return set(self.s.execute(select(models.Segment.name).where(
            models.Segment.environment_id == env_id)).scalars())

    def _validate_segment_refs(self, env_id: str, when) -> None:
        """Every segment a condition references must exist in this environment. A
        dangling reference is not "never matches" — the evaluator skips and counts the
        rule — so refuse it at write time where the author can fix it."""
        refs = _segment_refs(when)
        if not refs:
            return
        missing = sorted(refs - self._segment_names(env_id))
        if missing:
            raise TargetingError(
                f"unknown segment(s) {missing} in {env_id!r} — create the segment first")

    def _validate_segment_cycle(self, env_id: str, name: str, clauses) -> None:
        """Segments may reference segments; refuse a reference chain that leads back to
        the segment being saved (a -> b -> a) — such a rule could never match."""
        graph = {
            s.name: _segment_refs(s.clauses)
            for s in self.s.execute(select(models.Segment).where(
                models.Segment.environment_id == env_id)).scalars()
        }
        graph[name] = _segment_refs(clauses)
        path: list[str] = []
        seen: set[str] = set()

        def walk(node: str) -> bool:
            if node == name and path:
                return True
            if node in seen:
                return False
            seen.add(node)
            path.append(node)
            for nxt in sorted(graph.get(node, ())):
                if walk(nxt):
                    return True
            path.pop()
            return False

        if walk(name):
            raise TargetingError("segment cycle: " + " -> ".join(path + [name]))

    def _validate_rule_targets(self, scope: str, prompt_id: str | None, serve: dict) -> None:
        """Integrity: a prompt-scoped rule may only serve versions that exist for the
        prompt (§7), and a pinned SHA must be a validated commit for that
        prompt/version. A global rule serving an explicit version must name a version
        at least one prompt actively has (labels are not checked — a prompt without the
        label simply skips the rule)."""
        # (version_number, at, sha) targets carried by this serve.
        targets: list[tuple[int, str | None, str | None]] = []
        if "version" in serve:
            targets.append((int(serve["version"]), serve.get("at"), serve.get("sha")))
        if isinstance(serve.get("rollout"), dict):
            for band in serve["rollout"].get("weights", []):
                if band.get("version") is not None and not band.get("default"):
                    targets.append((int(band["version"]), None, None))
        if scope != "prompt" or not prompt_id:
            for version_number, _at, _sha in targets:
                exists = self.s.execute(select(models.Version.id).where(
                    models.Version.number == version_number,
                    models.Version.status == "active",
                )).first() is not None
                if not exists:
                    raise TargetingError(
                        f"version {version_number} exists (active) for no prompt — a global "
                        "rule serving it could never match")
            return
        for version_number, at, sha in targets:
            if not self._version_exists(prompt_id, version_number):
                raise TargetingError(
                    f"version {version_number} does not exist or is archived for "
                    f"prompt {prompt_id!r}")
            if at == "sha":
                if not sha:
                    raise TargetingError(
                        f"serve pins at a SHA but none was given for {prompt_id!r} "
                        f"v{version_number}")
                if not self._is_validated_for(prompt_id, version_number, sha):
                    raise TargetingError(
                        f"SHA {sha} is not a validated commit for {prompt_id!r} "
                        f"v{version_number}")

    # ── rules ────────────────────────────────────────────────────────

    def list_rules(self, env_id: str) -> list[models.Rule]:
        return list(self.s.execute(
            select(models.Rule).where(models.Rule.environment_id == env_id)
            .order_by(models.Rule.priority)
        ).scalars())

    def upsert_rule(self, env_id: str, rule: dict) -> models.Rule:
        env = self._env(env_id)
        # Validate serve/when shape early via the core parser.
        try:
            parse_core_rule({**rule, "id": rule.get("id", "tmp")})
        except (KeyError, TypeError, ValueError) as exc:
            raise TargetingError(f"invalid rule: {exc}") from exc
        rid = rule["id"]
        existing = self.s.get(models.Rule, rid)
        if existing is not None and existing.environment_id != env_id:
            # Rule ids are globally unique; refuse to edit a rule that lives in
            # another environment via this env's URL (cross-env capture).
            raise TargetingError(
                f"rule {rid!r} belongs to environment {existing.environment_id!r}, not {env_id!r}")
        # Compute the MERGED row first and validate it BEFORE the session is touched: a
        # caller that catches the TargetingError and carries on in the same session must
        # not find a half-built rule committed under it.
        scope = rule.get("scope", (existing.scope if existing else None) or "prompt")
        prompt_id = rule.get("prompt_id", existing.prompt_id if existing else None)
        if scope == "global":
            # The MERGED row must satisfy the global⇒no-prompt invariant even when a
            # service-level caller omits prompt_id while rescoping (the HTTP layer
            # always sends it explicitly; plain dicts may not). Without this, the
            # stale prompt_id survives the merge, the DB check rejects the flush,
            # and — had it landed — the next snapshot rebuild's strict parse would
            # take the whole environment down.
            prompt_id = None
        clauses = rule.get("when", rule.get("clauses"))
        serve = rule["serve"]
        # Integrity: reject targets that reference a non-existent version or an
        # unvalidated pinned SHA, and conditions naming segments this environment
        # lacks, before this write bumps rules_version.
        self._validate_rule_targets(scope, prompt_id, serve)
        self._validate_segment_refs(env_id, clauses)
        if existing is None:
            existing = models.Rule(id=rid, environment_id=env_id)
            self.s.add(existing)
        existing.scope = scope
        existing.prompt_id = prompt_id
        existing.priority = int(rule.get("priority", existing.priority or 10))
        existing.clauses = clauses
        existing.serve = serve
        existing.status = rule.get("status", existing.status or "active")
        existing.comment = rule.get("comment", existing.comment or "")
        self.s.flush()
        self._bump(env, "rule", _rule_snapshot(existing), rule_id=rid,
                   comment=existing.comment)
        record_audit(self.s, self.actor, "rule.upsert", "rule", rid, after=_rule_snapshot(existing))
        return existing

    def set_rule_status(self, env_id: str, rule_id: str, status: str) -> models.Rule:
        env = self._env(env_id)
        if status not in ("active", "paused", "archived"):
            raise TargetingError(f"invalid rule status {status!r}")
        r = self.s.get(models.Rule, rule_id)
        if r is None or r.environment_id != env_id:
            raise TargetingError(f"unknown rule {rule_id!r} in {env_id!r}")
        before = _rule_snapshot(r)
        r.status = status
        self.s.flush()
        self._bump(env, "rule", _rule_snapshot(r), rule_id=rule_id)
        record_audit(self.s, self.actor, f"rule.{status}", "rule", rule_id,
                     before=before, after=_rule_snapshot(r))
        return r

    # ── segments ─────────────────────────────────────────────────────

    def list_segments(self, env_id: str) -> list[models.Segment]:
        return list(self.s.execute(
            select(models.Segment).where(models.Segment.environment_id == env_id)
        ).scalars())

    def upsert_segment(self, env_id: str, name: str, clauses: dict) -> models.Segment:
        env = self._env(env_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise TargetingError("segment name must be 1-255 non-whitespace characters")
        try:
            parse_condition(clauses)
        except (KeyError, TypeError, ValueError) as exc:
            raise TargetingError(f"invalid segment condition: {exc}") from exc
        name = name.strip()
        self._validate_segment_refs(env_id, clauses)
        self._validate_segment_cycle(env_id, name, clauses)
        existing = self.s.execute(
            select(models.Segment).where(
                models.Segment.environment_id == env_id, models.Segment.name == name
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = models.Segment(environment_id=env_id, name=name, clauses=clauses, version=1)
            self.s.add(existing)
        else:
            existing.clauses = clauses
            existing.version += 1
        self.s.flush()
        self._bump(env, "segment", {"name": name, "clauses": clauses})
        record_audit(self.s, self.actor, "segment.upsert", "segment", name, after={"clauses": clauses})
        return existing

    # ── pointers (governed) ──────────────────────────────────────────

    def current_live(self, env_id: str, prompt_id: str, version_number: int) -> str | None:
        row = self.s.execute(
            select(models.PointerMove).where(
                models.PointerMove.environment_id == env_id,
                models.PointerMove.prompt_id == prompt_id,
                models.PointerMove.version_number == version_number,
            ).order_by(models.PointerMove.moved_at.desc(), models.PointerMove.id.desc())
        ).scalars().first()
        return row.to_sha if row else None

    def pointer_history(self, env_id: str, prompt_id: str, version_number: int) -> list[models.PointerMove]:
        return list(self.s.execute(
            select(models.PointerMove).where(
                models.PointerMove.environment_id == env_id,
                models.PointerMove.prompt_id == prompt_id,
                models.PointerMove.version_number == version_number,
            ).order_by(models.PointerMove.moved_at.desc(), models.PointerMove.id.desc())
        ).scalars())

    def make_live(
        self, env_id: str, prompt_id: str, version_number: int, to_sha: str,
        *, comment: str = "",
    ) -> MakeLiveOutcome:
        """Advance the live pointer directly. Pointer moves are unilateral — the
        route gates them to releaser; there is no propose→approve ceremony."""
        env = self._env(env_id)
        # Integrity (§7): the pointer may only serve a version that exists for the
        # prompt, pinned to a SHA validated *for that exact (prompt, version)* — not
        # merely validated for some other prompt/version that happens to share the
        # SHA set. This mirrors the write-time checks a rule pin gets in
        # `_validate_rule_targets`; before the fix `is_validated` waved through any
        # commit with any valid CommitValidation row, letting (prompt A, v2) point at
        # a commit validated only for prompt B or a different version of A.
        if not self._version_exists(prompt_id, version_number):
            raise TargetingError(
                f"version {version_number} does not exist or is archived for "
                f"prompt {prompt_id!r}")
        if not self._is_validated_for(prompt_id, version_number, to_sha):
            raise TargetingError(
                f"SHA {to_sha} is not a validated commit for {prompt_id!r} "
                f"v{version_number}; cannot make live")
        from_sha = self.current_live(env_id, prompt_id, version_number)
        move_id, rv = self._apply_make_live(env, prompt_id, version_number, to_sha,
                                            from_sha, comment)
        return MakeLiveOutcome("live", move_id, rv)

    def _apply_make_live(self, env, prompt_id, version_number, to_sha, from_sha, comment):
        move = models.PointerMove(
            environment_id=env.id, prompt_id=prompt_id, version_number=version_number,
            from_sha=from_sha, to_sha=to_sha, moved_by=self.actor, comment=comment,
        )
        self.s.add(move)
        self.s.flush()
        rv = self._bump(env, "pointer", {
            "prompt_id": prompt_id, "version": version_number,
            "from_sha": from_sha, "to_sha": to_sha,
        }, comment=comment)
        record_audit(self.s, self.actor, "pointer.make_live", "pointer",
                     f"{env.id}/{prompt_id}/v{version_number}",
                     before={"sha": from_sha}, after={"sha": to_sha})
        return move.id, rv

    # ── revisions & rollback ─────────────────────────────────────────

    def list_revisions(self, env_id: str, limit: int = 100) -> list[models.RuleRevision]:
        return list(self.s.execute(
            select(models.RuleRevision).where(models.RuleRevision.environment_id == env_id)
            .order_by(models.RuleRevision.id.desc()).limit(limit)
        ).scalars())

    def state_at(self, env_id: str, rules_version: int) -> dict | None:
        """The environment's exact targeting state at ``rules_version``.

        O(1) when that revision is a checkpoint (baseline/rollback/every Kth);
        otherwise the bounded slow path: take the nearest OLDER checkpoint and
        forward-apply the ≤K-1 per-object changes up to and including the target.
        ``None`` only when no revision with that number exists at all.

        Reconstructed states carry ``versions``' tips/labels/status as of the
        checkpoint — those move without bumping ``rules_version``, the same §9
        caveat class the checkpointed states already document.
        """
        if rules_version < 1:
            return None
        rev = self.s.execute(
            select(models.RuleRevision).where(
                models.RuleRevision.environment_id == env_id,
                models.RuleRevision.rules_version == rules_version,
            )
        ).scalar_one_or_none()
        if rev is None:
            return None
        if rev.state is not None:
            return rev.state

        base = self.s.execute(
            select(models.RuleRevision).where(
                models.RuleRevision.environment_id == env_id,
                models.RuleRevision.rules_version < rules_version,
                models.RuleRevision.state.isnot(None),
            ).order_by(models.RuleRevision.rules_version.desc())
        ).scalars().first()
        if base is None:  # pragma: no cover - every env gets a baseline before mutating
            return None
        deltas = self.s.execute(
            select(models.RuleRevision).where(
                models.RuleRevision.environment_id == env_id,
                models.RuleRevision.rules_version > base.rules_version,
                models.RuleRevision.rules_version <= rules_version,
            ).order_by(models.RuleRevision.rules_version)
        ).scalars().all()
        return _apply_deltas(base.state, deltas)

    def _rollback_rules(self, env_id: str, target: dict[str, dict]) -> int:
        """Restore the rule set to ``target`` ({rule_id -> rule snapshot}); rules
        created after the target are removed while their revision history remains.
        Returns rules changed."""
        changed = 0
        existing = {r.id: r for r in self.list_rules(env_id)}
        for rid, rule in existing.items():
            snap = target.get(rid)
            if snap is None:
                self.s.delete(rule)
                changed += 1
            else:
                rule.scope = snap.get("scope", rule.scope)
                rule.prompt_id = snap.get("prompt_id")
                rule.priority = snap.get("priority", rule.priority)
                rule.clauses = snap.get("when")
                rule.serve = snap.get("serve")
                rule.status = snap.get("status", "active")
                rule.comment = snap.get("comment", "")
                changed += 1
        # Recreate rules that existed at the target but are somehow gone now.
        for rid, snap in target.items():
            if rid not in existing:
                self.s.add(models.Rule(
                    id=rid, environment_id=env_id, scope=snap.get("scope", "prompt"),
                    prompt_id=snap.get("prompt_id"), priority=snap.get("priority", 10),
                    clauses=snap.get("when"), serve=snap.get("serve"),
                    status=snap.get("status", "active"), comment=snap.get("comment", ""),
                ))
                changed += 1
        return changed

    def rollback(self, env_id: str, to_rules_version: int) -> dict:
        """Restore the environment's COMPLETE targeting state as of
        ``to_rules_version`` — rules, segments, defaults, kill switches, and live
        pointers — from the revision's captured state (§7 "one-click rollback of …
        the whole environment's targeting state").

        Pointer restoration preserves the append-only model: a changed pointer gets
        a new move, while one absent at the target gets a ``None`` tombstone.  The
        rollback is itself a change and bumps ``rules_version``.
        """
        env = self._env(env_id)
        state = self.state_at(env_id, to_rules_version)
        if state is None:
            raise TargetingError(
                f"rules_version {to_rules_version} has no exact captured state for {env_id!r}"
            )

        changed = {"rules": self._rollback_rules(
            env_id, {r["id"]: r for r in state.get("rules", [])})}

        # Segments: restore the exact recorded set, including removals.
        changed["segments"] = 0
        existing_segments = {s.name: s for s in self.list_segments(env_id)}
        recorded_segments = {s["name"]: s for s in state.get("segments", [])}
        for name, seg in existing_segments.items():
            if name not in recorded_segments:
                self.s.delete(seg)
                changed["segments"] += 1
        for name, snap in recorded_segments.items():
            seg = existing_segments.get(name)
            if seg is None:
                self.s.add(models.Segment(
                    environment_id=env_id, name=name,
                    clauses=snap["clauses"], version=snap.get("version", 1)))
                changed["segments"] += 1
            elif seg.clauses != snap["clauses"] or seg.version != snap.get("version", 1):
                seg.clauses = snap["clauses"]
                seg.version = snap.get("version", 1)
                changed["segments"] += 1

        # Defaults: exactly the recorded map — updates, inserts, AND removals
        # (a default added after the target is part of what's being undone).
        changed["defaults"] = 0
        recorded = state.get("defaults", {})
        existing_defaults = {
            d.prompt_id: d for d in self.s.execute(
                select(models.EnvDefault).where(
                    models.EnvDefault.environment_id == env_id)
            ).scalars()
        }
        skipped_defaults = 0
        for pid, ver in recorded.items():
            d = existing_defaults.get(pid)
            if (d is None or d.version_number != ver) and not self._version_exists(pid, ver):
                # The recorded default names a version since archived (or gone): an
                # archived default is unservable, so leave the current default in place
                # and report the skip rather than restore a 409.
                skipped_defaults += 1
                continue
            if d is None:
                self.s.add(models.EnvDefault(
                    environment_id=env_id, prompt_id=pid, version_number=ver))
                changed["defaults"] += 1
            elif d.version_number != ver:
                d.version_number = ver
                changed["defaults"] += 1
        if skipped_defaults:
            changed["defaults_skipped"] = skipped_defaults
        for pid, d in existing_defaults.items():
            if pid not in recorded:
                self.s.delete(d)
                changed["defaults"] += 1

        # Kill switches: engaged set exactly as recorded.
        changed["kills"] = 0
        recorded_kills = set(state.get("kills", []))
        existing_kills = {
            k.prompt_id: k for k in self.s.execute(
                select(models.KillSwitch).where(
                    models.KillSwitch.environment_id == env_id)
            ).scalars()
        }
        for pid in recorded_kills - {p for p, k in existing_kills.items() if k.engaged}:
            k = existing_kills.get(pid)
            if k is None:
                self.s.add(models.KillSwitch(
                    environment_id=env_id, prompt_id=pid, engaged=True,
                    by=self.actor))
            else:
                k.engaged = True
                k.by = self.actor
            changed["kills"] += 1
        for pid, k in existing_kills.items():
            if k.engaged and pid not in recorded_kills:
                k.engaged = False
                k.by = self.actor
                changed["kills"] += 1

        # Live pointers: restore every registered key. ``None`` is an append-only
        # tombstone for a pointer that did not exist at the target.
        changed["pointers"] = 0
        skipped_pointers = 0
        recorded_versions = state.get("versions", {})
        current_keys = {
            _version_key(pid, version)
            for pid, version in self.s.execute(
                select(models.PointerMove.prompt_id, models.PointerMove.version_number)
                .where(models.PointerMove.environment_id == env_id)
                .distinct()
            )
        }
        for key in set(recorded_versions) | current_keys:
            recorded_sha = recorded_versions.get(key, {}).get("live")
            pid, _, vpart = key.rpartition("@v")
            version_number = int(vpart)
            current_sha = self.current_live(env_id, pid, version_number)
            if current_sha == recorded_sha:
                continue
            if recorded_sha and not self._is_validated_for(pid, version_number, recorded_sha):
                skipped_pointers += 1
                continue
            self.s.add(models.PointerMove(
                environment_id=env_id, prompt_id=pid,
                version_number=version_number, from_sha=current_sha,
                to_sha=recorded_sha, moved_by=self.actor,
                comment=f"rollback to rules_version {to_rules_version}",
            ))
            changed["pointers"] += 1
        if skipped_pointers:
            changed["pointers_skipped"] = skipped_pointers
        scope = "full"

        self.s.flush()
        rv = self._bump(env, "rollback",
                        {"to_rules_version": to_rules_version, "scope": scope,
                         "changed": changed},
                        comment=f"rollback to rules_version {to_rules_version}")
        record_audit(self.s, self.actor, "targeting.rollback", "environment", env_id,
                     after={"to_rules_version": to_rules_version, "scope": scope,
                            "changed": changed})
        return {"to_rules_version": to_rules_version, "scope": scope,
                "changed": changed, "rules_version": rv,
                # Back-compat for existing clients/UI: the rules count keeps its key.
                "rules_changed": changed.get("rules", 0)}

    # ── defaults ─────────────────────────────────────────────────────

    def set_default(self, env_id: str, prompt_id: str, version_number: int) -> models.EnvDefault:
        env = self._env(env_id)
        if not self._version_exists(prompt_id, version_number):
            raise TargetingError(
                f"version {version_number} does not exist or is archived for "
                f"prompt {prompt_id!r}")
        existing = self.s.execute(
            select(models.EnvDefault).where(
                models.EnvDefault.environment_id == env_id,
                models.EnvDefault.prompt_id == prompt_id,
            )
        ).scalar_one_or_none()
        before = existing.version_number if existing else None
        if existing is None:
            existing = models.EnvDefault(
                environment_id=env_id, prompt_id=prompt_id, version_number=version_number
            )
            self.s.add(existing)
        else:
            existing.version_number = version_number
        self.s.flush()
        self._bump(env, "default", {"prompt_id": prompt_id, "version": version_number})
        record_audit(self.s, self.actor, "default.set", "default",
                     f"{env_id}/{prompt_id}", before={"version": before},
                     after={"version": version_number})
        return existing

    # ── kill switches ────────────────────────────────────────────────

    def set_kill(self, env_id: str, prompt_id: str, engaged: bool) -> models.KillSwitch:
        env = self._env(env_id)
        existing = self.s.execute(
            select(models.KillSwitch).where(
                models.KillSwitch.environment_id == env_id,
                models.KillSwitch.prompt_id == prompt_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = models.KillSwitch(environment_id=env_id, prompt_id=prompt_id)
            self.s.add(existing)
        existing.engaged = engaged
        existing.by = self.actor
        self.s.flush()
        self._bump(env, "kill", {"prompt_id": prompt_id, "engaged": engaged})
        record_audit(self.s, self.actor, "kill.engage" if engaged else "kill.restore",
                     "kill", f"{env_id}/{prompt_id}", after={"engaged": engaged})
        return existing


def _apply_deltas(base_state: dict, deltas: list[models.RuleRevision]) -> dict:
    """Forward-apply per-object revision snapshots onto a checkpoint state (deep
    copy first — checkpoint states are shared history, never mutated). Every
    non-checkpoint kind's snapshot fully describes its change: rules carry the
    whole rule, pointers carry (prompt, version, to_sha) including tombstones,
    defaults/kills carry their new value, segments their new clauses. Baseline and
    rollback — the two kinds whose snapshot does NOT describe their effect — are
    always checkpoints, so they never appear as deltas."""
    import copy
    state = copy.deepcopy(base_state)
    rules = {r["id"]: r for r in state.get("rules", [])}
    segments = {s["name"]: s for s in state.get("segments", [])}
    for rev in deltas:
        snap = rev.snapshot or {}
        if rev.kind == "rule" and rev.rule_id:
            rules[rev.rule_id] = snap
        elif rev.kind == "segment":
            existing = segments.get(snap.get("name"))
            version = (existing.get("version", 0) + 1) if existing else 1
            segments[snap["name"]] = {"name": snap["name"],
                                      "clauses": snap.get("clauses"),
                                      "version": version}
        elif rev.kind == "default":
            state.setdefault("defaults", {})[snap["prompt_id"]] = snap["version"]
        elif rev.kind == "kill":
            kills = set(state.get("kills", []))
            (kills.add if snap.get("engaged") else kills.discard)(snap["prompt_id"])
            state["kills"] = sorted(kills)
        elif rev.kind == "pointer":
            key = _version_key(snap["prompt_id"], snap["version"])
            entry = state.setdefault("versions", {}).setdefault(
                key, {"live": None, "tip": None, "label": None, "status": "active"})
            entry["live"] = snap.get("to_sha")
        elif rev.kind == "version":
            # Registry metadata that targeting depends on (label, archived status).
            key = _version_key(snap["prompt_id"], snap["version"])
            entry = state.setdefault("versions", {}).setdefault(
                key, {"live": None, "tip": None, "label": None, "status": "active"})
            if "label" in snap:
                entry["label"] = snap["label"]
            if "status" in snap:
                entry["status"] = snap["status"]
    state["rules"] = list(rules.values())
    state["segments"] = list(segments.values())
    return state


def _segment_refs(cond) -> set[str]:
    """Segment names a raw condition (JSON shape) references, at any depth."""
    out: set[str] = set()

    def walk(c) -> None:
        if not isinstance(c, dict):
            return
        seg = c.get("segment")
        if isinstance(seg, str) and seg.strip():
            out.add(seg.strip())
        for k in ("all", "any"):
            if isinstance(c.get(k), list):
                for x in c[k]:
                    walk(x)
        if "not" in c:
            walk(c["not"])

    walk(cond)
    return out


def _rule_snapshot(r: models.Rule) -> dict:
    return {
        "id": r.id, "scope": r.scope, "prompt_id": r.prompt_id, "priority": r.priority,
        "when": r.clauses, "serve": r.serve, "status": r.status, "comment": r.comment,
    }
