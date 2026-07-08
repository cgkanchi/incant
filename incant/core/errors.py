"""Errors raised by the pure core. No I/O, no framework coupling."""

from __future__ import annotations


class CoreError(Exception):
    """Base class for all core errors."""


class UnresolvedPrompt(CoreError):
    """No rule matched and no default exists for the prompt in this environment."""

    def __init__(self, prompt_id: str, environment: str) -> None:
        self.prompt_id = prompt_id
        self.environment = environment
        super().__init__(f"no resolution for {prompt_id!r} in environment {environment!r}")


class Unservable(CoreError):
    """The resolved content cannot be served and no within-version fallback exists."""

    def __init__(self, prompt_id: str, version: int) -> None:
        self.prompt_id = prompt_id
        self.version = version
        super().__init__(
            f"nothing servable in the pointer history of {prompt_id}@v{version}"
        )


class IncludeCycle(CoreError):
    """A render-time include cycle was detected (static validation is the primary guard)."""

    def __init__(self, chain: list[str]) -> None:
        self.chain = chain
        super().__init__("include cycle: " + " -> ".join(chain))


class IncludeDepthExceeded(CoreError):
    def __init__(self, limit: int, chain: list[str]) -> None:
        self.limit = limit
        self.chain = chain
        super().__init__(
            f"include depth limit {limit} exceeded: " + " -> ".join(chain)
        )


class MissingVariable(CoreError):
    """A required variable was not supplied at render time (maps to HTTP 422)."""

    def __init__(self, name: str, prompt_id: str | None = None) -> None:
        self.name = name
        self.prompt_id = prompt_id
        where = f" in {prompt_id}" if prompt_id else ""
        super().__init__(f"missing required variable {name!r}{where}")


class RenderError(CoreError):
    """A template failed to render (bad syntax reaching render, runtime error)."""

    def __init__(self, message: str, prompt_id: str | None = None, lineno: int | None = None) -> None:
        self.prompt_id = prompt_id
        self.lineno = lineno
        super().__init__(message)
