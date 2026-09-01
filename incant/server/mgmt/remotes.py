"""Admin: backup remotes (§6). Write-only mirrors of the canonical repo — Incant
pushes its own lineage and reads nothing back (serve replicas fetch, but only ever
what the full node pushed). URLs are redacted in every response: an https remote
may embed a token, and listing remotes must not leak it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from ...gitstore import redact_url
from ...service import AppContext
from ...targeting.audit import record_audit
from ..auth import Identity
from ..deps import app_context, get_session, identity
from ..schemas import RemotePatchRequest, RemoteRequest
from .helpers import _require

router = APIRouter()


def _credential_warning(url: str) -> str | None:
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.password or parts.username:
        return ("credentials embedded in this URL are stored in the database; "
                "prefer an ssh deploy key or an https credential-store file "
                "(auth_ref) so the URL stays secret-free")
    return None


def _get_remote(session: Session, remote_id: int) -> models.Remote:
    r = session.get(models.Remote, remote_id)
    if r is None:
        raise HTTPException(404, f"unknown remote {remote_id}")
    return r


@router.get("/remotes")
def list_remotes(
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    """Every remote with its queue state: pending commits and lag (the §6 exposure
    window). Reading this also refreshes the backup gauges."""
    _require(ident, "admin")
    return {"remotes": [s.as_dict() for s in app.backup.status(session)]}


@router.post("/remotes")
def create_remote(
    req: RemoteRequest,
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    dup = session.execute(
        select(models.Remote).where(models.Remote.url == req.url)
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(409, f"remote {redact_url(req.url)!r} already registered")
    r = models.Remote(url=req.url, auth_ref=req.auth_ref, enabled=req.enabled)
    session.add(r)
    session.flush()
    record_audit(session, ident.name, "remote.create", "remote", str(r.id),
                 after={"url": redact_url(r.url), "enabled": r.enabled})
    out = {"ok": True, "id": r.id, "url": redact_url(r.url), "enabled": r.enabled}
    warning = _credential_warning(r.url)
    if warning:
        out["warning"] = warning
    return out


@router.patch("/remotes/{remote_id}")
def update_remote(
    remote_id: int, req: RemotePatchRequest,
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    r = _get_remote(session, remote_id)
    before = {"url": redact_url(r.url), "enabled": r.enabled}
    if req.url is not None and req.url != r.url:
        # A different repository has its own (empty) push history. (The schema has
        # already validated and stripped the URL.)
        r.url = req.url
        r.last_pushed_sha = None
        r.last_push_at = None
    if req.auth_ref is not None:
        r.auth_ref = req.auth_ref or None
    if req.enabled is not None:
        r.enabled = req.enabled
    session.flush()
    after = {"url": redact_url(r.url), "enabled": r.enabled}
    if after != before:
        record_audit(session, ident.name, "remote.update", "remote", str(r.id),
                     before=before, after=after)
    return {"ok": True, "id": r.id, "url": redact_url(r.url), "enabled": r.enabled}


@router.delete("/remotes/{remote_id}")
def delete_remote(
    remote_id: int,
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    r = _get_remote(session, remote_id)
    record_audit(session, ident.name, "remote.delete", "remote", str(r.id),
                 before={"url": redact_url(r.url), "enabled": r.enabled})
    session.delete(r)
    return {"ok": True, "id": remote_id}


@router.post("/remotes/{remote_id}/push")
def push_remote_now(
    remote_id: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    """Push this remote immediately (restore tooling, or verifying a new remote
    before trusting it with durability). Synchronous: the response reports whether
    the push actually landed. Works even on a disabled remote — deliberate, so an
    operator can test one before enabling it."""
    _require(ident, "admin")
    r = _get_remote(session, remote_id)
    status = app.backup.push_remote(session, r)
    record_audit(session, ident.name, "remote.push", "remote", str(r.id),
                 after={"ok": status.error is None,
                        "pushed_sha": status.last_pushed_sha, "error": status.error})
    return status.as_dict()
