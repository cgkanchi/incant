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

**Hardening from the architecture review** (all verified against the code before
fixing):

- **Security — remote credentials could execute shell commands.** `auth_ref` and
  `INCANT_KNOWN_HOSTS_PATH` are shell-quoted in `GIT_SSH_COMMAND` and the https
  credential helper, and `--` precedes every URL git receives. An admin-supplied
  credential path could previously run arbitrary commands on every node (backup loop,
  replica fetch loop, `POST /mgmt/remotes/{id}/push`). Remote URLs are restricted to
  `https://`, `http://`, `ssh://`, scp-like `user@host:path`, `file://` and absolute
  paths (`ext::` etc. refused, 422); `auth_ref` must be an absolute path of
  `[A-Za-z0-9._/@-]`. `INCANT_BOOTSTRAP_REMOTE(_KEY)` pass the same grammar and fail
  the boot loudly otherwise.
- **Security:** `GET /mgmt/envs` requires `viewer` in some scope; renderer-only keys can
  no longer enumerate environments.
- **Serve replicas learn about content changes.** A deployment-wide
  `environments.content_version` (migration `b3d8f5a17c92`) rides the control-plane poll
  beside `rules_version`: new validated commits (tips and the servable index), new
  versions, variable-refinement defaults and reconcile-adopted content now reach
  replicas within the poll interval. Previously a replica served a stale tip
  indefinitely and 409'd `pin.versions` naming a freshly validated SHA. The full node no
  longer empties its snapshot cache after a commit: affected snapshots are rebuilt inside
  the write and swapped in at commit, so there is no cold-build/503 window.
- **The single-writer role is monitored and fenced.** The advisory-lock connection is
  held in autocommit (never idle-in-transaction), ownership is re-checked every
  control-poll tick, a dropped connection is re-claimed, and if another full node holds
  the role the node fail-stops: readyz/healthz 503, management writes 503, writer loops
  halted, SIGTERM. New gauge `incant_writer_lock_held`. Full nodes must not sit behind a
  transaction-pooling PgBouncer (documented).
- **Kill switches beat replay for included fragments too:** a `pin.rules_version` replay
  is refused (409, `error: "killed"`) when any prompt that contributed to the render is
  killed in the current state, not only the root or pinned prompts.
- **Diverged publish recovery keeps the original SHA** — anchored at
  `refs/incant/recovered/<draft>` (mirror-pushed, never auto-deleted) so pointers, pins
  and replicas referencing it keep resolving; replays land their `CommitValidation` row
  before `main` moves (staged + `after_commit`, like a live publish); stranded refs are
  recovered in chain order; replayed content is statically re-validated against the tree
  it lands in (render verdict inherited).
- **Commit validation is never a 500 and never silently skipped:** a render-time include
  cycle/depth overrun or unfetchable content is an `invalid` verdict naming the test
  context (on commit and on the draft payload, which previously 500'd the editor); a
  missing default environment logs a warning and reports `render_checked: false` with
  `render_skipped_reason` on the commit response and the draft `lint`.
- **Observed flags survive DB outages:** retrying a failed flush no longer inflates the
  distinct-value count, so a flag with an ordinary number of values cannot be permanently
  suppressed by downtime; suppressions tripped during a failed pass persist on the next
  successful one.
- **Budgets:** `INCANT_MAX_REQUEST_BYTES` (1 MiB; 413 by Content-Length or mid-stream)
  and `INCANT_MAX_RENDER_BYTES` (2 MiB; 422 render error). Match the proxy's body limit.
- **Read-your-writes on the management API:** FastAPI ≥ 0.118 runs a yield-dependency's
  exit code after the response is sent, so the session commit landed after the client's
  200 — a client's immediate follow-up read could miss its own write. Writing routes now
  commit before the response (`scope="function"`).
- Prompt ids follow one grammar (`project/name`; lowercase segments of letters, digits,
  `.`, `_`, `-`; single `/`; ≤ 200 chars) at the API (422) and service layer. `acme/foo/`
  or `..` previously created an orphan row and a permanent 500 on the first draft write,
  and an empty id could bind a fresh deployment to project `""`. `GitStore.list_files`
  uses `ls-tree -z` so unusual paths are not dropped from DR adoption.
- The legacy→peppered key re-hash runs only on the full node (replicas are read-only)
  and a failed upgrade retries on the next cache reload, not per request.
- One validated-commit index is loaded per refresh pass / boot and shared across
  environments (was one full scan and one copy per environment).
- Render budgets actually bound the work: the output cap and a new wall-clock budget
  (`INCANT_MAX_RENDER_SECONDS`, default 5 s, 0 disables) are checked between template
  writes as the render streams, so a runaway loop is cut off mid-flight instead of
  after the full string materializes.
- The backup pass re-verifies advisory-lock ownership immediately before the mirror
  push; the docs now say plainly that monitoring narrows but does not eliminate the
  split-brain window (fencing generations planned with the content catalog).
- `render_checked` / `render_skipped_reason` are persisted on `commit_validations`
  (migration `c9f4b2e87a31`), and making a sha live warns when its stored verdict
  skipped CONFIGURED test contexts — the static-only "valid" no longer looks like a
  fully-checked one at publish time.
- Project ids share the prompt-segment grammar at every entry point (API, setup
  screen, registry): the first project can no longer be created with a name no valid
  prompt id can start with, which wedged the deployment permanently.
- Docs: content reaches replicas within `INCANT_BACKUP_POLL_SECONDS` +
  `INCANT_CONTENT_FETCH_SECONDS` (~45 s worst case), covered by the §10 within-version
  fallback — not "one fetch interval"; the boot migration creates `pg_trgm` itself
  (trusted on PG13+; PG12 or allow-listed managed instances must pre-create it).

**Second review pass — correctness, determinism and coherence.**

- **Delete-and-recreate of an environment id no longer serves stale content.** Each
  environment carries an immutable `incarnation` (migration `d6a1f4c83b57`); a node
  rebuilds — and drops the old life's replay cache — when the identity changes, closing
  the ABA where identical `(rules_version, content_version)` counters hid a recreated
  environment. `rename_env` now carries `content_version` and mints a fresh incarnation.
- **A slow control-plane poll can no longer overwrite a newer snapshot:** cache swaps are
  generation-checked and never move an environment backward.
- **`pin.rules_version` replays judge servability against current state:** the replay
  cached the validated-commit set and variable defaults at first build, wrongly rejecting
  (409) a pin naming a later-validated SHA and serving stale defaults; those overlays now
  refresh from current state each replay. The replay caches are also lock-guarded (a
  sporadic 500 under concurrency).
- **Equal-priority rules resolve deterministically** — ties break on rule id in both the
  evaluator and the snapshot build's SQL ordering, so replicas and rebuilds agree.
- **Boolean flags no longer compare equal to `0`/`1`** in `eq`/`neq`/`in`/`not_in`: a
  `true` flag never lands in a cohort keyed on the number `1`.
- **`commit_draft` refuses when a draft's git content has diverged from its recorded
  revision** (a failed save's residue), so recorded approvals can never authorize
  publishing bytes nobody reviewed; re-open/re-save recovers.
- **Variable refinements, test contexts and kill switches require the named prompt (and,
  for refinements, version) to exist** — a typo now fails with 404 at the route instead of
  creating dormant config that activates if that object is later created.
- **Example seeding is atomic and concurrency-safe:** concurrent seed requests serialize
  on a Postgres advisory lock (exactly one wins), and a mid-seed failure rolls the whole
  dataset back so a retry is accepted rather than refused forever.
- Malformed serving pins (`versions` not an object, a non-integer/`0`/negative/boolean
  version), draft version numbers and review states are 422s, not 500s or a database
  constraint error; condition trees are capped at 100 nesting levels (422, not
  `RecursionError`).
- History endpoints no longer 500 on commit messages containing field/record separator
  bytes; git subject parsing is bounded and framing-safe.
- Naming a nonexistent test context in a draft render or a rendered diff returns 404
  instead of silently rendering a different scenario. Password change is behind the same
  per-IP failed-auth throttle as sign-in.
- MCP tools that can archive, discard or overwrite live state (`edit_draft`,
  `set_prompt_metadata`, `upsert_rule`, `set_rule_status`) are annotated destructive so
  hosts gate them correctly.

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
