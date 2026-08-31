"""Typed errors mirroring the server's responses. Every error keeps the raw
status and payload so nothing the server said is ever lost."""

from __future__ import annotations

from typing import Any


class IncantError(Exception):
    """Base for every SDK error. `.status` is the HTTP status (0 for transport
    failures), `.detail` the server's human message, `.payload` the raw body."""

    def __init__(self, detail: str, *, status: int = 0, payload: Any = None) -> None:
        self.status = status
        self.detail = detail
        self.payload = payload
        super().__init__(detail)


class NotAuthorized(IncantError):
    """401/403 — bad key, or the key's role/scope doesn't cover this call."""


class PromptNotFound(IncantError):
    """404 — the server's message distinguishes an unknown prompt from one that
    exists but isn't targeted in this environment yet."""


class MissingVariable(IncantError):
    """422 render failure naming the missing template variable (`.variable`)."""

    def __init__(self, detail: str, *, variable: str, status: int = 422,
                 payload: Any = None) -> None:
        super().__init__(detail, status=status, payload=payload)
        self.variable = variable


class RenderError(IncantError):
    """Any other 4xx the server explains (bad pin shape, unservable content, …)."""


class IncantUnavailable(IncantError):
    """Connection failures and 5xx after retries — the deployment, not your call."""
