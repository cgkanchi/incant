"""TargetingService — per-environment rules, segments, pointers, defaults, kills.

Every mutation snapshots to rule_revisions and bumps the environment's monotonic
rules_version. Pointer-class changes (make-live, default) are the governed acts;
rule/segment edits are low-friction. Rules and pointers may only reference
validated SHAs.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..core.parse import parse_rule as parse_core_rule
from .audit import record_audit


class TargetingError(Exception):
    pass


@dataclass
class MakeLiveOutcome:
    status: str          # "live" | "proposed"
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
        self.s.add(models.RuleRevision(
            environment_id=env.id, rule_id=rule_id, kind=kind,
            snapshot=snapshot, actor=self.actor, comment=comment,
        ))
        self.s.flush()
        self.s.refresh(env)  # load the DB-computed value back onto the instance
        return env.rules_version

    def is_validated(self, sha: str) -> bool:
        return self.s.execute(
            select(models.CommitValidation).where(
                models.CommitValidation.sha == sha,
                models.CommitValidation.status == "valid",
            )
        ).first() is not None

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
        *, comment: str = "", approver: str | None = None, force: bool = False,
    ) -> MakeLiveOutcome:
        env = self._env(env_id)
        if not self.is_validated(to_sha):
            raise TargetingError(f"SHA {to_sha} is not a validated commit; cannot make live")

        from_sha = self.current_live(env_id, prompt_id, version_number)

        # Protected environments: pointer-class changes go through propose→approve
        # (approver != proposer), unless an approver is supplied inline or forced.
        if env.protected and not force and approver is None:
            appr = models.Approval(
                environment_id=env_id, proposed_by=self.actor,
                change={"kind": "make_live", "prompt_id": prompt_id,
                        "version": version_number, "to_sha": to_sha,
                        "from_sha": from_sha, "comment": comment},
                status="pending",
            )
            self.s.add(appr)
            self.s.flush()
            record_audit(self.s, self.actor, "pointer.propose", "approval", str(appr.id),
                         after=appr.change)
            return MakeLiveOutcome("proposed", None, env.rules_version)

        if approver is not None and approver == self.actor and not force:
            raise TargetingError("approver must differ from proposer")

        move = models.PointerMove(
            environment_id=env_id, prompt_id=prompt_id, version_number=version_number,
            from_sha=from_sha, to_sha=to_sha, moved_by=self.actor, comment=comment,
        )
        self.s.add(move)
        self.s.flush()
        rv = self._bump(env, "pointer", {
            "prompt_id": prompt_id, "version": version_number,
            "from_sha": from_sha, "to_sha": to_sha,
        }, comment=comment)
        record_audit(self.s, self.actor, "pointer.make_live", "pointer",
                     f"{env_id}/{prompt_id}/v{version_number}",
                     before={"sha": from_sha}, after={"sha": to_sha})
        return MakeLiveOutcome("live", move.id, rv)

    # ── defaults ─────────────────────────────────────────────────────

    def set_default(self, env_id: str, prompt_id: str, version_number: int) -> models.EnvDefault:
        env = self._env(env_id)
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
