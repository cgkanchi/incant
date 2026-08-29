"""targeting — rules, segments, live pointers, defaults, kill switches, snapshots."""

from __future__ import annotations

from .service import MakeLiveOutcome, TargetingError, TargetingService, capture_state
from .snapshot import build_snapshot, clear_servable_memo, snapshot_from_state

__all__ = ["MakeLiveOutcome", "TargetingError", "TargetingService", "build_snapshot",
           "capture_state", "clear_servable_memo", "snapshot_from_state"]
