# Changelog

## Unreleased

- **Archived versions stop serving** (DESIGN.md §5). A rule targeting an archived
  version is skipped and counted; labels ignore archived versions; archiving an
  environment's default is refused (409); unarchive to serve again. Label and status
  changes now propagate to replicas (a `version` revision per environment).
- **Labels are unique per prompt** (409 on a duplicate) and label resolution is
  deterministic (highest active version).
- **Broken conditions are counted skips, never matches**: a rule referencing a segment
  the environment lacks (or a segment cycle) is skipped and reported — previously it
  evaluated as false, so `not: {segment: gone}` matched everyone. Dangling segment
  references and segment cycles are refused at write time; global rules serving an
  explicit version must name one some prompt actively has.
- **Include failures are honest**: an include that cannot resolve reports the fragment
  ("included by …"), and a targeting-induced include cycle or depth overflow is a 409,
  not a 500.
- **Control-plane propagation isolates bad data**: a snapshot rebuild that fails for one
  environment no longer aborts the poll for every other environment
  (`incant_snapshot_build_failures_total{environment}`); environments deleted on
  another node are evicted from the serving cache on the next poll.
- **Rollback reports `defaults_skipped`** (a recorded default naming an archived
  version is not restored) alongside `pointers_skipped`.
- **The kill switch beats replay**: a pin naming a killed prompt — or a
  `rules_version` replay of one — returns 409 with `error: "killed"` instead of
  serving the killed content (reproducibility already yields to validation; now to
  the emergency lever too). Unpinned requests still degrade to the default.
- Fixes: renaming an environment no longer deletes its observed flags and
  suppressions; `GET /prompt/{id}/spec` no longer collapses `1` and `true` into one value.

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
