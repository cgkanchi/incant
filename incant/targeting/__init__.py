"""targeting — rules, segments, live pointers, defaults, kill switches, snapshots."""

from __future__ import annotations

from .service import MakeLiveOutcome, TargetingError, TargetingService
from .snapshot import build_snapshot

__all__ = ["MakeLiveOutcome", "TargetingError", "TargetingService", "build_snapshot"]
