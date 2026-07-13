"""Audit explorer read endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from ..auth import Identity
from ..deps import get_session, identity
from .helpers import _require

router = APIRouter()


@router.get("/audit")
def get_audit(
    actor: str | None = None,
    action: str | None = None,
    object: str | None = None,     # substring match on object_id
    limit: int = 100,
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer")
    limit = max(1, min(limit, 500))  # hard cap
    q = select(models.AuditLog)
    if actor:
        q = q.where(models.AuditLog.actor == actor)
    if action:
        q = q.where(models.AuditLog.action == action)
    if object:
        q = q.where(models.AuditLog.object_id.contains(object))
    q = q.order_by(models.AuditLog.at.desc(), models.AuditLog.id.desc()).limit(limit)
    rows = session.execute(q).scalars()
    # Distinct values for the filter dropdowns (over the whole log, not the page).
    actors = list(session.execute(
        select(models.AuditLog.actor).distinct().order_by(models.AuditLog.actor)
    ).scalars())
    actions = list(session.execute(
        select(models.AuditLog.action).distinct().order_by(models.AuditLog.action)
    ).scalars())
    return {
        "audit": [
            {"id": a.id, "actor": a.actor, "action": a.action, "object_type": a.object_type,
             "object_id": a.object_id, "before": a.before, "after": a.after,
             "at": a.at.isoformat()}
            for a in rows
        ],
        "actors": actors,
        "actions": actions,
    }
