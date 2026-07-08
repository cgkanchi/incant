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
    RolloutBand,
    Rule,
    Segment,
    SegmentRef,
    Serve,
    ServeLabel,
    ServeRollout,
    ServeVersion,
    VersionInfo,
)
from .parse import parse_condition, parse_rule, parse_segment, parse_serve
from .render import RenderResult, precompile, render, render_source
from .rollout import bucket_point, pick_band
from .variables import ExtractedVars, extract

__all__ = [
    "All", "Any_", "Clause", "Condition", "ContentBlob", "ContentProvider",
    "CoreError", "EnvSnapshot", "ExtractedVars", "IncludeCycle",
    "IncludeDepthExceeded", "MissingVariable", "Not", "RenderError",
    "RenderResult", "Resolution", "RolloutBand", "Rule", "Segment",
    "SegmentRef", "Serve", "ServeLabel", "ServeRollout", "ServeVersion",
    "Skip", "Unservable", "UnresolvedPrompt", "VersionInfo",
    "bucket_point", "extract", "parse_condition", "parse_rule",
    "parse_segment", "parse_serve", "pick_band", "precompile", "render",
    "render_source", "resolve",
]
