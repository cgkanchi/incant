"""incant.core — the pure evaluation/render library.

``(content, rules-as-data, flags, variables) -> (version, sha, text)`` with no
I/O. Embeddable and exhaustively unit-testable.
"""

from __future__ import annotations

from .errors import (
    CoreError,
    IncludeCycle,
    IncludeDepthExceeded,
    MissingVariable,
    RenderError,
    Unservable,
    UnresolvedPrompt,
)
from .evaluate import Skip, resolve
from .model import (
    All,
    Any_,
    Clause,
    Condition,
    ContentBlob,
    ContentProvider,
    EnvSnapshot,
    Not,
    Resolution,
    Rule,
    Serve,
    ServeVersion,
    VersionInfo,
)
from .parse import parse_condition, parse_rule, parse_serve
from .render import RenderResult, precompile, render, render_source
from .variables import ExtractedVars, extract

__all__ = [
    "All", "Any_", "Clause", "Condition", "ContentBlob", "ContentProvider",
    "CoreError", "EnvSnapshot", "ExtractedVars", "IncludeCycle",
    "IncludeDepthExceeded", "MissingVariable", "Not", "RenderError",
    "RenderResult", "Resolution", "Rule", "Serve", "ServeVersion",
    "Skip", "Unservable", "UnresolvedPrompt", "VersionInfo",
    "extract", "parse_condition", "parse_rule", "parse_serve", "precompile", "render",
    "render_source", "resolve",
]
