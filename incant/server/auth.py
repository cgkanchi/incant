"""AuthN + RBAC. Incant is the only door; service keys are bearer API keys.

Roles (DESIGN.md §11), most-privileged last, with implication:
    renderer   — serving API only
    viewer     — read prompts/versions/rules/history, previews
    editor      = viewer + authoring (drafts, commits subject to review policy)
    operator    = viewer + targeting (rules, segments, ramps, kills, pointers, defaults)
    releaser    = operator + approvals for protected pointer-class changes
    admin       — everything
Bindings are (principal, role, scope); scope = instance | project | (project, env).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models

# Which roles a held role satisfies (transitive implication baked in).
_IMPLIES: dict[str, set[str]] = {
    "renderer": {"renderer"},
    "viewer": {"viewer"},
    "editor": {"viewer", "editor"},
    "operator": {"viewer", "operator"},
    "releaser": {"viewer", "operator", "releaser"},
    "admin": {"renderer", "viewer", "editor", "operator", "releaser", "admin"},
}


class AuthError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


@dataclass
class Identity:
    principal_id: str
    name: str
    bindings: list[models.RoleBinding]

    def has(self, role: str, *, project: str | None = None, environment: str | None = None) -> bool:
        for b in self.bindings:
            if role not in _IMPLIES.get(b.role, set()):
                continue
            if b.project_id is not None and project is not None and b.project_id != project:
                continue
            if b.environment_id is not None and environment is not None and b.environment_id != environment:
                continue
            # An instance-scoped binding (both None) covers everything.
            return True
        return False

    def require(self, role: str, *, project: str | None = None, environment: str | None = None) -> None:
        if not self.has(role, project=project, environment=environment):
            raise AuthError(403, f"requires {role} on "
                                 f"{project or '*'}/{environment or '*'}")


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def key_prefix(raw: str) -> str:
    return raw[:16]


def authenticate(session: Session, authorization: str | None) -> Identity:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError(401, "missing bearer credential")
    raw = authorization[7:].strip()
    prefix = key_prefix(raw)
    key = session.execute(
        select(models.ApiKey).where(
            models.ApiKey.prefix == prefix, models.ApiKey.revoked.is_(False)
        )
    ).scalars().first()
    if key is None or key.hash != hash_key(raw):
        raise AuthError(401, "invalid credential")
    import datetime as dt
    key.last_used_at = dt.datetime.now(dt.timezone.utc)
    bindings = list(session.execute(
        select(models.RoleBinding).where(models.RoleBinding.principal_id == key.principal_id)
    ).scalars())
    principal = session.get(models.Principal, key.principal_id)
    return Identity(key.principal_id, principal.name if principal else key.principal_id, bindings)


def ensure_bootstrap_admin(session: Session, raw_key: str) -> None:
    """Create the bootstrap admin principal + key + instance-admin binding if absent."""

    existing = session.execute(
        select(models.ApiKey).where(models.ApiKey.prefix == key_prefix(raw_key))
    ).scalars().first()
    if existing is not None:
        return
    pid = "p_bootstrap_admin"
    if session.get(models.Principal, pid) is None:
        session.add(models.Principal(id=pid, kind="service", subject="bootstrap", name="bootstrap-admin"))
    session.add(models.ApiKey(
        principal_id=pid, prefix=key_prefix(raw_key), hash=hash_key(raw_key), name="bootstrap admin",
    ))
    session.add(models.RoleBinding(principal_id=pid, role="admin"))
