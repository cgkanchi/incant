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

import datetime as dt
import hashlib
import hmac
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
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


@dataclass(frozen=True)
class Binding:
    """A plain, session-free role binding for the in-memory auth cache."""
    role: str
    project_id: Optional[str]
    environment_id: Optional[str]


@dataclass
class Identity:
    principal_id: str
    name: str
    bindings: list  # of Binding (cache) or models.RoleBinding — both duck-type here

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


@dataclass(frozen=True)
class _KeyEntry:
    prefix: str
    hash: str
    revoked: bool
    expires_at: object  # datetime | None
    principal_id: str
    principal_name: str
    bindings: tuple[Binding, ...]


def _expired(expires_at, now: dt.datetime) -> bool:
    if expires_at is None:
        return False
    exp = expires_at
    if exp.tzinfo is None:  # SQLite returns naive datetimes; treat as UTC
        exp = exp.replace(tzinfo=dt.timezone.utc)
    return exp <= now


class AuthCache:
    """In-memory API-key table so the render hot path does no per-request DB read
    and keeps authenticating through a Postgres outage (§8, §10, §15).

    The whole (small) key set is snapshotted from the DB, refreshed on a TTL and
    on demand. On a miss we force one throttled reload to pick up a freshly issued
    key; if the DB is unreachable we keep serving the last-known-good table.
    """

    def __init__(self, ttl: float = 5.0, min_refresh: float = 1.0) -> None:
        self._entries: dict[str, _KeyEntry] = {}
        self._loaded = False
        self._last_refresh = 0.0
        self._ttl = ttl
        self._min_refresh = min_refresh
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """Force a reload on the next identify (after key issuance/revocation)."""
        self._loaded = False

    def _load(self, session: Session) -> None:
        principals = {p.id: p.name for p in
                      session.execute(select(models.Principal)).scalars()}
        bindings: dict[str, list[Binding]] = defaultdict(list)
        for b in session.execute(select(models.RoleBinding)).scalars():
            bindings[b.principal_id].append(Binding(b.role, b.project_id, b.environment_id))
        entries: dict[str, _KeyEntry] = {}
        for k in session.execute(select(models.ApiKey)).scalars():
            entries[k.prefix] = _KeyEntry(
                prefix=k.prefix, hash=k.hash, revoked=bool(k.revoked),
                expires_at=k.expires_at, principal_id=k.principal_id,
                principal_name=principals.get(k.principal_id, k.principal_id),
                bindings=tuple(bindings.get(k.principal_id, ())),
            )
        self._entries = entries
        self._loaded = True
        self._last_refresh = time.monotonic()

    def _maybe_refresh(self, session: Session, prefix: str) -> None:
        def _need() -> bool:
            age = time.monotonic() - self._last_refresh
            return (
                not self._loaded
                or age > self._ttl
                or (prefix not in self._entries and age >= self._min_refresh)
            )

        if not _need():
            return
        with self._lock:
            if not _need():
                return
            try:
                self._load(session)
            except SQLAlchemyError:
                # DB down: keep the last-known-good table so serving continues.
                try:
                    session.rollback()
                except SQLAlchemyError:
                    pass
                if not self._loaded:
                    raise AuthError(503, "auth cache cold and database unreachable")

    def identify(self, session: Session, authorization: str | None) -> Identity:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError(401, "missing bearer credential")
        raw = authorization[7:].strip()
        prefix = key_prefix(raw)
        self._maybe_refresh(session, prefix)
        entry = self._entries.get(prefix)
        if entry is None or entry.revoked or not hmac.compare_digest(entry.hash, hash_key(raw)):
            raise AuthError(401, "invalid credential")
        if _expired(entry.expires_at, dt.datetime.now(dt.timezone.utc)):
            raise AuthError(401, "credential expired")
        # No last_used_at write here — the serving path must not write (§8, §15).
        return Identity(entry.principal_id, entry.principal_name, list(entry.bindings))


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
        session.flush()  # parent row must exist before FK-bearing children insert
    session.add(models.ApiKey(
        principal_id=pid, prefix=key_prefix(raw_key), hash=hash_key(raw_key), name="bootstrap admin",
    ))
    session.add(models.RoleBinding(principal_id=pid, role="admin"))
