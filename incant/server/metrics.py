"""Prometheus metrics (DESIGN.md §14)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import Counter, Gauge, Histogram

if TYPE_CHECKING:
    from ..registry import MainReconcileResult

render_seconds = Histogram(
    "incant_render_seconds", "Render latency", buckets=(.0005, .001, .0025, .005, .01, .025, .05, .1),
)
renders_total = Counter(
    "incant_renders_total", "Renders", ["prompt", "environment", "stale_rules"],
)
content_fallbacks_total = Counter(
    "incant_content_fallbacks_total", "Within-version content fallbacks", ["prompt", "environment"],
)
rule_skips_total = Counter("incant_rule_skips_total", "Rules skipped as unservable")
# A render fell through every in-play rule to the environment default — dead-rule
# telemetry (§14): sustained growth means rules that never match anything.
flag_eval_fallthrough_total = Counter(
    "incant_flag_eval_fallthrough_total",
    "Renders that fell through every applicable rule to the environment default",
)
commits_total = Counter("incant_commits_total", "Commits", ["project"])
validation_failures_total = Counter("incant_validation_failures_total", "Validation failures")
# Compiled-template cache misses (§8/§14): ~0 expected on a warm node — the eager
# warm precompiles everything targeting references; growth means warming is
# incomplete or the working set outgrew the cache.
template_cache_misses_total = Counter(
    "incant_template_cache_misses_total", "Compiled-template cache misses",
)
# Seconds since each cached environment snapshot was last CONFIRMED fresh against the
# DB (§14): the control-plane poll resets it on every healthy pass, so a rising value
# is a poll that can't reach Postgres — targeting changes are not propagating.
rules_snapshot_age_seconds = Gauge(
    "incant_rules_snapshot_age_seconds",
    "Seconds since this environment's snapshot was last confirmed fresh",
    ["environment"],
)
# Serving is memory-FIRST, not memory-only: on a content-cache miss (cold/evicted blob,
# or an old validated pin) the ContentStore falls through to a git read. This counter
# makes that fall-through observable — it should sit at ~0 on a warm node; sustained
# growth means the working set outgrew the cache or warming is incomplete.
content_git_reads_total = Counter(
    "incant_content_git_reads_total", "Serving-path content reads that fell through to git",
)

# Backup pushes (§6, §14): the queue is commits between a remote's last_pushed_sha and
# main's head; lag is the age of the oldest un-pushed commit — the far edge of the
# content-durability exposure window. Both refresh on every pusher pass and on
# /mgmt/remotes reads. Depth is the max across enabled remotes; lag is per remote
# (labelled by remote id — URLs may embed credentials and don't belong in label values).
backup_queue_depth = Gauge(
    "incant_backup_queue_depth",
    "Commits not yet pushed to every enabled backup remote (max across remotes)",
)
backup_lag_seconds = Gauge(
    "incant_backup_lag_seconds",
    "Age of the oldest commit not yet pushed to this remote (0 when caught up)",
    ["remote"],
)

# Governance drift (DESIGN.md §3 "git owns content, the DB owns state"; §5 "Validation
# first"). `reconcile_main_commits` compares the git `main` tree against the DB control
# plane; it runs at boot and then on INCANT_RECONCILE_INTERVAL_SECONDS. These gauges
# carry its LATEST counts so a drifted node is continuously *visible* (page on nonzero)
# without ever being taken out of rotation — it still serves correctly from the last
# VALIDATED SHAs, so flipping readiness would turn a governance alarm into an outage.
# Gauges (not counters): each reconcile pass reports an absolute state, and a repaired
# drift should drop the number back toward zero.
reconcile_git_orphans = Gauge(
    "incant_reconcile_git_orphans",
    "Version files on refs/heads/main with no DB Version row (latest reconcile pass)",
)
reconcile_unvalidated_tips = Gauge(
    "incant_reconcile_unvalidated_tips",
    "Main tip commits with no CommitValidation row (latest reconcile pass)",
)
reconcile_missing_files = Gauge(
    "incant_reconcile_missing_files",
    "DB Version rows with no file on refs/heads/main (latest reconcile pass)",
)


def update_reconcile_metrics(result: MainReconcileResult) -> None:
    """Publish a `MainReconcileResult` onto the drift gauges. Called by the boot sweep
    and the periodic reconcile loop (server.app). Kept as a tiny explicit seam so the
    loop stays thin and the result→gauge mapping is unit-testable in isolation."""
    reconcile_git_orphans.set(result.git_orphans)
    reconcile_unvalidated_tips.set(result.unvalidated_tips)
    reconcile_missing_files.set(result.missing_files)
