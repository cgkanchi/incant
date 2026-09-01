"""Response models — frozen dataclasses over the wire payloads. Every model
keeps `.raw` (the untouched response dict) so new server fields are reachable
before the SDK grows attributes for them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuleMatch:
    scope: str          # "prompt"
    id: str


@dataclass(frozen=True)
class VersionPin:
    """What was served for one prompt (the root or an included fragment)."""

    version: int
    commit: str         # full 40-char SHA


@dataclass(frozen=True)
class SkippedRule:
    rule_id: str
    prompt_id: str
    reason: str


@dataclass(frozen=True)
class RenderResult:
    text: str
    prompt_id: str
    environment: str
    matched_rule: RuleMatch | str            # RuleMatch, or the string "default"
    versions: dict[str, VersionPin]          # prompt + every included fragment
    rules_version: int
    stale_rules: bool
    content_fallback: bool
    skipped_rules: tuple[SkippedRule, ...]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def __str__(self) -> str:
        return self.text

    @property
    def version(self) -> int:
        return self.versions[self.prompt_id].version

    @property
    def sha(self) -> str:
        return self.versions[self.prompt_id].commit

    @property
    def pin(self) -> dict[str, Any]:
        """The reproducibility token: log it beside your LLM call, feed it back
        as ``render(..., pin=...)`` for a byte-identical replay — same commits
        for the prompt and every fragment, same historical targeting."""
        return {
            "versions": {pid: {"version": vp.version, "commit": vp.commit}
                         for pid, vp in self.versions.items()},
            "rules_version": self.rules_version,
        }


@dataclass(frozen=True)
class Resolution:
    """Which version these flags would get — no variables, nothing rendered."""

    prompt_id: str
    version: int
    commit: str
    matched_rule: RuleMatch | str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class PromptInfo:
    id: str
    description: str
    versions: tuple[int, ...]
    default_version: int | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class Var:
    """One template variable: name + everything authors recorded about it."""

    name: str
    type: str
    required: bool
    default: Any = None
    description: str = ""
    versions: tuple[int, ...] = ()    # which resolvable versions use it


@dataclass(frozen=True)
class Flag:
    """One targeting flag the active rules consult, with its known values
    (enumerated from eq/in-style clauses; empty for free-form flags)."""

    name: str
    values: tuple[Any, ...] = ()


@dataclass(frozen=True)
class PromptSpec:
    """Everything to know before rendering: what to pass and what can come back."""

    prompt_id: str
    environment: str
    default_version: int | None
    resolvable_versions: tuple[int, ...]
    variables: tuple[Var, ...]
    flags: tuple[Flag, ...]
    includes: tuple[str, ...]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
