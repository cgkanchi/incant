# Incant Architecture and Code Review

Incant has a strong conceptual architecture and unusually thoughtful failure handling. The pure rendering core, staged git publication, append-only pointer history, RBAC, and test coverage are all solid foundations.

The repository should remain a modular monolith for now. The most important improvements are transactional correctness, stronger domain validation, cache coherence, and closing several security and concurrency gaps.

## Implementation update — findings 1–14 resolved

The original diagnoses are preserved below as the rationale and audit trail. Findings 1–14 have now been implemented and regression-tested.

| # | Resolution | Key behavior now enforced |
|---:|---|---|
| 1 | Resolved | Initial setup is serialized by a Postgres transaction advisory lock; concurrent callers cannot both create administrators. |
| 2 | Resolved | Invite redemption locks the user row with `SELECT FOR UPDATE`; exactly one concurrent redemption consumes the token. |
| 3 | Resolved | Every targeting mutation locks its environment before changing child state; revision numbers are unique and each revision captures the cumulative serialized state. |
| 4 | Resolved | Draft mutation and publication use locked, centralized lifecycle guards; terminal drafts are immutable. |
| 5 | Resolved | Conditions, rules, serve targets, rollouts, bounds, scopes, and statuses are validated at both API and service boundaries, with database checks for finite-state fields. |
| 6 | Resolved | Git remote failures use credential-redacted URLs and sanitized stderr across logs, API status, and audit payloads. |
| 7 | Resolved | Snapshot and auth caches invalidate only after a successful transaction commit; rollback discards scheduled callbacks. |
| 8 | Resolved | Snapshot servability is a complete in-memory set of valid `(prompt, version, SHA)` tuples; corrupt cross-prompt or cross-version references fail closed. |
| 9 | Resolved | Deep pin validation uses the snapshot validation index and performs no request-path database fallback. |
| 10 | Resolved | Rendering collects defaults from the actual flag/pin-resolved nested include closure. Explicit request variables win; conflicting contributor defaults raise a clear render error. |
| 11 | Resolved | Environments receive an exact baseline revision; rollback requires an existing exact revision and restores rules, segments, defaults, kills, and pointers, using append-only pointer tombstones where needed. |
| 12 | Resolved | Archived versions retain existing serving history but reject new drafts, commits, defaults, rules, and live moves. A management endpoint now controls label, notes, archive, and reactivation state. |
| 13 | Resolved | Draft, review, and comment records retain display-name snapshots but policy, uniqueness, and self-review checks use immutable principal IDs. |
| 14 | Resolved | Readiness treats an absent configured default environment as not ready. |

The schema changes are captured in [`e9a1c4f27b63_integrity_identity_and_exact_history.py`](./alembic/versions/e9a1c4f27b63_integrity_identity_and_exact_history.py). The migration backfills stable legacy identity keys, makes pointer tombstones representable, repairs only invalid or duplicate legacy revision identifiers, and installs the new uniqueness/check constraints.

## Highest-priority findings

### 1. Resolved — first-run setup could create multiple anonymous administrators

[`incant/server/sessions.py`](./incant/server/sessions.py#L173) checks `user_count() == 0`, then creates the admin without locking or a unique singleton guard.

Two concurrent unauthenticated requests with different email addresses can both observe zero users and both receive instance-wide admin access. The email uniqueness constraint does not prevent this.

Recommended changes:

- Serialize setup with a Postgres advisory lock or a singleton installation row locked with `SELECT ... FOR UPDATE`.
- Alternatively, use a conditional insert whose unique constraint decides the winner.
- Add a concurrent setup test proving exactly one request succeeds.

### 2. Resolved — invite tokens were not actually single-use under concurrency

[`incant/server/accounts.py`](./incant/server/accounts.py#L71) reads an invite without locking it. [`incant/server/sessions.py`](./incant/server/sessions.py#L205) only clears the token later in the transaction.

Two simultaneous redemptions can both validate the same token, set passwords, and mint sessions.

Recommended changes:

- Read the invite using `SELECT ... FOR UPDATE`.
- Better still, atomically consume it with a conditional `UPDATE ... WHERE invite_token_hash = ... RETURNING ...`.
- Add a test that submits the same token concurrently and verifies that only one request succeeds.

### 3. Resolved — concurrent targeting revisions could capture incorrect historical state

The atomic counter in [`incant/targeting/service.py`](./incant/targeting/service.py#L141) prevents lost `rules_version` increments, but `capture_state()` runs before the environment-row update is flushed and locked.

Two writers can therefore:

1. Mutate different targeting objects.
2. Each capture a state that cannot see the other uncommitted mutation.
3. Serialize their counter increments afterward.
4. Produce a later revision whose stored state omits an earlier committed change.

That breaks rollback and `pin.rules_version` replay even though the counter remains correct. The current concurrency test only asserts the counter and final rule count in [`tests/test_concurrency.py`](./tests/test_concurrency.py#L37).

Recommended changes:

- Lock the environment row before any mutation using `SELECT ... FOR UPDATE`.
- Mutate, capture, and bump while holding that lock.
- Add a unique constraint on `(environment_id, rules_version)`.
- Extend concurrency tests to inspect every captured revision state, not only the final counter.

### 4. Resolved — committed drafts could be edited and committed again

Neither [`put_draft_content()`](./incant/registry/service.py#L193) nor [`commit_draft()`](./incant/registry/service.py#L375) rejects terminal draft states.

After a commit deletes the draft ref, a later PUT can recreate it. On a project with review policy zero, the same committed draft can then be committed again. Review and comment routes also apply inconsistent terminal-state checks.

Recommended changes:

- Centralize draft state transitions in `RegistryService`.
- Allow only `open` or `approved` drafts to be edited or committed.
- Allow only open drafts to receive reviews.
- Lock the draft row during publication.
- Add tests for every operation against committed, discarded, and abandoned drafts.

### 5. Resolved — malformed segment data could poison an environment

The API models leave targeting fields largely untyped in [`incant/server/schemas.py`](./incant/server/schemas.py#L146). Segment conditions are stored without parsing in [`incant/targeting/service.py`](./incant/targeting/service.py#L267), then parsed later while building the serving snapshot in [`incant/targeting/snapshot.py`](./incant/targeting/snapshot.py#L264).

An operator can successfully save an invalid condition such as an unknown node. The next snapshot rebuild can then raise an unhandled exception, making that environment fail serving and management reads.

Recommended changes:

- Use Pydantic discriminated unions and `Literal` types for conditions, operators, rule scope/status, serve modes, and rollout arms.
- Validate segment and rule objects again inside the domain service.
- Add weight, version, and identifier bounds.
- Add database check constraints for finite state fields.
- Ensure invalid input consistently returns 400 or 422 and never lands in the database.

### 6. Resolved — backup credentials could leak through errors

Git errors embed the raw remote URL in [`incant/gitstore/store.py`](./incant/gitstore/store.py#L266). The backup layer logs that exception and exposes it as `status.error` in [`incant/gitstore/backup.py`](./incant/gitstore/backup.py#L132). Manual pushes then record and return it through [`incant/server/mgmt/remotes.py`](./incant/server/mgmt/remotes.py#L106).

For an HTTPS URL containing a token, a failed operation can leak credentials into:

- Application logs
- The management API response
- Audit JSON

Recommended changes:

- Introduce a structured `RemoteGitError` containing only a pre-redacted display URL.
- Sanitize stderr because git may echo the credential-bearing URL.
- Keep raw credentials out of generic exception strings.
- Add failure-path tests that use a credential-bearing URL and inspect logs, responses, and audit records.

### 7. Resolved — cache invalidation occurred before transaction commit

Management routes invalidate snapshots and auth immediately, while the actual commit happens afterward in the dependency teardown in [`incant/server/deps.py`](./incant/server/deps.py#L24).

For example, rule upsert invalidates the snapshot in [`incant/server/mgmt/targeting.py`](./incant/server/mgmt/targeting.py#L160) before the transaction commits. A concurrent request can rebuild the cache from the old database state, after which the transaction commits without another invalidation. Auth revocation and issuance have the same race.

Consequences include:

- Same-node targeting changes remaining stale until the poller notices.
- A locally revoked key potentially being reloaded as valid until the auth TTL expires.
- The documented immediate same-node freshness guarantee not always holding.

Recommended changes:

- Schedule cache invalidation or refresh in `after_commit`.
- Centralize this through a unit-of-work abstraction rather than individual route handlers.
- Do not mutate process caches before transaction success.
- Test a concurrent reader during a targeting mutation and key revocation.

## Serving and correctness findings

### 8. Resolved — the servability defense did not validate referenced SHAs

[`incant/targeting/snapshot.py`](./incant/targeting/snapshot.py#L193) adds pointer history and explicit rule pins directly to a `referenced` set. The final predicate treats membership as sufficient at [`snapshot.py`](./incant/targeting/snapshot.py#L284).

A corrupted, imported, or manually edited pointer or rule row can therefore make an unvalidated SHA appear servable. The read-side backstop trusts the records it is intended to verify.

Recommended changes:

- Build the referenced set by joining against valid `CommitValidation` records.
- Include the version number in the servability key.
- Add corruption tests that insert invalid pointer and rule rows directly, then assert serving fails closed.

### 9. Resolved — deep pin validation performed request-path database reads

[`_validated_in_db()`](./incant/targeting/snapshot.py#L75) opens a new database session on cache misses. This contradicts the documented memory-first behavior for old validated pins and changes outage semantics: a legitimate deep pin can become a 409 when Postgres is unavailable.

Possible approaches:

- Maintain a bounded in-memory validation index populated by the control-plane poller.
- Warm all explicitly supported replay entries.
- Store signed validation evidence in replay tokens.
- If DB-backed deep pins are intentional, narrow the documented memory-only guarantee.

### 10. Resolved — defaults for included fragments were ignored

The snapshot holds defaults for every `(prompt, version)`, but [`incant/service.py`](./incant/service.py#L460) passes only the root prompt's defaults into rendering.

If an included fragment has a refinement default, it will not be applied. The fragment's variable may render empty through optional-variable handling instead of using its configured default.

Recommended changes:

- Resolve defaults across the actual targeted include closure.
- Define precedence when multiple contributors specify defaults for the same variable.
- Reject or warn on incompatible default collisions.
- Add tests for nested fragments and flag-targeted fragment versions.

### 11. Resolved — rollback was not a total or exact restore

Several details diverge from the complete-targeting-state contract:

- [`state_at()`](./incant/targeting/service.py#L356) chooses the newest revision at or before the requested value rather than requiring an exact revision.
- A nonexistent or negative version can fall into legacy reconstruction and archive rules unexpectedly.
- Segments created after the target are intentionally retained in [`targeting/service.py`](./incant/targeting/service.py#L438).
- Pointers absent at the target remain present.

Recommended changes:

- Create a baseline state revision when an environment is created.
- Require an exact target revision and validate the requested range.
- Restore exact state, including removal or tombstoning of objects absent at the target.
- If exact restoration is not desired, rename and document the narrower rollback semantics.

### 12. Resolved — archived version policy was not enforced

`Version.status` exists, but `_version_exists()` accepts archived versions for new rules, and draft creation and commit do not prohibit new edits. There also appears to be no normal management API for changing version labels, notes, or status.

This contradicts the documented rule that archived versions keep existing pointers but accept no new commits or rules.

Recommended changes:

- Enforce version lifecycle inside `RegistryService` and `TargetingService`.
- Add explicit metadata and archive endpoints.
- Keep existing live pointers valid while rejecting new rules, drafts, and commits for archived versions.

### 13. Resolved — review identity used display names instead of principal IDs

Draft authors and reviewers are stored as strings in [`incant/models.py`](./incant/models.py#L106), and self-review separation compares those names in [`incant/registry/service.py`](./incant/registry/service.py#L319).

Display names are neither unique nor stable identifiers. This causes incorrect review deduplication and makes separation-of-duties enforcement ambiguous.

Recommended changes:

- Store `author_principal_id`, `reviewer_principal_id`, and `actor_principal_id`.
- Keep optional display-name snapshots for presentation and historical readability.
- Base uniqueness and self-review checks on principal IDs.

### 14. Resolved — the default environment could be absent while readiness was green

[`incant/server/app.py`](./incant/server/app.py#L123) uses:

```python
ready = primed and env_warm.get(default_env, True)
```

A fresh database with no environments therefore reports ready even though requests targeting the configured default environment cannot be served.

The missing default should evaluate to false, or initialization should create the configured default environment explicitly.

## Reliability and performance improvements

### Thread-safe caches

`ContentStore` mutates an `OrderedDict` without synchronization in [`incant/gitstore/content.py`](./incant/gitstore/content.py#L38). FastAPI synchronous routes run in a thread pool; eviction between `get()` and `move_to_end()` can raise. The compiled-template caches have similar compound unsynchronized operations.

Add locks or use a thread-safe bounded cache implementation. Include concurrent cache-churn tests with a deliberately small capacity.

### Targeting revision growth

Every targeting mutation stores a full environment snapshot, while `capture_state()` also scans all registered versions in [`incant/targeting/service.py`](./incant/targeting/service.py#L27). Batch updates capture this full state once per item.

Storage and write latency will trend toward `O(changes x total-state-size)`.

Prefer:

- One revision per user transaction rather than one per internal item.
- An append-only delta log.
- Periodic materialized snapshots for bounded replay time.
- Explicit retention or archival policies for old materializations.

### Snapshot construction scope

Snapshot construction loads every version and every variable refinement instance-wide, even when building one environment, in [`incant/targeting/snapshot.py`](./incant/targeting/snapshot.py#L210).

Add project or environment scoping where possible, or maintain projection tables optimized for serving snapshot construction.

### Resource limits and regex safety

Regex rules use unrestricted Python `re.search` in [`incant/core/clauses.py`](./incant/core/clauses.py#L57). Catastrophic patterns can consume a serving thread. Templates and response bodies similarly have no explicit size or output budgets.

Add:

- Regex restrictions, pre-validation, or timeout-capable matching
- Request-body size limits
- Template source size limits
- Render output limits
- Limits on variable collection sizes and total render work

### Blocking work in async background loops

Several background loops perform synchronous database or git-heavy work directly from async tasks. Move blocking passes through `asyncio.to_thread`, as the backup loop already does, or adopt an async database engine for those workers.

### Configuration validation

Configuration lacks validation for modes, positive intervals, pool behavior, and security-sensitive combinations. Invalid zero or negative polling intervals can produce tight loops.

Use Pydantic constraints and cross-field startup validation for:

- `mode`
- Poll and timeout intervals
- Cache TTLs
- Throttle settings
- Port ranges
- Serve-mode remote requirements
- TLS and cookie security expectations

### Database integrity

The schema relies heavily on application checks. Add database constraints for:

- Status, role, scope, and serve-mode values
- Unique role bindings
- Nonnegative version and review-policy values
- Prompt/version references
- Environment-scoped object integrity

## Frontend architecture

The UI has been partially separated into screen files, but [`incant/ui/js/main.js`](./incant/ui/js/main.js) remains a 1,436-line global event and state coordinator. Rendering relies on manual HTML-string escaping.

Continuing with plain JavaScript is reasonable, but the code would benefit from:

- ES modules instead of shared globals and `window` state
- Per-screen controllers with explicit inputs and cleanup
- A small typed API client contract
- DOM-safe rendering helpers that escape by default
- Unit tests for pure state transitions and HTML builders
- Browser tests focused on failure states, stale writes, and permission changes

## Operational architecture

Recommended deployment hardening:

- Run the application container as a non-root user.
- Pin the `uv` image by version or digest instead of `latest` in [`Dockerfile`](./Dockerfile#L4).
- Treat [`docker-compose.yaml`](./docker-compose.yaml) as development-only: it exposes Postgres publicly with default credentials.
- Document and enforce that only one `full` writer process owns the canonical git repository.
- Prevent multiple full workers from independently running reconciliation, backup, and session-sweep jobs.
- Use a dedicated worker or leader election if those jobs must run in a replicated deployment.
- Add graceful task cancellation and await background tasks during shutdown.

## Testing and engineering workflow

The current suite is substantial, but the most important missing cases align with the findings above:

- Concurrent first-run setup
- Concurrent redemption of one invite token
- Correct contents of every concurrent targeting revision
- Edits, reviews, and commits against terminal drafts
- Invalid targeting and segment payloads
- Cache invalidation racing transaction commit
- Credential redaction on remote failures
- Fragment refinement defaults
- Exact rollback to baseline and invalid revision numbers
- Concurrent cache churn and eviction

Additional workflow improvements:

- Add CI configuration for unit, integration, and browser suites.
- Run Alembic model-drift checks in CI against a temporary migrated database.
- Add formatting, linting, and static type checking.
- Add dependency and container scanning.
- Make the test database strategy compatible with isolated parallel workers before enabling `pytest-xdist`.

## Architectural strengths

- The pure `core` layer is a good boundary and independently testable.
- Staged publication and pending-ref recovery are substantially safer than a naive git-then-database transaction.
- Pointer history and exact SHA reporting are appropriate primitives for prompt reproducibility.
- Cookie security, CSRF protection, API-key hashing, auth-cache outage behavior, and scope-aware RBAC show strong security intent.
- Metrics and reconciliation make degradation visible instead of silently repairing uncertain state.
- Tests cover ordinary serving, rollback, auth, recovery, scale-sensitive queries, browser flows, and real Postgres concurrency.

## Verification performed

- Default test suite: **316 passed, 10 browser tests skipped by its opt-in gate**.
- Opt-in browser suite: **10 passed**.
- Python compilation check passed.
- Fresh/partial-schema Alembic adoption and upgrade tests passed against PostgreSQL.
- `git diff --check` passed.

## Remaining recommended implementation order

1. Make serving and compiled-template caches thread-safe.
2. Redesign revision storage for bounded growth and batch revisions.
3. Bound regex, template, request, and rendered-output work.
4. Move remaining blocking background passes off the event loop.
5. Add cross-field configuration validation.
6. Harden CI, containers, and multi-process worker ownership.
