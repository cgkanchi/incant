"""Management API — authoring, targeting, admin, and the read endpoints for the UI.

Devs and agents get the same flow as the UI: create draft -> put content ->
commit, same validation/review/audit. No side door.

This package assembles one ``/mgmt`` APIRouter from focused submodules; the split is
purely organizational — every route path, dependency, and behavior is unchanged, and
``from .mgmt import router`` keeps working.
"""

from __future__ import annotations

from fastapi import APIRouter

# Re-exported from the package root so callers (and the old `.mgmt` import site) keep
# working; StaleDraftWrite now lives in the registry package.
from ...registry import StaleDraftWrite
from . import admin, audit, drafts, prompts, targeting
from .helpers import ROLES

router = APIRouter(prefix="/mgmt", tags=["mgmt"])
router.include_router(prompts.router)
router.include_router(drafts.router)
router.include_router(targeting.router)
router.include_router(admin.router)
router.include_router(audit.router)

__all__ = ["ROLES", "StaleDraftWrite", "router"]
