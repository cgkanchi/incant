"""Data model for the pure evaluation/render core.

Everything here is plain data (dataclasses) so the core stays I/O-free and
exhaustively unit-testable. The server layer maps its DB rows / pydantic models
onto these structures to build an :class:`EnvSnapshot`, then hands it to the
evaluator and renderer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

# ─────────────────────────── conditions ───────────────────────────

Operator = Literal[
    "eq", "neq", "in", "not_in", "contains", "starts_with", "ends_with",
    "matches", "gt", "gte", "lt", "lte", "semver_gt", "semver_lt", "exists",
]


@dataclass(frozen=True)
class Clause:
    """A single flag condition. `value` for scalar ops, `values` for list ops."""

    flag: str
    op: Operator
    value: Any = None
    values: tuple[Any, ...] = ()


# A condition tree node is one of:
#   Clause
#   {"all": [node, ...]}   -> All
#   {"any": [node, ...]}   -> Any
#   {"not": node}          -> Not
#   {"segment": "name"}    -> SegmentRef
# None / empty => always matches.

@dataclass(frozen=True)
class All:
    of: tuple["Condition", ...]


@dataclass(frozen=True)
class Any_:
    of: tuple["Condition", ...]


@dataclass(frozen=True)
class Not:
    of: "Condition"


@dataclass(frozen=True)
class SegmentRef:
    name: str


Condition = Clause | All | Any_ | Not | SegmentRef | None


@dataclass(frozen=True)
class Segment:
    name: str
    condition: Condition
    version: int = 1


# ─────────────────────────── serve targets ───────────────────────────

At = Literal["live", "tip", "sha"]


@dataclass(frozen=True)
class ServeVersion:
    """Serve a specific version at its live pointer, its tip, or a pinned SHA."""

    version: int
    at: At = "live"
    sha: str | None = None


@dataclass(frozen=True)
class ServeLabel:
    """Serve whatever version carries this label on the prompt, at its live pointer.

    Used by global rules: prompts without the label skip the rule and continue.
    """

    label: str


@dataclass(frozen=True)
class RolloutBand:
    weight: float
    label: str | None = None
    version: int | None = None
    is_default: bool = False


@dataclass(frozen=True)
class ServeRollout:
    bucket_by: str
    weights: tuple[RolloutBand, ...]


Serve = ServeVersion | ServeLabel | ServeRollout


# ─────────────────────────── rules ───────────────────────────

Scope = Literal["global", "prompt"]
RuleStatus = Literal["active", "paused", "archived"]


@dataclass(frozen=True)
class Rule:
    id: str
    scope: Scope
    priority: int
    when: Condition
    serve: Serve
    status: RuleStatus = "active"
    prompt_id: str | None = None  # required when scope == "prompt"
    comment: str = ""


# ─────────────────────────── environment snapshot ───────────────────────────

@dataclass(frozen=True)
class VersionInfo:
    """What the evaluator needs to know about one version of one prompt."""

    version: int
    live_sha: str | None                       # current live pointer (newest pointer_move)
    tip_sha: str | None                        # newest validated commit on the file
    label: str | None = None
    status: Literal["active", "archived"] = "active"
    # Newest-first list of previously-live SHAs, for the §10 within-version fallback.
    previous_live: tuple[str, ...] = ()


@dataclass
class EnvSnapshot:
    """An immutable-ish snapshot of one environment's targeting + pointer state.

    The evaluator is a pure function of ``(EnvSnapshot, flags)``. Servability is
    injected via ``servable`` so the core never touches a cache or the git store.
    """

    environment: str
    rules_version: int
    rules: list[Rule] = field(default_factory=list)
    segments: dict[str, Segment] = field(default_factory=dict)
    defaults: dict[str, int] = field(default_factory=dict)  # prompt_id -> version number
    # (prompt_id, version_number) -> {var_name -> default value} for optional vars.
    # Folded into the snapshot so the render hot path needs no per-request DB read.
    refinement_defaults: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    # prompt_id -> {version_number -> VersionInfo}
    versions: dict[str, dict[int, VersionInfo]] = field(default_factory=dict)
    track_tip: bool = False
    stale: bool = False  # true iff serving frozen at last-known-good (DB outage)
    # Prompts whose kill switch is engaged: force the environment default, bypass rules.
    killed: set[str] = field(default_factory=set)
    # (prompt_id, sha) -> bool; default: everything is servable.
    servable: Callable[[str, str], bool] = lambda _p, _s: True

    # -- convenience accessors --------------------------------------------

    def global_rules(self) -> list[Rule]:
        return sorted(
            (r for r in self.rules if r.scope == "global" and r.status == "active"),
            key=lambda r: r.priority,
        )

    def prompt_rules(self, prompt_id: str) -> list[Rule]:
        return sorted(
            (
                r for r in self.rules
                if r.scope == "prompt" and r.prompt_id == prompt_id and r.status == "active"
            ),
            key=lambda r: r.priority,
        )

    def version_info(self, prompt_id: str, version: int) -> VersionInfo | None:
        return self.versions.get(prompt_id, {}).get(version)

    def version_for_label(self, prompt_id: str, label: str) -> int | None:
        for v in self.versions.get(prompt_id, {}).values():
            if v.label == label:
                return v.version
        return None

    def all_prompt_ids(self) -> list[str]:
        ids = set(self.versions) | set(self.defaults)
        return sorted(ids)


# ─────────────────────────── resolution result ───────────────────────────

MatchScope = Literal["global", "prompt", "default"]


@dataclass(frozen=True)
class Resolution:
    prompt_id: str
    version: int
    commit: str                    # the SHA actually served
    at: At                         # how it was chosen (live / tip / sha)
    match_scope: MatchScope
    rule_id: str | None = None
    label: str | None = None
    content_fallback: bool = False  # true iff a previous-live SHA served (§10)


# ─────────────────────────── content provider ───────────────────────────

@dataclass(frozen=True)
class ContentBlob:
    blob_sha: str
    source: str


class ContentProvider(Protocol):
    """Maps a resolved (prompt_id, version, commit_sha) to its template blob + source.

    The git store implements this; tests pass a dict-backed stub. Pure core never
    reads from disk — it only calls this protocol.
    """

    def get(self, prompt_id: str, version: int, commit_sha: str) -> ContentBlob: ...
