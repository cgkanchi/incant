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
from ..core.parse import parse_rule as parse_core_rule
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
        env = self.s.get(models.Environment, env_id)
        if env is None:
            raise TargetingError(f"unknown environment {env_id!r}")
        return env

    def _bump(self, env: models.Environment, kind: str, snapshot: dict,
              rule_id: str | None = None, comment: str = "") -> int:
        # Atomic increment at the database, not a Python read-modify-write: two
        # operators mutating targeting concurrently must both advance the counter.
        # Assigning a SQL expression emits `SET rules_version = rules_version + 1`,
        # which Postgres serializes under the row lock (no lost update).
        env.rules_version = models.Environment.rules_version + 1
        rev = models.RuleRevision(
            environment_id=env.id, rule_id=rule_id, kind=kind,
            snapshot=snapshot, actor=self.actor, comment=comment,
            # The complete post-change state (mutation already flushed by the
            # caller) — what total rollback and pin.rules_version replay read.
            state=capture_state(self.s, env.id),
        )
        self.s.add(rev)
        self.s.flush()
        self.s.refresh(env)  # load the DB-computed value back onto the instance
        rev.rules_version = env.rules_version  # stamp the revision with its version
        self.s.flush()
        return env.rules_version

    def _version_exists(self, prompt_id: str, version_number: int) -> bool:
        return self.s.execute(
            select(models.Version).where(
                models.Version.prompt_id == prompt_id,
                models.Version.number == version_number,
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

    def _validate_rule_targets(self, scope: str, prompt_id: str | None, serve: dict) -> None:
        """Integrity: a prompt-scoped rule may only serve versions that exist for the
        prompt (§7), and a pinned SHA must be a validated commit for that
        prompt/version. Global rules serve labels, so version existence is not checked
        here (a prompt without the label simply skips the rule)."""
        if scope != "prompt" or not prompt_id:
            return
        # (version_number, at, sha) targets carried by this serve.
        targets: list[tuple[int, str | None, str | None]] = []
        if "version" in serve:
            targets.append((int(serve["version"]), serve.get("at"), serve.get("sha")))
        if isinstance(serve.get("rollout"), dict):
            for band in serve["rollout"].get("weights", []):
                if band.get("version") is not None and not band.get("default"):
                    targets.append((int(band["version"]), None, None))
        for version_number, at, sha in targets:
            if not self._version_exists(prompt_id, version_number):
                raise TargetingError(
                    f"version {version_number} does not exist for prompt {prompt_id!r}")
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
        parse_core_rule({**rule, "id": rule.get("id", "tmp")})
        rid = rule["id"]
        existing = self.s.get(models.Rule, rid)
        if existing is not None and existing.environment_id != env_id:
            # Rule ids are globally unique; refuse to edit a rule that lives in
            # another environment via this env's URL (cross-env capture).
            raise TargetingError(
                f"rule {rid!r} belongs to environment {existing.environment_id!r}, not {env_id!r}")
        if existing is None:
            existing = models.Rule(id=rid, environment_id=env_id)
            self.s.add(existing)
        existing.scope = rule.get("scope", existing.scope or "prompt")
        existing.prompt_id = rule.get("prompt_id", existing.prompt_id)
        existing.priority = int(rule.get("priority", existing.priority or 10))
        existing.clauses = rule.get("when", rule.get("clauses"))
        existing.serve = rule["serve"]
        existing.status = rule.get("status", existing.status or "active")
        existing.comment = rule.get("comment", existing.comment or "")
        # Integrity: reject targets that reference a non-existent version or an
        # unvalidated pinned SHA before this write bumps rules_version.
        self._validate_rule_targets(existing.scope, existing.prompt_id, existing.serve)
        self.s.flush()
        rv = self._bump(env, "rule", _rule_snapshot(existing), rule_id=rid,
                        comment=existing.comment)
        record_audit(self.s, self.actor, "rule.upsert", "rule", rid, after=_rule_snapshot(existing))
        return existing

    def set_rule_status(self, env_id: str, rule_id: str, status: str) -> models.Rule:
        env = self._env(env_id)
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
                f"version {version_number} does not exist for prompt {prompt_id!r}")
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
        """The environment's complete targeting state as of ``rules_version``: the
        newest state-carrying revision at or before it. ``None`` when the target
        predates state-tracked revisions (pre-upgrade history)."""
        rev = self.s.execute(
            select(models.RuleRevision).where(
                models.RuleRevision.environment_id == env_id,
                models.RuleRevision.rules_version <= rules_version,
                models.RuleRevision.state.isnot(None),
            ).order_by(models.RuleRevision.rules_version.desc(),
                       models.RuleRevision.id.desc())
        ).scalars().first()
        return rev.state if rev is not None else None

    def _rollback_rules(self, env_id: str, target: dict[str, dict]) -> int:
        """Restore the rule set to ``target`` ({rule_id -> rule snapshot}); rules
        created after the target are archived (never deleted — ids are immutable
        and history must keep resolving). Returns rules changed."""
        changed = 0
        existing = {r.id: r for r in self.list_rules(env_id)}
        for rid, rule in existing.items():
            snap = target.get(rid)
            if snap is None:
                if rule.status != "archived":
                    rule.status = "archived"  # created after target -> stop serving
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

        Pointer restoration preserves the append-only model: a pointer whose
        current live SHA differs from the recorded one gets a NEW move back to it
        (history intact, §7), and a pointer that did not exist at the target simply
        keeps its current value — there is no "un-make-live"; it only serves if the
        restored rules/defaults reference it, which they by construction don't.

        Falls back to the legacy rules-only reconstruction (per-rule revisions) for
        targets that predate state-carrying revisions; the response says which.
        The rollback is itself a change and bumps ``rules_version``.
        """
        env = self._env(env_id)
        state = self.state_at(env_id, to_rules_version)

        if state is None:
            # Legacy fallback: replay per-rule revisions only (pre-upgrade history).
            revs = self.s.execute(
                select(models.RuleRevision).where(
                    models.RuleRevision.environment_id == env_id,
                    models.RuleRevision.kind == "rule",
                    models.RuleRevision.rules_version <= to_rules_version,
                ).order_by(models.RuleRevision.rules_version, models.RuleRevision.id)
            ).scalars().all()
            target = {r.rule_id: r.snapshot for r in revs if r.rule_id}
            changed = {"rules": self._rollback_rules(env_id, target)}
            scope = "rules"
        else:
            changed = {"rules": self._rollback_rules(
                env_id, {r["id"]: r for r in state.get("rules", [])})}

            # Segments: restore recorded clause sets. Extra segments (created after
            # the target) are left in place — the restored rules don't reference
            # them, and deleting a named object an operator may still want is worse.
            changed["segments"] = 0
            existing_segments = {s.name: s for s in self.list_segments(env_id)}
            for snap in state.get("segments", []):
                seg = existing_segments.get(snap["name"])
                if seg is None:
                    self.s.add(models.Segment(
                        environment_id=env_id, name=snap["name"],
                        clauses=snap["clauses"], version=snap.get("version", 1)))
                    changed["segments"] += 1
                elif seg.clauses != snap["clauses"]:
                    seg.clauses = snap["clauses"]
                    seg.version += 1
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
            for pid, ver in recorded.items():
                d = existing_defaults.get(pid)
                if d is None:
                    self.s.add(models.EnvDefault(
                        environment_id=env_id, prompt_id=pid, version_number=ver))
                    changed["defaults"] += 1
                elif d.version_number != ver:
                    d.version_number = ver
                    changed["defaults"] += 1
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

            # Live pointers: append a move back to the recorded SHA wherever the
            # current live differs (append-only history preserved). Skip — and
            # count — anything no longer validated (CommitValidation rows are never
            # deleted, so this is belt-and-braces, not an expected path).
            changed["pointers"] = 0
            skipped_pointers = 0
            for key, vinfo in state.get("versions", {}).items():
                recorded_sha = vinfo.get("live")
                if not recorded_sha:
                    continue
                pid, _, vpart = key.rpartition("@v")
                version_number = int(vpart)
                if self.current_live(env_id, pid, version_number) == recorded_sha:
                    continue
                if not self._is_validated_for(pid, version_number, recorded_sha):
                    skipped_pointers += 1
                    continue
                from_sha = self.current_live(env_id, pid, version_number)
                self.s.add(models.PointerMove(
                    environment_id=env_id, prompt_id=pid,
                    version_number=version_number, from_sha=from_sha,
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
                f"version {version_number} does not exist for prompt {prompt_id!r}")
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


def _rule_snapshot(r: models.Rule) -> dict:
    return {
        "id": r.id, "scope": r.scope, "prompt_id": r.prompt_id, "priority": r.priority,
        "when": r.clauses, "serve": r.serve, "status": r.status, "comment": r.comment,
    }
