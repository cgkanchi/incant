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
commits_total = Counter("incant_commits_total", "Commits", ["project"])
validation_failures_total = Counter("incant_validation_failures_total", "Validation failures")
# Serving is memory-FIRST, not memory-only: on a content-cache miss (cold/evicted blob,
# or an old validated pin) the ContentStore falls through to a git read. This counter
# makes that fall-through observable — it should sit at ~0 on a warm node; sustained
# growth means the working set outgrew the cache or warming is incomplete.
content_git_reads_total = Counter(
    "incant_content_git_reads_total", "Serving-path content reads that fell through to git",
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
