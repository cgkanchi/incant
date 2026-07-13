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
import os
import secrets
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, replace
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
            # A project-scoped binding satisfies only checks for *that* project;
            # in particular it must NOT satisfy an instance-wide (project=None)
            # check — otherwise a project operator gains instance-wide power.
            if b.project_id is not None and b.project_id != project:
                continue
            if b.environment_id is not None and b.environment_id != environment:
                continue
            # An instance-scoped binding (both None) covers everything.
            return True
        return False

    def require(self, role: str, *, project: str | None = None, environment: str | None = None) -> None:
        if not self.has(role, project=project, environment=environment):
            raise AuthError(403, f"requires {role} on "
                                 f"{project or '*'}/{environment or '*'}")


_V2_PREFIX = "v2$"  # marks a peppered HMAC-SHA256 hash; absence ⇒ legacy plain SHA-256


def _legacy_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _v2_hash(raw: str, pepper: str) -> str:
    return _V2_PREFIX + hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _current_pepper(pepper: str | None) -> str:
    from ..config import get_settings
    return get_settings().key_pepper if pepper is None else pepper


def hash_key(raw: str, pepper: str | None = None) -> str:
    """Hash a raw key for storage. With a pepper configured, produce a versioned
    `v2$` HMAC-SHA256(pepper, key); without one, the legacy plain SHA-256 (unchanged
    on-disk format). `pepper=None` reads the configured pepper (the normal path);
    tests pass it explicitly."""
    pepper = _current_pepper(pepper)
    return _v2_hash(raw, pepper) if pepper else _legacy_hash(raw)


def verify_key(raw: str, stored: str, pepper: str | None = None) -> bool:
    """Constant-time check of a raw key against a stored hash of either format. A
    `v2$` hash needs the pepper to verify (returns False if none configured); a
    legacy hash verifies by plain SHA-256 regardless of pepper."""
    pepper = _current_pepper(pepper)
    if stored.startswith(_V2_PREFIX):
        if not pepper:
            return False
        return hmac.compare_digest(stored, _v2_hash(raw, pepper))
    return hmac.compare_digest(stored, _legacy_hash(raw))


def needs_upgrade(stored: str, pepper: str | None = None) -> bool:
    """True iff a verified key is stored legacy but a pepper is now configured, so it
    should be opportunistically re-hashed to `v2$` in place."""
    return bool(_current_pepper(pepper)) and not stored.startswith(_V2_PREFIX)


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

    def _upgrade_hash(self, entry: _KeyEntry, raw: str) -> None:
        """Opportunistically re-hash a legacy key to `v2$` once a pepper is configured.

        This is the one auth-time write in the system, and it is deliberately *not*
        done on the caller's session: the serving path's session is read-only (§8/§15)
        and must never commit. Instead we open a dedicated short-lived committing
        session for just this UPDATE. The write is a one-shot per key — the row (and
        the in-memory entry) become `v2$`, so it never fires again for that key — so
        this is not a per-request write, and it is skipped entirely with no pepper set.
        """
        new_hash = hash_key(raw)
        try:
            from ..db import session_scope
            with session_scope() as s:
                row = s.execute(
                    select(models.ApiKey).where(
                        models.ApiKey.prefix == entry.prefix,
                        models.ApiKey.hash == entry.hash,  # guard against a concurrent change
                    )
                ).scalars().first()
                if row is None:
                    return
                row.hash = new_hash
        except SQLAlchemyError:
            return  # best-effort; a failed upgrade just retries next auth
        with self._lock:
            cur = self._entries.get(entry.prefix)
            if cur is not None and cur.hash == entry.hash:
                self._entries[entry.prefix] = replace(cur, hash=new_hash)

    def identify(self, session: Session, authorization: str | None) -> Identity:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError(401, "missing bearer credential")
        raw = authorization[7:].strip()
        prefix = key_prefix(raw)
        self._maybe_refresh(session, prefix)
        entry = self._entries.get(prefix)
        if entry is None or entry.revoked or not verify_key(raw, entry.hash):
            raise AuthError(401, "invalid credential")
        if _expired(entry.expires_at, dt.datetime.now(dt.timezone.utc)):
            raise AuthError(401, "credential expired")
        if needs_upgrade(entry.hash):
            self._upgrade_hash(entry, raw)
        # No last_used_at write here — the serving path must not write (§8, §15).
        return Identity(entry.principal_id, entry.principal_name, list(entry.bindings))


DEV_ADMIN_KEY = "incant_sk_dev_admin"  # the well-known, unsafe development key


class BootstrapError(RuntimeError):
    """Refuse to start: the configured bootstrap credential is unsafe."""


def _admin_exists(session: Session) -> bool:
    """True iff any principal already holds an instance-wide admin binding.

    "Instance-wide" = no project/environment scope; that is what can administer the
    whole instance and re-key itself, so its presence means the instance is not
    locked out and we must not mint another bootstrap admin.
    """
    return session.execute(
        select(models.RoleBinding).where(
            models.RoleBinding.role == "admin",
            models.RoleBinding.project_id.is_(None),
            models.RoleBinding.environment_id.is_(None),
        )
    ).first() is not None


def _insert_bootstrap_admin(session: Session, raw_key: str) -> None:
    """Create/attach a key on the bootstrap admin principal + instance-admin binding."""
    pid = "p_bootstrap_admin"
    if session.get(models.Principal, pid) is None:
        session.add(models.Principal(id=pid, kind="service", subject="bootstrap", name="bootstrap-admin"))
        session.flush()  # parent row must exist before FK-bearing children insert
        session.add(models.RoleBinding(principal_id=pid, role="admin"))
    session.add(models.ApiKey(
        principal_id=pid, prefix=key_prefix(raw_key), hash=hash_key(raw_key), name="bootstrap admin",
    ))


def _print_generated_key(raw_key: str) -> None:
    banner = (
        "\n" + "=" * 68 + "\n"
        "  INCANT — generated bootstrap admin key\n\n"
        f"      {raw_key}\n\n"
        "  Save this now — it will NOT be shown again.\n"
        "  Use it as:  Authorization: Bearer <key>\n"
        "  Pin your own key instead by setting INCANT_BOOTSTRAP_ADMIN_KEY.\n"
        + "=" * 68 + "\n"
    )
    print(banner, flush=True)


def ensure_bootstrap_admin(session: Session, raw_key: str) -> None:
    """Ensure the instance has an admin credential, safely (DESIGN.md §11).

    - Configured key empty/unset → on first boot (no instance admin yet) generate a
      strong random ``incant_sk_`` + 32-hex key, insert it, and print it once.
      Subsequent boots are a no-op (an admin already exists).
    - Configured key is the well-known ``incant_sk_dev_admin`` → allowed ONLY when
      ``INCANT_ALLOW_DEV_KEY=1`` (dev/test escape hatch); otherwise refuse to start.
    - Any other explicit key → create the principal/key/binding if that key is absent
      (idempotent; supports rotation onto the bootstrap principal).
    """
    raw_key = (raw_key or "").strip()

    if raw_key == DEV_ADMIN_KEY and os.environ.get("INCANT_ALLOW_DEV_KEY") != "1":
        raise BootstrapError(
            "INCANT_BOOTSTRAP_ADMIN_KEY is the well-known development key "
            f"'{DEV_ADMIN_KEY}', which is unsafe for a real instance. Fix: unset it so "
            "Incant generates a strong random admin key on first boot (printed once), or "
            "set INCANT_ALLOW_DEV_KEY=1 to explicitly allow the dev key (local/test only)."
        )

    if not raw_key:
        # Empty/unset: mint a random key on first boot only.
        if _admin_exists(session):
            return
        raw_key = "incant_sk_" + secrets.token_hex(16)  # incant_sk_ + 32 hex chars
        _insert_bootstrap_admin(session, raw_key)
        _print_generated_key(raw_key)
        return

    # Explicit (or dev-allowed) key: insert if this key is not already present.
    existing = session.execute(
        select(models.ApiKey).where(models.ApiKey.prefix == key_prefix(raw_key))
    ).scalars().first()
    if existing is not None:
        return
    _insert_bootstrap_admin(session, raw_key)
