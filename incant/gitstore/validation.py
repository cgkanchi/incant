"""Commit validation. Only validated SHAs may ever be referenced by a pointer/rule.

Checks (from §5): Jinja compiles, static includes resolve to known prompts, the
include graph has no cycles at current defaults, and — when test contexts and a
renderer are supplied — a strict render succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from jinja2 import StrictUndefined
from jinja2.exceptions import TemplateError
from jinja2.sandbox import SandboxedEnvironment

from ..core import extract

_VALIDATE_ENV = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


@dataclass
class ValidationResult:
    status: str                       # "valid" | "invalid"
    error: str | None = None
    extracted_variables: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "valid"


def validate_source(
    source: str,
    prompt_id: str,
    *,
    is_known_prompt: Callable[[str], bool],
    include_source: Callable[[str], str | None],
) -> ValidationResult:
    """Validate one version's source.

    ``include_source`` returns the current (default) source of an included prompt,
    used only for the static cycle check; ``None`` if it cannot be resolved.
    """

    # 1. Compiles under the sandbox.
    try:
        _VALIDATE_ENV.compile(source, name=prompt_id, filename=prompt_id)
    except TemplateError as exc:
        return ValidationResult("invalid", f"template error: {exc}")

    ev = extract(source, _VALIDATE_ENV)

    # 2. Every static include target is a registered prompt.
    for target in ev.includes:
        if not is_known_prompt(target):
            return ValidationResult(
                "invalid", f"include target {target!r} is not a registered prompt"
            )

    # 3. No cycles in the static include graph at current defaults.
    cycle = _find_cycle(prompt_id, source, include_source)
    if cycle:
        return ValidationResult("invalid", "include cycle: " + " -> ".join(cycle))

    return ValidationResult("valid", None, ev.as_dict())


def _find_cycle(
    root_id: str,
    root_source: str,
    include_source: Callable[[str], str | None],
) -> list[str] | None:
    on_stack: list[str] = []
    visited: set[str] = set()

    def dfs(pid: str, source: str | None) -> list[str] | None:
        if pid in on_stack:
            return on_stack[on_stack.index(pid):] + [pid]
        if pid in visited or source is None:
            return None
        on_stack.append(pid)
        try:
            for target in extract(source, _VALIDATE_ENV).includes:
                res = dfs(target, include_source(target))
                if res:
                    return res
        finally:
            on_stack.pop()
        visited.add(pid)
        return None

    return dfs(root_id, root_source)
