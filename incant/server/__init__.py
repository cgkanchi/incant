"""server — FastAPI serving API, mgmt API, RBAC, and UI."""

from __future__ import annotations

from .app import app, create_app

__all__ = ["app", "create_app"]
