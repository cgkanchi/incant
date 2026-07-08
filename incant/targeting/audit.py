"""Append-only audit helper — every control-plane mutation records who/what/when."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models


def record_audit(
    session: Session,
    actor: str,
    action: str,
    object_type: str,
    object_id: str,
    *,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    session.add(models.AuditLog(
        actor=actor, action=action, object_type=object_type, object_id=object_id,
        before=before, after=after,
    ))
