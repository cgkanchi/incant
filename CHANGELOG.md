# Changelog

## 1.1.0 — 2026-08-31

**Breaking — targeting is flags-only.** Segments, version labels, percentage
rollouts and global-scope rules are removed. A rule is a flag condition on ONE prompt
that serves ONE of that prompt's versions (`{version, at: live|tip|sha}`). Who is in a
cohort — a beta list, a percentage — is decided by the caller's feature-flag system
and arrives as a flag; Incant does not bucket users itself. This is the v1 surface; a
unified, cleaner audience/content abstraction is planned for v1.5/v2.

- Removed: `segments` (table, `/mgmt/envs/{env}/segments`, `{segment: name}`
  conditions), `versions.label` (and `label` in the versions/render/evaluate/`/prompts`
  responses and SDK models), `{label}` / `{rollout}` serve targets, `scope: global`
  (the `scope` field is gone; `prompt_id` is required). The MCP server loses
  `upsert_segment` (22 tools). Rule payloads that still carry `scope: "prompt"` are
  accepted. Migration `a9c4e17f2b60` REFUSES to run while any rule uses a removed
  construct — restate or archive those rules first.
- Replays (`pin.rules_version`) and rollbacks of revisions recorded before 1.1.0 that
  used removed constructs are refused honestly (422 / 400) rather than approximated.
- **Archived versions stop serving** (DESIGN.md §5). A rule targeting an archived
  version is skipped and counted; archiving an environment's default is refused (409);
  unarchive to serve again. Status changes propagate to replicas (a `version` revision
  per environment).
- **Include failures are honest**: an include that cannot resolve reports the fragment
  ("included by …"), and a targeting-induced include cycle or depth overflow is a 409,
  not a 500.
- **Control-plane propagation isolates bad data**: a snapshot rebuild that fails for one
  environment no longer aborts the poll for every other environment
  (`incant_snapshot_build_failures_total{environment}`); environments deleted on
  another node are evicted from the serving cache on the next poll.
- **The kill switch beats replay**: a pin naming a killed prompt — or a
  `rules_version` replay of one — returns 409 with `error: "killed"` instead of
  serving the killed content. Unpinned requests still degrade to the default.
- **Rollback reports `defaults_skipped`** (a recorded default naming an archived
  version is not restored) alongside `pointers_skipped`.
- Fixes: renaming an environment no longer deletes its observed flags and
  suppressions; `GET /prompt/{id}/spec` no longer collapses `1` and `true` into one
  value; `upsert_rule` validates before touching the session; semver comparison drops
  `+build` metadata.

- `GET /mgmt/prompts/{id}/diff` no longer requires `a_sha`/`b_sha`: each side defaults
  to what the environment serves for that version (its live pointer), else its tip. The
  MCP `diff_versions` tool now works as documented without shas.
- `GET /prompts` and `GET /prompt/{id}/spec` omit archived versions — they cannot serve.
- Removed fields fail loudly instead of being silently dropped: `scope: "global"` on a
  rule and `label` on a version PATCH both return 422 with the reason; `scope: "prompt"`
  is still tolerated. Malformed rule shapes (`"version": null`, a missing `serve`) are
  422s, not 500s. Rule batches are capped at 200 upserts.
- The migration guard matches removed constructs as JSON keys only — a flag literally
  named `segment` does not block the upgrade — and its remediation advice is accurate
  (offending rules must be restated or deleted; archiving does not clear the check).
- Rollback runs the flags-only parser over every rule it would restore, refusing exactly
  the states replay refuses; a refused replay is memoized so a retried bad pin costs no
  control-plane queries.
- SDK: `RuleMatch.id` is `None` on a pinned replay; `VersionPin.fallback`,
  `Var.inferred_required`, `Flag.observed`/`suppressed` are surfaced; `incant-mcp`
  requires `incant-sdk>=1.1`.

## 1.0.0 — 2026-08-31

First stable release.

**The product.** Git for content (one file per prompt version, staged publication
with crash recovery), Postgres for the control plane, memory-first serving
(p50 ~1 ms). Non-devs author, review against rendered output, test with a cohort,
and publish; services render with full reproducibility (`versions` map +
`rules_version`, replayable via `pin`).

**Highlights**
- Flag-based targeting: rules, segments, labels, coherent percentage rollouts,
  kill switches; exact environment rollback with append-only pointer history and
  tombstones; checkpointed revision storage (O(1) recent, bounded reconstruction).
- Review that judges what will be served: rendered before/after diffs per test
  context, approval policies keyed to immutable principal identity.
- Accounts: first-run setup, email+password sign-in (scrypt), single-use invite /
  reset links, immediate total disable; API keys for machine access only.
- Operations: backup remotes double as serve-replica content distribution;
  single-full-writer enforcement; per-environment readiness; credential-redacted
  remote errors; Prometheus metrics for every failure mode that matters.
- One project per deployment; environment-scoped RBAC
  (renderer < viewer < editor < operator < releaser < admin); audit log for every
  mutation.
- Deployment: `INCANT_BOOTSTRAP_REMOTE` first-boot clone (blank ⇒ fresh start,
  populated ⇒ content adoption, unreachable ⇒ refuse to boot), `_FILE`-suffixed
  secret envs, scheme-aware remote credentials (`auth_ref` is always a mounted
  file path — ssh key or https credential store; never a secret in the DB).
- First run: the default environment exists from boot; admin "Get set up"
  checklist; one-click example dataset on an empty library; backup-remote
  management in the UI (add/push/disable with live sync state).
- Python SDK (`sdk/python`, `pip install incant-sdk`): sync + async clients over
  the serving API — render with a one-attribute reproducibility `pin`, typed
  errors, automatic retries, and discovery (`prompts()`, `prompt(id)` spec via
  the new renderer-scoped `GET /prompt/{id}/spec`: variables merged across
  resolvable versions, targeting flags with known values, includes). The
  serving `GET /prompts` listing is renderer-scoped now, so production keys can
  discover what they can render.
- MCP server (`mcp/python`, `pip install incant-mcp`): 23 curated, task-shaped
  tools giving agents the console's reach (minus deployment admin) under the
  API key's roles — discovery, real-path render/evaluate, the draft lifecycle,
  review, atomic publish, targeting, and recovery; `--read-only` mode; paired
  agent skills in `skills/` (incant-authoring, incant-release) with plan/confirm
  gates for protected environments.

- **Observed flags** (DESIGN.md §7): the targeting composer suggests flag names and
  values as you type. The serving API records the flag/value pairs it sees — in memory
  on the request path, flushed to Postgres by a background writer, pruned and
  high-cardinality-suppressed hourly — so suggestions come from real traffic with no
  SDK change. New: `GET /mgmt/envs/{env}/flags`, `GET …/flags/{flag}/values?q=`
  (trigram-ranked typeahead, typed values), `DELETE …/flags/{flag}` (forget);
  `GET /prompt/{id}/spec` merges observed values into `flags[].values`; migration
  `f2a7c9d41e58` (creates `pg_trgm`). Config: `INCANT_OBSERVE_FLAGS`,
  `INCANT_OBSERVED_FLAGS_*`. Metrics: `incant_observed_flags_total{outcome}`,
  `incant_observed_flags_suppressed`.

See DESIGN.md for the architecture and §17 for deliberate divergences.
