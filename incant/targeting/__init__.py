"""targeting — rules, segments, live pointers, defaults, kill switches, snapshots."""

from __future__ import annotations

from .observed import FlagObserver, Observation, flush_observations, prune_and_census
from .service import MakeLiveOutcome, TargetingError, TargetingService, capture_state
from .snapshot import build_snapshot, clear_servable_memo, snapshot_from_state

__all__ = ["FlagObserver", "MakeLiveOutcome", "Observation", "TargetingError",
           "TargetingService", "build_snapshot", "capture_state", "clear_servable_memo",
           "flush_observations", "prune_and_census", "snapshot_from_state"]
