"""Prometheus metrics (DESIGN.md §14)."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

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
